"""Persistent WebDriver-BiDi client for the live Neko Firefox.

Firefox 157+ serves WebDriver BiDi on a plain WebSocket at
``ws://127.0.0.1:9222/session`` (the old CDP HTTP endpoints are gone).
Lifecycle rules learned against Firefox 157:

* ``session.new`` binds the session to the *current* connection.
* A short settle period is required after ``session.new`` before further
  commands are accepted ("invalid session id" race).
* Sessions are NOT freed when the WebSocket closes - they leak until
  Firefox restarts, and Firefox allows only a small number of concurrent
  sessions. Therefore this client keeps exactly ONE long-lived connection
  and deletes its session on graceful shutdown.
"""
import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets

from .config import CFG

log = logging.getLogger("neko_automation.bidi")


class BidiError(RuntimeError):
    def __init__(self, error: str, message: str = ""):
        super().__init__(f"{error}: {message}")
        self.error = error
        self.message = message


class BidiClient:
    def __init__(self, url: str = None):
        self.url = url or CFG.BIDI_URL
        self.ws = None
        self.session_id: Optional[str] = None
        self._id = 0
        self._lock = asyncio.Lock()
        self._events: list = []          # recent events (ring)
        self._event_waiters: list = []   # futures for wait_for_event
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self.connected_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.restarts_used = 0

    # ------------------------------------------------------------------ core
    async def _send(self, method: str, params: dict, timeout: float = 60) -> dict:
        if self.ws is None:
            raise BidiError("not connected", "BiDi websocket is not connected")
        self._id += 1
        mid = self._id
        payload = json.dumps({"id": mid, "method": method, "params": params})
        await self.ws.send(payload)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BidiError("timeout", f"{method} timed out after {timeout}s")
            try:
                raw = await asyncio.wait_for(self.ws.recv(), remaining)
            except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                raise BidiError("connection", str(e)[:120])
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if msg.get("type") == "error":
                    raise BidiError(msg.get("error", "error"),
                                    msg.get("message", ""))
                return msg.get("result", {})

    async def cmd(self, method: str, params: dict = None, timeout: float = 60) -> dict:
        async with self._lock:
            return await self._send(method, params or {}, timeout)

    async def _new_session(self) -> None:
        r = await self._send("session.new", {"capabilities": {}}, timeout=30)
        self.session_id = r.get("sessionId")
        await asyncio.sleep(CFG.BIDI_SETTLE)
        self.connected_at = time.time()

    # ------------------------------------------------------------ connection
    async def _connect_once(self) -> bool:
        try:
            self.ws = await websockets.connect(
                self.url, max_size=128 * 1024 * 1024, open_timeout=15)
        except Exception as e:
            self.last_error = str(e)[:160]
            return False
        try:
            await self._new_session()
        except BidiError as e:
            self.last_error = f"{e.error}: {e.message[:120]}"
            await self._close_ws()
            if e.error == "session not created" and "Maximum number" in e.message:
                # leaked sessions block us; restart firefox as last resort
                if CFG.ALLOW_BROWSER_RESTART and self.restarts_used < 3:
                    log.warning("BiDi sessions exhausted; restarting firefox")
                    if await self._restart_firefox():
                        self.restarts_used += 1
                        await asyncio.sleep(8)
            return False
        except Exception as e:
            self.last_error = str(e)[:160]
            await self._close_ws()
            return False
        self.last_error = None
        self.restarts_used = 0
        log.info("BiDi session established (%s)", self.session_id)
        return True

    async def _close_ws(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def _restart_firefox(self) -> bool:
        try:
            import subprocess
            r = subprocess.run(["supervisorctl", "-c", CFG.SUPERVISOR_CONF,
                                "restart", "firefox"],
                               capture_output=True, text=True, timeout=120)
            return r.returncode == 0
        except Exception as e:
            log.error("firefox restart failed: %s", e)
            return False

    async def _run(self) -> None:
        """Supervise the connection forever (with backoff)."""
        backoff = 2
        while not self._stop:
            ok = await self._connect_once()
            if ok:
                backoff = 2
                try:
                    # keepalive: cheap command; detect dead connections
                    while not self._stop:
                        await asyncio.sleep(20)
                        await self.cmd("session.status", timeout=20)
                except Exception as e:
                    log.warning("BiDi connection lost: %s", str(e)[:120])
                    try:
                        await self.cmd("session.delete", timeout=5)
                    except Exception:
                        pass
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        try:
            await self.cmd("session.delete", timeout=5)
        except Exception:
            pass
        await self._close_ws()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def connected(self) -> bool:
        return self.ws is not None and self.session_id is not None

    # ------------------------------------------------------------- operations
    async def get_tree(self, root: str = None) -> list:
        params = {"root": root} if root else {}
        r = await self.cmd("browsingContext.getTree", params)
        return r.get("contexts", [])

    async def create_tab(self, url: str = None, background: bool = False) -> dict:
        params = {"type": "tab"}
        if url:
            params["url"] = url
        r = await self.cmd("browsingContext.create", params)
        return r

    async def navigate(self, context: str, url: str, wait: str = "none") -> dict:
        return await self.cmd("browsingContext.navigate",
                              {"context": context, "url": url, "wait": wait})

    async def activate(self, context: str) -> dict:
        return await self.cmd("browsingContext.activate", {"context": context})

    async def close_tab(self, context: str) -> dict:
        return await self.cmd("browsingContext.close", {"context": context})

    async def reload(self, context: str, ignore_cache: bool = False) -> dict:
        return await self.cmd("browsingContext.reload",
                              {"context": context, "ignoreCache": ignore_cache})

    async def capture_screenshot(self, context: str) -> str:
        r = await self.cmd("browsingContext.captureScreenshot",
                           {"context": context, "format": "image/png"})
        return r.get("data", "")

    async def evaluate(self, context: str, expression: str,
                       await_promise: bool = False, timeout: float = 60) -> Any:
        r = await self.cmd("script.evaluate", {
            "expression": expression,
            "target": {"context": context},
            "awaitPromise": await_promise,
        }, timeout=timeout)
        res = r.get("result", {})
        if res.get("type") == "undefined":
            return None
        return res.get("value")


BIDI = BidiClient()
