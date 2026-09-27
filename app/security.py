"""密码哈希、TOTP、令牌、API Key 加密。

- 密码：scrypt（标准库，内存困难型 KDF），参数 N=2^15, r=8, p=1。
- TOTP：RFC 6238，30 秒步长，6 位，允许前后各 1 个时间窗。
- API Key：Fernet（AES-128-CBC + HMAC）加密，密钥由 DW_MASTER_KEY 派生。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024, dklen=len(expect))
        return hmac.compare_digest(dk, expect)
    except (ValueError, TypeError):
        return False


# ---------- TOTP ----------

def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    pad = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp_now(secret_b32: str, t: float | None = None) -> str:
    return _hotp(secret_b32, int((t or time.time()) // 30))


def verify_totp(secret_b32: str, code: str, t: float | None = None) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    step = int((t or time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret_b32, step + d), code) for d in (-1, 0, 1))


def totp_uri(secret_b32: str, account: str, issuer: str = "DocWork") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret_b32}&issuer={quote(issuer)}"


# ---------- 令牌 ----------

def new_access_token() -> str:
    """32 字节随机数，只在创建时显示一次。"""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


# ---------- API Key 加密 ----------

def _fernet() -> Fernet:
    mk = get_settings().master_key
    if not mk or len(mk) < 16:
        raise RuntimeError("未设置 DW_MASTER_KEY（至少 16 个字符），无法加密存储 API Key")
    key = base64.urlsafe_b64encode(hashlib.sha256(("docwork:" + mk).encode()).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(enc: str) -> str:
    if not enc:
        return ""
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken:
        raise RuntimeError("API Key 解密失败：DW_MASTER_KEY 可能已被修改")
