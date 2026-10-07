"""FastAPI application: the Neko automation API.

Every /api/* route requires ``Authorization: Bearer $NEKO_AUTOMATION_TOKEN``.
The router is included at both / and the optional NEKO_AUTOMATION_PREFIX so
it works behind path-prefix routing proxies with or without prefix
stripping.
"""
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .config import CFG
from .security import AUDIT, require_auth
from . import browser, tabs as tabops
from .bidi import BIDI
from .contexts import CONTEXTS
from .sessions import session_status
from .sites import SITES
from .jobs import JOBS, JOB_TYPES
from . import generic_jobs  # noqa: registers scrape.urls / scrape.search
from .adapters import all_adapters, build_recipe_job

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("neko_automation")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(CFG.STATE_DIR, exist_ok=True)
    JOBS.load_all()
    await BIDI.start()
    await JOBS.start()
    log.info("automation service up (token configured: %s)",
             "yes" if CFG.TOKEN else "NO - API disabled")
    try:
        yield
    finally:
        await JOBS.stop()
        await CONTEXTS.shutdown()
        await BIDI.stop()
        log.info("automation service down")


r = APIRouter()


# ------------------------------------------------------------------ health
@r.get("/health")
async def health():
    return {"status": "ok", "service": "neko-automation", "ts": time.time()}


# ------------------------------------------------------------------ status
@r.get("/api/automation/status")
async def status():
    progs = browser.supervisor_status()
    return {
        "service": "neko-automation",
        "neko_running": progs.get("neko") == "RUNNING",
        "browser_running": browser.firefox_running(),
        "browser_type": browser.browser_kind(),
        "automation_available": True,
        "bidi": {
            "connected": BIDI.connected,
            "session": BIDI.session_id,
            "connected_at": BIDI.connected_at,
            "last_error": BIDI.last_error,
        },
        "profile": browser.profile_info(),
        "playwright": {"available": True,
                       "contexts": len(CONTEXTS.contexts),
                       "pages": len(CONTEXTS.pages)},
        "jobs": {"known_types": sorted(JOB_TYPES),
                 "tracked": len(JOBS.jobs)},
        "supervisor": progs,
        "auth": {"token_configured": bool(CFG.TOKEN),
                 "cookie_export_enabled": CFG.ALLOW_COOKIE_EXPORT},
    }


# ------------------------------------------------------------------- sites
class SiteConfigIn(BaseModel):
    name: str
    domains: list
    auth_cookies: list = []
    login_path_patterns: list = ["*login*", "*signin*"]
    verification_path_patterns: list = ["*captcha*", "*verify*"]
    probe_url: Optional[str] = None
    probe_ua: Optional[str] = None
    notes: Optional[str] = None


@r.get("/api/automation/sites")
async def list_sites():
    return {"builtin_and_user": SITES.all_sites()}


@r.post("/api/automation/sites")
async def register_site(cfg: SiteConfigIn):
    AUDIT.log("site.register", name=cfg.name, domains=cfg.domains)
    return {"registered": SITES.register(cfg.model_dump())}


@r.delete("/api/automation/sites/{name}")
async def unregister_site(name: str):
    ok = SITES.unregister(name)
    AUDIT.log("site.unregister", name=name, ok=ok)
    return {"removed": ok}


# -------------------------------------------------------------------- tabs
@r.get("/api/automation/tabs")
async def get_tabs(include_auth: bool = True):
    return await tabops.list_tabs(include_auth=include_auth)


@r.get("/api/automation/tabs/active")
async def get_active_tab():
    t = await tabops.active_tab()
    if not t:
        raise HTTPException(404, "no tabs / browser unreachable")
    t["auth_state"] = tabops.quick_auth(t.get("domain", ""))
    return t


@r.get("/api/automation/tabs/find")
async def find_tabs(pattern: str):
    return {"pattern": pattern, "tabs": await tabops.find_tabs(pattern)}


@r.get("/api/automation/tabs/{context_id}")
async def get_tab(context_id: str):
    res = await tabops.list_tabs(include_auth=False)
    for t in res["tabs"]:
        if t["id"] == context_id:
            t["auth_state"] = tabops.quick_auth(t.get("domain", ""))
            return t
    raise HTTPException(404, f"tab {context_id} not found")


@r.post("/api/automation/tabs")
async def open_tab(url: str, background: bool = False):
    AUDIT.log("tab.open", url=url)
    out = await tabops.open_tab(url, background=background)
    if "error" in out:
        raise HTTPException(502, out)
    return out


@r.post("/api/automation/tabs/{context_id}/activate")
async def activate_tab(context_id: str):
    out = await tabops.tab_op(context_id, "activate")
    if "error" in out:
        raise HTTPException(502, out)
    return out


@r.post("/api/automation/tabs/{context_id}/navigate")
async def navigate_tab(context_id: str, url: str):
    AUDIT.log("tab.navigate", context=context_id, url=url)
    out = await tabops.tab_op(context_id, "navigate", url=url)
    if "error" in out:
        raise HTTPException(502, out)
    return out


@r.post("/api/automation/tabs/{context_id}/reload")
async def reload_tab(context_id: str):
    out = await tabops.tab_op(context_id, "reload")
    if "error" in out:
        raise HTTPException(502, out)
    return out


@r.post("/api/automation/tabs/{context_id}/close")
async def close_tab(context_id: str):
    AUDIT.log("tab.close", context=context_id)
    out = await tabops.tab_op(context_id, "close")
    if "error" in out:
        raise HTTPException(502, out)
    return out


@r.post("/api/automation/tabs/{context_id}/screenshot")
async def tab_screenshot(context_id: str):
    out = await tabops.tab_screenshot(context_id)
    if "error" in out:
        raise HTTPException(502, out)
    return out


class EvalIn(BaseModel):
    expression: str
    await_promise: bool = False


@r.post("/api/automation/tabs/{context_id}/evaluate")
async def tab_evaluate(context_id: str, body: EvalIn):
    AUDIT.log("tab.evaluate", context=context_id,
              expr_len=len(body.expression))
    out = await tabops.tab_evaluate(context_id, body.expression,
                                    await_promise=body.await_promise)
    if "error" in out:
        raise HTTPException(502, out)
    return out


# ----------------------------------------------------------------- session
@r.get("/api/automation/session/status")
async def session_status_route(domain: str, probe: bool = False,
                               probe_url: Optional[str] = None):
    return await session_status(domain, probe=probe, probe_url=probe_url)


# ------------------------------------------------------- cookie export (gated)
class CookieExportIn(BaseModel):
    domain: str
    names: Optional[list] = None


@r.post("/api/automation/admin/cookies")
async def export_cookies(body: CookieExportIn):
    if not CFG.ALLOW_COOKIE_EXPORT:
        AUDIT.log("cookie.export", ok=False, reason="disabled")
        raise HTTPException(403, "cookie export is disabled "
                                 "(NEKO_AUTOMATION_ALLOW_COOKIE_EXPORT=false)")
    AUDIT.log("cookie.export", domain=body.domain)
    from . import cookies as ck
    cookies = ck.read_cookies(domain=body.domain, names=body.names)
    return {"domain": body.domain,
            "format": "playwright",
            "cookies": ck.to_playwright(cookies)}


# --------------------------------------------------------------- contexts
class ContextIn(BaseModel):
    domain: Optional[str] = None
    reuse_existing_session: bool = True
    user_agent: Optional[str] = None
    viewport: Optional[dict] = None
    is_mobile: bool = False
    locale: str = "zh-CN"
    timezone_id: str = "Asia/Shanghai"


class NewPageIn(BaseModel):
    url: Optional[str] = None


@r.post("/api/automation/context")
async def create_context(body: ContextIn):
    AUDIT.log("context.create", domain=body.domain,
              reuse=body.reuse_existing_session)
    try:
        ctx_id = await CONTEXTS.create(
            domain=body.domain,
            reuse_existing_session=body.reuse_existing_session,
            user_agent=body.user_agent, viewport=body.viewport,
            is_mobile=body.is_mobile, locale=body.locale,
            timezone_id=body.timezone_id)
    except Exception as e:
        raise HTTPException(502, f"context creation failed: {e}")
    return {"context_id": ctx_id,
            "hint": f"POST /api/automation/contexts/{ctx_id}/pages to open a page"}


@r.get("/api/automation/contexts")
async def list_contexts():
    return {"contexts": await CONTEXTS.list_contexts()}


@r.delete("/api/automation/contexts/{ctx_id}")
async def close_context(ctx_id: str):
    await CONTEXTS.close(ctx_id)
    return {"closed": ctx_id}


@r.post("/api/automation/contexts/{ctx_id}/pages")
async def new_page(ctx_id: str, body: NewPageIn = None):
    url = body.url if body else None
    try:
        page = await CONTEXTS.new_page(ctx_id, url=url)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"page_id": page.page_id, "url": page.url}


@r.get("/api/automation/contexts/{ctx_id}/pages")
async def list_pages(ctx_id: str):
    return {"pages": [pid for pid, p in CONTEXTS.pages.items()
                      if p.ctx_id == ctx_id]}


# ------------------------------------------------------------------ pages
def _page(page_id: str):
    try:
        return CONTEXTS.page(page_id)
    except KeyError:
        raise HTTPException(404, f"unknown page {page_id}")


class NavigateIn(BaseModel):
    url: str
    wait_until: str = "domcontentloaded"
    timeout_ms: int = 45000


@r.post("/api/automation/pages/{page_id}/navigate")
async def page_navigate(page_id: str, body: NavigateIn):
    p = _page(page_id)
    try:
        return await p.navigate(body.url, wait_until=body.wait_until,
                                timeout=body.timeout_ms)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


@r.get("/api/automation/pages/{page_id}")
async def page_info(page_id: str):
    p = _page(page_id)
    return {"page_id": p.page_id, "url": p.url, "title": await p.title()}


@r.get("/api/automation/pages/{page_id}/content")
async def page_content(page_id: str):
    return PlainTextResponse(await _page(page_id).content())


@r.get("/api/automation/pages/{page_id}/text")
async def page_text(page_id: str):
    return PlainTextResponse(await _page(page_id).text())


@r.post("/api/automation/pages/{page_id}/evaluate")
async def page_evaluate(page_id: str, body: EvalIn):
    p = _page(page_id)
    AUDIT.log("page.evaluate", page=page_id, expr_len=len(body.expression))
    try:
        return {"result": await p.evaluate(body.expression,
                                           await_promise=body.await_promise)}
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


class ClickIn(BaseModel):
    selector: str
    timeout_ms: int = 10000


@r.post("/api/automation/pages/{page_id}/click")
async def page_click(page_id: str, body: ClickIn):
    p = _page(page_id)
    try:
        return await p.click(body.selector, timeout=body.timeout_ms)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


class FillIn(BaseModel):
    selector: str
    value: str
    timeout_ms: int = 10000


@r.post("/api/automation/pages/{page_id}/fill")
async def page_fill(page_id: str, body: FillIn):
    p = _page(page_id)
    try:
        return await p.fill(body.selector, body.value, timeout=body.timeout_ms)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


class ScrollIn(BaseModel):
    dy: int = 1000
    dx: int = 0


@r.post("/api/automation/pages/{page_id}/scroll")
async def page_scroll(page_id: str, body: ScrollIn):
    return await _page(page_id).scroll(body.dy, body.dx)


class WaitIn(BaseModel):
    selector: Optional[str] = None
    text: Optional[str] = None
    ms: Optional[int] = None
    state: str = "visible"
    timeout_ms: int = 20000
    network_idle: bool = False


@r.post("/api/automation/pages/{page_id}/wait")
async def page_wait(page_id: str, body: WaitIn):
    p = _page(page_id)
    if body.network_idle:
        return await p.wait_network_idle(timeout=body.timeout_ms)
    return await p.wait(selector=body.selector, text=body.text, ms=body.ms,
                        state=body.state, timeout=body.timeout_ms)


@r.get("/api/automation/pages/{page_id}/query")
async def page_query(page_id: str, selector: str, limit: int = 50):
    return {"items": await _page(page_id).query(selector, limit)}


@r.post("/api/automation/pages/{page_id}/screenshot")
async def page_screenshot(page_id: str, full_page: bool = False):
    b64 = await _page(page_id).screenshot(full_page=full_page)
    return {"image_png_b64": b64}


@r.get("/api/automation/pages/{page_id}/network")
async def page_network(page_id: str, pattern: Optional[str] = None,
                       limit: int = 50, decode_json: bool = False):
    p = _page(page_id)
    if decode_json:
        return {"entries": p.network.bodies_json(pattern=pattern, limit=limit)}
    return {"entries": p.network.list(pattern=pattern, limit=limit)}


class WaitNetIn(BaseModel):
    pattern: str
    timeout_ms: int = 20000


@r.post("/api/automation/pages/{page_id}/wait-for-response")
async def page_wait_response(page_id: str, body: WaitNetIn):
    p = _page(page_id)
    entry = await p.network.wait_for(body.pattern, timeout=body.timeout_ms / 1000)
    if not entry:
        raise HTTPException(404, f"no response matching {body.pattern} "
                                 f"within {body.timeout_ms}ms")
    return {"entry": entry}


@r.delete("/api/automation/pages/{page_id}")
async def page_close(page_id: str):
    await _page(page_id).close()
    return {"closed": page_id}


# -------------------------------------------------------------------- jobs
class JobIn(BaseModel):
    type: str
    params: dict = {}


@r.post("/api/automation/jobs")
async def create_job(body: JobIn):
    AUDIT.log("job.submit", type=body.type)
    try:
        job = JOBS.submit(body.type, body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return job.to_dict(with_result=False)


@r.get("/api/automation/jobs")
async def list_jobs():
    return {"jobs": [j.to_dict(with_result=False)
                     for j in sorted(JOBS.jobs.values(),
                                     key=lambda j: -j.created)]}


@r.get("/api/automation/jobs/{job_id}")
async def get_job(job_id: str, with_result: bool = True):
    job = JOBS.jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"no such job {job_id}")
    return job.to_dict(with_result=with_result)


@r.post("/api/automation/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    try:
        return JOBS.cancel(job_id).to_dict(with_result=False)
    except KeyError:
        raise HTTPException(404, f"no such job {job_id}")


@r.post("/api/automation/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    AUDIT.log("job.resume", job=job_id)
    try:
        return JOBS.resume(job_id).to_dict(with_result=False)
    except KeyError:
        raise HTTPException(404, f"no such job {job_id}")
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------- adapters
@r.get("/api/automation/adapters")
async def adapters():
    return {"adapters": all_adapters()}


class RecipeIn(BaseModel):
    adapter: str
    recipe: str
    query: dict = {}


@r.post("/api/automation/adapters/recipes/run")
async def run_recipe(body: RecipeIn):
    AUDIT.log("recipe.run", adapter=body.adapter, recipe=body.recipe)
    try:
        spec = build_recipe_job(body.adapter, body.recipe, body.query)
    except ValueError as e:
        raise HTTPException(400, str(e))
    job = JOBS.submit(spec["job_type"], spec["params"])
    return job.to_dict(with_result=False)


# ------------------------------------------------------------------- app
app = FastAPI(title="Neko Automation API", version="1.0.0",
              lifespan=lifespan, docs_url=None, redoc_url=None)
app.middleware("http")(require_auth)
app.include_router(r)
if CFG.PREFIX:
    app.include_router(r, prefix=CFG.PREFIX)
