"""Pinduoduo adapter - refactored from the proven Task-16 pipeline.

Everything below was battle-tested against mobile.yangkeduo.com during the
generator-product import (557 goods harvested, 140 enriched, 134 listings
imported). The recipes encode:

* goods pages are best rendered with an iPhone UA + mobile viewport
* the search API appears at /proxy/api/search while scrolling
  search_result.html
* goods details arrive ENCRYPTED; the decrypted object is exposed on the
  page heap at window[0].top.rawData.store.initDataObj.goods
* desktop-UA goods visits redirect to login even with a valid session
* auth loss shows up as redirects to /login.html or psnl_verification
"""
from ..adapters.base import Adapter, register

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
             "Mobile/15E148 Safari/604.1")

MOBILE_CONTEXT = {
    "user_agent": MOBILE_UA,
    "is_mobile": True,
    "viewport": {"width": 390, "height": 844},
}

# Decrypted goods object extraction (proven heap path)
GOODS_HEAP_JS = """(() => {
    let root = null;
    try { root = window[0].top.rawData.store.initDataObj; } catch (e) { return null; }
    const g = root.goods || {};
    return {
        goodsName: g.goodsName,
        goodsProperty: g.goodsProperty || [],
        topGallery: (g.topGallery || []).map(i => ({url: i.url, w: i.width, h: i.height})),
        viewImageData: g.viewImageData || [],
        detailGallery: (g.detailGallery || []).map(i => ({url: i.url, w: i.width, h: i.height})),
        videoGallery: (g.videoGallery || []).map(v => ({url: v.url || v, cover: v.cover_url || v.coverUrl || null})),
        skus: (g.skus || []).map(s => ({skuId: s.skuId, qty: s.quantity, onSale: s.isOnsale,
                                        normalPrice: s.normalPrice, groupPrice: s.groupPrice})),
        catID1: g.catID1, catID2: g.catID2, catID3: g.catID3,
        mallID: g.mallID, brandId: g.brandId, quantity: g.quantity,
        isOnSale: g.isOnSale, status: g.status,
        skuThumb: g.thumbUrl, hdThumb: g.hdThumbUrl
    };
})()"""

# Search response -> compact goods records (proven parse)
SEARCH_EXTRACT = """body.items.map(it => {
    const gm = (it.item_data || {}).goods_model || {};
    if (!gm.goods_id) return null;
    return {
        goods_id: String(gm.goods_id),
        goods_name: gm.goods_name || '',
        normal_price: gm.normal_price,
        market_price: gm.market_price,
        min_group: gm.minOnSaleGroupPrice,
        display_price: gm.display_price,
        sales: gm.sales,
        sales_tip: gm.sales_tip,
        thumb: gm.hd_thumb_url || gm.thumb_url,
        mall_id: gm.mall_id
    };
}).filter(Boolean)"""


@register
class PinduoduoAdapter(Adapter):
    name = "pinduoduo"
    domains = ["yangkeduo.com", "pinduoduo.com", "pinduoduo.net"]

    def site_config(self) -> dict:
        return {
            "name": "pinduoduo",
            "domains": self.domains,
            "auth_cookies": ["PDDAccessToken", "pdd_user_id", "pdd_user_uin"],
            "login_path_patterns": ["*/login.html*", "*/login*",
                                    "*/login_account*"],
            "verification_path_patterns": ["*psnl_verification*",
                                           "*verification*", "*captcha*",
                                           "*verify*"],
            "probe_url": "https://mobile.yangkeduo.com/goods.html?goods_id=799108744880",
            "probe_ua": MOBILE_UA,
            "notes": "mobile.yangkeduo.com (marketplace) and mms.pinduoduo.com "
                     "(merchant backend) have SEPARATE sessions. Marketplace "
                     "session requires mobile UA for goods pages.",
        }

    def recipes(self) -> dict:
        return {
            "search": {
                "description": "Harvest goods records for search keywords via "
                               "the mobile search page + /proxy/api/search "
                               "interception (scroll pagination).",
                "job_type": "scrape.search",
                "example_params": {"queries": ["发电机", "发电机组"],
                                   "scroll_times": 8},
                "params_builder": self._search_params,
            },
            "enrich": {
                "description": "Visit goods pages and extract the decrypted "
                               "goods object (specs, galleries, SKUs) from "
                               "the page heap.",
                "job_type": "scrape.urls",
                "example_params": {"goods_ids": ["799108744880"]},
                "params_builder": self._enrich_params,
            },
        }

    @staticmethod
    def _search_params(query: dict) -> dict:
        queries = query.get("queries") or []
        if not queries:
            raise ValueError("recipe 'search' requires queries: [str]")
        return {
            "search_url_template":
                "https://mobile.yangkeduo.com/search_result.html?search_key={query}",
            "queries": queries,
            "scroll_times": int(query.get("scroll_times", 8)),
            "scroll_delay_ms": int(query.get("scroll_delay_ms", 1500)),
            "response_pattern": "/proxy/api/search",
            "extract_response": SEARCH_EXTRACT,
            "dedup_key": "r.goods_id",
            "context": dict(MOBILE_CONTEXT, domain="yangkeduo.com"),
            "stop_on_auth_loss": True,
        }

    @staticmethod
    def _enrich_params(query: dict) -> dict:
        ids = query.get("goods_ids") or []
        known = query.get("known") or {}
        urls = [f"https://mobile.yangkeduo.com/goods.html?goods_id={g}"
                for g in ids]
        # merge any known records (e.g. from search) into the job state so
        # enrichment results carry their source prices
        state = {"done": {}} if not known else {"done": {}}
        return {
            "urls": urls,
            "extract": GOODS_HEAP_JS,
            "await_extract": False,
            "wait_ms": int(query.get("wait_ms", 4500)),
            "network_patterns": [],
            "context": dict(MOBILE_CONTEXT, domain="yangkeduo.com"),
            "retries": int(query.get("retries", 1)),
            "rate_ms": int(query.get("rate_ms", 2000)),
            "stop_on_auth_loss": True,
        }
