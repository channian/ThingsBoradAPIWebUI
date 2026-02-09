# -*- coding: utf-8 -*-
"""
背景任務管理器
- 將批次新增/刪除包裝成背景任務
- 透過 Queue 推送即時事件給 SSE 端點
- 記錄每筆處理結果供匯出
"""

import queue
import threading
import uuid
import time
import random
import datetime
import json
import logging
from typing import Dict, Optional, List

from app.tb_client import ThingsBoardClient

log = logging.getLogger("task_manager")


# ── 任務模型 ───────────────────────────────────────────

class Task:
    def __init__(self, task_id: str, task_type: str, params: dict):
        self.id = task_id
        self.type = task_type          # "create" | "delete"
        self.params = params
        self.events: queue.Queue = queue.Queue()
        self.done = False
        self.cancelled = False
        self.log_history: list = []    # 供斷線重連用
        self.progress = {"current": 0, "total": 0,
                         "success": 0, "fail": 0, "skip": 0}
        self.summary: Optional[dict] = None
        # 新增：結果追蹤
        self.results: list = []        # 每筆處理結果
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None
        self.csv_filename: str = ""
        self.on_complete = None        # 完成回呼

    def push_log(self, level: str, message: str):
        entry = {"level": level, "message": message}
        self.log_history.append(entry)
        self.events.put({"type": "log", **entry})

    def push_progress(self, current, total, success, fail, skip):
        self.progress = {
            "current": current, "total": total,
            "success": success, "fail": fail, "skip": skip,
        }
        self.events.put({"type": "progress", **self.progress})

    def push_complete(self, summary: dict):
        self.summary = summary
        self.ended_at = datetime.datetime.now().isoformat()
        self.events.put({"type": "complete", **summary})
        self.done = True
        if self.on_complete:
            try:
                self.on_complete(self)
            except Exception as e:
                log.error(f"on_complete callback error: {e}")

    def add_result(self, name: str, status: str, detail: str = ""):
        """記錄單筆處理結果 (status: success/skip/fail)"""
        self.results.append({
            "name": name, "status": status, "detail": detail
        })


# ── 任務管理器 ─────────────────────────────────────────

class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}

    def create_task(self, task_type: str, params: dict) -> Task:
        task_id = uuid.uuid4().hex[:8]
        task = Task(task_id, task_type, params)
        self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def run_in_background(self, task: Task, func):
        thread = threading.Thread(target=func, args=(task,), daemon=True)
        thread.start()


# ── 批次新增執行器 ─────────────────────────────────────

def execute_batch_create(task: Task):
    """背景執行批次新增"""
    task.started_at = datetime.datetime.now().isoformat()
    params = task.params
    rows: List[dict] = params["rows"]
    dry_run: bool = params.get("dry_run", True)
    delay: float = params.get("delay", 0.2)
    batch_size: int = params.get("batch_size", 50)
    batch_pause: float = params.get("batch_pause", 5)

    client = ThingsBoardClient(params["tb_url"])
    try:
        client.login(params["tb_username"], params["tb_password"])
        task.push_log("info", "ThingsBoard 登入成功")
    except Exception as e:
        task.push_log("error", f"ThingsBoard 登入失敗: {e}")
        task.push_complete({"total": len(rows), "success": 0,
                            "fail": 0, "skip": 0, "error": str(e)})
        return

    mode_text = "[DRY RUN 預演]" if dry_run else "[LIVE 真實執行]"
    task.push_log("info", f"模式: {mode_text} | 共 {len(rows)} 筆待處理")

    total = len(rows)
    success = 0
    fail = 0
    skip = 0

    for idx, row in enumerate(rows, 1):
        if task.cancelled:
            task.push_log("warning", "任務已被使用者取消")
            break

        d_name = _smart_get(row, "name")
        d_type = _smart_get(row, "type") or "default"
        d_label = _smart_get(row, "label")
        d_desc = _smart_get(row, "description")

        if not d_name:
            skip += 1
            task.add_result("(空白)", "skip", "名稱為空")
            task.push_progress(idx, total, success, fail, skip)
            continue

        # 檢查是否已存在
        try:
            existing = client.get_device_by_name(d_name)
            if existing:
                task.push_log("warning", f"[略過] 裝置已存在: {d_name}")
                skip += 1
                task.add_result(d_name, "skip", "裝置已存在")
                task.push_progress(idx, total, success, fail, skip)
                _throttle(delay, idx, success, batch_size, batch_pause, task)
                continue
        except Exception as e:
            task.push_log("error", f"[異常] 查詢 {d_name} 失敗: {e}")
            fail += 1
            task.add_result(d_name, "fail", f"查詢異常: {e}")
            task.push_progress(idx, total, success, fail, skip)
            continue

        # 執行新增
        if dry_run:
            task.push_log("info", f"[預演] 模擬新增: {d_name} (Profile: {d_type})")
            success += 1
            task.add_result(d_name, "success", f"預演 | Profile: {d_type}")
        else:
            try:
                payload = {
                    "name": d_name,
                    "type": d_type,
                    "label": d_label,
                    "additionalInfo": {"description": d_desc},
                }
                resp = client.create_device(payload)
                if resp.status_code == 200:
                    task.push_log("success", f"[成功] 已建立: {d_name} | Profile: {d_type}")
                    success += 1
                    task.add_result(d_name, "success", f"Profile: {d_type}")
                else:
                    msg = f"HTTP {resp.status_code}: {resp.text}"
                    task.push_log("error", f"[失敗] 新增 {d_name} ({msg})")
                    fail += 1
                    task.add_result(d_name, "fail", msg)
            except Exception as e:
                task.push_log("error", f"[異常] 連線錯誤: {e}")
                fail += 1
                task.add_result(d_name, "fail", f"連線異常: {e}")

        task.push_progress(idx, total, success, fail, skip)
        _throttle(delay, idx, success, batch_size, batch_pause, task)

    summary = {"total": total, "success": success, "fail": fail, "skip": skip}
    task.push_log("info", "==========================================")
    task.push_log("info", f"新增作業結束 | 成功: {success} | 略過: {skip} | 失敗: {fail}")
    task.push_log("info", "==========================================")
    task.push_complete(summary)


# ── 批次刪除執行器 ─────────────────────────────────────

def execute_batch_delete(task: Task):
    """背景執行批次刪除"""
    task.started_at = datetime.datetime.now().isoformat()
    params = task.params
    rows: List[dict] = params["rows"]
    dry_run: bool = params.get("dry_run", True)
    delay: float = params.get("delay", 0.2)
    batch_size: int = params.get("batch_size", 50)
    batch_pause: float = params.get("batch_pause", 5)

    client = ThingsBoardClient(params["tb_url"])
    try:
        client.login(params["tb_username"], params["tb_password"])
        task.push_log("info", "ThingsBoard 登入成功")
    except Exception as e:
        task.push_log("error", f"ThingsBoard 登入失敗: {e}")
        task.push_complete({"total": len(rows), "success": 0,
                            "fail": 0, "skip": 0, "error": str(e)})
        return

    mode_text = "[DRY RUN 預演]" if dry_run else "[LIVE 真實執行]"
    task.push_log("info", f"模式: {mode_text} | 共 {len(rows)} 筆待處理")

    total = len(rows)
    success = 0
    fail = 0
    skip = 0

    for idx, row in enumerate(rows, 1):
        if task.cancelled:
            task.push_log("warning", "任務已被使用者取消")
            break

        target_name = _smart_get(row, "name")
        target_type = _smart_get(row, "type")

        if not target_name:
            skip += 1
            task.add_result("(空白)", "skip", "名稱為空")
            task.push_progress(idx, total, success, fail, skip)
            continue

        # 查詢裝置是否存在
        try:
            device_info = client.get_device_by_name(target_name)
        except Exception as e:
            task.push_log("error", f"[異常] 查詢 {target_name} 失敗: {e}")
            fail += 1
            task.add_result(target_name, "fail", f"查詢異常: {e}")
            task.push_progress(idx, total, success, fail, skip)
            continue

        if not device_info:
            task.push_log("warning", f"[略過] 系統無此裝置: {target_name}")
            skip += 1
            task.add_result(target_name, "skip", "系統無此裝置")
            task.push_progress(idx, total, success, fail, skip)
            _throttle(delay, idx, success, batch_size, batch_pause, task)
            continue

        real_id = device_info["id"]["id"]
        real_type = device_info.get("type", "")

        # Type 安全驗證
        if target_type and target_type != real_type:
            task.push_log("warning",
                          f"[阻擋] 型別不符 ({target_name}): "
                          f"CSV[{target_type}] vs 系統[{real_type}]，保留不刪")
            skip += 1
            task.add_result(target_name, "skip",
                            f"型別不符: CSV[{target_type}] vs 系統[{real_type}]")
            task.push_progress(idx, total, success, fail, skip)
            _throttle(delay, idx, success, batch_size, batch_pause, task)
            continue

        # 執行刪除
        if dry_run:
            task.push_log("info", f"[預演] 模擬刪除: {target_name} (Type: {real_type})")
            success += 1
            task.add_result(target_name, "success", f"預演 | Type: {real_type}")
        else:
            try:
                resp = client.delete_device(real_id)
                if resp.status_code == 200:
                    task.push_log("success", f"[成功] 已刪除: {target_name}")
                    success += 1
                    task.add_result(target_name, "success", f"Type: {real_type}")
                elif resp.status_code == 429:
                    task.push_log("error",
                                  f"[流量限制] 刪除 {target_name} 失敗 (429)，系統冷卻中...")
                    fail += 1
                    task.add_result(target_name, "fail", "429 流量限制")
                    time.sleep(5)
                else:
                    msg = f"HTTP {resp.status_code}: {resp.text}"
                    task.push_log("error", f"[失敗] 刪除 {target_name} ({msg})")
                    fail += 1
                    task.add_result(target_name, "fail", msg)
            except Exception as e:
                task.push_log("error", f"[異常] 連線錯誤: {e}")
                fail += 1
                task.add_result(target_name, "fail", f"連線異常: {e}")

        task.push_progress(idx, total, success, fail, skip)
        _throttle(delay, idx, success, batch_size, batch_pause, task)

    summary = {"total": total, "success": success, "fail": fail, "skip": skip}
    task.push_log("info", "==========================================")
    task.push_log("info", f"刪除作業結束 | 成功: {success} | 略過: {skip} | 失敗: {fail}")
    task.push_log("info", "==========================================")
    task.push_complete(summary)


# ── 工具函式 ───────────────────────────────────────────

def _smart_get(row: dict, key: str) -> str:
    """忽略大小寫讀取 CSV 欄位值（修復原腳本的 BOM/大小寫問題）"""
    target = key.lower()
    for k in row:
        if k and k.strip().lower() == target:
            val = row[k]
            return val.strip() if val else ""
    return ""


def _throttle(delay, idx, success_count, batch_size, batch_pause, task):
    """流量控制：單筆延遲 + 批次暫停"""
    time.sleep(delay + random.uniform(0.01, 0.05))
    if success_count > 0 and success_count % batch_size == 0:
        task.push_log("info",
                      f"--- 已處理 {success_count} 筆，系統冷卻中 (暫停 {batch_pause} 秒) ---")
        time.sleep(batch_pause)
