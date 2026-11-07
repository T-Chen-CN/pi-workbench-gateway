"""配对码：生成、规范化、哈希、消费。

规格（CLOUD_AGENT §4.1 / §4.2 / §4.5）：

* 10 位 Crockford Base32，显示成 `XXXXX-XXXXX`，约 50 bit 熵；
* TTL 600 秒，单次使用，`BEGIN IMMEDIATE` 事务内消费；
* 服务端只存 `sha256(规范化码)`，不存明文。
"""

from __future__ import annotations

import hashlib
import secrets
import time

from .db import Database

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford：去掉 I L O U
CODE_LENGTH = 10
CODE_TTL_SECONDS = 600

# Crockford 的容错映射：手抄时最常见的混淆
_TRANSLATE = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})


def generate(n_parts: int = CODE_LENGTH, *, randbelow=secrets.randbelow) -> str:
    """生成显示用的码（含连字符）。"""
    raw = "".join(ALPHABET[randbelow(len(ALPHABET))] for _ in range(n_parts))
    return f"{raw[:5]}-{raw[5:]}"


def normalize(code: str) -> str:
    """去掉分隔符、统一大小写、修正易混淆字符。"""
    cleaned = "".join(ch for ch in str(code or "").upper() if ch not in "- \t\r\n")
    return cleaned.translate(_TRANSLATE)


def is_valid(code: str) -> bool:
    normalized = normalize(code)
    return len(normalized) == CODE_LENGTH and all(ch in ALPHABET for ch in normalized)


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize(code).encode("utf-8")).hexdigest()


def issue(db: Database, *, now: int | None = None, ttl: int = CODE_TTL_SECONDS) -> dict[str, object]:
    """签发一个连接码。没有任何部分失败：要么全成，要么抛。"""
    at = int(now if now is not None else time.time())
    expires_at = at + int(ttl)
    code = generate()
    db.insert_pairing_code(code_hash=hash_code(code), created_at=at, expires_at=expires_at)
    return {"code": code, "expiresAt": expires_at}


def consume(
    db: Database, code: str, *, device_id: str, now: int | None = None
) -> str:
    """消费连接码，返回 `ok` 或错误 code（由调用方转成 `HubError`）。"""
    at = int(now if now is not None else time.time())
    if not is_valid(code):
        return "pairing_not_found"
    return db.consume_pairing_code(hash_code(code), now=at, used_by_device=device_id)


def pairing_url(code: str, public_url: str, *, base_url: str = "") -> str:
    """优先 `PI_HUB_PUBLIC_URL`，其次由请求推导。"""
    base = (public_url or base_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/#pair={normalize(code)}"
