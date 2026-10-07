#!/usr/bin/env python3
"""One-shot Firefox profile seeder (bootstrap / disaster recovery).

Runs BEFORE Firefox starts (wrapped into the firefox supervisord program).
If a profile seed archive is configured and the current profile has no
cookie database yet, the archive is downloaded and extracted into the
profile root. This is how a brand-new volume (or a rebuilt container)
receives the user's saved sessions without any manual terminal access.

Environment:
  NEKO_AUTOMATION_PROFILE_SEED_URL     https URL of a .tar.gz containing
                                       the `firefox/` profile tree
  NEKO_AUTOMATION_PROFILE_SEED_TOKEN   optional bearer token for that URL

The seeder is idempotent: it never runs when cookies.sqlite already exists
or after it has successfully seeded once (marker file). Any failure is
logged and ignored - Firefox must still start.
"""
import os
import sys
import tarfile
import tempfile
import urllib.request

PROFILE_ROOT = os.environ.get(
    "NEKO_AUTOMATION_PROFILE_DIR", "/home/neko/.mozilla/firefox")
SEED_URL = os.environ.get("NEKO_AUTOMATION_PROFILE_SEED_URL", "").strip()
SEED_TOKEN = os.environ.get("NEKO_AUTOMATION_PROFILE_SEED_TOKEN", "").strip()
MARKER = ".automation-seeded"


def log(msg):
    print(f"[seed_profile] {msg}", file=sys.stderr, flush=True)


def default_profile_dir():
    import configparser
    ini = os.path.join(PROFILE_ROOT, "profiles.ini")
    cp = configparser.ConfigParser()
    cp.read(ini)
    for section in cp.sections():
        if section.lower().startswith("profile"):
            path = cp.get(section, "Path", fallback=None)
            if path and cp.getboolean(section, "IsRelative", fallback=True):
                return os.path.join(PROFILE_ROOT, path)
            if path:
                return path
    return os.path.join(PROFILE_ROOT, "profile.default")


def safe_members(tf):
    out = []
    for m in tf.getmembers():
        name = m.name.lstrip("./")
        if name.startswith("/") or ".." in name.split("/"):
            log(f"skipping unsafe member {m.name}")
            continue
        if not (name == "firefox" or name.startswith("firefox/")):
            log(f"skipping member outside firefox/: {m.name}")
            continue
        if m.issym() or m.islnk() or m.isdev():
            log(f"skipping link/device member {m.name}")
            continue
        out.append(m)
    return out


def main():
    if not SEED_URL:
        return
    profile = default_profile_dir()
    cookies = os.path.join(profile, "cookies.sqlite")
    if os.path.exists(cookies) and os.path.getsize(cookies) > 0:
        log("profile already has cookies.sqlite - skipping seed")
        return
    if os.path.exists(os.path.join(profile, MARKER)):
        log("seed marker present - skipping")
        return
    log(f"seeding profile from {SEED_URL.split('?')[0]}")
    req = urllib.request.Request(SEED_URL, headers={"User-Agent": "neko-seeder"})
    if SEED_TOKEN:
        req.add_header("Authorization", f"Bearer {SEED_TOKEN}")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    tmp.write(chunk)
            tmp_path = tmp.name
        except Exception as e:
            log(f"download failed (ignoring): {e}")
            return
    try:
        with open(tmp_path, "rb") as f:
            magic = f.read(2)
        if magic != b"\x1f\x8b":
            log("download is not gzip - ignoring")
            return
        os.makedirs(os.path.dirname(profile), exist_ok=True)
        with tarfile.open(tmp_path, "r:gz") as tf:
            members = safe_members(tf)
            log(f"extracting {len(members)} members to {PROFILE_ROOT}")
            tf.extractall(PROFILE_ROOT, members=members)
        with open(os.path.join(profile, MARKER), "w") as f:
            f.write("seeded\n")
        log("seed complete")
    except Exception as e:
        log(f"extract failed (ignoring): {e}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
