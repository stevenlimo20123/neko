"""Persistent, resumable job manager for long-running browser automation.

Jobs survive service restarts (state persisted under STATE_DIR/jobs/).
A job that was running when the service stopped is marked "interrupted"
and can be resumed. Generic job types work for ANY website:

* ``scrape.urls``    - visit a list of URLs, extract data via a JS
                       expression, optionally capture network responses
* ``scrape.search``  - run search queries against a search-URL template,
                       scroll-paginate, and harvest structured records from
                       intercepted XHR responses

Site adapters (see adapters/) register additional recipes that parameterise
these generic types.
"""
import asyncio
import inspect
import json
import logging
import os
import time
import traceback
import uuid
from typing import Callable, Dict, Optional

from .config import CFG
from . import sites as st

log = logging.getLogger("neko_automation.jobs")

JOB_TYPES: Dict[str, Callable] = {}


def register_job_type(name: str):
    def deco(fn):
        JOB_TYPES[name] = fn
        return fn
    return deco


class Job:
    def __init__(self, job_type: str, params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type
        self.params = params
        self.status = "queued"
        self.created = time.time()
        self.started: Optional[float] = None
        self.updated = time.time()
        self.finished: Optional[float] = None
        self.error: Optional[str] = None
        self.progress = {"done": 0, "total": 0}
        self.state: dict = {}          # resumable scratch state
        self.result: Optional[dict] = None
        self.attention: Optional[dict] = None  # e.g. LOGIN_REQUIRED

    def to_dict(self, with_result: bool = True) -> dict:
        d = {k: getattr(self, k) for k in (
            "id", "type", "params", "status", "created", "started",
            "updated", "finished", "error", "progress", "state", "attention")}
        if with_result:
            d["result"] = self.result
        return d


class JobManager:
    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers = []
        self._running = False

    # ------------------------------------------------------------ storage
    @property
    def _dir(self) -> str:
        return os.path.join(CFG.STATE_DIR, "jobs")

    def _path(self, job_id: str) -> str:
        return os.path.join(self._dir, f"{job_id}.json")

    def persist(self, job: Job):
        os.makedirs(self._dir, exist_ok=True)
        job.updated = time.time()
        # results can be large; cap the persisted result payload
        d = job.to_dict()
        s = json.dumps(d, ensure_ascii=False, default=str)
        if len(s) > 8 * 1024 * 1024 and isinstance(job.result, dict):
            for key in ("records", "results", "rows"):
                if key in job.result and isinstance(job.result[key], list):
                    job.result["_truncated_" + key] = len(job.result[key])
                    job.result[key] = job.result[key][:500]
            d = job.to_dict()
        with open(self._path(job.id), "w") as f:
            json.dump(d, f, ensure_ascii=False, default=str)

    def load_all(self):
        os.makedirs(self._dir, exist_ok=True)
        for fn in os.listdir(self._dir):
            if not fn.endswith(".json"):
                continue
            try:
                d = json.load(open(os.path.join(self._dir, fn)))
                job = Job(d["type"], d.get("params", {}))
                job.id = d["id"]
                job.status = d.get("status", "queued")
                job.created = d.get("created", time.time())
                job.started = d.get("started")
                job.updated = d.get("updated", time.time())
                job.finished = d.get("finished")
                job.error = d.get("error")
                job.progress = d.get("progress", {"done": 0, "total": 0})
                job.state = d.get("state", {})
                job.result = d.get("result")
                job.attention = d.get("attention")
                if job.status == "running":
                    job.status = "interrupted"
                    self.persist(job)
                self.jobs[job.id] = job
            except Exception:
                continue

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        self._running = True
        for i in range(max(1, CFG.JOB_CONCURRENCY)):
            w = asyncio.create_task(self._worker(i))
            self._workers.append(w)

    async def stop(self):
        self._running = False
        for job in self.jobs.values():
            if job.status == "running":
                job.status = "interrupted"
                self.persist(job)
        for t in self.tasks.values():
            t.cancel()
        for w in self._workers:
            w.cancel()

    async def _worker(self, i: int):
        while self._running:
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            job = self.jobs.get(job_id)
            if not job or job.status not in ("queued", "resume"):
                continue
            fn = JOB_TYPES.get(job.type)
            if fn is None:
                job.status = "failed"
                job.error = f"unknown job type {job.type}"
                self.persist(job)
                continue
            job.status = "running"
            job.started = job.started or time.time()
            self.persist(job)
            try:
                result = fn(job, self) if not inspect.iscoroutinefunction(fn) \
                    else await fn(job, self)
                if job.status == "running":     # may have been cancelled
                    job.status = "completed"
                    job.result = result
                    job.finished = time.time()
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.finished = time.time()
            except JobAttention as a:
                job.status = "needs_attention"
                job.attention = {"reason": a.reason, "detail": a.detail,
                                 "resume_hint": a.hint}
                job.finished = time.time()
            except Exception as e:
                job.status = "failed"
                job.error = f"{e}"[:500]
                log.error("job %s failed: %s\n%s", job.id, e,
                          traceback.format_exc()[-800:])
                job.finished = time.time()
            self.persist(job)

    def submit(self, job_type: str, params: dict) -> Job:
        if job_type not in JOB_TYPES:
            raise ValueError(f"unknown job type {job_type}; "
                             f"known: {sorted(JOB_TYPES)}")
        job = Job(job_type, params)
        self.jobs[job.id] = job
        self.persist(job)
        self._queue.put_nowait(job.id)
        return job

    def resume(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(f"no such job {job_id}")
        if job.status not in ("interrupted", "needs_attention", "cancelled",
                              "failed"):
            raise ValueError(f"job {job_id} is {job.status}; nothing to resume")
        job.status = "resume"
        job.attention = None
        job.error = None
        self.persist(job)
        self._queue.put_nowait(job.id)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(f"no such job {job_id}")
        t = self.tasks.get(job_id)
        if t and not t.done():
            t.cancel()
        if job.status in ("queued", "running", "resume"):
            job.status = "cancelled"
            job.finished = time.time()
            self.persist(job)
        return job

    def prune(self, keep: int = 200):
        done = sorted((j for j in self.jobs.values()
                       if j.status in ("completed", "failed", "cancelled")),
                      key=lambda j: j.finished or 0)
        for j in done[:-keep] if len(done) > keep else []:
            self.jobs.pop(j.id, None)
            try:
                os.remove(self._path(j.id))
            except OSError:
                pass


class JobAttention(Exception):
    """Raised by job implementations when human intervention is needed
    (login expired, CAPTCHA shown, ...). The job pauses in a resumable
    state; after the user fixes the session in the Neko browser UI, the
    job can be resumed and will continue with fresh cookies."""

    def __init__(self, reason: str, detail: str = "", hint: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.hint = hint or ("Ask the user to fix the session in the Neko "
                             "browser UI, then POST /api/automation/jobs/"
                             "<id>/resume")


JOBS = JobManager()


# ----------------------------------------------------------------- helpers
def check_auth_loss(page_url: str, domain: str):
    """Raise JobAttention if a page navigated into login/verification."""
    if not domain:
        return
    site = st.SITES.for_domain(domain)
    patterns_v = (site or {}).get("verification_path_patterns",
                                  ["*captcha*", "*verify*"])
    patterns_l = (site or {}).get("login_path_patterns", ["*login*"])
    if st.path_matches(page_url, patterns_v):
        raise JobAttention("VERIFICATION_REQUIRED", page_url)
    if st.path_matches(page_url, patterns_l):
        raise JobAttention("LOGIN_REQUIRED", page_url)
