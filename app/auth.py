# -*- coding: utf-8 -*-
"""
認證模組：JWT Token 產生/驗證、密碼雜湊、FastAPI 依賴注入
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt
from passlib.context import CryptContext

log = logging.getLogger("auth")

# ── 設定 ──────────────────────────────────────────────

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "kepitsimple-default-secret-change-me")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── 密碼工具 ──────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT 工具 ──────────────────────────────────────────

def create_token(username: str, role: str, display_name: str = "") -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": username,
        "role": role,
        "name": display_name,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token 無效或已過期")


# ── FastAPI 依賴注入 ─────────────────────────────────

def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token")


def get_current_user(request: Request) -> dict:
    """驗證 token 並回傳使用者資訊 {"sub", "role", "name"}"""
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未登入")
    return decode_token(token)


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """限定 admin 角色"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要 admin 權限")
    return user
