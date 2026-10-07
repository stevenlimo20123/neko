"""Per-page network/XHR response capture with a ring buffer."""
import asyncio
import base64
import json
import re
import time
from collections import deque
from typing import Optional

from .config import CFG


class NetworkTracker:
    def __init__(self, buffer_size: Optional[int] = None,
                 body_limit: Optional[int] = None):
        self.entries = deque(maxlen=buffer_size or CFG.NETWORK_BUFFER)
        self.body_limit = body_limit or CFG.NETWORK_BODY_LIMIT
        self._waiters = []

    async def on_response(self, response):
        try:
            entry = {
                "ts": time.time(),
                "url": response.url,
                "status": response.status,
                "method": response.request.method,
            }
            try:
                entry["resource_type"] = response.request.resource_type
                entry["headers"] = dict(response.headers)
            except Exception:
                pass
            rt = entry.get("resource_type", "")
            if rt in ("xhr", "fetch") or "/json" in \
                    (response.headers or {}).get("content-type", ""):
                try:
                    body = await response.body()
                    if body and len(body) <= self.body_limit:
                        entry["body_b64"] = base64.b64encode(body).decode()
                        entry["body_size"] = len(body)
                except Exception:
                    pass
            self.entries.append(entry)
            self._wake_waiters(entry)
        except Exception:
            pass

    def _wake_waiters(self, entry):
        for w in list(self._waiters):
            if not w["done"].is_set() and self._matches(entry, w["pattern"]):
                w["done"].set()

    @staticmethod
    def _matches(entry, pattern: str) -> bool:
        if not pattern:
            return True
        try:
            if re.search(pattern, entry.get("url", "")):
                return True
        except re.error:
            pass
        return pattern in entry.get("url", "")

    def list(self, pattern: Optional[str] = None, limit: int = 50,
             with_bodies: bool = True) -> list:
        out = []
        for e in reversed(self.entries):  # newest first
            if pattern and not self._matches(e, pattern):
                continue
            item = dict(e)
            item["headers"] = {k: v for k, v in (e.get("headers") or {}).items()
                               if k.lower() not in ("set-cookie", "authorization")}
            if not with_bodies:
                item.pop("body_b64", None)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def bodies_json(self, pattern: Optional[str] = None, limit: int = 20) -> list:
        """Decode captured bodies that parse as JSON."""
        out = []
        for e in self.list(pattern=pattern, limit=limit):
            if "body_b64" in e:
                try:
                    raw = base64.b64decode(e["body_b64"])
                    e["json"] = json.loads(raw)
                    e.pop("body_b64", None)
                    out.append(e)
                except Exception:
                    pass
            else:
                out.append(e)
        return out

    async def wait_for(self, pattern: str, timeout: float = 20.0) -> Optional[dict]:
        # check history first
        for e in reversed(self.entries):
            if self._matches(e, pattern):
                return e
        waiter = {"pattern": pattern, "done": asyncio.Event()}
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter["done"].wait(), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters.remove(waiter)
        for e in reversed(self.entries):
            if self._matches(e, pattern):
                return e
        return None
