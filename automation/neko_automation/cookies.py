"""Safe Firefox cookie-database reading + format conversion.

Reading strategy (proven in production): copy cookies.sqlite together with
its -wal/-shm sidecar files to a temp location and open the COPY. This never
interferes with the live Firefox process and never locks its database.

Security: cookie VALUES never leave this module except through the tightly
gated export path in main.py. APIs only expose names/hosts/counts.
"""
import os
import shutil
import sqlite3
import tempfile
import time
from typing import List, Optional


def _safe_copy(profile: Optional[str] = None) -> str:
    from .browser import find_profile
    profile = profile or find_profile()
    if not profile:
        raise RuntimeError("firefox profile not found")
    src = os.path.join(profile, "cookies.sqlite")
    if not os.path.exists(src):
        raise RuntimeError("cookies.sqlite not found in profile")
    tmpdir = tempfile.mkdtemp(prefix="neko-ck-")
    dst = os.path.join(tmpdir, "cookies.sqlite")
    shutil.copy2(src, dst)
    for ext in ("-wal", "-shm"):
        if os.path.exists(src + ext):
            shutil.copy2(src + ext, dst + ext)
    return dst


def read_cookies(domain: Optional[str] = None,
                 names: Optional[List[str]] = None,
                 profile: Optional[str] = None) -> List[dict]:
    """Return cookies (with values) filtered by domain suffix / names."""
    db = _safe_copy(profile)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA wal_checkpoint(FULL)")
        except sqlite3.DatabaseError:
            pass
        q = "SELECT host, name, value, path, expiry, isSecure, isHttpOnly " \
            "FROM moz_cookies"
        rows = con.execute(q).fetchall()
        con.close()
    finally:
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)

    out = []
    now = time.time()
    for host, name, value, path, expiry, secure, http_only in rows:
        if domain:
            d = domain.lstrip(".")
            h = host.lstrip(".")
            if not (h == d or h.endswith("." + d) or d.endswith("." + h)):
                continue
        if names is not None and name not in names:
            continue
        exp_raw = int(expiry) if expiry else 0
        # Firefox >= ~122 stores expiry in milliseconds
        exp_ms = exp_raw if exp_raw > 10 ** 12 else exp_raw * 1000
        out.append({
            "host": host, "name": name, "value": value,
            "path": path or "/",
            "expiry": exp_raw,
            "expiry_ms": exp_ms,
            "secure": bool(secure), "httpOnly": bool(http_only),
            "expired": bool(exp_raw) and exp_ms < now * 1000,
        })
    return out


def cookie_summary(domain: Optional[str] = None) -> dict:
    """Non-sensitive summary for APIs: names/hosts/counts only."""
    cookies = read_cookies(domain=domain)
    fresh = [c for c in cookies if not c["expired"]]
    return {
        "domain": domain,
        "total": len(cookies),
        "fresh": len(fresh),
        "expired": len(cookies) - len(fresh),
        "names": sorted({c["name"] for c in fresh}),
        "hosts": sorted({c["host"] for c in fresh}),
    }


def to_playwright(cookies: List[dict]) -> List[dict]:
    """Convert our cookie dicts to Playwright's add_cookies format
    (proven conversion from the Pinduoduo scraping pipeline; Firefox >= ~122
    stores moz_cookies.expiry in MILLISECONDS, Playwright wants seconds)."""
    out = []
    for c in cookies:
        if c.get("expired"):
            continue
        exp = int(c.get("expiry") or 0)
        if exp > 10 ** 12:          # milliseconds -> seconds
            exp = exp // 1000
        if exp <= 0:
            exp = -1                 # session cookie
        cookie = {
            "name": c["name"], "value": c["value"], "expires": exp,
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
            "sameSite": "Lax",
        }
        host = c["host"]
        if host.startswith("."):
            cookie["domain"] = host
            cookie["path"] = c.get("path") or "/"
        else:
            cookie["url"] = f"https://{host}/"
        out.append(cookie)
    return out


def secure_delete(path: str) -> None:
    """Overwrite and remove a temporary file containing secrets."""
    if not os.path.exists(path):
        return
    try:
        size = os.path.getsize(path)
        with open(path, "ba+") as f:
            f.write(b"\x00" * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def auth_signals(domain: str, auth_cookie_names: List[str]) -> dict:
    """Which configured auth cookies exist and are fresh for this domain."""
    cookies = read_cookies(domain=domain, names=list(auth_cookie_names))
    fresh = [c for c in cookies if not c["expired"]]
    return {
        "present": sorted({c["name"] for c in fresh}),
        "missing": sorted(set(auth_cookie_names) - {c["name"] for c in fresh}),
        "fresh_count": len(fresh),
    }
