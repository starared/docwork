from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from typing import Any


def now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:20]


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def rand_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def stable_hash(o: Any) -> str:
    return sha256_hex(json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":")))[:24]


_CJK = re.compile(r"[　-鿿가-힯＀-￯]")


def estimate_tokens(text: str) -> int:
    """接口不返回用量时的估算：中日韩字符约 1 token/字，其余约 4 字符/token。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + max(0, len(text) - cjk) // 4 + 1


def safe_filename(name: str, default: str = "file") -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name or "").strip(" .")
    return name[:150] or default


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class UserError(Exception):
    """可直接展示给用户的错误。"""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
