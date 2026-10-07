"""Playwright context manager with Firefox-session reuse.

This is the proven approach from the Pinduoduo pipeline: launch headless
Chromium from inside the Neko container (same network identity / IP as the
user's browser), inject the cookies read from the live Firefox profile, and
render pages that require JavaScript. The automation therefore shares the
user's logged-in sessions WITHOUT touching the visible browser.
"""
import asyncio
import logging
import time
import uuid
from typing import Optional

log = logging.getLogger("neko_automation.contexts")


class ManagedPage:
    def __init__(self, manager, ctx_id: str, page):
        from .network import NetworkTracker
        self.manager = manager
        self.ctx_id = ctx_id
        self.page_id = uuid.uuid4().hex[:12]
        self.page = page
        self.network = NetworkTracker()
        self.created = time.time()
        self.last_used = time.time()
        page.on("response", self.network.on_response)

    async def touch(self):
        self.last_used = time.time()
        self.manager.touch(self.ctx_id)

    # ------------------------------------------------------------- helpers
    async def _eval(self, expression: str, await_promise: bool = False,
                    timeout: float = 30):
        await self.touch()
        return await self.page.evaluate(
            expression, arg=None) if not await_promise else \
            await self.page.evaluate(
                f"async () => {{ return await (async () => {{ {expression} }})() }}")

    # -------------------------------------------------------------- ops
    async def navigate(self, url: str, wait_until: str = "domcontentloaded",
                       timeout: float = 45000) -> dict:
        await self.touch()
        await self.page.goto(url, wait_until=wait_until, timeout=timeout)
        return {"url": self.page.url}

    @property
    def url(self) -> str:
        return self.page.url

    async def title(self) -> str:
        await self.touch()
        return await self.page.title()

    async def content(self) -> str:
        await self.touch()
        return await self.page.content()

    async def text(self) -> str:
        await self.touch()
        return await self.page.evaluate("document.body ? document.body.innerText : ''")

    async def evaluate(self, expression: str, await_promise: bool = False):
        await self.touch()
        if await_promise:
            wrapped = ("(async () => { try { return await (async () => { %s })(); }"
                       " catch (e) { return {__error__: String(e)}; } })()") % expression
            return await self.page.evaluate(wrapped)
        return await self.page.evaluate(expression)

    async def click(self, selector: str, timeout: float = 10000) -> dict:
        await self.touch()
        await self.page.click(selector, timeout=timeout)
        return {"url": self.page.url}

    async def fill(self, selector: str, value: str, timeout: float = 10000) -> dict:
        await self.touch()
        await self.page.fill(selector, value, timeout=timeout)
        return {"url": self.page.url}

    async def press(self, key: str):
        await self.touch()
        await self.page.keyboard.press(key)
        return {"url": self.page.url}

    async def scroll(self, dy: int = 1000, dx: int = 0):
        await self.touch()
        await self.page.mouse.wheel(dx, dy)
        return {"url": self.page.url}

    async def query(self, selector: str, limit: int = 50) -> list:
        await self.touch()
        return await self.page.evaluate(
            """(sel, limit) => {
                const els = Array.from(document.querySelectorAll(sel)).slice(0, limit);
                return els.map(e => ({
                    tag: e.tagName.toLowerCase(),
                    text: (e.innerText || '').slice(0, 300),
                    attrs: Object.fromEntries(
                        Array.from(e.attributes).slice(0, 12)
                             .map(a => [a.name, a.value.slice(0, 300)]))
                }));
            }""", {"sel": selector, "limit": limit}) or []

    async def wait(self, selector: Optional[str] = None, text: Optional[str] = None,
                   ms: Optional[int] = None, state: str = "visible",
                   timeout: float = 20000) -> dict:
        await self.touch()
        if ms:
            await self.page.wait_for_timeout(ms)
        elif selector:
            await self.page.wait_for_selector(selector, state=state, timeout=timeout)
        elif text:
            await self.page.wait_for_selector(f"text={text}", timeout=timeout)
        else:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        return {"url": self.page.url}

    async def wait_network_idle(self, timeout: float = 20000) -> dict:
        await self.touch()
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass
        return {"url": self.page.url}

    async def screenshot(self, full_page: bool = False) -> str:
        await self.touch()
        return await self.page.screenshot(full_page=full_page, type="png")

    async def close(self):
        try:
            await self.page.close()
        except Exception:
            pass
        self.manager.pages.pop(self.page_id, None)


class ContextManager:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self.contexts = {}   # ctx_id -> {"ctx": pw_context, "meta": {...}}
        self.pages = {}      # page_id -> ManagedPage

    async def ensure_browser(self):
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return
            from playwright.async_api import async_playwright
            if self._pw is None:
                self._pw = await async_playwright().start()
            try:
                self._browser = await self._pw.chromium.launch(headless=True)
            except Exception as e:
                log.error("chromium launch failed: %s", e)
                raise

    async def create(self, domain: Optional[str] = None,
                     reuse_existing_session: bool = True,
                     user_agent: Optional[str] = None,
                     viewport: Optional[dict] = None,
                     is_mobile: bool = False,
                     locale: str = "zh-CN",
                     timezone_id: str = "Asia/Shanghai",
                     extra: Optional[dict] = None) -> str:
        await self.ensure_browser()
        ctx = await self._browser.new_context(
            user_agent=user_agent,
            viewport=viewport or ({"width": 390, "height": 844} if is_mobile
                                  else {"width": 1366, "height": 900}),
            device_scale_factor=2 if is_mobile else 1,
            is_mobile=is_mobile, has_touch=is_mobile,
            locale=locale, timezone_id=timezone_id)
        if reuse_existing_session:
            from . import cookies as ck
            cookies = ck.read_cookies(domain=domain)
            if cookies:
                await ctx.add_cookies(ck.to_playwright(cookies))
        ctx_id = uuid.uuid4().hex[:12]
        self.contexts[ctx_id] = {
            "ctx": ctx, "created": time.time(), "last_used": time.time(),
            "meta": {"domain": domain, "reuse": reuse_existing_session,
                     "user_agent": user_agent, "is_mobile": is_mobile}}
        return ctx_id

    def touch(self, ctx_id: str):
        if ctx_id in self.contexts:
            self.contexts[ctx_id]["last_used"] = time.time()

    async def new_page(self, ctx_id: str, url: Optional[str] = None) -> ManagedPage:
        if ctx_id not in self.contexts:
            raise KeyError(f"unknown context {ctx_id}")
        entry = self.contexts[ctx_id]
        page = await entry["ctx"].new_page()
        mp = ManagedPage(self, ctx_id, page)
        self.pages[mp.page_id] = mp
        if url:
            await mp.navigate(url)
        return mp

    def page(self, page_id: str) -> ManagedPage:
        if page_id not in self.pages:
            raise KeyError(f"unknown page {page_id}")
        return self.pages[page_id]

    async def close(self, ctx_id: str):
        entry = self.contexts.pop(ctx_id, None)
        if not entry:
            return
        for pid in [pid for pid, p in self.pages.items() if p.ctx_id == ctx_id]:
            self.pages.pop(pid, None)
        try:
            await entry["ctx"].close()
        except Exception:
            pass

    async def list_contexts(self) -> list:
        out = []
        for ctx_id, e in self.contexts.items():
            pages = [pid for pid, p in self.pages.items() if p.ctx_id == ctx_id]
            out.append({"id": ctx_id, "pages": pages,
                        "age_s": int(time.time() - e["created"]),
                        "idle_s": int(time.time() - e["last_used"]),
                        **e["meta"]})
        return out

    async def reap_idle(self, ttl: int):
        from .config import CFG
        now = time.time()
        for ctx_id, e in list(self.contexts.items()):
            if now - e["last_used"] > (ttl or CFG.CONTEXT_IDLE_TTL):
                await self.close(ctx_id)

    async def shutdown(self):
        for ctx_id in list(self.contexts):
            await self.close(ctx_id)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass


CONTEXTS = ContextManager()
