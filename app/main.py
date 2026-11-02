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
import random
import re
import requests
import threading
import time
import uuid
import datetime
from collections import Counter
from dataclasses import dataclass
from typing import Optional

# 載入 .env：必須在任何 app.* 模組被匯入之前執行。
# app.auth 在模組頂層直接計算 SECRET_KEY = os.getenv("JWT_SECRET_KEY", ...)，
# 若 load_dotenv() 太晚呼叫（例如放在檔案後段），.env 內容根本還沒進 os.environ，
# 即使 .env 有正確設定 JWT_SECRET_KEY 也永遠讀到空值，導致每次啟動都誤判為未設定
# 並產生臨時隨機金鑰（重啟後所有 Token 失效）。
from dotenv import load_dotenv
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from app.task_manager import TaskManager, _smart_get
from app.config_manager import ConfigManager
from app.pg_client import PGClient
from app.kw_gw_client import KepwareGatewayClient
from app.gateway_router import (
    resolve_gateway, find_unmapped, localize_reason, group_by_gateway, is_routable,
)
from app.gateway_queue import GatewayQueue, QueueFullError, gateway_key, normalize_gateway_url
from app.gateway_jobs import PartSpec, run_job
from app.safety import check_batch_size, get_limits, SafetyError
from app.auth import (
    hash_password, verify_password, create_token,
    get_current_user, require_admin, require_role, VALID_ROLES,
    check_login_allowed, record_login_failure, reset_login_failures,
)

# ── Gateway 密碼加解密 (Fernet) ──────────────────────────

from cryptography.fernet import Fernet, InvalidToken
import base64, hashlib

_kw_key_warned = False


def _get_fernet() -> Fernet:
    global _kw_key_warned
    raw_key = os.getenv("KW_ENCRYPT_KEY", "").strip()
    if not raw_key:
        raw_key = "kepitsimple-default-encrypt-key"
        if not _kw_key_warned:
            # 與 JWT_SECRET_KEY 同類風險，但此金鑰用於加密「已持久化在 DB 的 Gateway 密碼」，
            # 不能像 JWT 一樣隨機產生臨時金鑰（否則每次重啟都會讓既有 Gateway 密碼全部解密失敗）。
            # 故僅記錄警告、沿用內建預設值；正式環境應設定 KW_ENCRYPT_KEY 並在設定頁重新輸入一次
            # 既有 Gateway 密碼（金鑰不符時系統已會回傳清楚的 400 提示，不會是不明的 500）。
            logging.getLogger("main").warning(
                "未設定環境變數 KW_ENCRYPT_KEY，使用程式碼內建的公開預設金鑰加密 Gateway 密碼；"
                "任何取得原始碼者皆可解密資料庫中已儲存的 Gateway 密碼。"
                "正式環境請務必設定 KW_ENCRYPT_KEY（設定後既有 Gateway 密碼需在設定頁重新輸入一次）。"
            )
            _kw_key_warned = True
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
# （BASE_DIR 已在檔案最上方載入 .env 時定義）

STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

os.makedirs(DATA_DIR, exist_ok=True)

config_manager = ConfigManager(CONFIG_FILE)


# ── 全域狀態 ───────────────────────────────────────────

task_manager = TaskManager()

# ── Gateway 作業佇列 ────────────────────────────────────
# Kepware GW 限速為每分鐘 60 次（per 帳號+路徑），建點/Scale/刪除共用同一額度桶。
# 同一 Gateway 若併發執行多個背景任務，實際速率會疊加而撞限速，所以「同一台一次只跑一個」；
# 但不再拒絕（舊版回 409），而是排隊（先進先出），不同台的 Gateway 則互相並行。
# 佇列內容在記憶體：伺服器重啟會遺失排隊與執行中的任務（與背景任務一貫行為相同）。
# 詳見 app/gateway_queue.py（佇列）與 app/gateway_jobs.py（一個請求 → 單一 task_id → 每台一個 part）。
gateway_queue = GatewayQueue()

# 歷史紀錄檔的「讀→改→寫」要整段互斥：多個 part／多個任務可能在同一瞬間結束
_history_lock = threading.Lock()


# 暫存上傳的 CSV 資料 — 檔案式儲存，伺服器重啟不會遺失
CSV_STORE_DIR = os.path.join(DATA_DIR, "csv_uploads")
os.makedirs(CSV_STORE_DIR, exist_ok=True)

# 單檔上傳大小/筆數上限，避免惡意或誤傳的超大檔案塞爆磁碟/記憶體
CSV_UPLOAD_MAX_BYTES = int((os.getenv("CSV_UPLOAD_MAX_BYTES") or "").strip() or str(10 * 1024 * 1024))
CSV_UPLOAD_MAX_ROWS = int((os.getenv("CSV_UPLOAD_MAX_ROWS") or "").strip() or "50000")
# 暫存檔保留天數，超過即視為過期並於每週清理排程中一併刪除
CSV_UPLOAD_CLEANUP_DAYS = int((os.getenv("CSV_UPLOAD_CLEANUP_DAYS") or "").strip() or "30")


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


pg_client = PGClient()


def _ensure_admin_user():
    """啟動時確保至少有一個 admin 帳號"""
    try:
        if pg_client.count_users() == 0:
            # .env 中若寫成 ADMIN_PASSWORD=（空值），os.getenv 會回傳空字串而非套用預設值，
            # 導致初始管理員以空密碼建立；以 or 補一層防呆確保套用預設
            admin_user = (os.getenv("ADMIN_USERNAME") or "").strip() or "admin"
            admin_pass = (os.getenv("ADMIN_PASSWORD") or "").strip() or "admin"
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


LOG_CLEANUP_DAYS = int((os.getenv("LOG_CLEANUP_DAYS") or "").strip() or "7")
_cleanup_stop = threading.Event()


def _cleanup_csv_uploads(days: int) -> int:
    """刪除超過指定天數的暫存 CSV 上傳檔（依檔案 mtime 判斷），避免無限累積佔用磁碟"""
    cutoff = time.time() - days * 86400
    deleted = 0
    for name in os.listdir(CSV_STORE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(CSV_STORE_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                deleted += 1
        except OSError as e:
            log.warning(f"[cleanup] 刪除過期上傳檔失敗 {name}: {e}")
    return deleted


def _weekly_cleanup():
    """每週清理過期日誌 + 過期 CSV 上傳暫存檔的背景執行緒"""
    while not _cleanup_stop.wait(timeout=3600):
        now = datetime.datetime.now()
        if now.weekday() == 0 and now.hour == 3:
            try:
                count = pg_client.cleanup_activity_logs(days=LOG_CLEANUP_DAYS)
                log.info(f"[cleanup] 每週日誌清理完成，清除 {count} 筆")
            except Exception as e:
                log.warning(f"[cleanup] 日誌清理失敗: {e}")
            try:
                count = _cleanup_csv_uploads(CSV_UPLOAD_CLEANUP_DAYS)
                log.info(f"[cleanup] 每週 CSV 上傳暫存檔清理完成，清除 {count} 筆")
            except Exception as e:
                log.warning(f"[cleanup] CSV 上傳暫存檔清理失敗: {e}")


@app.on_event("startup")
async def on_startup():
    try:
        pg_client.ensure_schema()
        log.info("[startup] PG schema 檢查完成（參照表與額外欄位）")
    except Exception as e:
        log.warning(f"[startup] PG schema 檢查失敗（PG 可能尚未連線）: {e}")
    _ensure_admin_user()
    t = threading.Thread(target=_weekly_cleanup, daemon=True)
    t.start()
    log.info("[cleanup] 每週清理排程已啟動（每週一凌晨 3 點，含日誌與 CSV 上傳暫存檔）")


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
    """任務完成後寫入歷史紀錄。

    由 push_complete 的 on_complete 呼叫，可能在多條執行緒同時進來（兩個任務同時結束），
    所以「讀→改→寫」整段用 _history_lock 互斥，否則後寫的會把先寫的蓋掉。
    started_at 只在第一個 part 真正開始執行時才有值；全部在排隊時就被取消的任務沒有，
    退而用 ended_at，歷史列表才不會出現空的開始時間。"""
    summary = task.summary or {}
    entry = {
        "task_id": task.id,
        "type": task.type,
        "dry_run": task.params.get("dry_run", True),
        "csv_filename": task.csv_filename,
        "started_at": task.started_at or task.ended_at,
        "ended_at": task.ended_at,
        "total": summary.get("total", 0),
        "success": summary.get("success", 0),
        "fail": summary.get("fail", 0),
        "skip": summary.get("skip", 0),
        "cancelled": bool(summary.get("cancelled", False)),
    }
    with _history_lock:
        records = _load_history()
        records.insert(0, entry)  # 最新在最前
        records = records[:200]   # 保留最近 200 筆
        _save_history(records)
    log.info(f"歷史紀錄已儲存: {task.type} | {task.csv_filename}")


# ── Kepware 429 限速退避 ─────────────────────────────

def _retry_after_seconds(resp, default_wait: float) -> float:
    """從 429 回應取得建議等待秒數：優先 Retry-After 標頭，其次 JSON body 的
    retry_after 欄位，都沒有才用 default_wait。上限 120 秒避免任務停滯過久。"""
    wait = None
    try:
        ra = (resp.headers.get("Retry-After") or "").strip()
        if ra:
            wait = float(ra)
    except Exception:
        wait = None
    if wait is None:
        try:
            wait = float(resp.json().get("retry_after"))
        except Exception:
            wait = None
    if wait is None:
        wait = default_wait
    return max(1.0, min(wait, 120.0))


# 可中斷睡眠的切片長度：取消要求最慢在一個切片（秒）內被察覺，
# 不必等整段 delay / batch_pause / 429 退避（最長 120 秒）睡完
_SLEEP_SLICE = 0.5


def _sleep_interruptible(view, seconds: float) -> bool:
    """睡 seconds 秒，但以 _SLEEP_SLICE 為單位切片，每片之前先看 view.cancelled。

    view 是 PartView（或任何有 cancelled 屬性的物件；沒有該屬性視為永不取消）。
    回傳 True＝睡滿且未被取消；False＝途中（或睡完時）發現已取消。
    刻意用「累減剩餘秒數」而不是比對時鐘：行為只取決於 time.sleep 被呼叫的次數與長度，
    測試把 time.sleep 換成假的也不會空轉。"""
    remaining = float(seconds)
    while remaining > 0:
        if getattr(view, "cancelled", False):
            return False
        step = min(_SLEEP_SLICE, remaining)
        time.sleep(step)
        remaining -= step
    return not getattr(view, "cancelled", False)


def _pace(view, delay: float) -> None:
    """逐筆之間的限速等待（delay 加一點隨機抖動），可被取消打斷。"""
    _sleep_interruptible(view, delay + random.uniform(0.01, 0.05))


def _kw_call_with_429_retry(task, desc: str, fn, max_retries: int = 3, default_wait: float = 5.0):
    """呼叫 Kepware API，遇 429 依伺服器建議時間等待後重試（最多 max_retries 次）。

    Kepware API Gateway 的限速是滑動視窗制（預設 60 次/分鐘），視窗要等舊請求
    過期才會釋放額度——固定等 5 秒重試幾乎必定再撞、且會造成後續連環失敗。
    429 回應帶有 Retry-After 標頭指出額度何時釋出，故以它為準。
    非 429 的錯誤原樣拋出，由呼叫端處理（409 已存在、404 不存在等）。

    task 可以是 Task 或 PartView（只用到 push_log 與可選的 cancelled）。
    退避等待中收到取消：不再重試，直接以最後一次的 429 錯誤結束
    （該筆會被呼叫端記為失敗，之後的迴圈偵測到取消就會停止）。"""
    attempt = 0
    while True:
        try:
            return fn()
        except requests.HTTPError as e:
            if (e.response is not None and e.response.status_code == 429
                    and attempt < max_retries):
                wait = _retry_after_seconds(e.response, default_wait)
                attempt += 1
                task.push_log("warning",
                              f"{desc} 收到 429 限速，暫停 {wait:.0f} 秒後重試（第 {attempt}/{max_retries} 次）")
                if not _sleep_interruptible(task, wait):
                    task.push_log("warning", f"{desc} 等待重試期間收到取消，放棄重試")
                    raise
                continue
            raise


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
    """Step 3 手動覆寫（部分更新）：只有請求中「有送」的欄位會被更新，沒送的欄位維持資料庫原值。
    下列預設值只用於讓欄位可省略，是否更新一律看 model_fields_set，不看值。"""
    id: int
    channel: str = ""
    device: str = ""
    tag_groups: str = ""
    data_type: Optional[int] = None

    @field_validator("data_type", mode="before")
    @classmethod
    def _blank_data_type_is_none(cls, v):
        # 空字串 / 純空白視同 null（清除覆寫），與 PGClient.update_kw_overrides 的語意一致
        if isinstance(v, str) and not v.strip():
            return None
        return v


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
    # 預設 1.1 秒/筆：Kepware API Gateway 限速為滑動視窗 60 次/分鐘（per 帳號+路徑），
    # 建點/Scale/刪除都打 /api/config/tags 共用同一額度；1.1 秒 ≈ 每分鐘 48 筆，留有安全餘裕。
    # 若 Gateway 端調高限速，可透過 UI 限速設定或請求參數調低此值加速。
    delay: float = 1.1
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
    # 預設 1.1 秒/筆：Kepware API Gateway 限速為滑動視窗 60 次/分鐘（per 帳號+路徑），
    # 建點/Scale/刪除都打 /api/config/tags 共用同一額度；1.1 秒 ≈ 每分鐘 48 筆，留有安全餘裕。
    # 若 Gateway 端調高限速，可透過 UI 限速設定或請求參數調低此值加速。
    delay: float = 1.1
    batch_size: int = 50
    batch_pause: float = 5.0


class KwBatchDeleteRequest(BaseModel):
    upload_id: str
    gateway_id: Optional[int] = None
    kw_gw_url: Optional[str] = None
    kw_gw_username: Optional[str] = None
    kw_gw_password: Optional[str] = None
    dry_run: bool = True
    # 預設 1.1 秒/筆：Kepware API Gateway 限速為滑動視窗 60 次/分鐘（per 帳號+路徑），
    # 建點/Scale/刪除都打 /api/config/tags 共用同一額度；1.1 秒 ≈ 每分鐘 48 筆，留有安全餘裕。
    # 若 Gateway 端調高限速，可透過 UI 限速設定或請求參數調低此值加速。
    delay: float = 1.1
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


class GatewayRouteRequest(BaseModel):
    match_type: str          # "site"（棟別）或 "zone"（園區）
    match_value: str
    gateway_id: int


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
    """使用者登入取得 JWT Token（含連續失敗鎖定，防暴力破解）"""
    # 檢查此帳號是否因連續登入失敗而被暫時鎖定
    check_login_allowed(req.username)
    user = pg_client.get_user_by_username(req.username)
    if not user:
        record_login_failure(req.username)
        _log_activity(request, {"sub": req.username}, "login_failed", "登入失敗：帳號不存在")
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    if not user.get("is_active"):
        raise HTTPException(status_code=403, detail="帳號已停用")
    if not verify_password(req.password, user["password_hash"]):
        record_login_failure(req.username)
        _log_activity(request, {"sub": req.username}, "login_failed", "登入失敗：密碼錯誤")
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    # 登入成功，清除失敗計數
    reset_login_failures(req.username)
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
async def upload_csv(file: UploadFile = File(...),
                     user: dict = Depends(require_role("admin", "operator"))):
    """上傳 CSV 檔案，解析後暫存並回傳預覽"""
    content_bytes = await file.read()

    if len(content_bytes) > CSV_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"檔案大小超過上限（{CSV_UPLOAD_MAX_BYTES // (1024*1024)}MB），請分批上傳",
        )

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

    if len(rows) > CSV_UPLOAD_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"資料筆數超過上限（{CSV_UPLOAD_MAX_ROWS} 筆），請分批上傳",
        )

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
async def get_history(user: dict = Depends(get_current_user)):
    return _load_history()


@app.delete("/api/history")
async def clear_history(user: dict = Depends(require_admin)):
    with _history_lock:
        _save_history([])
    return {"success": True}


# 網址去掉 userinfo（user:pass@）用的 scheme 判斷；規則與 gateway_queue.normalize_gateway_url 一致
_URL_WITH_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://)([^/?#]*)(.*)$", re.DOTALL)


def _display_url(url) -> str:
    """正規化 Gateway URL 並移除 userinfo（user:pass@），可安全顯示在 API 回應或畫面上。
    手動憑證模式用網址當 Gateway 名稱，網址裡若夾帶帳密不能外洩給其他登入者。"""
    text = normalize_gateway_url(url)
    m = _URL_WITH_SCHEME_RE.match(text)
    if m:
        scheme, authority, rest = m.groups()
        return f"{scheme}{authority.rpartition('@')[2]}{rest}"
    head, slash, rest = text.partition("/")
    return f"{head.rpartition('@')[2]}{slash}{rest}"


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str, user: dict = Depends(get_current_user)):
    """查詢背景任務狀態。

    舊欄位 task_id / done / progress / summary 不變；新增：
      status     task.status()：queued / running / done / cancelled
      cancelled  是否已收到取消要求
      type       任務類型（kw_execute / pg_execute_scale / kw_delete）
      username   送出者
      gateways   每台 Gateway 的即時狀態 [{gateway_id, name, state, total, current,
                 success, fail, skip, position}]；position 只有 state=queued 才有值
                 （0＝輪到它了、n＝前面還有 n 個），其餘為 None。
    刻意不回傳佇列 key、憑證或網址（手動憑證模式的 key 含網址，可能夾帶帳密）。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    gateways = []
    for g in list(task.gateways):
        state = g.get("state")
        gateways.append({
            "gateway_id": g.get("gateway_id"),
            "name": g.get("name"),
            "state": state,
            "total": g.get("total", 0),
            "current": g.get("current", 0),
            "success": g.get("success", 0),
            "fail": g.get("fail", 0),
            "skip": g.get("skip", 0),
            "position": (gateway_queue.position(g.get("key"), task.id)
                         if state == "queued" else None),
        })
    return {
        "task_id": task.id,
        "done": task.done,
        "progress": task.progress,
        "summary": task.summary,
        "status": task.status(),
        "cancelled": task.cancelled,
        "type": task.type,
        "username": (task.params or {}).get("username", ""),
        "gateways": gateways,
    }


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request,
                      user: dict = Depends(require_role("admin", "operator"))):
    """取消背景任務（排隊中或執行中皆可）。

    - 排隊中的 Gateway：立刻離開佇列（不會執行）。
    - 執行中的 Gateway：處理完「目前這一筆」後停止；已成功的列照常寫回狀態，
      尚未處理的筆數記在 summary.cancelled_remaining。
    重複取消是冪等的（仍回 200）；任務已結束回 409；不是本機制產生的任務（沒有 gateways）回 400。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="找不到任務")
    if task.done:
        raise HTTPException(status_code=409, detail="任務已結束，無法取消")
    if not task.gateways:
        raise HTTPException(status_code=400, detail="此任務不支援取消")
    if not task.cancelled:
        task.cancelled = True
        task.push_log("warning", "收到取消要求")
        _log_activity(request, user, "cancel_task", f"取消任務 {task.id}（{task.type}）")
    # 無論是否第一次都喚醒：讓排隊中的等待者立刻重新檢查 cancelled，不必等下一次輪詢
    gateway_queue.wake()
    return {"success": True, "status": task.status()}


def _queue_key_info(key: str, names: dict):
    """佇列 key → (gateway_id, 顯示名稱)。dry-run 的專屬 key 回 None（不是真的 Gateway 佇列）。"""
    if key.startswith("dry:"):
        return None
    if key.startswith("gw:"):
        try:
            gid = int(key[3:])
        except ValueError:
            return None, key
        return gid, names.get(gid) or f"Gateway {gid}"
    if key.startswith("url:"):
        return None, _display_url(key[4:])      # 手動憑證：以去除 userinfo 的網址當名稱
    return None, key


@app.get("/api/kw/queue")
async def kw_queue(user: dict = Depends(get_current_user)):
    """目前各 Gateway 的作業佇列（佇列頭 position=0＝執行中，其後為排隊中）。"""
    try:
        names = {g["id"]: g["name"] for g in pg_client.get_gateways()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查詢 Gateway 失敗: {e}")
    result = []
    for key, task_ids in gateway_queue.snapshot().items():
        info = _queue_key_info(key, names)
        if info is None:
            continue
        gid, name = info
        tasks = []
        for position, tid in enumerate(task_ids):
            task = task_manager.get_task(tid)
            entry = next((g for g in list(task.gateways) if g.get("key") == key), None) if task else None
            tasks.append({
                "task_id": tid,
                "type": task.type if task else "",
                "username": ((task.params or {}).get("username", "") if task else ""),
                "position": position,
                "total": entry.get("total", 0) if entry else 0,
            })
        result.append({"gateway_id": gid, "gateway_name": name, "tasks": tasks})
    result.sort(key=lambda r: (r["gateway_id"] is None, r["gateway_id"] or 0, r["gateway_name"]))
    return result


# ── API: CSV 範本下載 ─────────────────────────────────

@app.get("/api/templates/{template_type}")
async def download_template(template_type: str, user: dict = Depends(get_current_user)):
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


# ── API: Gateway 自動路由（棟別/園區 → Gateway 對應表）──────

def _load_route_context() -> dict:
    """載入路由判斷所需的全部資料（結構快取 channel 索引、對應表、Gateway 清單）。
    每個請求只在開頭呼叫一次，之後逐列判斷（_route_for）純記憶體運算、不再查 DB。"""
    site_map, zone_map = pg_client.get_route_maps()
    return {
        "channel_index": pg_client.get_channel_index(),
        "site_map": site_map,
        "zone_map": zone_map,
        # 系統只有 1 台 Gateway → 一律走單台旁路（行為與多 Gateway 改版前一致）
        "only_gateway_id": pg_client.get_only_gateway_id(),
        "names": {g["id"]: g["name"] for g in pg_client.get_gateways()},
    }


def _resolve_route(item: dict, ctx: dict):
    """依列（需含 channel_name / site / zone）判斷該送哪台 Gateway，回傳 RouteResult。
    derive 預覽（_route_for）與三個執行端點（_plan_gateway_parts）共用同一個判斷。"""
    return resolve_gateway(
        item.get("channel_name"), item.get("site"), item.get("zone"),
        ctx["channel_index"], ctx["site_map"], ctx["zone_map"],
        ctx["only_gateway_id"],
    )


def _route_for(item: dict, ctx: dict) -> dict:
    """依 derive 結果列（需含 channel_name / site / zone）判斷該送哪台 Gateway，
    回傳可直接放進 API 回應的 dict；reason 中的 Gateway id 已換成名稱。"""
    res = _resolve_route(item, ctx)
    return {
        "gateway_id": res.gateway_id,
        "gateway_name": ctx["names"].get(res.gateway_id),
        "status": res.status,
        "reason": localize_reason(res.reason, ctx["names"]),
    }


@app.get("/api/kw/routes")
async def kw_list_routes(user: dict = Depends(get_current_user)):
    """列出「棟別/園區 → Gateway」對應表，並列出 location_config 中尚未被涵蓋的棟別/園區"""
    try:
        routes = pg_client.list_gateway_routes()
        site_map = {r["match_value"]: r["gateway_id"]
                    for r in routes if r["match_type"] == "site"}
        zone_map = {r["match_value"]: r["gateway_id"]
                    for r in routes if r["match_type"] == "zone"}
        unmapped_sites, unmapped_zones = find_unmapped(
            pg_client.get_locations(), site_map, zone_map)
        return {
            "routes": routes,
            "unmapped_sites": unmapped_sites,
            "unmapped_zones": unmapped_zones,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查詢 Gateway 對應表失敗: {e}")


@app.post("/api/kw/routes")
async def kw_upsert_route(req: GatewayRouteRequest, request: Request,
                          user: dict = Depends(require_admin)):
    """新增或更新一筆對應（同 match_type + match_value 已存在則改指向新的 Gateway）"""
    try:
        if not pg_client.get_gateway(req.gateway_id):
            raise HTTPException(status_code=404, detail="Gateway 不存在")
        route = pg_client.upsert_gateway_route(
            req.match_type, req.match_value, req.gateway_id)
        _log_activity(request, user, "upsert_gateway_route",
                      f"設定 Gateway 對應: {route['match_type']}={route['match_value']}"
                      f" → Gateway '{route['gateway_name']}' (ID={route['gateway_id']})")
        return route
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"設定 Gateway 對應失敗: {e}")


@app.delete("/api/kw/routes/{route_id}")
async def kw_delete_route(route_id: int, request: Request,
                          user: dict = Depends(require_admin)):
    """刪除一筆對應"""
    try:
        ok = pg_client.delete_gateway_route(route_id)
        if not ok:
            raise HTTPException(status_code=404, detail="對應不存在")
        _log_activity(request, user, "delete_gateway_route", f"刪除 Gateway 對應 ID={route_id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刪除 Gateway 對應失敗: {e}")


# ── Gateway 作業提交（Step 3 建點 / Step 5 Scale / 批次刪除 三個端點共用）─────────
#
# 請求處理函式的同步部分（都在回應 task_id 之前完成）：
#   1. 安全鎖：批次點數上限（_check_batch，超過回 422）
#   2. 推導列（各端點自己的邏輯）
#   3. _plan_gateway_parts：決定模式、路由、分組、解析憑證 → _GatewayPlan（失敗回 400/404，尚未 enqueue）
#   4. _dispatch_gateway_job：建 Task → 同步 enqueue（佇列深度上限，滿了回 429）→ run_job → 組回應
# 之後每台 Gateway 在自己的背景執行緒排隊，輪到時呼叫 worker(view, part)。
#
# 重要設計：排隊中的任務使用的是「送出當下」的推導快照（part.items），輪到時不會重新推導。
# 這是刻意的——輪到時只重新檢查狀態欄位（避免重複送出），不重新計算 channel/device/tag_groups/data_type。
# 所以排隊期間若修改了 Step 3 手動覆寫（kw-override），已在排隊的任務不會套用；
# 請先取消該任務、再重新送出。

# 路由失敗的狀態 → 給人看的說明（組 400 訊息用）
_ROUTE_STATUS_LABELS = {
    "conflict": "結構快取與對應表指向不同台",
    "conflict_channel": "channel 同時存在於多台 Gateway",
    "unrouted": "找不到 channel 也沒有對應表",
}


@dataclass
class _GatewayPlan:
    """一個請求要送往哪些 Gateway 的計畫（_plan_gateway_parts 的結果）。"""
    mode: str                  # manual（手動憑證，不路由）／filter（指定 gateway_id）／auto（自動）
    parts: list                # [PartSpec, ...]，每台 Gateway 一個
    blocked: list              # [{"name", "reason"}, ...]：無法判斷所屬 Gateway、不送出的列（僅 auto）
    excluded: Optional[dict]   # 僅 filter：{"other_gateway": n, "blocked": m}，不送出也不計入 total


def _check_batch(operation: str, count: int) -> None:
    """安全鎖：單次批次點數上限。超過回 422（訊息已含補救方式）。"""
    try:
        check_batch_size(operation, count)
    except SafetyError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _part_credentials(gateway_id, kw_gw_url=None, kw_gw_username=None, kw_gw_password=None) -> dict:
    """解析某台 Gateway 的連線憑證，放進 PartSpec.payload（不放進 task.params，
    避免明文密碼留在 task 物件上）。密碼解密失敗等 HTTPException 原樣往上拋——
    呼叫時機一定在任何 enqueue 之前。"""
    url, user, password, verify = _resolve_gw_credentials(
        gateway_id, kw_gw_url, kw_gw_username, kw_gw_password)
    return {"url": url, "user": user, "password": password, "verify": verify}


def _unroutable_digest(pairs: list) -> str:
    """[(item, RouteResult), ...] → 「各狀態筆數」說明文字（組 400 訊息用）。"""
    counts = Counter(res.status for _, res in pairs)
    return "、".join(f"{_ROUTE_STATUS_LABELS.get(s, s)}（{s}）{n} 筆" for s, n in counts.items())


def _plan_gateway_parts(items: list, gateway_id, kw_gw_url, kw_gw_username, kw_gw_password) -> _GatewayPlan:
    """依請求欄位決定三種模式之一，產生 _GatewayPlan。items 每筆需含 tag_name / channel_name / site / zone。

    - 手動憑證（舊相容）：沒有 gateway_id 但有 kw_gw_url → 單一 part，不路由；
      佇列 key 用正規化後的網址（尾斜線、大小寫不同的同一台共用同一個佇列）。
    - 指定 Gateway（篩選）：有 gateway_id → 只處理路由到這台且可放行的列；其他台的、
      無法判斷的列排除在本任務之外（不送、不計入 total），數量回報在 excluded。
    - 自動：兩者都沒有 → 逐列路由並依 Gateway 分組；無法判斷的列不送出（狀態維持原樣），
      以 blocked 交給 run_job 計入摘要。
    整批都沒有可送的列 → 400（附原因）。系統只有 1 台 Gateway 時路由結果為 single，行為同舊版。
    路由資料（結構快取、對應表、Gateway 清單）每個請求只載入一次。
    """
    manual_url = (kw_gw_url or "").strip()

    if not gateway_id and manual_url:
        cred = _part_credentials(None, manual_url, kw_gw_username, kw_gw_password)
        part = PartSpec(None, _display_url(manual_url), gateway_key(None, manual_url),
                        list(items), cred)
        return _GatewayPlan("manual", [part], [], None)

    ctx = _load_route_context()
    names = ctx["names"]
    routes = [_resolve_route(it, ctx) for it in items]

    if gateway_id:
        if gateway_id not in names:
            raise HTTPException(status_code=404, detail=f"Gateway ID={gateway_id} 不存在")
        mine: list = []
        other = 0
        unroutable: list = []
        for it, res in zip(items, routes):
            if not is_routable(res):
                unroutable.append((it, res))
            elif res.gateway_id == gateway_id:
                mine.append(it)
            else:
                other += 1
        if not mine:
            detail = f"沒有屬於 Gateway「{names[gateway_id]}」的待處理資料"
            notes = []
            if other:
                notes.append(f"屬於其他 Gateway 的 {other} 筆")
            if unroutable:
                notes.append(f"無法判斷所屬 Gateway 的 {len(unroutable)} 筆")
            if notes:
                detail += "（已排除：" + "、".join(notes) + "）"
            if unroutable:
                samples = "；".join(
                    f"{it.get('tag_name', '')}：{localize_reason(res.reason, names)}"
                    for it, res in unroutable[:3])
                detail += f"。無法判斷的前 3 筆：{samples}"
            raise HTTPException(status_code=400, detail=detail)
        part = PartSpec(gateway_id, names[gateway_id], gateway_key(gateway_id, None),
                        mine, _part_credentials(gateway_id))
        return _GatewayPlan("filter", [part], [],
                            {"other_gateway": other, "blocked": len(unroutable)})

    # 自動模式
    if not names:
        raise HTTPException(
            status_code=400,
            detail="尚未設定任何 Gateway，無法自動判斷；請先到設定新增 Gateway，或提供 kw_gw_url",
        )
    groups, blocked_pairs = group_by_gateway(items, routes)
    if not groups:
        samples = "；".join(
            f"{it.get('tag_name', '')}：{localize_reason(res.reason, names)}"
            for it, res in blocked_pairs[:3])
        raise HTTPException(
            status_code=400,
            detail=(f"全部 {len(items)} 筆都無法判斷所屬 Gateway，未送出任何資料"
                    f"（{_unroutable_digest(blocked_pairs)}）。前 3 筆原因：{samples}。"
                    "請確認各 Gateway 已「同步結構」，或在 Gateway 對應表設定棟別/園區"),
        )
    parts = [PartSpec(gid, names.get(gid) or f"Gateway {gid}", gateway_key(gid, None),
                      gitems, _part_credentials(gid))
             for gid, gitems in groups.items()]
    blocked = [{"name": it.get("tag_name", ""), "reason": localize_reason(res.reason, names)}
               for it, res in blocked_pairs]
    return _GatewayPlan("auto", parts, blocked, None)


def _job_message(plan: _GatewayPlan, ahead: dict, dry_run: bool) -> str:
    """回應給使用者的一句話：排隊與否、blocked / excluded 的筆數。"""
    queued = [p for p in plan.parts if ahead.get(p.key, 0) > 0]
    started = [p for p in plan.parts if ahead.get(p.key, 0) == 0]
    if queued:
        msg = "、".join(
            f"Gateway「{p.name}」已有任務進行中，已進入佇列（前面 {ahead[p.key]} 個）" for p in queued
        ) + "；輪到時會自動開始"
        if started:
            msg += "；" + "、".join(f"Gateway「{p.name}」" for p in started) + "立即開始"
    elif plan.parts:
        names = "、".join(f"「{p.name}」" for p in plan.parts)
        msg = (f"{'預演' if dry_run else '任務'}已開始執行（Gateway{names}，共 "
               f"{sum(len(p.items) for p in plan.parts)} 筆）")
    else:
        msg = "沒有可處理的資料，任務已結束"
    if plan.blocked:
        msg += f"；另有 {len(plan.blocked)} 筆無法判斷所屬 Gateway，未送出"
    ex = plan.excluded
    if ex and (ex["other_gateway"] or ex["blocked"]):
        msg += f"；已排除屬於其他 Gateway 的 {ex['other_gateway']} 筆、無法判斷的 {ex['blocked']} 筆"
    return msg


def _dispatch_gateway_job(task_type: str, csv_filename: str, params: dict, plan: _GatewayPlan,
                          worker, dry_run: bool = False, initial_logs: Optional[list] = None) -> dict:
    """建 Task → 同步 enqueue → run_job → 組回應（三個端點共用）。順序不可變：

    1. create_task（要先有 task.id 才能排隊）。
    2. enqueue_many：必須在請求處理期間「同步」完成（回應 task_id 之前），排隊順序才等於請求到達順序，
       且「深度檢查＋加入」是單一原子操作。佇列已滿 → 把剛建的 task 移除、回 429。
    3. run_job：每台一條背景執行緒（立刻返回）。若 enqueue 成功後、run_job 啟動前出任何例外，
       必須對每個 key release 並移除 task，否則佇列頭會永遠卡著、後面的任務全部排不到。
    dry_run（批次刪除預演）不碰 Gateway，所以不該與任何人排隊：每個 part 改用專屬 key
    （dry:<task_id>:<序號>），照樣走同一條流程（仍可看到路由結果）。
    """
    task = task_manager.create_task(task_type, params)
    task.csv_filename = csv_filename
    task.on_complete = _append_history
    for level, message in (initial_logs or []):
        task.push_log(level, message)

    parts = plan.parts
    if dry_run:
        for i, p in enumerate(parts):
            p.key = f"dry:{task.id}:{i}"
    keys = [p.key for p in parts]

    ahead: dict = {}
    if keys:
        try:
            ahead = gateway_queue.enqueue_many(
                keys, task.id, max_depth=get_limits().queue_max_per_gateway)
        except QueueFullError as e:
            task_manager.tasks.pop(task.id, None)    # 沒有要執行的 task 不能殘留
            name = next((p.name for p in parts if p.key == e.key), e.key)
            raise HTTPException(
                status_code=429,
                detail=(f"Gateway「{name}」的任務佇列已滿（排隊中＋執行中共 {e.depth} 個，"
                        f"上限 {e.limit} 個）。請稍後再試，或取消排隊中的任務；"
                        "若需提高上限，請由管理者調整環境變數 KIS_LIMIT_QUEUE_PER_GATEWAY。"),
            )
        except BaseException:
            task_manager.tasks.pop(task.id, None)
            raise

    try:
        run_job(task, parts, worker, gateway_queue, blocked=plan.blocked)
    except BaseException:
        for key in keys:
            gateway_queue.release(key, task.id)
        task_manager.tasks.pop(task.id, None)
        raise

    resp = {
        "task_id": task.id,
        "queued": any(n > 0 for n in ahead.values()),
        "queue": [{"gateway_id": p.gateway_id, "gateway_name": p.name, "ahead": ahead.get(p.key, 0)}
                  for p in parts],
        "blocked": len(plan.blocked),
        "message": _job_message(plan, ahead, dry_run),
    }
    if plan.excluded is not None:
        resp["excluded"] = plan.excluded
    return resp


# ── 三個背景 worker（每台 Gateway 呼叫一次：worker(view, part)）────────────────
#
# 共同約定：
# - part.items 是送出當下的推導快照，part.payload 是憑證（url/user/password/verify）；
#   限速參數（delay / batch_size / batch_pause）與 username / ip 在 view.params。
# - 不得呼叫 push_complete：總摘要與完成通知由 gateway_jobs.run_job 統一負責，
#   worker 只 return 該台的摘要 {"total","success","fail","skip","errors",["cancelled_remaining"]}。
# - Step 3 / Step 5 在輪到時先用 get_staging_statuses 重新檢查狀態，不是 pending 的列
#   （被先前排在前面的任務處理過了）直接略過、不呼叫 Kepware。
# - 取消：每筆之間輪詢 view.cancelled；所有等待都用可中斷的睡眠；已成功的列在 finally 寫回狀態。
# - 登入失敗＝這台待處理的列全數記為 fail。

def _kw_execute_worker(view, part) -> dict:
    """Step 3 建點：預建 tag group → 逐筆建 tag → 成功的列 tb_status 改 done。"""
    params = view.params
    delay, batch_size, batch_pause = params["delay"], params["batch_size"], params["batch_pause"]
    cred = part.payload
    items = part.items
    total = len(items)

    # 輪到時重新檢查：排在前面的任務可能已經處理過同一批列（狀態已不是 pending）
    statuses = pg_client.get_staging_statuses([it["id"] for it in items], "tb_status")
    todo = []
    for it in items:
        if statuses.get(it["id"]) == "pending":
            todo.append(it)
        else:
            view.add_result(it["tag_name"], "skip", "已被先前任務處理")
    skipped = total - len(todo)
    if skipped:
        view.push_log("info", f"{skipped} 筆已被先前任務處理（tb_status 已不是 pending），略過")
    if not todo:
        view.push_progress(total, total, 0, 0, skipped)
        return {"total": total, "success": 0, "fail": 0, "skip": skipped, "errors": []}
    if view.cancelled:
        return {"total": total, "success": 0, "fail": 0, "skip": skipped, "errors": [],
                "cancelled_remaining": len(todo)}

    try:
        client = KepwareGatewayClient(cred["url"], verify=cred["verify"])
        client.login(cred["user"], cred["password"])
        view.push_log("info", "Kepware Gateway 登入成功")
    except Exception as e:
        view.push_log("error", f"Kepware 登入失敗: {e}")
        view.push_progress(total, total, 0, len(todo), skipped)
        return {"total": total, "success": 0, "fail": len(todo), "skip": skipped,
                "errors": [{"name": "*", "reason": str(e)}]}

    # 預先建立所有需要的 tag group（收集 unique paths 後逐層建立）
    # 注意：這段同樣受 Kepware 限速管制（/api/config/tag_groups 路徑），
    # 必須與建 tag 迴圈一樣節流 + 429 退避，否則 group 一多就會開場連環 429
    unique_groups = {}
    for item in todo:
        ch, dev, grp = item["channel_name"], item["device_name"], item["tag_groups"]
        if grp:
            unique_groups[(ch, dev, grp)] = True
    for (ch, dev, grp) in unique_groups:
        if view.cancelled:
            break
        try:
            _kw_call_with_429_retry(
                view, f"Tag group {ch}/{dev}/{grp}",
                lambda ch=ch, dev=dev, grp=grp: client.ensure_tag_groups(ch, dev, grp))
        except Exception as e:
            view.push_log("warning", f"Tag group 建立失敗 {ch}/{dev}/{grp}: {e}")
        _pace(view, delay)

    success_ids = []
    failed = []
    success_count = 0
    cancelled_remaining = 0
    try:
        for idx, item in enumerate(todo, 1):
            if view.cancelled:
                cancelled_remaining = len(todo) - (idx - 1)
                view.push_log("warning", f"已取消，尚有 {cancelled_remaining} 筆未處理")
                break
            tag = item["tag_name"]
            try:
                _kw_call_with_429_retry(
                    view, f"Tag {tag}",
                    lambda item=item: client.create_tag(
                        channel_name=item["channel_name"],
                        device_name=item["device_name"],
                        tag_name=item["tag_name"],
                        address=item.get("address") or None,
                        description=item.get("description") or None,
                        tag_group=item["tag_groups"] or None,
                        data_type=item.get("data_type", 8),
                    ))
                success_ids.append(item["id"])
                success_count += 1
                view.add_result(tag, "success", "")
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 409:
                    success_ids.append(item["id"])
                    success_count += 1
                    view.add_result(tag, "success", "已存在")
                else:
                    status = e.response.status_code if e.response is not None else "?"
                    text = e.response.text[:200] if e.response is not None else str(e)
                    failed.append({"name": tag, "reason": f"HTTP {status}: {text}"})
                    view.add_result(tag, "fail", f"HTTP {status}")
            except Exception as e:
                failed.append({"name": tag, "reason": str(e)})
                view.add_result(tag, "fail", str(e))

            view.push_progress(skipped + idx, total, success_count, len(failed), skipped)
            _pace(view, delay)
            if success_count > 0 and success_count % batch_size == 0:
                view.push_log("info", f"已處理 {success_count} 筆，冷卻暫停 {batch_pause} 秒")
                _sleep_interruptible(view, batch_pause)
    finally:
        # 取消或例外時，已成功建好的列仍要寫回 done，否則下次會被當成待建點重送
        if success_ids:
            pg_client.update_staging_status(success_ids, "tb_status", "done")

    try:
        pg_client.add_activity_log(
            params["username"], "execute_tb",
            f"Kepware 建點（{part.name}）成功 {len(success_ids)} 筆，失敗 {len(failed)} 筆",
            params["ip"])
    except Exception as e:
        log.warning(f"[activity_log] 寫入失敗: {e}")

    return {"total": total, "success": len(success_ids), "fail": len(failed), "skip": skipped,
            "errors": failed, "cancelled_remaining": cancelled_remaining}


def _scale_worker(view, part) -> dict:
    """Step 5 Scale：逐筆呼叫 Kepware 設定 Scale；成功→scale_status=done、未啟用→skip。"""
    params = view.params
    delay, batch_size, batch_pause = params["delay"], params["batch_size"], params["batch_pause"]
    cred = part.payload
    items = part.items
    total = len(items)

    # 輪到時重新檢查：scale_status 已不是 pending（done / skip）的列是被先前任務處理過的
    statuses = pg_client.get_staging_statuses([it["id"] for it in items], "scale_status")
    todo = []
    for it in items:
        if statuses.get(it["id"]) == "pending":
            todo.append(it)
        else:
            view.add_result(it["tag_name"], "skip", "已被先前任務處理")
    prior_skipped = total - len(todo)
    if prior_skipped:
        view.push_log("info", f"{prior_skipped} 筆已被先前任務處理（scale_status 已不是 pending），略過")
    if not todo:
        view.push_progress(total, total, 0, 0, prior_skipped)
        return {"total": total, "success": 0, "fail": 0, "skip": prior_skipped, "errors": []}
    if view.cancelled:
        return {"total": total, "success": 0, "fail": 0, "skip": prior_skipped, "errors": [],
                "cancelled_remaining": len(todo)}

    try:
        client = KepwareGatewayClient(cred["url"], verify=cred["verify"])
        client.login(cred["user"], cred["password"])
        view.push_log("info", "Kepware Gateway 登入成功")
    except Exception as e:
        view.push_log("error", f"Kepware 登入失敗: {e}")
        view.push_progress(total, total, 0, len(todo), prior_skipped)
        return {"total": total, "success": 0, "fail": len(todo), "skip": prior_skipped,
                "errors": [{"tag_name": "*", "reason": str(e)}]}

    success_ids = []
    failed = []
    skip_ids = []
    success_count = 0
    skipped_count = 0            # 「未啟用 Scale」的略過；與 prior_skipped（已被先前任務處理）分開計
    cancelled_remaining = 0
    try:
        for idx, item in enumerate(todo, 1):
            if view.cancelled:
                cancelled_remaining = len(todo) - (idx - 1)
                view.push_log("warning", f"已取消，尚有 {cancelled_remaining} 筆未處理")
                break
            tag_name = item["tag_name"]

            # 資料不全（scale_enabled=true 但缺範圍值）：不呼叫 Kepware，直接標記失敗，
            # 且不可進 success_ids，讓該筆 scale_status 維持 pending 供修正後重跑
            scale_error = item.get("scale_error")
            if scale_error:
                view.push_log("error", f"{tag_name} 資料不全: {scale_error}")
                failed.append({"tag_name": tag_name, "reason": scale_error})
                view.add_result(tag_name, "fail", scale_error)
                view.push_progress(prior_skipped + idx, total, success_count, len(failed),
                                   prior_skipped + skipped_count)
                continue

            # scale_enabled=false 的點本來就不需要 Scale，不必呼叫 Kepware 浪費限速額度，
            # 直接標記為 skip（暫存表 scale_status 支援此值）
            if item.get("scaling_type") == 0 and not item.get("scale_error"):
                skip_ids.append(item["id"])
                skipped_count += 1
                view.add_result(tag_name, "skip", "未啟用 Scale")
                view.push_progress(prior_skipped + idx, total, success_count, len(failed),
                                   prior_skipped + skipped_count)
                continue

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
                _kw_call_with_429_retry(
                    view, f"Scale {tag_name}",
                    lambda config=config: client.set_tag_scaling(config))
                success_ids.append(item["id"])
                success_count += 1
                view.add_result(tag_name, "success", "")
            except Exception as e:
                view.push_log("error", f"{tag_name} 失敗: {e}")
                failed.append({"tag_name": tag_name, "reason": str(e)})
                view.add_result(tag_name, "fail", str(e))

            view.push_progress(prior_skipped + idx, total, success_count, len(failed),
                               prior_skipped + skipped_count)
            _pace(view, delay)
            if success_count > 0 and success_count % batch_size == 0:
                view.push_log("info", f"已處理 {success_count} 筆，冷卻暫停 {batch_pause} 秒")
                _sleep_interruptible(view, batch_pause)
    finally:
        # 取消或例外時，已處理完的列仍要寫回狀態（done / skip）
        if success_ids:
            pg_client.update_staging_status(success_ids, "scale_status", "done")
        if skip_ids:
            pg_client.update_staging_status(skip_ids, "scale_status", "skip")

    try:
        pg_client.add_activity_log(
            params["username"], "execute_scale",
            f"Scale 設定（{part.name}）成功 {len(success_ids)} 筆，失敗 {len(failed)} 筆",
            params["ip"])
    except Exception as e:
        log.warning(f"[activity_log] 寫入失敗: {e}")

    return {"total": total, "success": len(success_ids), "fail": len(failed),
            "skip": prior_skipped + skipped_count, "errors": failed,
            "cancelled_remaining": cancelled_remaining}


def _kw_delete_worker(view, part) -> dict:
    """批次刪除：逐筆刪 Tag（404 視為成功）。dry_run 只模擬、不連 Kepware。
    刪除沒有暫存列，所以不做「輪到時重新檢查」（重複刪除本身是冪等的：404 算成功）。"""
    params = view.params
    dry_run = params.get("dry_run", True)
    delay, batch_size, batch_pause = params["delay"], params["batch_size"], params["batch_pause"]
    cred = part.payload
    items = part.items
    total = len(items)

    if not dry_run:
        try:
            client = KepwareGatewayClient(cred["url"], verify=cred.get("verify", True))
            client.login(cred["user"], cred["password"])
            view.push_log("info", "Kepware Gateway 登入成功")
        except Exception as e:
            view.push_log("error", f"Kepware 登入失敗: {e}")
            view.push_progress(total, total, 0, total, 0)
            return {"total": total, "success": 0, "fail": total, "skip": 0,
                    "errors": [{"name": "*", "reason": str(e)}]}

    success = fail = 0
    failed_items = []
    cancelled_remaining = 0
    for idx, item in enumerate(items, 1):
        if view.cancelled:
            cancelled_remaining = total - (idx - 1)
            view.push_log("warning", f"已取消，尚有 {cancelled_remaining} 筆未處理")
            break
        tag = item["tag_name"]
        ch = item["channel_name"]
        dev = item["device_name"]
        grp = item["tag_groups"] or None
        if dry_run:
            view.push_log("info", f"[預演] 模擬刪除 Tag: {ch}/{dev}/{grp or ''}/{tag}")
            success += 1
            view.add_result(tag, "success", "預演")
        else:
            try:
                _kw_call_with_429_retry(
                    view, f"刪除 {tag}",
                    lambda ch=ch, dev=dev, tag=tag, grp=grp: client.delete_tag(ch, dev, tag, tag_group=grp))
                view.push_log("success", f"[成功] 已刪除 Tag: {tag}")
                success += 1
                view.add_result(tag, "success", "")
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                if status == 404:
                    view.push_log("warning", f"[略過] Tag 不存在: {tag}")
                    success += 1
                    view.add_result(tag, "success", "不存在")
                else:
                    view.push_log("error", f"[失敗] Tag: {tag} (HTTP {status})")
                    fail += 1
                    failed_items.append({"name": tag, "reason": f"HTTP {status}"})
                    view.add_result(tag, "fail", f"HTTP {status}")
            except Exception as e:
                view.push_log("error", f"[異常] {tag}: {e}")
                fail += 1
                failed_items.append({"name": tag, "reason": str(e)})
                view.add_result(tag, "fail", str(e))

        view.push_progress(idx, total, success, fail, 0)
        _pace(view, delay)
        if success > 0 and success % batch_size == 0:
            _sleep_interruptible(view, batch_pause)

    return {"total": total, "success": success, "fail": fail, "skip": 0,
            "errors": failed_items, "cancelled_remaining": cancelled_remaining}


def _derive_delete_items(csv_rows: list) -> list:
    """CSV 的 name / type → 刪除用的列（type 以 - 拆出 channel / device / tag_groups）。
    刪除沒有暫存列，也沒有棟別/園區，路由只靠 channel（site / zone 皆空）。"""
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
            "site": "",
            "zone": "",
        })
    return derived


@app.post("/api/kw/delete-batch")
async def kw_delete_batch(req: KwBatchDeleteRequest, request: Request,
                          user: dict = Depends(require_role("admin", "operator"))):
    """透過 Kepware API 批次刪除 Tag（從 CSV 推導路徑；背景任務）。

    Gateway 自動判斷（依 type 的 channel）；同一台忙碌時排隊而非拒絕；可用
    POST /api/tasks/{task_id}/cancel 取消。dry_run=true 只模擬、不碰 Kepware，不排隊、
    不受批次點數上限限制。回應與輪詢方式見 API_SCHEMA.md。"""
    rows = _csv_store_get(req.upload_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="找不到上傳的 CSV 資料，請重新上傳")

    derived = _derive_delete_items(rows)
    if not req.dry_run:
        _check_batch("delete", len(derived))     # 預演不碰 Kepware，不受批次上限限制

    username = user.get("sub", "") if isinstance(user, dict) else str(user)
    params = {
        "dry_run": req.dry_run,
        "delay": req.delay, "batch_size": req.batch_size, "batch_pause": req.batch_pause,
        "username": username,
    }
    initial_logs = []
    try:
        if derived:
            plan = _plan_gateway_parts(derived, req.gateway_id, req.kw_gw_url,
                                       req.kw_gw_username, req.kw_gw_password)
        else:
            # 沒有可處理的列：維持舊行為（任務立刻以 total=0 結束），不開任何 part
            plan = _GatewayPlan("auto", [], [], None)
            initial_logs.append(("warning", "無可處理的 Tag 資料"))
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[deleteBatch] 準備階段失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"批次刪除準備失敗: {e}")

    return _dispatch_gateway_job("kw_delete", "(Kepware 批次刪除)", params, plan,
                                 _kw_delete_worker, dry_run=req.dry_run,
                                 initial_logs=initial_logs)


# ── API: 設定管理 ─────────────────────────────────────

@app.get("/api/config")
async def get_config(user: dict = Depends(require_admin)):
    """取得完整設定"""
    return config_manager.get_all()


@app.get("/api/config/dropdown")
async def get_dropdown_options(user: dict = Depends(require_admin)):
    """取得所有下拉選項"""
    return config_manager.get_dropdown_options()


@app.put("/api/config/dropdown")
async def update_dropdown_field(req: DropdownUpdateRequest,
                                user: dict = Depends(require_admin)):
    """更新指定欄位的下拉選項列表"""
    config_manager.set_dropdown_field(req.field, req.values)
    return {"success": True, "field": req.field, "values": req.values}


@app.post("/api/config/dropdown/add")
async def add_dropdown_value(req: DropdownValueRequest,
                             user: dict = Depends(require_admin)):
    """新增一個下拉選項值"""
    added = config_manager.add_dropdown_value(req.field, req.value)
    if not added:
        raise HTTPException(status_code=409, detail=f"值 '{req.value}' 已存在於 '{req.field}'")
    return {"success": True}


@app.post("/api/config/dropdown/remove")
async def remove_dropdown_value(req: DropdownValueRequest,
                                user: dict = Depends(require_admin)):
    """移除一個下拉選項值"""
    removed = config_manager.remove_dropdown_value(req.field, req.value)
    if not removed:
        raise HTTPException(status_code=404, detail=f"值 '{req.value}' 不存在於 '{req.field}'")
    return {"success": True}


@app.get("/api/config/defaults")
async def get_defaults(user: dict = Depends(require_admin)):
    """取得預設值"""
    return config_manager.get_defaults()


@app.put("/api/config/defaults")
async def update_defaults(req: DefaultsUpdateRequest,
                          user: dict = Depends(require_admin)):
    """更新預設值"""
    config_manager.set_defaults(req.defaults)
    return {"success": True}


@app.get("/api/config/mapping")
async def get_mapping_rules(user: dict = Depends(require_admin)):
    """取得所有映射規則"""
    return config_manager.get_mapping_rules()


@app.put("/api/config/mapping")
async def update_mapping_rule(req: MappingRuleUpdateRequest,
                              user: dict = Depends(require_admin)):
    """更新指定映射規則"""
    config_manager.set_mapping_rule(req.rule_name, req.mapping)
    return {"success": True, "rule_name": req.rule_name}


@app.delete("/api/config/mapping/{rule_name}")
async def delete_mapping_rule(rule_name: str, user: dict = Depends(require_admin)):
    """刪除映射規則"""
    deleted = config_manager.delete_mapping_rule(rule_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"規則 '{rule_name}' 不存在")
    return {"success": True}


# ── API: PostgreSQL 連線 ─────────────────────────────

@app.get("/api/pg/test")
async def pg_test_connection(user: dict = Depends(get_current_user)):
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
async def pg_staging_query(req: StagingQueryRequest,
                           user: dict = Depends(get_current_user)):
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
    """儲存 Step 3 手動覆寫的 channel / device / tag_groups / data_type（部分更新）。
    只更新請求中「有送」的欄位，沒送的欄位維持資料庫原值；有送的欄位語意不變
    （channel/device 空字串 → NULL；tag_groups 空字串 → 空字串＝根層級覆寫；data_type null/空 → NULL）。"""
    try:
        fields = {k: getattr(req, k)
                  for k in ("channel", "device", "tag_groups", "data_type")
                  if k in req.model_fields_set}
        if not fields:
            raise HTTPException(status_code=400, detail="至少需提供一個要更新的欄位")
        ok = pg_client.patch_kw_overrides(req.id, fields)
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
async def pg_staging_update_status(req: StagingStatusRequest,
                                   user: dict = Depends(require_role("admin", "operator"))):
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
async def pg_get_locations(user: dict = Depends(get_current_user)):
    try:
        return pg_client.get_locations()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/locations")
async def pg_add_location(req: RefLocationRequest,
                          user: dict = Depends(require_role("admin", "operator"))):
    try:
        row_id = pg_client.upsert_location(req.bu, req.site, req.zone)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pg/ref/ownerships")
async def pg_get_ownerships(user: dict = Depends(get_current_user)):
    try:
        return pg_client.get_ownerships()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/ownerships")
async def pg_add_ownership(req: RefOwnershipRequest,
                           user: dict = Depends(require_role("admin", "operator"))):
    try:
        row_id = pg_client.upsert_ownership(req.department, req.data_owner)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pg/ref/devices")
async def pg_get_devices(user: dict = Depends(get_current_user)):
    try:
        return pg_client.get_devices()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/devices")
async def pg_add_device(req: RefDeviceRequest,
                        user: dict = Depends(require_role("admin", "operator"))):
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
async def pg_get_systems(user: dict = Depends(get_current_user)):
    try:
        return pg_client.get_systems()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/systems")
async def pg_add_system(req: RefSystemRequest,
                        user: dict = Depends(require_role("admin", "operator"))):
    try:
        row_id = pg_client.upsert_system(req.system_code, req.system_name, req.description)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/pg/ref/tb-profiles")
async def pg_get_tb_profiles(user: dict = Depends(get_current_user)):
    try:
        return pg_client.get_tb_profiles()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/tb-profiles")
async def pg_add_tb_profile(req: RefTbProfileRequest,
                            user: dict = Depends(require_role("admin", "operator"))):
    try:
        row_id = pg_client.upsert_tb_profile(req.name, req.description)
        return {"success": True, "id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pg/ref/delete")
async def pg_delete_ref(req: RefDeleteRequest,
                        user: dict = Depends(require_role("admin", "operator"))):
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
            page=0, page_size=None,
            tb_status=req.tb_status,
        )
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        results = pg_client.derive_kw_fields(rows)

        # 自動路由預覽：不論請求是否帶 gateway_id 都回傳 route（資料於此處一次取齊）
        route_ctx = _load_route_context()
        for item in results:
            item["route"] = _route_for(item, route_ctx)

        # validation 仍針對請求指定的 gateway_id，與 route 無關
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
            page=0, page_size=None,
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
    """執行 Kepware 建點（背景任務）：推導欄位 → 建立 tag group + tag → 更新 tb_status（含限速）

    回傳 task_id，前端輪詢 GET /api/tasks/{task_id} 取得進度與最終結果
    （與 /api/kw/delete-batch 相同的背景任務模式）。批次建點動輒數分鐘，
    先前以 async def 內直接 time.sleep 執行會阻塞 event loop 導致全站卡死；
    現在推導/驗證仍同步完成（快、可立即回饋錯誤），實際呼叫 Kepware API 的
    慢速迴圈交給背景執行緒，並透過 task.progress 讓前端可顯示進度。

    Gateway 判斷（三擇一，見 _plan_gateway_parts）：手動憑證（kw_gw_url）／指定 gateway_id
    （只處理屬於它的列）／兩者皆無＝自動依 channel 路由。同一台忙碌時不再回 409，
    而是排隊（回應 queued=true）；佇列深度或批次點數超過安全鎖上限回 429 / 422。
    排隊中的任務使用「送出當下」的推導快照，排隊期間若修改了手動覆寫，請取消後重送。"""
    try:
        staging = pg_client.get_staging_list(page=0, page_size=None, tb_status="pending")
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        _check_batch("kw_execute", len(rows))
        if not rows:
            raise HTTPException(status_code=400, detail="無待建點資料")

        derived = pg_client.derive_kw_fields(rows)
        if not derived:
            raise HTTPException(status_code=400, detail="無待建點資料")
        plan = _plan_gateway_parts(derived, req.gateway_id, req.kw_gw_url,
                                   req.kw_gw_username, req.kw_gw_password)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[executeKW] 準備階段失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Kepware 建點準備失敗: {e}")

    log.info(f"[executeKW] 共 {len(derived)} 筆（模式={plan.mode}，Gateway {len(plan.parts)} 台，"
             f"無法判斷 {len(plan.blocked)} 筆），delay={req.delay}, batch_size={req.batch_size}, "
             f"batch_pause={req.batch_pause}")

    ip = request.client.host if request.client else ""
    username = user.get("sub", "") if isinstance(user, dict) else str(user)
    params = {
        "delay": req.delay, "batch_size": req.batch_size, "batch_pause": req.batch_pause,
        "username": username, "ip": ip,
    }
    return _dispatch_gateway_job("kw_execute", "(Kepware 建點)", params, plan, _kw_execute_worker)


@app.post("/api/pg/execute/pg")
async def pg_execute_pg(req: ExecutePgRequest, request: Request,
                        user: dict = Depends(require_role("admin", "operator"))):
    """執行 PG 寫入：推導欄位 → 寫入正式表 + Collector tags 表 → 更新 pg_status"""
    try:
        # 1. 取得 pending 的暫存資料並推導
        staging = pg_client.get_staging_list(page=0, page_size=None, pg_status="pending")
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
        # 注意：derive_pg_fields 會跳過 tag_name 空白的列，此時 rows 與 derived 長度不一致，
        # 用 zip(rows, derived) 會造成錯位配對，故改用 derived 每筆自帶的 id
        success_ids = [d["id"] for d in derived if d["tagname"] not in error_tagnames]
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
            if collector_result["errors"]:
                resp["message"] += (
                    f"（Collector 失敗 {len(collector_result['errors'])} 筆，"
                    "該批 pg_status 維持 pending，修正後可重新執行）"
                )
        return resp
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[executePG] PG 寫入失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PG 寫入失敗: {e}")


# ── API: Kepware Gateway Scale ────────────────────────

@app.post("/api/kw-gw/test")
async def kw_gw_test(req: KwGwSettingsRequest,
                     user: dict = Depends(require_role("admin", "operator"))):
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
            page=0, page_size=None,
            scale_status=req.scale_status,
        )
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        results = pg_client.derive_scale_fields(rows)
        route_ctx = _load_route_context()
        for item in results:
            item["route"] = _route_for(item, route_ctx)
        # 尚未在 Kepware 建點（tb_status != done）的筆數：Scale 是 PUT（更新語意），
        # 該 tag 若在 Kepware 不存在必然失敗；此處只提示、不阻擋
        # （合法用法：Kepware 上已手動建好點位，只想補設定）。
        not_built = sum(1 for r in rows if r.get("tb_status") != "done")
        return {"data": results, "total": len(results), "not_built_count": not_built}
    except Exception as e:
        log.error(f"[deriveScale] 推導失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"推導失敗: {e}")


@app.post("/api/pg/execute/scale")
async def pg_execute_scale(req: ExecuteScaleRequest, request: Request,
                           user: dict = Depends(require_role("admin", "operator"))):
    """執行 Scale 設定（背景任務）：推導欄位 → 呼叫 Kepware GW API → 更新 scale_status

    回傳 task_id，前端輪詢 GET /api/tasks/{task_id} 取得進度與最終結果
    （與 /api/kw/execute、/api/kw/delete-batch 相同的背景任務模式，
    理由同上：批次呼叫 Kepware API 耗時，不能在 event loop 內直接 sleep）。
    Gateway 判斷、排隊、安全鎖、取消與快照語意同 /api/kw/execute。"""
    try:
        staging = pg_client.get_staging_list(page=0, page_size=None, scale_status="pending")
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        _check_batch("scale", len(rows))
        if not rows:
            raise HTTPException(status_code=400, detail="無待設定 Scale 的資料")

        derived = pg_client.derive_scale_fields(rows)
        if not derived:
            raise HTTPException(status_code=400,
                                detail="無啟用 Scale 的資料（scale_enabled=false 或缺少範圍值）")

        plan = _plan_gateway_parts(derived, req.gateway_id, req.kw_gw_url,
                                   req.kw_gw_username, req.kw_gw_password)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[executeScale] 準備階段失敗: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scale 設定準備失敗: {e}")

    log.info(f"[executeScale] 共 {len(derived)} 筆（模式={plan.mode}，Gateway {len(plan.parts)} 台，"
             f"無法判斷 {len(plan.blocked)} 筆），delay={req.delay}, batch_size={req.batch_size}")

    ip = request.client.host if request.client else ""
    username = user.get("sub", "") if isinstance(user, dict) else str(user)
    params = {
        "delay": req.delay, "batch_size": req.batch_size, "batch_pause": req.batch_pause,
        "username": username, "ip": ip,
    }
    return _dispatch_gateway_job("pg_execute_scale", "(Kepware Scale 設定)", params, plan, _scale_worker)


# ── API: Collector DB + Reload ────────────────────────

@app.get("/api/collector/test")
async def collector_test_connection(user: dict = Depends(get_current_user)):
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
    user: dict = Depends(require_role("admin", "operator")),
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
async def io_mapping_download(mapping_id: str, user: dict = Depends(get_current_user)):
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
