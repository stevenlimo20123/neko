"""Authentication-state detection for any domain.

States reported:
  AUTHENTICATED            configured auth cookies present, fresh, and
                           (when probed) the site shows a logged-in state
  PROBABLE_AUTHENTICATED   heuristic cookie signals only (no site config)
  LOGIN_REQUIRED           no auth cookies, or probe redirected to login
  VERIFICATION_REQUIRED    probe landed on a captcha/verification page
  SESSION_EXPIRED          auth cookies exist but the live probe says
                           they are no longer accepted
  NO_SESSION               no cookies for this domain at all
  UNKNOWN                  cannot determine
"""
import asyncio
from typing import Optional

from . import cookies as ck
from . import sites as st


def _cookie_state(domain: str) -> dict:
    site = st.SITES.for_domain(domain)
    if site:
        names = site["auth_cookies"]
        sig = ck.auth_signals(domain, names)
        total = len(ck.read_cookies(domain=domain))
        if sig["fresh_count"] == 0 and total == 0:
            state = "NO_SESSION"
        elif sig["fresh_count"] == 0:
            state = "LOGIN_REQUIRED"
        elif sig["fresh_count"] >= max(1, len(names) - 1):
            state = "AUTHENTICATED"
        else:
            state = "PROBABLE_AUTHENTICATED"
        return {"state": state, "site": site["name"], "signals": {
                    "present": sig["present"], "missing": sig["missing"]},
                "total_cookies": total}
    # unknown site -> generic heuristic
    names = st.generic_auth_cookie_names(domain)
    total = len(ck.read_cookies(domain=domain))
    if total == 0:
        return {"state": "NO_SESSION", "site": None,
                "signals": {"present": [], "missing": []},
                "total_cookies": 0, "heuristic": True}
    if not names:
        return {"state": "UNKNOWN", "site": None,
                "signals": {"present": [], "missing": []},
                "total_cookies": total, "heuristic": True}
    return {"state": "PROBABLE_AUTHENTICATED", "site": None,
            "signals": {"present": names, "missing": []},
            "total_cookies": total, "heuristic": True}


PROBE_JS = """(() => {
  const txt = (document.body ? document.body.innerText : '').slice(0, 4000);
  const markers = {
    login: /\\b(log ?in|sign ?in|登录|登陸|登陆)\\b/i.test(txt) &&
            !!document.querySelector(
              'a[href*="login"], button, [class*="login"], [id*="login"]'),
    userish: /\\b(log ?out|sign ?out|退出登录|我的账户|account|personal)\\b/i.test(txt)
  };
  return JSON.stringify({url: location.href, readyState: document.readyState,
                         markers});
})()"""


async def probe_domain(domain: str, probe_url: Optional[str] = None,
                       user_agent: Optional[str] = None,
                       timeout_ms: int = 25000) -> dict:
    """Live probe: visit a URL with the Firefox session cookies injected
    into a fresh Playwright page and classify where we land."""
    from .contexts import CONTEXTS
    site = st.SITES.for_domain(domain)
    if not probe_url:
        probe_url = site.get("probe_url") if site else None
        if not probe_url:
            probe_url = f"https://{domain}/"
    ua = user_agent or (site or {}).get("probe_ua")

    ctx_id, page_id = None, None
    try:
        ctx_id = await CONTEXTS.create(domain=domain, reuse_existing_session=True,
                                       user_agent=ua)
        page = await CONTEXTS.new_page(ctx_id)
        page_id = page.page_id
        await page.navigate(probe_url, wait_until="domcontentloaded",
                            timeout=timeout_ms)
        await asyncio.sleep(2.5)
        url = page.url
        raw = await page.evaluate(PROBE_JS, await_promise=False) or "{}"
        import json as _json
        info = _json.loads(raw) if isinstance(raw, str) else {}
        login_hit = st.path_matches(url, (site or {}).get("login_path_patterns",
                                                          ["*login*"]))
        verify_hit = st.path_matches(url, (site or {}).get(
            "verification_path_patterns", ["*captcha*", "*verify*"]))
        if verify_hit:
            return {"probe": {"url": url, "verdict": "VERIFICATION_REQUIRED"}}
        if login_hit:
            return {"probe": {"url": url, "verdict": "LOGIN_REQUIRED"}}
        markers = info.get("markers", {})
        if markers.get("userish") and not markers.get("login"):
            return {"probe": {"url": url, "verdict": "AUTHENTICATED"}}
        if markers.get("login") and not markers.get("userish"):
            return {"probe": {"url": url, "verdict": "LOGIN_REQUIRED"}}
        # page loaded without login markers: cannot prove either way,
        # but a redirect to login would have been caught above
        return {"probe": {"url": url, "verdict": "RENDERED",
                          "readyState": info.get("readyState")}}
    except Exception as e:
        return {"probe": {"error": str(e)[:200]}}
    finally:
        try:
            if ctx_id:
                await CONTEXTS.close(ctx_id)
        except Exception:
            pass


async def session_status(domain: str, probe: bool = False,
                          probe_url: Optional[str] = None) -> dict:
    base = _cookie_state(domain)
    if probe or probe_url:
        base.update(await probe_domain(domain, probe_url=probe_url))
        pr = base.get("probe", {})
        verdict = pr.get("verdict")
        if verdict == "LOGIN_REQUIRED" and base["state"] in (
                "AUTHENTICATED", "PROBABLE_AUTHENTICATED"):
            base["state"] = "SESSION_EXPIRED"
        elif verdict in ("VERIFICATION_REQUIRED",):
            base["state"] = verdict
        elif verdict == "AUTHENTICATED":
            base["state"] = "AUTHENTICATED"
    base["hints"] = _hints(base["state"])
    return base


def _hints(state: str) -> str:
    return {
        "AUTHENTICATED": "Session is reusable. Create an automation context "
                         "with reuseExistingSession=true.",
        "PROBABLE_AUTHENTICATED": "Cookie signals look authenticated but no "
                                  "site config exists; register the site for "
                                  "precise detection or run with probe=true.",
        "LOGIN_REQUIRED": "No usable session. Ask the user to log in through "
                          "the Neko browser (neko UI), then re-check.",
        "VERIFICATION_REQUIRED": "The site is showing a CAPTCHA/verification "
                                 "challenge. The user must complete it in the "
                                 "Neko browser; automation resumes afterwards.",
        "SESSION_EXPIRED": "Cookies exist but the site rejects them. The user "
                           "must log in again through the Neko browser.",
        "NO_SESSION": "The browser has never visited / has no cookies for "
                      "this domain.",
        "UNKNOWN": "Cannot determine. Register a site config or probe.",
    }.get(state, "")
