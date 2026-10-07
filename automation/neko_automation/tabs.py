"""Tab discovery and control for the live Neko Firefox.

Primary source: WebDriver BiDi (real-time, full control).
Fallback: sessionstore recovery.jsonlz4 (read-only, ~15s stale) when BiDi
is unavailable.
"""
import asyncio
from typing import Optional
from urllib.parse import urlsplit

from . import sites as st
from .bidi import BIDI, BidiError

READY_JS = ("document.readyState + '|' + document.title")


def _domain(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except Exception:
        return ""


async def _tab_details(ctx: dict) -> dict:
    """Enrich a BiDi context dict with title/readyState/auth guess."""
    tab = {
        "id": ctx["context"],
        "url": ctx.get("url", ""),
        "children": len(ctx.get("children", [])),
        "domain": _domain(ctx.get("url", "")),
    }
    try:
        info = await BIDI.evaluate(ctx["context"], READY_JS)
        if isinstance(info, str) and "|" in info:
            ready, title = info.split("|", 1)
            tab["ready_state"] = ready
            tab["title"] = title
            tab["loading"] = ready != "complete"
        else:
            tab["title"] = None
            tab["ready_state"] = None
            tab["loading"] = False
    except Exception:
        tab["title"] = None
        tab["ready_state"] = None
        tab["loading"] = None
    return tab


async def list_tabs(include_auth: bool = True) -> dict:
    source = "bidi"
    tabs = []
    bidi_ok = BIDI.connected
    if bidi_ok:
        try:
            tree = await BIDI.get_tree()
            # top-level contexts are tabs; flatten one level of iframes info
            for ctx in tree:
                tabs.append(await _tab_details(ctx))
        except BidiError:
            bidi_ok = False
    if not bidi_ok:
        from .sessionstore import read_recovery_tabs
        source = "sessionstore"
        for t in read_recovery_tabs():
            tabs.append({"id": t["id"], "url": t["url"], "title": t.get("title"),
                         "domain": _domain(t["url"]), "loading": None,
                         "ready_state": None, "selected": t.get("selected", False)})
    if include_auth:
        for tab in tabs:
            tab["auth_state"] = quick_auth(tab["domain"])
    return {"source": source, "bidi_connected": bidi_ok, "tabs": tabs}


def quick_auth(domain: str) -> str:
    """Cheap cookie-based auth guess for a tab's domain (no probe)."""
    if not domain:
        return "UNKNOWN"
    try:
        from . import cookies as ck
        site = st.SITES.for_domain(domain)
        if site:
            sig = ck.auth_signals(domain, site["auth_cookies"])
            if sig["fresh_count"] == 0:
                return "LOGIN_REQUIRED"
            if sig["fresh_count"] >= max(1, len(site["auth_cookies"]) - 1):
                return "AUTHENTICATED"
            return "PROBABLE_AUTHENTICATED"
        names = st.generic_auth_cookie_names(domain)
        if names:
            return "PROBABLE_AUTHENTICATED"
        return "NO_SESSION"
    except Exception:
        return "UNKNOWN"


async def find_tabs(pattern: str) -> list:
    """Find tabs whose URL or domain matches a substring/regex."""
    import re
    res = await list_tabs(include_auth=False)
    out = []
    for t in res["tabs"]:
        hay = f"{t['url']} {t['domain']}"
        try:
            hit = re.search(pattern, hay)
        except re.error:
            hit = pattern in hay
        if hit:
            out.append(t)
    return out


async def active_tab() -> Optional[dict]:
    """The foreground tab: BiDi has no direct 'active' query, so we use the
    window's selected tab via a tiny evaluate on each context... practically
    we take the tab whose document has focus."""
    res = await list_tabs(include_auth=False)
    for t in res["tabs"]:
        if t.get("selected"):
            return t
    try:
        tree = await BIDI.get_tree()
        for ctx in tree:
            focused = await BIDI.evaluate(ctx["context"], "document.hasFocus()")
            if focused:
                return await _tab_details(ctx)
    except Exception:
        pass
    return res["tabs"][0] if res["tabs"] else None


async def open_tab(url: str, background: bool = False) -> dict:
    r = await BIDI.create_tab(url=url)
    ctx = r.get("context")
    if not ctx:
        return {"error": "no context returned"}
    if not background:
        try:
            await BIDI.activate(ctx)
        except BidiError:
            pass
    tab = await _tab_details({"context": ctx, "url": url})
    return tab


async def tab_op(context_id: str, op: str, url: Optional[str] = None) -> dict:
    ops = {
        "activate": lambda: BIDI.activate(context_id),
        "close": lambda: BIDI.close_tab(context_id),
        "reload": lambda: BIDI.reload(context_id),
        "navigate": lambda: BIDI.navigate(context_id, url),
    }
    if op not in ops:
        return {"error": f"unknown op {op}"}
    try:
        await ops[op]()
        if op in ("activate", "reload", "navigate"):
            try:
                tree = await BIDI.get_tree(root=context_id)
                if tree:
                    return await _tab_details(tree[0])
            except BidiError:
                pass
        return {"ok": True}
    except BidiError as e:
        return {"error": e.error, "message": e.message[:200]}


async def tab_screenshot(context_id: str) -> dict:
    try:
        data = await BIDI.capture_screenshot(context_id)
        return {"image_png_b64": data} if data else {"error": "empty screenshot"}
    except BidiError as e:
        return {"error": e.error, "message": e.message[:200]}


async def tab_evaluate(context_id: str, expression: str,
                       await_promise: bool = False) -> dict:
    try:
        val = await BIDI.evaluate(context_id, expression,
                                  await_promise=await_promise)
        return {"result": val}
    except BidiError as e:
        return {"error": e.error, "message": e.message[:200]}
