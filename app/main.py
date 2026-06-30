# -*- coding: utf-8 -*-
"""
FastAPI 應用主程式
提供 REST API 端點 + SSE 即時推送 + 靜態檔案服務
"""

import csv
import io
import json
import logging
import os
import requests
import threading
import uuid
import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.task_manager import TaskManager
from app.config_manager import ConfigManager
from app.pg_client import PGClient
from app.kw_gw_client import KepwareGatewayClient
from app.auth import (
    hash_password, verify_password, create_token,
    get_current_user, require_admin, require_role, VALID_ROLES,
)

# ── Gateway 密碼加解密 (Fernet) ──────────────────────────

from cryptography.fernet import Fernet, InvalidToken
import base64, hashlib

def _get_fernet() -> Fernet:
    raw_key = os.getenv("KW_ENCRYPT_KEY", "kepitsimple-default-encrypt-key")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode()).digest())
    return Fernet(key)

def _encrypt_password(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()

def _decrypt_password(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


def _decrypt_gw_password(gw: dict) -> str:
    """解密 Gateway 密碼；金鑰變更導致無法解密時回傳清楚的 400 提示而非 opaque 500"""
    try:
        return _decrypt_password(gw["password_enc"])
    except InvalidToken:
        raise HTTPException(
            status_code=400,
            detail=f"Gateway「{gw.get('name', gw.get('id'))}」的密碼無法解密"
                   f"（加密金鑰 KW_ENCRYPT_KEY 可能已變更）。請在設定中編輯此 Gateway 並重新輸入密碼。",
        )


def _resolve_gw_credentials(gateway_id: int = None,
                             kw_gw_url: str = None,
                             kw_gw_username: str = None,
                             kw_gw_password: str = None) -> tuple:
    """Resolve gateway credentials from DB (by id) or manual params.
    Returns (url, username, password, verify_ssl).
    """
    if gateway_id:
        gw = pg_client.get_gateway(gateway_id)
        if not gw:
            raise HTTPException(status_code=404, detail=f"Gateway ID={gateway_id} 不存在")
        return (gw["url"], gw["username"],
                _decrypt_gw_password(gw),
                gw.get("verify_ssl", True))
    if not kw_gw_url:
        raise HTTPException(status_code=400, detail="請指定 gateway_id 或提供 kw_gw_url")
    return (kw_gw_url, kw_gw_username, kw_gw_password, True)


# ── Logging ────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

# ── FastAPI App ────────────────────────────────────────

app = FastAPI(title="Kep It Simple · Kepware 點位管理系統")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 路徑常數 ──────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

os.makedirs(DATA_DIR, exist_ok=True)

config_manager = ConfigManager(CONFIG_FILE)


# ── 全域狀態 ───────────────────────────────────────────

task_manager = TaskManager()

# 暫存上傳的 CSV 資料 — 檔案式儲存，伺服器重啟不會遺失
CSV_STORE_DIR = os.path.join(DATA_DIR, "csv_uploads")
os.makedirs(CSV_STORE_DIR, exist_ok=True)


def _csv_store_put(upload_id: str, rows: list):
    """將 CSV 資料寫入暫存檔"""
    path = os.path.join(CSV_STORE_DIR, f"{upload_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def _csv_store_get(upload_id: str):
    """從暫存檔讀取 CSV 資料，找不到回傳 None"""
    path = os.path.join(CSV_STORE_DIR, f"{upload_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

# 載入 .env
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

pg_client = PGClient()


def _ensure_admin_user():
    """啟動時確保至少有一個 admin 帳號"""
    try:
        if pg_client.count_users() == 0:
            admin_user = os.getenv("ADMIN_USERNAME", "admin")
            admin_pass = os.getenv("ADMIN_PASSWORD", "admin")
            pg_client.create_user(
                username=admin_user,
                password_hash=hash_password(admin_pass),
                display_name="管理員",
                role="admin",
            )
            log.info(f"[auth] 自動建立初始管理員帳號: {admin_user}")
    except Exception as e:
        log.warning(f"[auth] 無法檢查/建立初始帳號（PG 可能尚未連線）: {e}")


def _log_activity(request, user: dict, action: str, detail: str = ""):
    """記錄使用者操作日誌"""
    try:
        ip = request.client.host if request.client else ""
        username = user.get("sub", "") if isinstance(user, dict) else str(user)
        pg_client.add_activity_log(username, action, detail, ip)
    except Exception as e:
        log.warning(f"[activity_log] 寫入失敗: {e}")


LOG_CLEANUP_DAYS = int(os.getenv("LOG_CLEANUP_DAYS", "7"))
_cleanup_stop = threading.Event()


def _weekly_log_cleanup():
    """每週清理過期日誌的背景執行緒"""
    while not _cleanup_stop.wait(timeout=3600):
        now = datetime.datetime.now()
        if now.weekday() == 0 and now.hour == 3:
            try:
                count = pg_client.cleanup_activity_logs(days=LOG_CLEANUP_DAYS)
                log.info(f"[cleanup] 每週日誌清理完成，清除 {count} 筆")
            except Exception as e:
                log.warning(f"[cleanup] 日誌清理失敗: {e}")


@app.on_event("startup")
async def on_startup():
    _ensure_admin_user()
    t = threading.Thread(target=_weekly_log_cleanup, daemon=True)
    t.start()
    log.info("[cleanup] 日誌清理排程已啟動（每週一凌晨 3 點）")


@app.on_event("shutdown")
async def on_shutdown():
    """關閉時通知背景排程停止（reload 重啟時可乾淨結束）"""
    _cleanup_stop.set()
    log.info("[shutdown] 已通知背景排程停止")


# ── 歷史紀錄管理 ──────────────────────────────────────

def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_history(records: list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _append_history(task):
    """任務完成後寫入歷史紀錄"""
    records = _load_history()
    entry = {
        "task_id": task.id,
        "type": task.type,
        "dry_run": task.params.get("dry_run", True),
        "csv_filename": task.csv_filename,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "total": task.summary.get("total", 0) if task.summary else 0,
        "success": task.summary.get("success", 0) if task.summary else 0,
        "fail": task.summary.get("fail", 0) if task.summary else 0,
        "skip": task.summary.get("skip", 0) if task.summary else 0,
    }
    records.insert(0, entry)  # 最新在最前
    records = records[:200]   # 保留最近 200 筆
    _save_history(records)
    log.info(f"歷史紀錄已儲存: {task.type} | {task.csv_filename}")


# ── 靜態檔案 ───────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# ── 資料模型 ───────────────────────────────────────────

class DropdownUpdateRequest(BaseModel):
    field: str
    values: list


class DropdownValueRequest(BaseModel):
    field: str
    value: str


class DefaultsUpdateRequest(BaseModel):
    defaults: dict


class MappingRuleUpdateRequest(BaseModel):
    rule_name: str
    mapping: dict


class StagingQueryRequest(BaseModel):
    page: int = 0
    page_size: int = 50
    tb_status: Optional[str] = None
    pg_status: Optional[str] = None
    scale_status: Optional[str] = None


class KwOverrideRequest(BaseModel):
    id: int
    channel: str = ""
    device: str = ""
    tag_groups: str = ""


class TagCondition(BaseModel):
    field: str
    op: str = "contains"
    value: str = ""


class TagsSearchRequest(BaseModel):
    conditions: list = []
    logic: str = "AND"
    page: int = 0
    page_size: int = 50


class StagingStatusRequest(BaseModel):
    ids: list
    field: str
    status: str


class StagingDeleteRequest(BaseModel):
    ids: list


class StagingImportRequest(BaseModel):
    upload_id: str


class RefLocationRequest(BaseModel):
    bu: str
    site: str
    zone: str


class RefOwnershipRequest(BaseModel):
    department: str
    data_owner: str


class RefDeviceRequest(BaseModel):
    device_name: str
    driver_type: str
    site: Optional[str] = None
    system_code: Optional[str] = None
    ip_address: Optional[str] = None
    description: Optional[str] = None


class RefSystemRequest(BaseModel):
    system_code: str
    system_name: Optional[str] = None
    description: Optional[str] = None


class RefTbProfileRequest(BaseModel):
    name: str
    description: Optional[str] = None


class RefDeleteRequest(BaseModel):
    table: str
    id: int


class DeriveRequest(BaseModel):
    ids: Optional[list] = None
    tb_status: Optional[str] = "pending"
    pg_status: Optional[str] = "pending"
    gateway_id: Optional[int] = None


class ExecuteKwRequest(BaseModel):
    gateway_id: Optional[int] = None
    kw_gw_url: Optional[str] = None
    kw_gw_username: Optional[str] = None
    kw_gw_password: Optional[str] = None
    ids: Optional[list] = None
    delay: float = 0.2
    batch_size: int = 50
    batch_pause: float = 5.0


class ExecutePgRequest(BaseModel):
    ids: Optional[list] = None


class DeriveScaleRequest(BaseModel):
    scale_status: Optional[str] = "pending"
    ids: Optional[list] = None


class ExecuteScaleRequest(BaseModel):
    gateway_id: Optional[int] = None
    kw_gw_url: Optional[str] = None
    kw_gw_username: Optional[str] = None
    kw_gw_password: Optional[str] = None
    ids: Optional[list] = None
    delay: float = 0.2
    batch_size: int = 50
    batch_pause: float = 5.0


class KwBatchDeleteRequest(BaseModel):
    upload_id: str
    gateway_id: Optional[int] = None
    kw_gw_url: Optional[str] = None
    kw_gw_username: Optional[str] = None
    kw_gw_password: Optional[str] = None
    dry_run: bool = True
    delay: float = 0.2
    batch_size: int = 50
    batch_pause: float = 5.0


class KwGwSettingsRequest(BaseModel):
    gateway_id: Optional[int] = None
    kw_gw_url: Optional[str] = None
    kw_gw_username: Optional[str] = None
    kw_gw_password: Optional[str] = None


class GatewayCreateRequest(BaseModel):
    name: str
    url: str
    username: str
    password: str
    verify_ssl: bool = True
    zone: Optional[str] = None
    is_default: bool = False


class GatewayUpdateRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify_ssl: Optional[bool] = None
    zone: Optional[str] = None
    is_default: Optional[bool] = None


class UserLoginRequest(BaseModel):
    username: str
    password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "operator"


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class UserPasswordRequest(BaseModel):
    old_password: Optional[str] = None
    new_password: str


# ── API: 使用者認證 ───────────────────────────────────

@app.post("/api/user/login")
async def user_login(req: UserLoginRequest, request: Request):
    """使用者登入取得 JWT Token"""
    user = pg_client.get_user_by_username(req.username)
    if not user:
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    if not user.get("is_active"):
        raise HTTPException(status_code=403, detail="帳號已停用")
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    token = create_token(user["username"], user["role"],
                         user.get("display_name", ""))
    _log_activity(request, {"sub": user["username"]}, "login", "使用者登入")
    return {
        "token": token,
        "username": user["username"],
        "role": user["role"],
        "display_name": user.get("display_name", ""),
    }


@app.get("/api/user/me")
async def user_me(user: dict = Depends(get_current_user)):
    """取得目前登入者資訊"""
    return user


@app.post("/api/user/change-password")
async def user_change_password(req: UserPasswordRequest,
                               user: dict = Depends(get_current_user)):
    """使用者自行修改密碼"""
    db_user = pg_client.get_user_by_username(user["sub"])
    if not db_user:
        raise HTTPException(status_code=404, detail="找不到使用者")
    if req.old_password and not verify_password(req.old_password, db_user["password_hash"]):
        raise HTTPException(status_code=400, detail="舊密碼錯誤")
    pg_client.update_user_password(db_user["id"], hash_password(req.new_password))
    return {"success": True, "message": "密碼已更新"}


# ── API: 帳號管理 (admin only) ────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(user: dict = Depends(require_admin)):
    """取得所有使用者列表"""
    return pg_client.get_all_users()


@app.post("/api/admin/users")
async def admin_create_user(req: UserCreateRequest, request: Request,
                            user: dict = Depends(require_admin)):
    """新增使用者"""
    if pg_client.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail=f"帳號 {req.username} 已存在")
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"角色必須是 {'/'.join(VALID_ROLES)}")
    result = pg_client.create_user(
        username=req.username,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
        role=req.role,
    )
    _log_activity(request, user, "create_user", f"新增使用者 {req.username} (角色: {req.role})")
    return result


@app.put("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, req: UserUpdateRequest,
                            request: Request,
                            user: dict = Depends(require_admin)):
    """更新使用者資訊"""
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if "role" in fields and fields["role"] not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"角色必須是 {'/'.join(VALID_ROLES)}")
    pg_client.update_user(user_id, **fields)
    _log_activity(request, user, "update_user", f"更新使用者 ID={user_id} {fields}")
    return {"success": True}


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, req: UserPasswordRequest,
                               user: dict = Depends(require_admin)):
    """管理員重設使用者密碼"""
    pg_client.update_user_password(user_id, hash_password(req.new_password))
    return {"success": True, "message": "密碼已重設"}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int,
                            user: dict = Depends(require_admin)):
    """刪除使用者"""
    target = pg_client.get_user_by_username(user["sub"])
    if target and target["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能刪除自己的帳號")
    pg_client.delete_user(user_id)
    return {"success": True}


# ── API: 操作日誌 ────────────────────────────────────

class LogQueryRequest(BaseModel):
    page: int = 1
    page_size: int = 50
    username: Optional[str] = None
    action: Optional[str] = None


@app.post("/api/admin/logs/query")
async def admin_query_logs(req: LogQueryRequest,
                           user: dict = Depends(require_admin)):
    """查詢操作日誌（分頁 + 篩選）"""
    try:
        return pg_client.get_activity_logs(
            page=req.page, page_size=req.page_size,
            username=req.username, action=req.action,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查詢日誌失敗: {e}")


@app.post("/api/admin/logs/cleanup")
async def admin_cleanup_logs(request: Request,
                             user: dict = Depends(require_admin)):
    """手動清理過期日誌"""
    try:
        count = pg_client.cleanup_activity_logs(days=LOG_CLEANUP_DAYS)
        _log_activity(request, user, "cleanup_logs", f"手動清理 {count} 筆日誌")
        return {"success": True, "deleted": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清理日誌失敗: {e}")


# ── API: CSV 上傳與預覽 ───────────────────────────────

@app.post("/api/csv/upload")
async def upload_csv(file: UploadFile = File(...)):
    """上傳 CSV 檔案，解析後暫存並回傳預覽"""
    content_bytes = await file.read()

    # 自動偵測編碼 (UTF-8-BOM > UTF-8 > Big5)
    rows = None
    headers = None
    for encoding in ["utf-8-sig", "utf-8", "big5", "gb2312"]:
        try:
            text = content_bytes.decode(encoding)
            reader = csv.DictReader(io.StringIO(text))
            headers = reader.fieldnames
            if not headers:
                continue
            rows = [row for row in reader]
            break
        except (UnicodeDecodeError, Exception):
            continue

    if rows is None or headers is None:
        raise HTTPException(status_code=400, detail="無法解析 CSV 檔案，請確認編碼格式")

    # 清理標頭中的 BOM 與空白
    headers = [h.strip() for h in headers if h and h.strip()]

    # 清理每一行的 key
    cleaned_rows = []
    for row in rows:
        cleaned = {}
        for k, v in row.items():
            if k and k.strip():
                cleaned[k.strip()] = (v or "").strip()
        if any(cleaned.values()):
            cleaned_rows.append(cleaned)

    rows = cleaned_rows

    upload_id = uuid.uuid4().hex[:8]
    _csv_store_put(upload_id, rows)

    preview = rows[:10]

    # 提取不重複的 type 值（供 DeviceProfile 驗證）
    type_key = None
    for h in headers:
        if h.lower() == "type":
            type_key = h
            break
    unique_types = []
    if type_key:
        seen_types = set()
        for row in rows:
            t = (row.get(type_key) or "").strip()
            if t and t not in seen_types:
                seen_types.add(t)
                unique_types.append(t)

    # 檢查 name 重複（CSV 內部）
    name_key = None
    for h in headers:
        if h.lower() == "name":
            name_key = h
            break
    duplicate_names = []
    if name_key:
        name_counts = {}
        for row in rows:
            n = (row.get(name_key) or "").strip()
            if n:
                name_counts[n] = name_counts.get(n, 0) + 1
        duplicate_names = [
            {"name": n, "count": c}
            for n, c in name_counts.items() if c > 1
        ]

    return {
        "upload_id": upload_id,
        "headers": headers,
        "preview": preview,
        "total_rows": len(rows),
        "filename": file.filename,
        "unique_types": unique_types,
        "duplicate_names": duplicate_names,
    }


# ── API: 歷史紀錄 ─────────────────────────────────────

@app.get("/api/history")
async def get_history():
    return _load_history()


@app.delete("/api/history")
async def clear_history():
    _save_history([])
    return {"success": True}


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    """查詢背景任務狀態"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.id,
        "done": task.done,
        "progress": task.progress,
        "summary": task.summary,
    }


# ── API: CSV 範本下載 ─────────────────────────────────

@app.get("/api/templates/{template_type}")
async def download_template(template_type: str):
    """下載 CSV 範本"""
    output = io.StringIO()
    writer = csv.writer(output)

    if template_type == "create":
        writer.writerow(["name", "type", "label", "description"])
        writer.writerow(["Device-001", "MyDeviceProfile", "裝置標籤", "說明文字"])
        writer.writerow(["Device-002", "MyDeviceProfile", "裝置標籤", "說明文字"])
        filename = "template_create.csv"
    elif template_type == "delete":
        writer.writerow(["name", "type"])
        writer.writerow(["Device-001", "MyDeviceProfile"])
        writer.writerow(["Device-002", "MyDeviceProfile"])
        filename = "template_delete.csv"
    else:
        raise HTTPException(status_code=404, detail="Unknown template type")

    content = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )




# ── API: Kepware Gateway 管理 ───────────────────────────

@app.get("/api/kw/gateways")
async def kw_list_gateways(user: dict = Depends(get_current_user)):
    """列出所有 Gateway（密碼不回傳）"""
    try:
        return pg_client.get_gateways()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查詢 Gateway 失敗: {e}")


@app.post("/api/kw/gateways")
async def kw_create_gateway(req: GatewayCreateRequest, request: Request,
                            user: dict = Depends(require_admin)):
    """新增 Gateway"""
    try:
        result = pg_client.create_gateway(
            name=req.name,
            url=req.url.rstrip("/"),
            username=req.username,
            password_enc=_encrypt_password(req.password),
            verify_ssl=req.verify_ssl,
            zone=req.zone,
            is_default=req.is_default,
        )
        _log_activity(request, user, "create_gateway", f"新增 Gateway: {req.name}")
        return result
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"Gateway 名稱 '{req.name}' 已存在")
        raise HTTPException(status_code=500, detail=f"新增 Gateway 失敗: {e}")


@app.put("/api/kw/gateways/{gw_id}")
async def kw_update_gateway(gw_id: int, req: GatewayUpdateRequest,
                            request: Request,
                            user: dict = Depends(require_admin)):
    """更新 Gateway（密碼為空則不更新）"""
    try:
        fields = {}
        if req.name is not None:
            fields["name"] = req.name
        if req.url is not None:
            fields["url"] = req.url.rstrip("/")
        if req.username is not None:
            fields["username"] = req.username
        if req.password:
            fields["password_enc"] = _encrypt_password(req.password)
        if req.verify_ssl is not None:
            fields["verify_ssl"] = req.verify_ssl
        if req.zone is not None:
            fields["zone"] = req.zone
        if req.is_default is not None:
            fields["is_default"] = req.is_default
        ok = pg_client.update_gateway(gw_id, **fields)
        if not ok:
            raise HTTPException(status_code=404, detail="Gateway 不存在")
        _log_activity(request, user, "update_gateway", f"更新 Gateway ID={gw_id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新 Gateway 失敗: {e}")


@app.delete("/api/kw/gateways/{gw_id}")
async def kw_delete_gateway(gw_id: int, request: Request,
                            user: dict = Depends(require_admin)):
    """刪除 Gateway"""
    try:
        ok = pg_client.delete_gateway(gw_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Gateway 不存在")
        _log_activity(request, user, "delete_gateway", f"刪除 Gateway ID={gw_id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刪除 Gateway 失敗: {e}")


@app.post("/api/kw/gateways/{gw_id}/test")
async def kw_test_gateway(gw_id: int, user: dict = Depends(get_current_user)):
    """測試指定 Gateway 連線"""
    gw = pg_client.get_gateway(gw_id)
    if not gw:
        raise HTTPException(status_code=404, detail="Gateway 不存在")
    try:
        password = _decrypt_gw_password(gw)
        client = KepwareGatewayClient(gw["url"], verify=gw.get("verify_ssl", True))
        client.login(gw["username"], password)
        return {"success": True, "message": f"Gateway '{gw['name']}' 連線成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"連線失敗: {e}")


@app.post("/api/kw/gateways/{gw_id}/sync")
async def kw_sync_structure(gw_id: int, request: Request,
                            user: dict = Depends(require_role("admin", "operator"))):
    """從指定 Gateway 拉取 Channel/Device/TagGroup 結構並寫入 DB"""
    gw = pg_client.get_gateway(gw_id)
    if not gw:
        raise HTTPException(status_code=404, detail="Gateway 不存在")
    try:
        password = _decrypt_gw_password(gw)
        client = KepwareGatewayClient(gw["url"], verify=gw.get("verify_ssl", True))
        client.login(gw["username"], password)
        structure = client.fetch_structure()
        counts = pg_client.sync_structure(gw_id, structure)
        _log_activity(request, user, "sync_structure",
                      f"Gateway '{gw['name']}' 結構同步: {counts['channels']}ch/{counts['devices']}dev/{counts['groups']}grp")
        return {"success": True, **counts}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[sync_structure] 同步失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"結構同步失敗: {e}")


@app.get("/api/kw/gateways/{gw_id}/structure")
async def kw_get_structure(gw_id: int, user: dict = Depends(get_current_user)):
    """取得指定 Gateway 的快取結構"""
    gw = pg_client.get_gateway(gw_id)
    if not gw:
        raise HTTPException(status_code=404, detail="Gateway 不存在")
    structure = pg_client.get_structure(gw_id)
    synced_at = pg_client.get_structure_synced_at(gw_id)
    return {
        "gateway_id": gw_id,
        "gateway_name": gw["name"],
        "structure": structure,
        "synced_at": synced_at.isoformat() if synced_at else None,
    }


@app.post("/api/kw/delete-batch")
async def kw_delete_batch(req: KwBatchDeleteRequest, request: Request,
                          user: dict = Depends(require_role("admin", "operator"))):
    """透過 Kepware API 批次刪除 Tag（從 CSV 推導路徑）"""
    rows = _csv_store_get(req.upload_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="找不到上傳的 CSV 資料，請重新上傳")

    gw_url, gw_user, gw_pass, gw_verify = _resolve_gw_credentials(
        req.gateway_id, req.kw_gw_url, req.kw_gw_username, req.kw_gw_password)

    task = task_manager.create_task("kw_delete", {
        "rows": rows,
        "dry_run": req.dry_run,
        "kw_gw_url": gw_url,
        "kw_gw_username": gw_user,
        "kw_gw_password": gw_pass,
        "kw_gw_verify": gw_verify,
        "delay": req.delay,
        "batch_size": req.batch_size,
        "batch_pause": req.batch_pause,
    })
    task.csv_filename = "(Kepware 批次刪除)"
    task.on_complete = _append_history

    def _exec_kw_delete(task):
        import datetime as dt, time, random
        from app.task_manager import _smart_get
        task.started_at = dt.datetime.now().isoformat()
        params = task.params
        csv_rows = params["rows"]
        dry_run = params.get("dry_run", True)

        # 從 CSV 的 name/type 直接拆解 Kepware 路徑
        derived = []
        for row in csv_rows:
            tag_name = _smart_get(row, "name")
            tb_type = _smart_get(row, "type")
            if not tag_name or not tb_type:
                continue
            tp = tb_type.split("-")
            derived.append({
                "tag_name": tag_name,
                "channel_name": tp[0] if len(tp) > 0 else "",
                "device_name": tp[1] if len(tp) > 1 else "",
                "tag_groups": ".".join(tp[2:]) if len(tp) > 2 else "",
            })
        if not derived:
            task.push_log("warning", "無可處理的 Tag 資料")
            task.push_complete({"total": 0, "success": 0, "fail": 0, "skip": 0})
            return

        if not dry_run:
            try:
                client = KepwareGatewayClient(params["kw_gw_url"],
                                              verify=params.get("kw_gw_verify", True))
                client.login(params["kw_gw_username"], params["kw_gw_password"])
                task.push_log("info", "Kepware Gateway 登入成功")
            except Exception as e:
                task.push_log("error", f"Kepware 登入失敗: {e}")
                task.push_complete({"total": len(derived), "success": 0, "fail": 0, "skip": 0})
                return

        total = len(derived)
        success = fail = 0
        for idx, item in enumerate(derived, 1):
            tag = item["tag_name"]
            ch = item["channel_name"]
            dev = item["device_name"]
            grp = item["tag_groups"] or None
            if dry_run:
                task.push_log("info", f"[預演] 模擬刪除 Tag: {ch}/{dev}/{grp or ''}/{tag}")
                success += 1
                task.add_result(tag, "success", "預演")
            else:
                try:
                    client.delete_tag(ch, dev, tag, tag_group=grp)
                    task.push_log("success", f"[成功] 已刪除 Tag: {tag}")
                    success += 1
                    task.add_result(tag, "success", "")
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else "?"
                    if status == 404:
                        task.push_log("warning", f"[略過] Tag 不存在: {tag}")
                        success += 1
                        task.add_result(tag, "success", "不存在")
                    else:
                        task.push_log("error", f"[失敗] Tag: {tag} (HTTP {status})")
                        fail += 1
                        task.add_result(tag, "fail", f"HTTP {status}")
                except Exception as e:
                    task.push_log("error", f"[異常] {tag}: {e}")
                    fail += 1
                    task.add_result(tag, "fail", str(e))

            task.push_progress(idx, total, success, fail, 0)
            time.sleep(params.get("delay", 0.2) + random.uniform(0.01, 0.05))
            if success > 0 and success % params.get("batch_size", 50) == 0:
                time.sleep(params.get("batch_pause", 5))

        task.push_complete({"total": total, "success": success, "fail": fail, "skip": 0})

    task_manager.run_in_background(task, _exec_kw_delete)
    return {"task_id": task.id}


# ── API: 設定管理 ─────────────────────────────────────

@app.get("/api/config")
async def get_config():
    """取得完整設定"""
    return config_manager.get_all()


@app.get("/api/config/dropdown")
async def get_dropdown_options():
    """取得所有下拉選項"""
    return config_manager.get_dropdown_options()


@app.put("/api/config/dropdown")
async def update_dropdown_field(req: DropdownUpdateRequest):
    """更新指定欄位的下拉選項列表"""
    config_manager.set_dropdown_field(req.field, req.values)
    return {"success": True, "field": req.field, "values": req.values}


@app.post("/api/config/dropdown/add")
async def add_dropdown_value(req: DropdownValueRequest):
    """新增一個下拉選項值"""
    added = config_manager.add_dropdown_value(req.field, req.value)
    if not added:
        raise HTTPException(status_code=409, detail=f"值 '{req.value}' 已存在於 '{req.field}'")
    return {"success": True}


@app.post("/api/config/dropdown/remove")
async def remove_dropdown_value(req: DropdownValueRequest):
    """移除一個下拉選項值"""
    removed = config_manager.remove_dropdown_value(req.field, req.value)
    if not removed:
        raise HTTPException(status_code=404, detail=f"值 '{req.value}' 不存在於 '{req.field}'")
    return {"success": True}


@app.get("/api/config/defaults")
async def get_defaults():
    """取得預設值"""
    return config_manager.get_defaults()


@app.put("/api/config/defaults")
async def update_defaults(req: DefaultsUpdateRequest):
    """更新預設值"""
    config_manager.set_defaults(req.defaults)
    return {"success": True}


@app.get("/api/config/mapping")
async def get_mapping_rules():
    """取得所有映射規則"""
    return config_manager.get_mapping_rules()


@app.put("/api/config/mapping")
async def update_mapping_rule(req: MappingRuleUpdateRequest):
    """更新指定映射規則"""
    config_manager.set_mapping_rule(req.rule_name, req.mapping)
    return {"success": True, "rule_name": req.rule_name}


@app.delete("/api/config/mapping/{rule_name}")
async def delete_mapping_rule(rule_name: str):
    """刪除映射規則"""
    deleted = config_manager.delete_mapping_rule(rule_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"規則 '{rule_name}' 不存在")
    return {"success": True}


# ── API: PostgreSQL 連線 ─────────────────────────────

@app.get("/api/pg/test")
async def pg_test_connection():
    """測試 PG 連線"""
    try:
        result = pg_client.test_connection()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PG 連線失敗: {e}")


# ── API: 暫存表 (scada_tag_config) ───────────────────

@app.post("/api/pg/staging/import")
async def pg_staging_import(req: StagingImportRequest, request: Request,
                            user: dict = Depends(require_role("admin", "operator"))):
    """將已上傳的 CSV 資料匯入 PG 暫存表"""
    rows = _csv_store_get(req.upload_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="找不到上傳的 CSV 資料，請重新上傳")
    try:
        result = pg_client.import_staging(rows)
        _log_activity(request, user, "staging_import", f"匯入暫存表 {result.get('inserted',0)} 筆")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"匯入暫存表失敗: {e}")


@app.post("/api/pg/staging/query")
async def pg_staging_query(req: StagingQueryRequest):
    """分頁查詢暫存表"""
    try:
        return pg_client.get_staging_list(
            page=req.page, page_size=req.page_size,
            tb_status=req.tb_status, pg_status=req.pg_status,
            scale_status=req.scale_status,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查詢暫存表失敗: {e}")


@app.post("/api/pg/staging/kw-override")
async def pg_staging_kw_override(req: KwOverrideRequest,
                                  user: dict = Depends(require_role("admin", "operator"))):
    """儲存 Step 3 手動覆寫的 channel / device / tag_groups"""
    try:
        ok = pg_client.update_kw_overrides(req.id, req.channel, req.device, req.tag_groups)
        if not ok:
            raise HTTPException(status_code=404, detail=f"找不到暫存列 id={req.id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/tags/search")
async def pg_tags_search(req: TagsSearchRequest, user: dict = Depends(get_current_user)):
    """多條件查詢正式 tags 表"""
    try:
        conditions = [c if isinstance(c, dict) else c.dict() for c in req.conditions]
        return pg_client.search_formal_tags(
            conditions=conditions,
            logic=req.logic,
            page=req.page,
            page_size=min(req.page_size, 200),
        )
    except Exception as e:
        log.error(f"[tags/search] {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"查詢失敗: {e}")


@app.post("/api/pg/tags/export")
async def pg_tags_export(req: TagsSearchRequest, user: dict = Depends(get_current_user)):
    """將查詢結果匯出為 CSV"""
    try:
        conditions = [c if isinstance(c, dict) else c.dict() for c in req.conditions]
        result = pg_client.search_formal_tags(
            conditions=conditions,
            logic=req.logic,
            page=0,
            page_size=10000,
        )
        rows = result.get("data", [])
        if not rows:
            raise HTTPException(status_code=404, detail="查無資料可匯出")

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        content = "﻿" + output.getvalue()

        filename = f"tags_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return StreamingResponse(
            iter([content]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[tags/export] {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"匯出失敗: {e}")


@app.post("/api/pg/staging/status")
async def pg_staging_update_status(req: StagingStatusRequest):
    """批次更新暫存表狀態"""
    try:
        count = pg_client.update_staging_status(req.ids, req.field, req.status)
        return {"success": True, "updated": count}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/pg/staging/delete")
async def pg_staging_delete(req: StagingDeleteRequest,
                            user: dict = Depends(require_admin)):
    """刪除暫存表資料（admin only）"""
    try:
        count = pg_client.delete_staging(req.ids)
        return {"success": True, "deleted": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/pg/staging/clear")
async def pg_staging_clear(user: dict = Depends(require_admin)):
    """清空暫存表（admin only）"""
    try:
        count = pg_client.clear_staging()
        return {"success": True, "deleted": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── API: 參照表 CRUD ─────────────────────────────────

@app.get("/api/pg/ref/locations")
async def pg_get_locations():
    try:
        return pg_client.get_locations()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/locations")
async def pg_add_location(req: RefLocationRequest):
    try:
        row_id = pg_client.upsert_location(req.bu, req.site, req.zone)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pg/ref/ownerships")
async def pg_get_ownerships():
    try:
        return pg_client.get_ownerships()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/ownerships")
async def pg_add_ownership(req: RefOwnershipRequest):
    try:
        row_id = pg_client.upsert_ownership(req.department, req.data_owner)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pg/ref/devices")
async def pg_get_devices():
    try:
        return pg_client.get_devices()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/devices")
async def pg_add_device(req: RefDeviceRequest):
    try:
        row_id = pg_client.upsert_device(
            req.device_name, req.driver_type,
            req.site, req.system_code,
            req.ip_address, req.description,
        )
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pg/ref/systems")
async def pg_get_systems():
    try:
        return pg_client.get_systems()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/systems")
async def pg_add_system(req: RefSystemRequest):
    try:
        row_id = pg_client.upsert_system(req.system_code, req.system_name, req.description)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pg/ref/tb-profiles")
async def pg_get_tb_profiles():
    try:
        return pg_client.get_tb_profiles()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/tb-profiles")
async def pg_add_tb_profile(req: RefTbProfileRequest):
    try:
        row_id = pg_client.upsert_tb_profile(req.name, req.description)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/delete")
async def pg_delete_ref(req: RefDeleteRequest):
    """刪除參照表資料"""
    try:
        ok = pg_client.delete_ref_row(req.table, req.id)
        return {"success": ok}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── API: Tag 推導 ────────────────────────────────────

@app.post("/api/kw/derive")
async def kw_derive(req: DeriveRequest,
                    user: dict = Depends(require_role("admin", "operator"))):
    """推導 Kepware 建點欄位（tag group + tag），若指定 gateway_id 則比對結構快取"""
    try:
        staging = pg_client.get_staging_list(
            page=0, page_size=9999,
            tb_status=req.tb_status,
        )
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        results = pg_client.derive_kw_fields(rows)

        if req.gateway_id:
            structure = pg_client.get_structure(req.gateway_id)
            for item in results:
                ch = item["channel_name"]
                dev = item["device_name"]
                grp = item["tag_groups"]
                ch_exists = ch in structure
                dev_exists = ch_exists and dev in structure.get(ch, {})
                grp_exists = dev_exists and grp in structure.get(ch, {}).get(dev, [])
                item["validation"] = {
                    "channel_exists": ch_exists,
                    "device_exists": dev_exists,
                    "group_exists": grp_exists if grp else True,
                    "group_auto_create": dev_exists and not grp_exists and bool(grp),
                }

        return {"data": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"推導失敗: {e}")


@app.post("/api/pg/derive/pg")
async def pg_derive_pg(req: DeriveRequest,
                       user: dict = Depends(require_role("admin", "operator"))):
    """推導 PG 正式表欄位"""
    try:
        staging = pg_client.get_staging_list(
            page=0, page_size=9999,
            pg_status=req.pg_status,
        )
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        results = pg_client.derive_pg_fields(rows)
        return {"data": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"推導失敗: {e}")


# ── API: 執行建點 / 寫入 ──────────────────────────────

@app.post("/api/kw/execute")
async def kw_execute(req: ExecuteKwRequest, request: Request,
                     user: dict = Depends(require_role("admin", "operator"))):
    """執行 Kepware 建點：推導欄位 → 建立 tag group + tag → 更新 tb_status（含限速）"""
    import time, random
    try:
        staging = pg_client.get_staging_list(page=0, page_size=9999, tb_status="pending")
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        if not rows:
            return {"success": 0, "failed": 0, "errors": [], "message": "無待建點資料"}

        derived = pg_client.derive_kw_fields(rows)
        log.info(f"[executeKW] 共 {len(derived)} 筆，delay={req.delay}, batch_size={req.batch_size}, batch_pause={req.batch_pause}")

        gw_url, gw_user, gw_pass, gw_verify = _resolve_gw_credentials(
            req.gateway_id, req.kw_gw_url, req.kw_gw_username, req.kw_gw_password)
        client = KepwareGatewayClient(gw_url, verify=gw_verify)
        client.login(gw_user, gw_pass)

        # 預先建立所有需要的 tag group（收集 unique paths 後逐層建立）
        unique_groups = {}
        for item in derived:
            ch = item["channel_name"]
            dev = item["device_name"]
            grp = item["tag_groups"]
            if grp:
                unique_groups[(ch, dev, grp)] = True
        for (ch, dev, grp) in unique_groups:
            try:
                client.ensure_tag_groups(ch, dev, grp)
            except Exception as e:
                log.warning(f"[executeKW] tag group 建立失敗 {ch}/{dev}/{grp}: {e}")

        success_ids = []
        failed = []
        success_count = 0
        for idx, item in enumerate(derived):
            try:
                client.create_tag(
                    channel_name=item["channel_name"],
                    device_name=item["device_name"],
                    tag_name=item["tag_name"],
                    address=item.get("address") or None,
                    description=item.get("description") or None,
                    tag_group=item["tag_groups"] or None,
                )
                success_ids.append(item["id"])
                success_count += 1
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 409:
                    success_ids.append(item["id"])
                    success_count += 1
                elif e.response is not None and e.response.status_code == 429:
                    log.warning(f"[executeKW] 收到 429 限速，暫停 {req.batch_pause} 秒")
                    time.sleep(req.batch_pause)
                    try:
                        client.create_tag(
                            channel_name=item["channel_name"],
                            device_name=item["device_name"],
                            tag_name=item["tag_name"],
                            address=item.get("address") or None,
                            description=item.get("description") or None,
                            tag_group=item["tag_groups"] or None,
                        )
                        success_ids.append(item["id"])
                        success_count += 1
                    except Exception as retry_e:
                        failed.append({"name": item["tag_name"], "reason": str(retry_e)})
                else:
                    status = e.response.status_code if e.response is not None else "?"
                    text = e.response.text[:200] if e.response is not None else str(e)
                    failed.append({"name": item["tag_name"], "reason": f"HTTP {status}: {text}"})
            except Exception as e:
                failed.append({"name": item["tag_name"], "reason": str(e)})

            time.sleep(req.delay + random.uniform(0.01, 0.05))
            if success_count > 0 and success_count % req.batch_size == 0:
                log.info(f"[executeKW] 已處理 {success_count} 筆，冷卻暫停 {req.batch_pause} 秒")
                time.sleep(req.batch_pause)

        if success_ids:
            pg_client.update_staging_status(success_ids, "tb_status", "done")

        _log_activity(request, user, "execute_tb", f"Kepware 建點成功 {len(success_ids)} 筆，失敗 {len(failed)} 筆")
        return {
            "success": len(success_ids),
            "failed": len(failed),
            "errors": failed,
            "message": f"成功建立 {len(success_ids)} 筆 Tag"
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[executeKW] Kepware 建點失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Kepware 建點失敗: {e}")


@app.post("/api/pg/execute/pg")
async def pg_execute_pg(req: ExecutePgRequest, request: Request,
                        user: dict = Depends(require_role("admin", "operator"))):
    """執行 PG 寫入：推導欄位 → 寫入正式表 + Collector tags 表 → 更新 pg_status"""
    try:
        # 1. 取得 pending 的暫存資料並推導
        staging = pg_client.get_staging_list(page=0, page_size=9999, pg_status="pending")
        rows = staging["data"]
        log.info(f"[executePG] 取得 staging 資料 {len(rows)} 筆")
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
            log.info(f"[executePG] 篩選後 {len(rows)} 筆")
        if not rows:
            log.warning("[executePG] 無待寫入資料")
            return {"inserted": 0, "skipped": 0, "errors": [], "message": "無待寫入資料"}

        derived = pg_client.derive_pg_fields(rows)
        log.info(f"[executePG] 推導完成 {len(derived)} 筆，範例: {derived[0] if derived else 'N/A'}")

        # 2. 寫入正式表
        result = pg_client.import_formal(derived)
        log.info(f"[executePG] import_formal 結果: {result}")

        # 3. 寫入 Collector DB tags 表
        collector_result = None
        collector_db = os.getenv("COLLECTOR_DB_DATABASE", "").strip()
        if collector_db:
            collector_rows = []
            error_tagnames_formal = {e["tagname"] for e in result.get("errors", [])}
            for d in derived:
                if d["tagname"] in error_tagnames_formal:
                    continue
                collector_rows.append({
                    "tagname": d["tagname"],
                    "tag_address": d.get("tag_address", ""),
                    "scan_group": d.get("scan_group", ""),
                    "description": d.get("description", ""),
                    "enable": True,
                    "target_table": d.get("tabname", ""),
                })
            if collector_rows:
                try:
                    collector_result = pg_client.import_collector_tags(collector_rows)
                    log.info(f"[executePG] Collector 寫入結果: {collector_result}")
                except Exception as ce:
                    log.error(f"[executePG] Collector 寫入失敗，回滾本地正式表: {ce}", exc_info=True)
                    # 回滾：只刪除本次「新插入」的 tagname，避免誤刪原本就存在、只是被更新的資料
                    collector_names = {r["tagname"] for r in collector_rows}
                    rollback_names = [t for t in result.get("new_tagnames", []) if t in collector_names]
                    try:
                        pg_client.delete_formal_by_tagnames(rollback_names)
                        log.info(f"[executePG] 已回滾 {len(rollback_names)} 筆新插入的正式表資料"
                                 f"（{len(collector_names) - len(rollback_names)} 筆為既有資料，保留不刪）")
                    except Exception as re:
                        log.error(f"[executePG] 回滾失敗: {re}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Collector DB 寫入失敗（已回滾本次新增的正式表資料）: {ce}"
                    )
        else:
            log.info("[executePG] 未設定 COLLECTOR_DB_DATABASE，跳過 Collector 寫入")

        # 4. 更新成功的暫存資料狀態（排除有錯誤的 tagname）
        error_tagnames = {e["tagname"] for e in result.get("errors", [])}
        if collector_result:
            error_tagnames |= {e["tagname"] for e in collector_result.get("errors", [])}
        success_ids = [r["id"] for r, d in zip(rows, derived) if d["tagname"] not in error_tagnames]
        if success_ids:
            pg_client.update_staging_status(success_ids, "pg_status", "done")
            log.info(f"[executePG] 更新 {len(success_ids)} 筆狀態為 done")

        _log_activity(request, user, "execute_pg", f"PG 寫入成功 {result['inserted']} 筆")
        resp = {
            **result,
            "message": f"成功寫入 {result['inserted']} 筆至正式表"
        }
        if collector_result:
            resp["collector_inserted"] = collector_result["inserted"]
            resp["collector_errors"] = collector_result["errors"]
            resp["message"] += f"，Collector {collector_result['inserted']} 筆"
        return resp
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[executePG] PG 寫入失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PG 寫入失敗: {e}")


# ── API: Kepware Gateway Scale ────────────────────────

@app.post("/api/kw-gw/test")
async def kw_gw_test(req: KwGwSettingsRequest):
    """測試 Kepware API Gateway 連線（支援 gateway_id 或手動輸入）"""
    try:
        gw_url, gw_user, gw_pass, gw_verify = _resolve_gw_credentials(
            req.gateway_id, req.kw_gw_url, req.kw_gw_username, req.kw_gw_password)
        client = KepwareGatewayClient(gw_url, verify=gw_verify)
        client.login(gw_user, gw_pass)
        return {"success": True, "message": "Kepware API Gateway 連線成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"連線失敗: {e}")


@app.post("/api/pg/derive/scale")
async def pg_derive_scale(req: DeriveScaleRequest,
                          user: dict = Depends(require_role("admin", "operator"))):
    """推導 Scale 配置欄位（從暫存表的 scale_enabled/raw_low/raw_high/scaled_low/scaled_high）"""
    try:
        staging = pg_client.get_staging_list(
            page=0, page_size=9999,
            scale_status=req.scale_status,
        )
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        results = pg_client.derive_scale_fields(rows)
        return {"data": results, "total": len(results)}
    except Exception as e:
        log.error(f"[deriveScale] 推導失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"推導失敗: {e}")


@app.post("/api/pg/execute/scale")
async def pg_execute_scale(req: ExecuteScaleRequest, request: Request,
                           user: dict = Depends(require_role("admin", "operator"))):
    """執行 Scale 設定：推導欄位 → 呼叫 Kepware GW API → 更新 scale_status"""
    import time, random
    try:
        # 1. 取得 pending 的暫存資料並推導
        staging = pg_client.get_staging_list(page=0, page_size=9999, scale_status="pending")
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        if not rows:
            return {"success": 0, "skipped": 0, "errors": [], "message": "無待設定 Scale 的資料"}

        derived = pg_client.derive_scale_fields(rows)
        if not derived:
            return {"success": 0, "skipped": len(rows), "errors": [],
                    "message": "無啟用 Scale 的資料（scale_enabled=false 或缺少範圍值）"}

        log.info(f"[executeScale] 共 {len(derived)} 筆，delay={req.delay}, batch_size={req.batch_size}")

        # 2. 登入 Kepware Gateway
        gw_url, gw_user, gw_pass, gw_verify = _resolve_gw_credentials(
            req.gateway_id, req.kw_gw_url, req.kw_gw_username, req.kw_gw_password)
        client = KepwareGatewayClient(gw_url, verify=gw_verify)
        client.login(gw_user, gw_pass)

        success_ids = []
        failed = []
        success_count = 0
        for idx, item in enumerate(derived):
            config = {
                "channel_name": item["channel_name"],
                "device_name": item["device_name"],
                "tag_name": item["full_tag_name"],
                "data_type": item["data_type"],
                "scaling_type": item["scaling_type"],
            }
            # Linear 才帶 scaling 參數
            if item["scaling_type"] == 1:
                config.update({
                    "scaling_raw_low": item["scaling_raw_low"],
                    "scaling_raw_high": item["scaling_raw_high"],
                    "scaling_scaled_low": item["scaling_scaled_low"],
                    "scaling_scaled_high": item["scaling_scaled_high"],
                    "scaling_clamp_low": item.get("scaling_clamp_low", True),
                    "scaling_clamp_high": item.get("scaling_clamp_high", True),
                    "scaling_scaled_data_type": item.get("scaling_scaled_data_type", 8),
                })

            try:
                client.set_tag_scaling(config)
                success_ids.append(item["id"])
                success_count += 1
            except Exception as e:
                log.error(f"[executeScale] {item['tag_name']} 失敗: {e}")
                failed.append({"tag_name": item["tag_name"], "reason": str(e)})

            # 限速控制
            time.sleep(req.delay + random.uniform(0.01, 0.05))
            if success_count > 0 and success_count % req.batch_size == 0:
                log.info(f"[executeScale] 已處理 {success_count} 筆，冷卻暫停 {req.batch_pause} 秒")
                time.sleep(req.batch_pause)

        # 3. 更新成功的暫存資料狀態
        if success_ids:
            pg_client.update_staging_status(success_ids, "scale_status", "done")

        _log_activity(request, user, "execute_scale", f"Scale 設定成功 {len(success_ids)} 筆，失敗 {len(failed)} 筆")
        return {
            "success": len(success_ids),
            "failed": len(failed),
            "errors": failed,
            "message": f"成功設定 {len(success_ids)} 筆 Scale"
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[executeScale] Scale 設定失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scale 設定失敗: {e}")


# ── API: Collector DB + Reload ────────────────────────

@app.get("/api/collector/test")
async def collector_test_connection():
    """測試 Collector DB 連線"""
    try:
        result = pg_client.test_collector_connection()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Collector DB 連線失敗: {e}")


@app.post("/api/collector/reload")
async def collector_reload(request: Request,
                           user: dict = Depends(require_role("admin", "operator"))):
    """呼叫 Collector /reload API 重新載入 tag 清單"""
    reload_url = os.getenv("COLLECTOR_RELOAD_URL", "").strip()
    reload_token = os.getenv("COLLECTOR_RELOAD_TOKEN", "").strip()
    if not reload_url:
        raise HTTPException(status_code=400, detail="未設定 COLLECTOR_RELOAD_URL 環境變數")
    if not reload_token:
        raise HTTPException(status_code=400, detail="未設定 COLLECTOR_RELOAD_TOKEN 環境變數")

    try:
        resp = requests.post(
            reload_url,
            headers={"X-Reload-Token": reload_token},
            timeout=30,
        )
        data = resp.json()
        if resp.status_code == 200:
            _log_activity(request, user, "collector_reload",
                          f"Collector 重載成功，共 {data.get('total_tags', '?')} 筆 tag")
            return {"success": True, **data}
        else:
            raise HTTPException(status_code=resp.status_code,
                                detail=data.get("message", f"Collector 回傳 HTTP {resp.status_code}"))
    except requests.RequestException as e:
        log.error(f"[collector_reload] 呼叫失敗: {e}")
        raise HTTPException(status_code=502, detail=f"無法連線 Collector: {e}")
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[collector_reload] 未預期錯誤: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Collector 重載失敗: {e}")


# ── IO Mapping ────────────────────────────────────────

def _parse_io_csv(content_bytes: bytes) -> tuple:
    """解析 A 檔案（IO List / 需求表），回傳 (headers, rows)"""
    for enc in ["utf-8-sig", "utf-8", "big5", "cp950", "gb2312"]:
        try:
            text = content_bytes.decode(enc)
            reader = csv.DictReader(io.StringIO(text))
            headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
            if not headers:
                continue
            rows = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]
            return headers, rows
        except (UnicodeDecodeError, Exception):
            continue
    return None, None


def _parse_ifix_csv(content_bytes: bytes) -> tuple:
    """解析 B 檔案（iFIX 導出表），處理 ! 前綴和 [section] 標記"""
    for enc in ["cp950", "utf-8-sig", "utf-8", "big5", "gb2312"]:
        try:
            text = content_bytes.decode(enc)
            break
        except (UnicodeDecodeError, Exception):
            continue
    else:
        return None, None

    lines = text.splitlines()
    sample = lines[0] if lines else ""
    delimiter = "\t" if "\t" in sample else ","

    current_header = []
    b_data = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        if not row or row[0].startswith("["):
            continue
        if row[0].startswith("!"):
            current_header = [h.strip("! ") for h in row]
            continue
        if current_header:
            row_dict = {}
            for idx, val in enumerate(row):
                if idx < len(current_header):
                    row_dict[current_header[idx]] = val.strip("! ")
            b_data.append(row_dict)

    if not b_data:
        return None, None
    all_keys = list(dict.fromkeys(k for r in b_data for k in r))
    return all_keys, b_data


IO_MAPPING_FIELDS = {
    "A_IODV": "I/O DEVICE",
    "A_IOAD": "I/O ADDRESS",
    "A_SCALE_ENABLED": "SCALE Enabled",
    "A_SCALE_RAWLOW": "Raw Low",
    "A_SCALE_RAWHIGH": "Raw High",
    "A_ELO": "Scaled Low",
    "A_EHI": "Scaled High",
    "A_DESC": "Description",
}

IO_OUTPUT_COLUMNS = [
    "Site", "System", "SCADA Node Name", "Tag Name",
    "I/O DEVICE", "I/O ADDRESS", "SCALE Enabled",
    "Raw Low", "Raw High", "Scaled Low", "Scaled High", "Description",
]

_io_mapping_cache = {}
_IO_MAPPING_CACHE_MAX = 50  # 最多保留 50 筆 mapping 結果，超過則淘汰最舊的


@app.post("/api/io-mapping/execute")
async def io_mapping_execute(
    a_file: UploadFile = File(...),
    b_file: UploadFile = File(...),
):
    """上傳 IO List (A) 與 iFIX 導出表 (B)，執行 mapping"""
    a_bytes = await a_file.read()
    b_bytes = await b_file.read()

    a_headers, a_rows = _parse_io_csv(a_bytes)
    if a_rows is None:
        raise HTTPException(status_code=400, detail="無法解析 A 檔案（IO List），請確認 CSV 格式與編碼")

    b_headers, b_rows = _parse_ifix_csv(b_bytes)
    if b_rows is None:
        raise HTTPException(status_code=400, detail="無法解析 B 檔案（iFIX 導出表），請確認檔案格式")

    has_a_tag = any("A_TAG" in r for r in b_rows)
    if not has_a_tag:
        raise HTTPException(status_code=400, detail="B 檔案中找不到 A_TAG 欄位")

    b_index = {}
    for row in b_rows:
        key = row.get("A_TAG", "").strip().upper()
        if key:
            b_index[key] = row

    results = []
    match_count = 0
    unmatch_count = 0
    for a_row in a_rows:
        tag = a_row.get("Tag Name", "").strip()
        join_key = tag.upper()
        b_match = b_index.get(join_key)

        out = dict(a_row)
        if b_match:
            match_count += 1
            out["_matched"] = True
            for src, tgt in IO_MAPPING_FIELDS.items():
                if src in b_match:
                    out[tgt] = b_match[src]
        else:
            unmatch_count += 1
            out["_matched"] = False

        for col in IO_OUTPUT_COLUMNS:
            if col not in out:
                out[col] = ""
        results.append(out)

    mapping_id = str(uuid.uuid4())[:8]
    _io_mapping_cache[mapping_id] = results
    # 限制快取大小：超過上限時淘汰最舊插入的項目（dict 保留插入順序）
    while len(_io_mapping_cache) > _IO_MAPPING_CACHE_MAX:
        _io_mapping_cache.pop(next(iter(_io_mapping_cache)))

    preview = []
    for r in results:
        preview.append({col: r.get(col, "") for col in IO_OUTPUT_COLUMNS + ["_matched"]})

    return {
        "mapping_id": mapping_id,
        "total": len(a_rows),
        "matched": match_count,
        "unmatched": unmatch_count,
        "columns": IO_OUTPUT_COLUMNS,
        "data": preview,
    }


@app.get("/api/io-mapping/download/{mapping_id}")
async def io_mapping_download(mapping_id: str):
    """下載 mapping 結果 CSV"""
    results = _io_mapping_cache.get(mapping_id)
    if not results:
        raise HTTPException(status_code=404, detail="Mapping 結果不存在或已過期")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=IO_OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in results:
        writer.writerow({col: row.get(col, "") for col in IO_OUTPUT_COLUMNS})

    content = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=IO_Mapping_Result_{mapping_id}.csv"},
    )
