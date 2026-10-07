"""Generic, site-agnostic job types (work for ANY website)."""
import asyncio
import json
import time

from . import sites as st
from .contexts import CONTEXTS
from .jobs import JOBS, JobAttention, register_job_type, check_auth_loss


def _ctx_options(params: dict) -> dict:
    """Common Playwright context options accepted by all job types."""
    return {
        "domain": params.get("domain"),
        "reuse_existing_session": params.get("reuse_existing_session", True),
        "user_agent": params.get("user_agent"),
        "is_mobile": params.get("is_mobile", False),
        "viewport": params.get("viewport"),
        "locale": params.get("locale", "zh-CN"),
        "timezone_id": params.get("timezone_id", "Asia/Shanghai"),
    }


def _domain_of(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).netloc
    except Exception:
        return ""


@register_job_type("scrape.urls")
async def scrape_urls(job, mgr):
    """Visit a list of URLs and extract data with a JS expression.

    params:
      urls:                [str] URLs to visit (resumable - done ones skipped)
      extract:             str JS expression evaluated on each page; must
                           return JSON-serialisable data (default: null)
      wait_ms:             int settle time after load (default 4000)
      network_patterns:    [str] also capture matching XHR bodies
      context:             dict Playwright context options (domain, is_mobile,
                           user_agent, ...)
      retries:             int per-URL retries (default 1)
      rate_ms:             int delay between URLs (default 1500)
      stop_on_auth_loss:   bool pause job when login/verify redirect (default true)
    """
    p = job.params
    urls = p.get("urls") or []
    extract = p.get("extract") or "null"
    wait_ms = int(p.get("wait_ms", 4000))
    net_pats = p.get("network_patterns") or []
    retries = int(p.get("retries", 1))
    rate_ms = int(p.get("rate_ms", 1500))
    stop_auth = p.get("stop_on_auth_loss", True)

    state = job.state
    state.setdefault("done", {})       # url -> result
    state.setdefault("failed", {})     # url -> error
    state.setdefault("retries_used", {})
    pending = [u for u in urls if u not in state["done"]]
    job.progress = {"done": len(state["done"]), "total": len(urls)}

    ctx_id = await CONTEXTS.create(**_ctx_options(p.get("context") or {}))
    page = None
    try:
        page = await CONTEXTS.new_page(ctx_id)
        for i, url in enumerate(pending):
            if job.status in ("cancelled", "interrupted"):
                break
            attempt = 0
            while attempt <= retries:
                try:
                    await page.navigate(url, timeout=p.get("timeout_ms", 45000))
                    await asyncio.sleep(wait_ms / 1000)
                    if stop_auth:
                        check_auth_loss(page.url, _domain_of(url))
                    data = await page.evaluate(
                        "(async () => { try { return await (async () => { %s })(); }"
                        " catch (e) { return {__error__: String(e)}; } })()"
                        % extract) if p.get("await_extract") else \
                        await page.evaluate(extract)
                    rec = {"url": url, "final_url": page.url, "data": data}
                    if net_pats:
                        rec["network"] = {
                            pat: page.network.bodies_json(pattern=pat, limit=20)
                            for pat in net_pats}
                    state["done"][url] = rec
                    break
                except JobAttention:
                    raise
                except Exception as e:
                    attempt += 1
                    state["retries_used"][url] = attempt
                    if attempt > retries:
                        state["failed"][url] = str(e)[:300]
                    await asyncio.sleep(1.5)
            job.progress["done"] = len(state["done"])
            if (i + 1) % 5 == 0 or i == len(pending) - 1:
                mgr.persist(job)
            await asyncio.sleep(rate_ms / 1000)
    finally:
        try:
            await CONTEXTS.close(ctx_id)
        except Exception:
            pass
    job.result = {
        "records": list(state["done"].values()),
        "failed": state["failed"],
        "stats": {"done": len(state["done"]), "failed": len(state["failed"]),
                  "total": len(urls)},
    }
    if state["failed"] and not state["done"]:
        job.error = f"all {len(state['failed'])} URLs failed"
    return job.result


@register_job_type("scrape.search")
async def scrape_search(job, mgr):
    """Search harvesting via URL template + XHR interception + scrolling.

    params:
      search_url_template: str with {query} (and optional {page})
      queries:             [str] search terms
      pages:               int pages per query via {page} (default 1)
      scroll_times:        int scroll iterations per page for lazy loading
                           (default 8 when no {page} in template)
      scroll_delay_ms:     int (default 1500)
      response_pattern:    str regex/substring for the XHR to harvest
      extract_response:    str JS expression over the parsed JSON body,
                           accessible as `body` (default: 'body')
      dedup_key:           str JS expression over a record for dedup
                           (default: 'r.id || r.goods_id || JSON.stringify(r)')
      context:             dict Playwright context options
      stop_on_auth_loss:   bool (default true)
    """
    p = job.params
    template = p["search_url_template"]
    queries = p.get("queries") or []
    pages = int(p.get("pages", 1))
    scroll_times = int(p.get("scroll_times",
                             0 if "{page}" in template else 8))
    scroll_delay = int(p.get("scroll_delay_ms", 1500)) / 1000
    resp_pat = p["response_pattern"]
    extract_expr = p.get("extract_response", "body")
    dedup_expr = p.get("dedup_key",
                       "r.id || r.goods_id || r.goodsId || JSON.stringify(r)")
    stop_auth = p.get("stop_on_auth_loss", True)

    state = job.state
    state.setdefault("records", {})      # dedup_key -> record
    state.setdefault("queries_done", [])
    state.setdefault("per_query", {})    # query -> count
    total_work = len(queries) * max(1, pages)
    job.progress = {"done": len(state["queries_done"]) * max(1, pages),
                    "total": total_work}

    ctx_id = await CONTEXTS.create(**_ctx_options(p.get("context") or {}))
    try:
        page = await CONTEXTS.new_page(ctx_id)
        for query in queries:
            if query in state["queries_done"]:
                continue
            if job.status in ("cancelled", "interrupted"):
                break
            q_added = 0
            for pg in range(1, max(1, pages) + 1):
                url = template.format(query=query, page=pg)
                if "{page}" not in template and pg > 1:
                    break
                try:
                    page.network.entries.clear()
                    await page.navigate(url, timeout=60000)
                    if stop_auth:
                        check_auth_loss(page.url, _domain_of(url))
                    import random
                    for _ in range(scroll_times):
                        await page.page.mouse.wheel(
                            0, random.randint(1600, 2400))
                        await asyncio.sleep(
                            scroll_delay + random.random() * 0.6)
                    await asyncio.sleep(1.5)
                    bodies = page.network.bodies_json(pattern=resp_pat, limit=50)
                    for entry in bodies:
                        body = entry.get("json")
                        if body is None:
                            continue
                        try:
                            items = await _extract_items(
                                page, extract_expr, body)
                        except Exception:
                            items = body if isinstance(body, list) else [body]
                        for r in (items or []):
                            if not isinstance(r, dict):
                                continue
                            key = _dedup_key(r, dedup_expr)
                            if key and key not in state["records"]:
                                rec = dict(r)
                                rec.setdefault("_query", query)
                                rec.setdefault("_ts", int(time.time()))
                                state["records"][key] = rec
                                q_added += 1
                except JobAttention:
                    raise
                except Exception as e:
                    state["per_query"].setdefault("errors", []).append(
                        {"query": query, "page": pg, "error": str(e)[:200]})
            state["queries_done"].append(query)
            state["per_query"][query] = q_added
            job.progress["done"] = len(state["queries_done"]) * max(1, pages)
            mgr.persist(job)
            await asyncio.sleep(int(p.get("query_delay_ms", 3000)) / 1000)
    finally:
        try:
            await CONTEXTS.close(ctx_id)
        except Exception:
            pass
    job.result = {
        "records": list(state["records"].values()),
        "stats": {"unique_records": len(state["records"]),
                  "queries_done": state["queries_done"],
                  "per_query": state["per_query"]},
    }
    return job.result


async def _extract_items(page, extract_expr, body):
    """Evaluate extract_response with `body` bound to the parsed response."""
    if extract_expr in ("body", "", None):
        return body if isinstance(body, list) else [body]
    wrapped = ("(function() { const body = arguments[0]; const r = arguments[1];"
               " return (%s); })") % extract_expr
    # evaluate with body via a JSON round-trip
    js = ("(body => { const r = body; return (%s); })" % extract_expr)
    return await page.evaluate(js, body) if False else \
        await page.evaluate(
            "body => { const r = body; return (%s); }" % extract_expr, body)


def _dedup_key(rec: dict, expr: str) -> str:
    try:
        val = rec.get("id") or rec.get("goods_id") or rec.get("goodsId")
        if val:
            return str(val)
        return json.dumps(rec, ensure_ascii=False, sort_keys=True)[:200]
    except Exception:
        import hashlib
        return hashlib.sha256(json.dumps(rec, default=str).encode()
                              ).hexdigest()[:16]
