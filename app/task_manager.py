# -*- coding: utf-8 -*-
"""
背景任務管理器
- 將批次操作包裝成背景任務
- 透過 Queue 推送即時事件給 SSE 端點
- 記錄每筆處理結果供匯出
"""

import queue
import threading
import uuid
import time
import random
import datetime
import logging
from typing import Dict, Optional, List

log = logging.getLogger("task_manager")


# ── 任務模型 ───────────────────────────────────────────

class Task:
    def __init__(self, task_id: str, task_type: str, params: dict):
        self.id = task_id
        self.type = task_type
        self.params = params
        self.events: queue.Queue = queue.Queue()
        self.done = False
        self.cancelled = False
        self.log_history: list = []
        self.progress = {"current": 0, "total": 0,
                         "success": 0, "fail": 0, "skip": 0}
        self.summary: Optional[dict] = None
        self.results: list = []
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None
        self.csv_filename: str = ""
        self.on_complete = None

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


# ── 工具函式 ───────────────────────────────────────────

def _smart_get(row: dict, key: str) -> str:
    """忽略大小寫讀取 CSV 欄位值"""
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
