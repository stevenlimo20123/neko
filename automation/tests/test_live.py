#!/usr/bin/env python3
"""Live integration test suite for the Neko automation API.

Run after deployment: python3 test_live.py <base-url> <token>
"""
import base64
import json
import subprocess
import sys
import time

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://neko.tingsrepo.com/automation"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else open("/tmp/automation_token.txt").read().strip()

PASS, FAIL = [], []


def call(method, path, body=None, expect=200, auth=True, timeout=90):
    """HTTP via curl (Cloudflare's bot filter bans some python TLS
    fingerprints; curl's passes)."""
    url = f"{BASE}{path}"
    cmd = ["curl", "-s", "--max-time", str(timeout), "-X", method,
           "-H", "Content-Type: application/json",
           "-w", "\n%{http_code}"]
    if auth:
        cmd += ["-H", f"Authorization: Bearer {TOKEN}"]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    out = r.stdout.rsplit("\n", 1)
    code = int(out[1]) if len(out) == 2 and out[1].isdigit() else -1
    try:
        data = json.loads(out[0]) if out[0] else {}
    except json.JSONDecodeError:
        data = {"raw": out[0][:200]}
    return code, data


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name} {detail}")


def main():
    print(f"== Neko automation live tests against {BASE} ==")

    print("[1] health + auth")
    s, d = call("GET", "/health", auth=False)
    check("health no-auth", s == 200 and d.get("status") == "ok", f"got {s}")
    s, d = call("GET", "/api/automation/status", auth=False)
    check("status without token -> 401", s == 401, f"got {s}")
    s, d = call("GET", "/api/automation/status")
    check("status with token", s == 200, f"got {s}")
    if s == 200:
        check("neko server running", d.get("neko_running"))
        check("browser running", d.get("browser_running"))
        check("browser type firefox", d.get("browser_type") == "firefox")
        check("bidi connected", d.get("bidi", {}).get("connected"))
        check("profile found", d.get("profile", {}).get("found"))
        check("profile has cookies db", d.get("profile", {}).get("cookies_db"))
        check("job types registered",
              "scrape.urls" in d.get("jobs", {}).get("known_types", []))

    print("[2] tabs")
    s, d = call("GET", "/api/automation/tabs")
    check("tabs list", s == 200 and "tabs" in d, f"got {s}")
    if s == 200:
        check("tab source is bidi (live)", d.get("source") == "bidi", d.get("source"))
        tabs = d.get("tabs", [])
        print(f"       ({len(tabs)} tabs: "
              f"{[t.get('domain') or 'blank' for t in tabs[:5]]})")
        check("tabs have urls", all("url" in t for t in tabs))
    s, d = call("GET", "/api/automation/tabs/find?pattern=yangkeduo")
    check("find tabs by pattern", s == 200 and "tabs" in d)

    print("[3] tab open/navigate/close cycle")
    s, d = call("POST", "/api/automation/tabs?url=https://example.com/&background=true")
    check("open tab", s == 200 and "id" in d, f"got {s} {d}")
    tab_id = d.get("id")
    if tab_id:
        time.sleep(3)
        s, d = call("POST", f"/api/automation/tabs/{tab_id}/evaluate",
                    {"expression": "document.title"})
        check("evaluate on live tab", s == 200 and
              isinstance(d.get("result"), str), f"got {s}")
        s, d = call("POST", f"/api/automation/tabs/{tab_id}/screenshot")
        check("tab screenshot", s == 200 and len(d.get("image_png_b64", "")) > 1000)
        s, d = call("POST", f"/api/automation/tabs/{tab_id}/close")
        check("close tab", s == 200)

    print("[4] session status")
    s, d = call("GET", "/api/automation/session/status?domain=mobile.yangkeduo.com")
    check("yangkeduo session status", s == 200, f"got {s}")
    if s == 200:
        print(f"       state={d.get('state')} signals={d.get('signals')}")
        check("yangkeduo authenticated (recovered cookies)",
              d.get("state") in ("AUTHENTICATED", "PROBABLE_AUTHENTICATED"),
              d.get("state"))
    s, d = call("GET", "/api/automation/session/status?domain=mms.pinduoduo.com")
    check("mms session status", s == 200, f"got {s}")
    if s == 200:
        print(f"       mms state={d.get('state')} (merchant backend - "
              f"expected LOGIN_REQUIRED)")

    print("[5] playwright context + page ops (the scraping path)")
    s, d = call("POST", "/api/automation/context",
                {"domain": "yangkeduo.com", "is_mobile": True})
    check("create context", s == 200 and "context_id" in d, f"got {s} {d}")
    ctx = d.get("context_id")
    page = None
    if ctx:
        s, d = call("POST", f"/api/automation/contexts/{ctx}/pages",
                    {"url": "https://mobile.yangkeduo.com/goods.html?goods_id=799108744880"})
        check("open page (goods)", s == 200 and "page_id" in d, f"got {s} {d}")
        page = d.get("page_id")
    if page:
        time.sleep(4)
        s, d = call("POST", f"/api/automation/pages/{page}/evaluate",
                    {"expression": "(() => { try { const g = window[0].top.rawData.store.initDataObj.goods; return JSON.stringify({name: (g.goodsName||'').slice(0,40), specs: (g.goodsProperty||[]).length}); } catch(e) { return 'ERR:' + e.message; } })()"})
        ok = s == 200 and isinstance(d.get("result"), str) and "specs" in d.get("result", "")
        check("heap extraction (goods object)", ok, f"got {s} {str(d)[:120]}")
        if ok:
            print(f"       {d['result'][:100]}")
        s, d = call("GET", f"/api/automation/pages/{page}/text")
        check("page text", s == 200)
        s, d = call("POST", f"/api/automation/pages/{page}/screenshot")
        check("page screenshot", s == 200 and len(d.get("image_png_b64", "")) > 1000)

    print("[6] network capture")
    if ctx:
        s, d = call("POST", f"/api/automation/contexts/{ctx}/pages",
                    {"url": "https://mobile.yangkeduo.com/search_result.html?search_key=%E5%8F%91%E7%94%B5%E6%9C%BA"})
        npage = d.get("page_id")
        if npage:
            time.sleep(5)
            s, d = call("GET", f"/api/automation/pages/{npage}/network?pattern=proxy/api&decode_json=true")
            check("network capture of search API", s == 200 and
                  len(d.get("entries", [])) > 0, f"got {s}")
            if s == 200 and d.get("entries"):
                print(f"       captured {len(d['entries'])} matching responses")

    print("[7] jobs")
    s, d = call("POST", "/api/automation/jobs", {
        "type": "scrape.urls",
        "params": {
            "urls": ["https://example.com/", "https://example.org/"],
            "extract": "({title: document.title, h1: (document.querySelector('h1')||{}).innerText||null})",
            "wait_ms": 1500, "rate_ms": 200, "retries": 0,
            "context": {"is_mobile": False}}})
    check("submit job", s == 200 and "id" in d, f"got {s} {str(d)[:150]}")
    jid = d.get("id")
    if jid:
        for _ in range(30):
            time.sleep(3)
            s, d = call("GET", f"/api/automation/jobs/{jid}")
            if d.get("status") in ("completed", "failed", "needs_attention"):
                break
        check("job completes", d.get("status") == "completed",
              f"status={d.get('status')} err={d.get('error')}")
        if d.get("status") == "completed":
            recs = d.get("result", {}).get("records", [])
            check("job results present", len(recs) == 2, f"got {len(recs)}")
            print(f"       records: {[r.get('data') for r in recs]}")

    print("[8] adapter recipes")
    s, d = call("GET", "/api/automation/adapters")
    check("adapters listed", s == 200 and "pinduoduo" in d.get("adapters", {}))
    s, d = call("POST", "/api/automation/adapters/recipes/run", {
        "adapter": "pinduoduo", "recipe": "search",
        "query": {"queries": ["发电机"], "scroll_times": 2}})
    check("pinduoduo search recipe starts", s == 200 and "id" in d,
          f"got {s} {str(d)[:120]}")
    pjid = d.get("id")
    if pjid:
        for _ in range(40):
            time.sleep(4)
            s, d = call("GET", f"/api/automation/jobs/{pjid}",
                        expect=200)
            st = d.get("status")
            if st in ("completed", "failed", "needs_attention", "cancelled"):
                break
        print(f"       pdd search job: {st}, progress={d.get('progress')}")
        if st == "completed":
            n = d.get("result", {}).get("stats", {}).get("unique_records", 0)
            check("pdd search harvested records", n > 0, f"got {n}")
        elif st == "needs_attention":
            print(f"       (needs attention: {d.get('attention', {}).get('reason')})")
            check("auth-loss detected properly",
                  d.get("attention", {}).get("reason") in
                  ("LOGIN_REQUIRED", "VERIFICATION_REQUIRED"))
        else:
            check("pdd search job", False, f"status={st} err={d.get('error')}")
        if st in ("running", "queued"):
            call("POST", f"/api/automation/jobs/{pjid}/cancel")

    print("[9] site registry")
    s, d = call("POST", "/api/automation/sites", {
        "name": "test-shop", "domains": ["testshop.example"],
        "auth_cookies": ["sid"]})
    check("register site", s == 200)
    s, d = call("GET", "/api/automation/sites")
    check("site listed", s == 200 and any(
        x["name"] == "test-shop" for x in d.get("builtin_and_user", [])))
    s, d = call("DELETE", "/api/automation/sites/test-shop")
    check("unregister site", s == 200)

    print("[10] security")
    s, d = call("POST", "/api/automation/admin/cookies", {"domain": "example.com"})
    check("cookie export disabled by default", s == 403, f"got {s}")

    if ctx:
        call("DELETE", f"/api/automation/contexts/{ctx}")

    print(f"\n== RESULTS: {len(PASS)} passed, {len(FAIL)} failed ==")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
