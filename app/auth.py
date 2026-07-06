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

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

log = logging.getLogger("auth")

# ── 設定 ──────────────────────────────────────────────

_env_secret = os.getenv("JWT_SECRET_KEY", "").strip()
if _env_secret:
    SECRET_KEY = _env_secret
else:
    # 未設定 JWT_SECRET_KEY：產生臨時隨機金鑰。
    # 重啟後金鑰會改變，所有既有 Token 會失效；正式環境務必設定 JWT_SECRET_KEY。
    SECRET_KEY = secrets.token_hex(32)
    log.warning(
        "未設定環境變數 JWT_SECRET_KEY，已產生臨時隨機金鑰；"
        "服務重啟後所有 Token 將失效，正式環境請務必設定 JWT_SECRET_KEY。"
    )
TOKEN_EXPIRE_HOURS = int((os.getenv("JWT_EXPIRE_HOURS") or "").strip() or "24")

VALID_ROLES = ("admin", "operator", "user")


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


def create_token(username: str, role: str, display_name: str = "") -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    exp = int(time.time()) + TOKEN_EXPIRE_HOURS * 3600
    payload = _b64url_encode(json.dumps({
        "sub": username, "role": role, "name": display_name, "exp": exp,
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

# 用 FastAPI 的 HTTPBearer 安全性類別（而非手動解析 Request header）取得 token，
# 讓 FastAPI 自動把「需要 Bearer Token」註冊進 OpenAPI schema——
# 這樣 /docs（Swagger UI）才會出現「Authorize」鎖頭按鈕，可貼上登入取得的 token 統一測試；
# auto_error=False 讓我們自行拋出與原本一致的 401 訊息，而非套件預設的錯誤格式。
# 已移除 query string ?token= 支援（會外洩到存取記錄，且前端下載已改用帶認證的 fetch）。
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="未登入")
    return decode_token(credentials.credentials)


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要 admin 權限")
    return user


def require_role(*allowed_roles):
    """檢查使用者角色是否在允許清單中"""
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"需要 {'/'.join(allowed_roles)} 角色權限",
            )
        return user
    return _check


# ── 登入失敗鎖定（記憶體內、防暴力破解）────────────────

# 每個帳號的失敗紀錄：username -> (失敗次數, 視窗起始時間 epoch)
_LOGIN_FAILURES: "dict[str, tuple[int, float]]" = {}

LOGIN_MAX_ATTEMPTS = 5        # 視窗內達此次數即鎖定
LOGIN_LOCKOUT_SECONDS = 300   # 鎖定/計數視窗長度（5 分鐘）


def check_login_allowed(username: str) -> None:
    """若該帳號因連續登入失敗而被鎖定，raise HTTPException(status_code=429)。鎖定期滿後自動解除。"""
    rec = _LOGIN_FAILURES.get(username)
    if not rec:
        return
    count, window_start = rec
    elapsed = time.time() - window_start
    if elapsed >= LOGIN_LOCKOUT_SECONDS:
        # 視窗已過期，視為全新狀態
        _LOGIN_FAILURES.pop(username, None)
        return
    if count >= LOGIN_MAX_ATTEMPTS:
        remaining = int(LOGIN_LOCKOUT_SECONDS - elapsed) + 1
        raise HTTPException(
            status_code=429,
            detail=f"登入失敗次數過多，帳號已暫時鎖定，請於 {remaining} 秒後再試。",
        )


def record_login_failure(username: str) -> None:
    """記錄一次登入失敗。累積達上限即進入鎖定。"""
    now = time.time()
    rec = _LOGIN_FAILURES.get(username)
    if not rec or (now - rec[1]) >= LOGIN_LOCKOUT_SECONDS:
        # 尚無紀錄或視窗已過期：開啟新的計數視窗
        _LOGIN_FAILURES[username] = (1, now)
    else:
        _LOGIN_FAILURES[username] = (rec[0] + 1, rec[1])


def reset_login_failures(username: str) -> None:
    """登入成功時呼叫，清除該帳號的失敗計數。"""
    _LOGIN_FAILURES.pop(username, None)
