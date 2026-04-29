# -*- coding: utf-8 -*-
"""
認證模組：JWT Token 產生/驗證、密碼雜湊、FastAPI 依賴注入
純 Python 實作（不依賴 cryptography / cffi）
"""

import os
import hmac
import json
import time
import base64
import hashlib
import secrets
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request

log = logging.getLogger("auth")

# ── 設定 ──────────────────────────────────────────────

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "kepitsimple-default-secret-change-me")
TOKEN_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))


# ── 密碼工具（PBKDF2-SHA256）──────────────────────────

def hash_password(plain: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:260000${salt}${dk.hex()}"


def verify_password(plain: str, hashed: str) -> bool:
    try:
        _, salt, stored_dk = hashed.split("$", 2)
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260_000)
        return hmac.compare_digest(dk.hex(), stored_dk)
    except Exception:
        return False


# ── JWT 工具（HS256）──────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_token(username: str, role: str, display_name: str = "",
                 perm_group: str = "viewer") -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    exp = int(time.time()) + TOKEN_EXPIRE_HOURS * 3600
    payload = _b64url_encode(json.dumps({
        "sub": username, "role": role, "name": display_name,
        "grp": perm_group, "exp": exp,
    }).encode())
    sig_input = f"{header}.{payload}".encode()
    sig = _b64url_encode(hmac.new(SECRET_KEY.encode(), sig_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def decode_token(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("bad token format")
        sig_input = f"{parts[0]}.{parts[1]}".encode()
        expected = hmac.new(SECRET_KEY.encode(), sig_input, hashlib.sha256).digest()
        actual = _b64url_decode(parts[2])
        if not hmac.compare_digest(expected, actual):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(parts[1]))
        if payload.get("exp", 0) < time.time():
            raise ValueError("token expired")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Token 無效或已過期")


# ── FastAPI 依賴注入 ─────────────────────────────────

def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token")


def get_current_user(request: Request) -> dict:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未登入")
    return decode_token(token)


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要 admin 權限")
    return user


PERM_GROUPS = ("viewer", "operator", "kepware_admin")
PERM_LEVELS = {g: i for i, g in enumerate(PERM_GROUPS)}


def require_group(*allowed_groups):
    """檢查使用者是否屬於允許的權限群組（admin 角色自動放行）"""
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") == "admin":
            return user
        user_group = user.get("grp", "viewer")
        if user_group not in allowed_groups:
            raise HTTPException(
                status_code=403,
                detail=f"需要 {'/'.join(allowed_groups)} 群組權限",
            )
        return user
    return _check
