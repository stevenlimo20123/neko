"""Browser environment discovery: Firefox process, profile, supervisor."""
import configparser
import os
import subprocess
from typing import Optional


def _proc_cmdlines() -> list:
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace").split("\x00")
            out.append((int(pid), [c for c in cmd if c]))
        except OSError:
            continue
    return out


def firefox_running() -> bool:
    for _pid, cmd in _proc_cmdlines():
        if any("firefox" in c and c.endswith("/firefox") or c == "firefox" for c in cmd):
            if any("crashhelper" not in c and c for c in cmd):
                return True
    return False


def browser_kind() -> str:
    for _pid, cmd in _proc_cmdlines():
        joined = " ".join(cmd)
        if "/firefox" in joined and "crashhelper" not in joined:
            return "firefox"
    return "unknown"


def supervisorctl(*args, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run supervisorctl against the neko supervisord instance."""
    return subprocess.run(
        ["supervisorctl", "-c", "/etc/neko/supervisord.conf", *args],
        capture_output=True, text=True, timeout=timeout)


def supervisor_status() -> dict:
    """{"firefox": "RUNNING", "neko": "RUNNING", ...}"""
    try:
        r = supervisorctl("status")
        progs = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                progs[parts[0]] = parts[1]
        return progs
    except Exception as e:
        return {"error": str(e)[:120]}


def find_profile() -> Optional[str]:
    """Locate the default Firefox profile directory from profiles.ini."""
    ini = os.path.join(os.environ.get(
        "NEKO_AUTOMATION_PROFILE_DIR",
        "/home/neko/.mozilla/firefox"), "profiles.ini")
    root = os.path.dirname(ini)
    if not os.path.exists(ini):
        # fallback: single obvious profile dir
        guess = os.path.join(root, "profile.default")
        return guess if os.path.isdir(guess) else None
    cp = configparser.ConfigParser()
    cp.read(ini)
    best = None
    default_path = None
    for section in cp.sections():
        if not section.lower().startswith("profile"):
            continue
        path = cp.get(section, "Path", fallback=None)
        if not path:
            continue
        if not cp.getboolean(section, "IsRelative", fallback=True):
            full = path
        else:
            full = os.path.join(root, path)
        if cp.getboolean(section, "Default", fallback=False):
            default_path = full
        if best is None:
            best = full
    profile = default_path or best
    if profile and os.path.isdir(profile):
        return profile
    return None


def profile_available() -> bool:
    p = find_profile()
    return bool(p and os.path.exists(os.path.join(p, "cookies.sqlite")))


def profile_info() -> dict:
    p = find_profile()
    if not p:
        return {"found": False}
    cookies = os.path.join(p, "cookies.sqlite")
    return {
        "found": True,
        "path": p,
        "cookies_db": os.path.exists(cookies),
        "size_mb": round(sum(
            os.path.getsize(os.path.join(p, f)) for f in os.listdir(p)
            if os.path.isfile(os.path.join(p, f))) / 1e6, 1),
    }
