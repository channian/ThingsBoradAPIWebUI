# -*- coding: utf-8 -*-
"""
FastAPI 應用主程式
提供 REST API 端點 + SSE 即時推送 + 靜態檔案服務
"""

import asyncio
import csv
import io
import json
import logging
import os
import queue
import uuid
import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.tb_client import ThingsBoardClient
from app.task_manager import (
    TaskManager,
    execute_batch_create,
    execute_batch_delete,
)
from app.config_manager import ConfigManager
from app.pg_client import PGClient

# ── Logging ────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

# ── FastAPI App ────────────────────────────────────────

app = FastAPI(title="Kepware ThingsBoard 點位管理系統")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# ── 路徑常數 ──────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

os.makedirs(DATA_DIR, exist_ok=True)

config_manager = ConfigManager(CONFIG_FILE)

# 載入 .env
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

pg_client = PGClient()


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
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── 資料模型 ───────────────────────────────────────────

class LoginRequest(BaseModel):
    tb_url: str
    username: str
    password: str


class ExecuteRequest(BaseModel):
    upload_id: str
    operation: str          # "create" | "delete"
    dry_run: bool = True
    tb_url: str
    tb_username: str
    tb_password: str
    delay: float = 0.2
    batch_size: int = 50
    batch_pause: float = 5
    csv_filename: str = ""


class DeviceQueryRequest(BaseModel):
    tb_url: str
    tb_token: str
    page: int = 0
    page_size: int = 20
    text_search: Optional[str] = None


class DeviceExportRequest(BaseModel):
    tb_url: str
    tb_token: str
    text_search: Optional[str] = None


class DirectDeleteRequest(BaseModel):
    tb_url: str
    tb_username: str
    tb_password: str
    device_ids: list
    dry_run: bool = True


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


class DeviceProfileCheckRequest(BaseModel):
    tb_url: str
    tb_token: str
    type_names: list


class StagingQueryRequest(BaseModel):
    page: int = 0
    page_size: int = 50
    tb_status: Optional[str] = None
    pg_status: Optional[str] = None


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


class ExecuteTbRequest(BaseModel):
    tb_url: str
    tb_username: str
    tb_password: str
    ids: Optional[list] = None


class ExecutePgRequest(BaseModel):
    ids: Optional[list] = None


# ── API: 認證 ──────────────────────────────────────────

@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    """測試 ThingsBoard 連線並取得 Token"""
    client = ThingsBoardClient(req.tb_url)
    try:
        token = client.login(req.username, req.password)
        return {"success": True, "token": token}
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


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


# ── API: 任務執行 ─────────────────────────────────────

@app.post("/api/tasks/execute")
async def execute_task(req: ExecuteRequest):
    """啟動批次新增或刪除任務"""
    rows = _csv_store_get(req.upload_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="找不到上傳的 CSV 資料，請重新上傳")

    task = task_manager.create_task(req.operation, {
        "rows": rows,
        "dry_run": req.dry_run,
        "tb_url": req.tb_url,
        "tb_username": req.tb_username,
        "tb_password": req.tb_password,
        "delay": req.delay,
        "batch_size": req.batch_size,
        "batch_pause": req.batch_pause,
    })
    task.csv_filename = req.csv_filename
    task.on_complete = _append_history

    executor = execute_batch_create if req.operation == "create" else execute_batch_delete
    task_manager.run_in_background(task, executor)

    return {"task_id": task.id}


# ── API: 任務 SSE 串流 ────────────────────────────────

@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    """Server-Sent Events 即時推送任務進度"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator():
        for entry in list(task.log_history):
            yield f"data: {json.dumps({'type': 'log', **entry}, ensure_ascii=False)}\n\n"
        if task.progress["total"] > 0:
            yield f"data: {json.dumps({'type': 'progress', **task.progress}, ensure_ascii=False)}\n\n"
        if task.done and task.summary:
            yield f"data: {json.dumps({'type': 'complete', **task.summary}, ensure_ascii=False)}\n\n"
            return

        while True:
            try:
                event = task.events.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") == "complete":
                    return
            except queue.Empty:
                if task.done:
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/tasks/{task_id}/status")
async def task_status(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.id,
        "type": task.type,
        "done": task.done,
        "progress": task.progress,
        "summary": task.summary,
    }


# ── API: 任務結果匯出 CSV ─────────────────────────────

@app.get("/api/tasks/{task_id}/export")
async def export_task_results(task_id: str):
    """匯出任務的逐筆處理結果為 CSV"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "status", "detail"])
    for r in task.results:
        writer.writerow([r["name"], r["status"], r["detail"]])

    content = output.getvalue().encode("utf-8-sig")  # BOM for Excel
    op_name = {"create": "新增", "delete": "刪除"}.get(task.type, task.type)
    filename = f"result_{op_name}_{task.id}.csv"

    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── API: 歷史紀錄 ─────────────────────────────────────

@app.get("/api/history")
async def get_history():
    return _load_history()


@app.delete("/api/history")
async def clear_history():
    _save_history([])
    return {"success": True}


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


# ── API: 裝置查詢 ─────────────────────────────────────

@app.post("/api/devices/query")
async def query_devices(req: DeviceQueryRequest):
    client = ThingsBoardClient(req.tb_url)
    client.set_token(req.tb_token)
    try:
        result = client.get_devices_page(
            page=req.page,
            page_size=req.page_size,
            text_search=req.text_search,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── API: 裝置清單匯出 CSV ─────────────────────────────

@app.post("/api/devices/export")
async def export_devices(req: DeviceExportRequest):
    """匯出 ThingsBoard 裝置清單為 CSV（最多 2000 筆）"""
    client = ThingsBoardClient(req.tb_url)
    client.set_token(req.tb_token)

    all_devices = []
    page = 0
    while True:
        result = client.get_devices_page(
            page=page, page_size=200,
            text_search=req.text_search,
        )
        devices = result.get("data", [])
        all_devices.extend(devices)
        if not result.get("hasNext", False) or len(all_devices) >= 2000:
            break
        page += 1

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "type", "label", "createdTime"])
    for d in all_devices:
        created = datetime.datetime.fromtimestamp(
            d.get("createdTime", 0) / 1000
        ).strftime("%Y-%m-%d %H:%M:%S") if d.get("createdTime") else ""
        writer.writerow([
            d.get("name", ""),
            d.get("type", ""),
            d.get("label", ""),
            created,
        ])

    content = output.getvalue().encode("utf-8-sig")
    today = datetime.datetime.now().strftime("%Y%m%d")
    filename = f"devices_export_{today}.csv"

    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── API: 直接刪除裝置 ─────────────────────────────────

@app.post("/api/devices/delete-direct")
async def delete_devices_direct(req: DirectDeleteRequest):
    task = task_manager.create_task("delete_direct", {
        "rows": [],
        "device_ids": req.device_ids,
        "dry_run": req.dry_run,
        "tb_url": req.tb_url,
        "tb_username": req.tb_username,
        "tb_password": req.tb_password,
        "delay": 0.1,
        "batch_size": 50,
        "batch_pause": 3,
    })
    task.csv_filename = "(查詢頁面直接刪除)"
    task.on_complete = _append_history

    def _exec_direct_delete(task):
        import datetime as dt
        task.started_at = dt.datetime.now().isoformat()
        params = task.params
        device_ids = params["device_ids"]
        dry_run = params.get("dry_run", True)

        client = ThingsBoardClient(params["tb_url"])
        try:
            client.login(params["tb_username"], params["tb_password"])
            task.push_log("info", "ThingsBoard 登入成功")
        except Exception as e:
            task.push_log("error", f"登入失敗: {e}")
            task.push_complete({"total": len(device_ids), "success": 0,
                                "fail": 0, "skip": 0})
            return

        total = len(device_ids)
        success = 0
        fail = 0

        for idx, did in enumerate(device_ids, 1):
            if dry_run:
                task.push_log("info", f"[預演] 模擬刪除 ID: {did}")
                success += 1
                task.add_result(did, "success", "預演")
            else:
                try:
                    resp = client.delete_device(did)
                    if resp.status_code == 200:
                        task.push_log("success", f"[成功] 已刪除 ID: {did}")
                        success += 1
                        task.add_result(did, "success", "")
                    else:
                        task.push_log("error",
                                      f"[失敗] ID: {did} (HTTP {resp.status_code})")
                        fail += 1
                        task.add_result(did, "fail", f"HTTP {resp.status_code}")
                except Exception as e:
                    task.push_log("error", f"[異常] {e}")
                    fail += 1
                    task.add_result(did, "fail", str(e))
            task.push_progress(idx, total, success, fail, 0)

        task.push_complete({"total": total, "success": success,
                            "fail": fail, "skip": 0})

    task_manager.run_in_background(task, _exec_direct_delete)
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


# ── API: DeviceProfile 驗證 ──────────────────────────

@app.post("/api/device-profiles/check")
async def check_device_profiles(req: DeviceProfileCheckRequest):
    """檢查指定的 type 名稱是否為合法的 DeviceProfile"""
    client = ThingsBoardClient(req.tb_url)
    client.set_token(req.tb_token)
    try:
        existing_profiles = client.get_all_device_profile_names()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"無法取得 DeviceProfile 列表: {e}")

    results = {}
    for name in req.type_names:
        results[name] = name in existing_profiles

    return {
        "existing_profiles": existing_profiles,
        "check_results": results,
        "total_checked": len(req.type_names),
        "valid_count": sum(1 for v in results.values() if v),
        "invalid_count": sum(1 for v in results.values() if not v),
    }


@app.post("/api/device-profiles/list")
async def list_device_profiles(req: DeviceQueryRequest):
    """列出 ThingsBoard 上所有的 DeviceProfile"""
    client = ThingsBoardClient(req.tb_url)
    client.set_token(req.tb_token)
    try:
        existing_profiles = client.get_all_device_profile_names()
        return {"profiles": existing_profiles, "total": len(existing_profiles)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"無法取得 DeviceProfile 列表: {e}")


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
async def pg_staging_import(req: StagingImportRequest):
    """將已上傳的 CSV 資料匯入 PG 暫存表"""
    rows = _csv_store_get(req.upload_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="找不到上傳的 CSV 資料，請重新上傳")
    try:
        result = pg_client.import_staging(rows)
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
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查詢暫存表失敗: {e}")


@app.post("/api/pg/staging/status")
async def pg_staging_update_status(req: StagingStatusRequest):
    """批次更新暫存表狀態"""
    try:
        count = pg_client.update_staging_status(req.ids, req.field, req.status)
        return {"success": True, "updated": count}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/pg/staging/delete")
async def pg_staging_delete(req: StagingDeleteRequest):
    """刪除暫存表資料"""
    try:
        count = pg_client.delete_staging(req.ids)
        return {"success": True, "deleted": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/pg/staging/clear")
async def pg_staging_clear():
    """清空暫存表"""
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

@app.post("/api/pg/derive/tb")
async def pg_derive_tb(req: DeriveRequest):
    """推導 TB 建點欄位"""
    try:
        staging = pg_client.get_staging_list(
            page=0, page_size=9999,
            tb_status=req.tb_status,
        )
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        results = pg_client.derive_tb_fields(rows)
        return {"data": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"推導失敗: {e}")


@app.post("/api/pg/derive/pg")
async def pg_derive_pg(req: DeriveRequest):
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

@app.post("/api/pg/execute/tb")
async def pg_execute_tb(req: ExecuteTbRequest):
    """執行 TB 建點：推導欄位 → 呼叫 TB API 建立裝置 → 更新 tb_status"""
    try:
        # 1. 取得 pending 的暫存資料並推導
        staging = pg_client.get_staging_list(page=0, page_size=9999, tb_status="pending")
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        if not rows:
            return {"success": 0, "failed": 0, "errors": [], "message": "無待建點資料"}

        derived = pg_client.derive_tb_fields(rows)

        # 2. 登入 TB 並逐筆建立裝置
        client = ThingsBoardClient(req.tb_url)
        client.login(req.tb_username, req.tb_password)

        success_ids = []
        failed = []
        for item in derived:
            # 先檢查是否已存在
            try:
                existing = client.get_device_by_name(item["tb_name"])
                if existing:
                    success_ids.append(item["id"])  # 視為已完成
                    continue
            except Exception:
                pass

            # 組裝 TB API payload（與 task_manager 格式一致）
            payload = {
                "name": item["tb_name"],
                "type": item["tb_type"],
                "label": item["tb_label"],
                "additionalInfo": {"description": item.get("tb_description", "")},
            }
            try:
                resp = client.create_device(payload)
                if resp.status_code in (200, 201):
                    success_ids.append(item["id"])
                else:
                    failed.append({
                        "name": item["tb_name"],
                        "reason": f"HTTP {resp.status_code}: {resp.text[:200]}"
                    })
            except Exception as e:
                failed.append({"name": item["tb_name"], "reason": str(e)})

        # 3. 更新成功的暫存資料狀態
        if success_ids:
            pg_client.update_staging_status(success_ids, "tb_status", "done")

        return {
            "success": len(success_ids),
            "failed": len(failed),
            "errors": failed,
            "message": f"成功建立 {len(success_ids)} 筆裝置"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TB 建點失敗: {e}")


@app.post("/api/pg/execute/pg")
async def pg_execute_pg(req: ExecutePgRequest):
    """執行 PG 寫入：推導欄位 → 寫入正式表 → 更新 pg_status"""
    try:
        # 1. 取得 pending 的暫存資料並推導
        staging = pg_client.get_staging_list(page=0, page_size=9999, pg_status="pending")
        rows = staging["data"]
        if req.ids:
            rows = [r for r in rows if r["id"] in req.ids]
        if not rows:
            return {"inserted": 0, "skipped": 0, "errors": [], "message": "無待寫入資料"}

        derived = pg_client.derive_pg_fields(rows)

        # 2. 寫入正式表
        result = pg_client.import_formal(derived)

        # 3. 更新成功的暫存資料狀態（排除有錯誤的 tagname）
        error_tagnames = {e["tagname"] for e in result.get("errors", [])}
        success_ids = [r["id"] for r, d in zip(rows, derived) if d["tagname"] not in error_tagnames]
        if success_ids:
            pg_client.update_staging_status(success_ids, "pg_status", "done")

        return {
            **result,
            "message": f"成功寫入 {result['inserted']} 筆至正式表"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PG 寫入失敗: {e}")
