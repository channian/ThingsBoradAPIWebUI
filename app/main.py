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

# ── 靜態檔案 ───────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


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


class DeviceQueryRequest(BaseModel):
    tb_url: str
    tb_token: str
    page: int = 0
    page_size: int = 20
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
        # 跳過全空行
        if any(cleaned.values()):
            cleaned_rows.append(cleaned)

    rows = cleaned_rows

    # 產生 upload_id 並暫存
    import uuid
    upload_id = uuid.uuid4().hex[:8]
    csv_store[upload_id] = rows

    # 回傳預覽 (前 10 筆)
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
        # 先推送歷史紀錄 (供斷線重連)
        for entry in list(task.log_history):
            yield f"data: {json.dumps({'type': 'log', **entry}, ensure_ascii=False)}\n\n"
        if task.progress["total"] > 0:
            yield f"data: {json.dumps({'type': 'progress', **task.progress}, ensure_ascii=False)}\n\n"
        if task.done and task.summary:
            yield f"data: {json.dumps({'type': 'complete', **task.summary}, ensure_ascii=False)}\n\n"
            return

        # 即時推送新事件
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
    """查詢任務目前狀態"""
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


# ── API: 裝置查詢 ─────────────────────────────────────

@app.post("/api/devices/query")
async def query_devices(req: DeviceQueryRequest):
    """查詢 ThingsBoard 裝置列表"""
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


# ── API: 直接刪除裝置 ─────────────────────────────────

@app.post("/api/devices/delete-direct")
async def delete_devices_direct(req: DirectDeleteRequest):
    """直接刪除指定的裝置 (用於裝置查詢頁面的勾選刪除)"""
    # 把 device_ids 轉成假的 row 格式，復用 batch delete
    rows = [{"name": "__direct__", "id": did} for did in req.device_ids]

    task = task_manager.create_task("delete_direct", {
        "rows": rows,
        "device_ids": req.device_ids,
        "dry_run": req.dry_run,
        "tb_url": req.tb_url,
        "tb_username": req.tb_username,
        "tb_password": req.tb_password,
        "delay": 0.1,
        "batch_size": 50,
        "batch_pause": 3,
    })

    def _exec_direct_delete(task):
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
            else:
                try:
                    resp = client.delete_device(did)
                    if resp.status_code == 200:
                        task.push_log("success", f"[成功] 已刪除 ID: {did}")
                        success += 1
                    else:
                        task.push_log("error",
                                      f"[失敗] ID: {did} (HTTP {resp.status_code})")
                        fail += 1
                except Exception as e:
                    task.push_log("error", f"[異常] {e}")
                    fail += 1
            task.push_progress(idx, total, success, fail, 0)

        task.push_complete({"total": total, "success": success,
                            "fail": fail, "skip": 0})

    task_manager.run_in_background(task, _exec_direct_delete)
    return {"task_id": task.id}
