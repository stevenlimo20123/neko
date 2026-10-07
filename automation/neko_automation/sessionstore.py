"""Sessionstore parsing (fallback tab discovery when BiDi is unavailable).

Firefox continuously writes the open tabs to
``<profile>/sessionstore-backups/recovery.jsonlz4`` (mozLz4-compressed
JSON, refreshed roughly every 15 seconds).
"""
import json
import os
import struct
from typing import List, Optional


def _mozlz4_decompress(data: bytes) -> bytes:
    if data[:8] != b"mozLz40\0":
        raise ValueError("not a mozLz4 file")
    size = struct.unpack("<I", data[8:12])[0]
    body = data[12:]
    try:
        import lz4.block
        return lz4.block.decompress(body, uncompressed_size=size)
    except ImportError:
        return _lz4_block_pure(body, size)


def _lz4_block_pure(src: bytes, size: int) -> bytes:
    """Minimal pure-python LZ4 block decompressor (slow but dependency-free)."""
    dst = bytearray()
    i = 0
    n = len(src)
    while i < n and len(dst) < size:
        token = src[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                b = src[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        dst += src[i:i + lit_len]
        i += lit_len
        if i >= n or len(dst) >= size:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        match_len = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while True:
                b = src[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        start = len(dst) - offset
        for j in range(match_len):
            dst.append(dst[start + j])
    return bytes(dst[:size])


def read_recovery_tabs(profile: Optional[str] = None) -> List[dict]:
    """Return [{url, title, index, selected}] from recovery.jsonlz4."""
    from .browser import find_profile
    profile = profile or find_profile()
    if not profile:
        return []
    path = os.path.join(profile, "sessionstore-backups", "recovery.jsonlz4")
    if not os.path.exists(path):
        path = os.path.join(profile, "sessionstore-backups", "recovery.baklz4")
    if not os.path.exists(path):
        return []
    try:
        raw = _mozlz4_decompress(open(path, "rb").read())
        data = json.loads(raw)
    except Exception:
        return []
    tabs = []
    for w in data.get("windows", []):
        selected = w.get("selected", 0)
        for idx, t in enumerate(w.get("tabs", [])):
            entries = t.get("entries", [])
            cur = entries[t.get("index", 1) - 1] if entries else {}
            tabs.append({
                "id": f"ss-{w.get('zIndex', 0)}-{idx}",
                "url": cur.get("url", ""),
                "title": cur.get("title", ""),
                "selected": (idx + 1) == selected,
            })
    return tabs
