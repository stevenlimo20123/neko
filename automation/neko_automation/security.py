"""Security: bearer-token auth middleware, audit log and redaction helpers."""
import hashlib
import hmac
import json
import os
import re
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.security.utils import get_authorization_scheme_param

from .config import CFG

# Patterns that look like secrets; used to scrub log lines.
_SECRETY = re.compile(r"(token|secret|password|authorization|cookie)", re.I)


def token_ok(provided: Optional[str]) -> bool:
    if not CFG.TOKEN:
        return False
    if not provided:
        return False
    return hmac.compare_digest(provided, CFG.TOKEN)


async def require_auth(request: Request, call_next):
    path = request.url.path
    # health endpoint is intentionally unauthenticated (container healthcheck)
    for base in ("/health", "/healthz"):
        if path == base or path.endswith(base):
            return await call_next(request)
    # strip optional mount prefix for the check
    if CFG.PREFIX and path.startswith(CFG.PREFIX + "/"):
        path = path[len(CFG.PREFIX):]
    if not path.startswith("/api/"):
        return await call_next(request)
    if not CFG.TOKEN:
        return JSONResponse(status_code=503, content={
            "detail": "NEKO_AUTOMATION_TOKEN is not configured; "
                      "API disabled for safety"})
    scheme, param = get_authorization_scheme_param(request.headers.get("Authorization", ""))
    if scheme.lower() != "bearer" or not token_ok(param):
        return JSONResponse(status_code=401,
                            content={"detail": "invalid or missing bearer token"})
    return await call_next(request)


class Audit:
    """Append-only JSONL audit log. Never logs secret values."""

    def __init__(self) -> None:
        self.path = CFG.AUDIT_LOG or os.path.join(CFG.STATE_DIR, "audit.log")
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.enabled = True
        except OSError:
            self.enabled = False  # e.g. read-only fs: audit disabled

    def log(self, action: str, ok: bool = True, **details) -> None:
        if not self.enabled:
            return
        entry = {"ts": int(time.time()), "action": action, "ok": ok}
        for k, v in details.items():
            entry[k] = self.redact(v)
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def redact(value, _depth: int = 0):
        if _depth > 3:
            return "..."
        if isinstance(value, str):
            if _SECRETY.search(value):
                return f"<redacted:{len(value)} chars>"
            return value[:200]
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if _SECRETY.search(str(k)) or k in ("value", "token", "secret"):
                    out[k] = "<redacted>"
                else:
                    out[k] = Audit.redact(v, _depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return [Audit.redact(v, _depth + 1) for v in value[:20]]
        return value


AUDIT = Audit()


def sha8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]
