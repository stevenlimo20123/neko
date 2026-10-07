"""Site registry: teach the automation layer about ANY website.

A SiteConfig describes how to detect the login state for one site family:
which cookies imply an authenticated session, which URL paths mean
"login page" / "human verification", and optionally how to run a live probe.

Configs come from three sources (later overrides earlier):
1. Built-in defaults defined here (generic heuristics).
2. ``sites.json`` in the state dir (managed via POST /api/automation/sites).
3. Adapter-provided configs (e.g. the Pinduoduo adapter).

Future agents can register a new site at runtime - no code required.
"""
import fnmatch
import json
import os
import re
from typing import Dict, List, Optional

from .config import CFG

BUILTIN_SITES: List[dict] = [
    {
        "name": "pinduoduo",
        "domains": ["yangkeduo.com", "pinduoduo.com", "pinduoduo.net"],
        "auth_cookies": ["PDDAccessToken", "pdd_user_id", "pdd_user_uin"],
        "login_path_patterns": ["*/login.html*", "*/login*"],
        "verification_path_patterns": ["*psnl_verification*", "*verification*",
                                       "*captcha*", "*verify*"],
        "notes": "Pinduoduo marketplace (mobile.yangkeduo.com) and merchant "
                 "backend (mms.pinduoduo.com) have SEPARATE sessions. The "
                 "goods pages render best with a mobile iPhone UA.",
    },
    {
        "name": "taobao",
        "domains": ["taobao.com", "tmall.com"],
        "auth_cookies": ["cookie2", "sgcookie", "tracknick", "_l_g_"],
        "login_path_patterns": ["*login*"],
        "verification_path_patterns": ["*punish*", "*captcha*", "*verify*"],
    },
    {
        "name": "alibaba-1688",
        "domains": ["1688.com"],
        "auth_cookies": ["cna", "cookie17", "sgcookie"],
        "login_path_patterns": ["*login*"],
        "verification_path_patterns": ["*punish*", "*captcha*", "*slide*"],
    },
    {
        "name": "aliexpress",
        "domains": ["aliexpress.com"],
        "auth_cookies": ["aep_usuc_f", "xman_us_f", "sc_g_cfg"],
        "login_path_patterns": ["*login*"],
        "verification_path_patterns": ["*captcha*", "*nvc*", "*verify*"],
    },
    {
        "name": "jd",
        "domains": ["jd.com"],
        "auth_cookies": ["pt_key", "pt_pin"],
        "login_path_patterns": ["*login*"],
        "verification_path_patterns": ["*captcha*", "*verify*"],
    },
]


class SiteRegistry:
    def __init__(self) -> None:
        self.user_sites: List[dict] = []
        self.load()

    # ------------------------------------------------------------- storage
    @property
    def _path(self) -> str:
        return os.path.join(CFG.STATE_DIR, "sites.json")

    def load(self) -> None:
        self.user_sites = []
        try:
            if os.path.exists(self._path):
                data = json.load(open(self._path))
                self.user_sites = data.get("sites", [])
        except Exception:
            self.user_sites = []

    def save(self) -> None:
        os.makedirs(CFG.STATE_DIR, exist_ok=True)
        json.dump({"sites": self.user_sites}, open(self._path, "w"),
                  ensure_ascii=False, indent=1)

    # -------------------------------------------------------------- queries
    def all_sites(self) -> List[dict]:
        merged: Dict[str, dict] = {}
        for s in BUILTIN_SITES:
            merged[s["name"]] = s
        for s in self.user_sites:
            merged[s["name"]] = s
        for cfg in merged.values():
            cfg.setdefault("auth_cookies", [])
            cfg.setdefault("login_path_patterns", ["*login*", "*signin*"])
            cfg.setdefault("verification_path_patterns",
                           ["*captcha*", "*verification*", "*verify*"])
        return list(merged.values())

    def for_domain(self, domain: str) -> Optional[dict]:
        domain = (domain or "").lstrip(".").lower()
        best = None
        for site in self.all_sites():
            for d in site["domains"]:
                if domain == d or domain.endswith("." + d):
                    if best is None or len(d) > len(best[0]):
                        best = (d, site)
        return best[1] if best else None

    # -------------------------------------------------------------- writers
    def register(self, config: dict) -> dict:
        name = config.get("name")
        if not name or not config.get("domains"):
            raise ValueError("site config requires 'name' and 'domains'")
        config["name"] = str(name)
        config["domains"] = [str(d).lower() for d in config["domains"]]
        self.user_sites = [s for s in self.user_sites if s["name"] != name]
        self.user_sites.append(config)
        self.save()
        return config

    def unregister(self, name: str) -> bool:
        before = len(self.user_sites)
        self.user_sites = [s for s in self.user_sites if s["name"] != name]
        if len(self.user_sites) != before:
            self.save()
            return True
        return False


SITES = SiteRegistry()


# ---------------------------------------------------------------- matching
def path_matches(url: str, patterns: List[str]) -> bool:
    """Match a URL against a site's login/verification path patterns."""
    try:
        path = url.split("//", 1)[-1]
    except Exception:
        return False
    for pat in patterns or []:
        if fnmatch.fnmatch(path, pat):
            return True
        try:
            if re.search(pat, url):
                return True
        except re.error:
            pass
    return False


GENERIC_AUTH_COOKIE_RE = re.compile(
    r"(token|session|sess|auth|login|logged|uid|userid|user_id|sid|passport|"
    r"access_token|jwt|bearer)", re.I)


def generic_auth_cookie_names(domain: str) -> List[str]:
    """For unknown sites: cookie names that usually imply authentication."""
    from .cookies import read_cookies
    cookies = read_cookies(domain=domain)
    return sorted({c["name"] for c in cookies
                   if GENERIC_AUTH_COOKIE_RE.search(c["name"])})
