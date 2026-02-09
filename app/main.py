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

# 暫存上傳的 CSV 資料 (upload_id -> rows)
csv_store: dict = {}

# ── 路徑常數 ──────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

os.makedirs(DATA_DIR, exist_ok=True)


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
    csv_store[upload_id] = rows

    preview = rows[:10]
    return {
        "upload_id": upload_id,
        "headers": headers,
        "preview": preview,
        "total_rows": len(rows),
        "filename": file.filename,
    }


# ── API: 任務執行 ─────────────────────────────────────

@app.post("/api/tasks/execute")
async def execute_task(req: ExecuteRequest):
    """啟動批次新增或刪除任務"""
    rows = csv_store.get(req.upload_id)
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
