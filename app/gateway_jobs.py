# -*- coding: utf-8 -*-
"""
多 Gateway 背景作業執行器。

「一個請求 → 一個 Task（單一 task_id）→ 每台 Gateway 一個工作單元（part）」的編排集中在這裡，
Step 3 建點 / Step 5 Scale / 批次刪除三個端點共用。

- 每個 part 一條 daemon 執行緒，各自等「自己那台」Gateway 的佇列（見 app/gateway_queue.py）：
  不同台並行、同一台嚴格先進先出。Kepware 限速是每台各自計算，所以這樣不會互相拖慢。
- worker（由呼叫端提供，內容就是原本的逐筆處理迴圈）只負責處理「一台 Gateway 的列」，
  回傳該台的摘要；**不得自行呼叫 task.push_complete**，總摘要與完成通知由本模組負責，
  且保證恰好一次。
- 本模組只依賴 app.task_manager 與 app.gateway_queue（皆為純標準函式庫），
  不 import FastAPI / 資料庫 / app.main，可獨立測試。

呼叫端流程（在請求處理函式內同步完成前兩步，排隊順序才會等於請求到達順序）::

    ahead = queue.enqueue_many([p.key for p in parts], task.id, max_depth)  # 可能拋 QueueFullError
    run_job(task, parts, worker, queue, blocked=blocked)                    # 立刻返回
    return {"task_id": task.id}

取消：端點先設 ``task.cancelled = True``，再呼叫 ``queue.wake()``。
- 還在排隊的 part：立刻離開佇列，狀態 cancelled，整批列都算 cancelled_remaining；
- 執行中的 part：worker 自行輪詢 ``view.cancelled``，提早返回（可回報 cancelled_remaining）。

part 狀態（task.gateways[i]["state"]）：
    queued    排隊中（用 queue.position(key, task.id) 可查前面還有幾個）
    running   執行中
    done      worker 正常返回（含取消後提早返回；剩餘筆數看摘要的 cancelled_remaining）
    cancelled 還在排隊時就被取消，完全沒執行
    failed    worker 丟出例外（該 part 全數記為失敗，不影響其他 part）
"""

from __future__ import annotations

import datetime
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.gateway_queue import GatewayQueue
from app.task_manager import Task

log = logging.getLogger("gateway_jobs")

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"


def _now_iso() -> str:
    """與既有 started_at 相同格式（datetime.now().isoformat()）；獨立成函式方便測試注入。"""
    return datetime.datetime.now().isoformat()


# ── 資料結構 ───────────────────────────────────────────

@dataclass
class PartSpec:
    """一台 Gateway 的工作單元。

    gateway_id: Gateway id（手動憑證為 None）。
    name: 顯示用 Gateway 名稱。
    key: 佇列 key（gateway_queue.gateway_key(...) 的結果）；同一個 job 內不可重複。
    items: 這台要處理的列。
    payload: worker 自用資料（憑證等），本模組不解讀。
    """
    gateway_id: Optional[int]
    name: str
    key: str
    items: list
    payload: dict = field(default_factory=dict)


class PartView:
    """給 worker 用的 Task 代理：介面刻意和 Task 的 push_log / add_result / push_progress 相同，
    既有的逐筆迴圈與 _kw_call_with_429_retry(task, ...) / _throttle(..., task) 可直接傳它。

    屬性：id（= task.id）、params（= task.params）、part（本台的 PartSpec）。
    與 Task 的差異（刻意）：
    - 沒有 push_complete / started_at 等：完成通知由 run_job 統一處理；
    - push_progress 傳入的是「本台」的進度，會自動加總所有 part 後才更新 Task；
    - 多於 1 個 part 時，push_log 的訊息會加前綴 ``[Gateway 名稱] ``。
    """

    def __init__(self, job: "_Job", index: int) -> None:
        self._job = job
        self._index = index
        self.id: str = job.task.id
        self.params: dict = job.task.params
        self.part: PartSpec = job.parts[index]

    def push_log(self, level: str, message: str) -> None:
        self._job.log(self.part, level, message)

    def add_result(self, name: str, status: str, detail: str = "") -> None:
        self._job.task.add_result(name, status, detail)

    def push_progress(self, current, total, success, fail, skip) -> None:
        self._job.update_progress(self._index, current, total, success, fail, skip)

    @property
    def cancelled(self) -> bool:
        """使用者是否已要求取消；worker 應在每筆之間輪詢，為真時儘早結束並回報 cancelled_remaining。"""
        return self._job.task.cancelled


# ── 內部：一次 run_job 的共享狀態 ───────────────────────

def _int_of(raw: dict, key: str, default: int) -> int:
    value = raw.get(key)
    return default if value is None else int(value)


class _Job:
    """一次 run_job 的共享狀態；self.lock 保護：part 計數/狀態、剩餘 part 數、完成旗標、
    對 task.push_progress 的呼叫（在鎖內呼叫，聚合進度才不會被較舊的值蓋回去）。"""

    def __init__(self, task: Task, parts: list, worker: Callable, queue: GatewayQueue,
                 blocked: Optional[list], on_all_done: Optional[Callable]) -> None:
        self.task = task
        self.parts = parts
        self.worker = worker
        self.queue = queue
        self.blocked = list(blocked or [])
        self.on_all_done = on_all_done
        self.multi = len(parts) > 1          # 多於 1 個 part 時，log 才加 Gateway 前綴
        self.lock = threading.Lock()
        self.remaining = len(parts)          # 尚未結束的 part 數
        self.completed = False               # 是否已負責 push_complete（保證恰好一次）
        self.results: list = [None] * len(parts)
        self.entries: list = [
            {"gateway_id": p.gateway_id, "name": p.name, "key": p.key, "state": STATE_QUEUED,
             "total": len(p.items), "current": 0, "success": 0, "fail": 0, "skip": 0}
            for p in parts
        ]
        task.gateways = self.entries

    # ── log / 進度 ──

    def log(self, part: PartSpec, level: str, message: str) -> None:
        if self.multi:
            message = f"[{part.name}] {message}"
        self.task.push_log(level, message)

    def _push_aggregate_locked(self) -> None:
        """（須持有 self.lock）把所有 part 的目前值加總（blocked 視為已處理的失敗）後更新 Task。"""
        nb = len(self.blocked)
        entries = self.entries
        self.task.push_progress(
            nb + sum(e["current"] for e in entries),
            nb + sum(e["total"] for e in entries),
            sum(e["success"] for e in entries),
            nb + sum(e["fail"] for e in entries),
            sum(e["skip"] for e in entries),
        )

    def update_progress(self, index: int, current, total, success, fail, skip) -> None:
        with self.lock:
            self.entries[index].update(current=current, total=total, success=success,
                                       fail=fail, skip=skip)
            self._push_aggregate_locked()

    def _mark_running(self, index: int) -> None:
        with self.lock:
            self.entries[index]["state"] = STATE_RUNNING
            if self.task.started_at is None:      # 只在第一個 part 開始時設
                self.task.started_at = _now_iso()

    # ── 單一 part 的執行 ──

    @staticmethod
    def _failed_result(part: PartSpec, exc: BaseException) -> dict:
        n = len(part.items)
        reason = str(exc) or type(exc).__name__
        return {"total": n, "success": 0, "fail": n, "skip": 0,
                "errors": [{"name": part.name, "reason": reason}], "cancelled_remaining": 0}

    @staticmethod
    def _normalize(part: PartSpec, raw) -> dict:
        """把 worker 回傳的摘要整理成固定欄位；不是 dict 或數值無法轉整數就拋例外（視為 worker 失敗）。"""
        if not isinstance(raw, dict):
            raise TypeError(f"worker 必須回傳摘要 dict，實際回傳 {type(raw).__name__}")
        return {
            "total": _int_of(raw, "total", len(part.items)),
            "success": _int_of(raw, "success", 0),
            "fail": _int_of(raw, "fail", 0),
            "skip": _int_of(raw, "skip", 0),
            "errors": list(raw.get("errors") or []),
            "cancelled_remaining": _int_of(raw, "cancelled_remaining", 0),
        }

    def _execute(self, index: int):
        """排隊 → 執行 worker。回傳 (state, 摘要)；Exception 一律在此消化，不往外丟。"""
        part = self.parts[index]
        task = self.task
        try:
            got = self.queue.wait_turn(part.key, task.id, cancelled=lambda: task.cancelled)
        except Exception as e:
            log.exception("等待 Gateway 佇列時發生例外 (task=%s, gateway=%s)", task.id, part.name)
            self.log(part, "error", f"等待佇列時發生例外：{e}")
            return STATE_FAILED, self._failed_result(part, e)

        if not got:
            n = len(part.items)
            if task.cancelled:
                self.log(part, "warning", "已取消（尚在排隊，未執行）")
            else:
                # 不是取消卻不在佇列：呼叫端沒有事先 enqueue_many，或被別處 release 了
                log.warning("作業 %s 不在 %s 的佇列中，略過 Gateway %s", task.id, part.key, part.name)
                self.log(part, "warning", "不在佇列中，未執行")
            return STATE_CANCELLED, {"total": n, "success": 0, "fail": 0, "skip": 0,
                                     "errors": [], "cancelled_remaining": n}

        self._mark_running(index)
        try:
            raw = self.worker(PartView(self, index), part)
            return STATE_DONE, self._normalize(part, raw)
        except Exception as e:
            # 該 part 全數記為失敗，不影響其他 part；worker 應自行處理逐筆例外，這裡只是最後防線
            log.exception("worker 例外 (task=%s, gateway=%s)", task.id, part.name)
            self.log(part, "error", f"執行失敗：{e}")
            return STATE_FAILED, self._failed_result(part, e)

    def thread_main(self, index: int) -> None:
        part = self.parts[index]
        # 預設值：就算 _execute 被 BaseException（SystemExit 等）打斷，finally 仍有東西可記錄
        state = STATE_FAILED
        result = self._failed_result(part, RuntimeError("執行緒異常中止"))
        try:
            state, result = self._execute(index)
        finally:
            self.end_part(index, state, result)

    def end_part(self, index: int, state: str, result: dict) -> None:
        """part 結束的收尾：先釋放佇列（讓下一個作業立刻接手）、再記錄結果；
        若是最後一個結束的 part，由它負責 push_complete（恰好一次）。"""
        part = self.parts[index]
        try:
            self.queue.release(part.key, self.task.id)
        finally:
            with self.lock:
                e = self.entries[index]
                e.update(state=state, total=result["total"], success=result["success"],
                         fail=result["fail"], skip=result["skip"],
                         current=result["success"] + result["fail"] + result["skip"])
                self.results[index] = result
                self.remaining -= 1
                is_last = self.remaining == 0 and not self.completed
                if is_last:
                    self.completed = True
                self._push_aggregate_locked()
            if is_last:
                self.complete()

    # ── 完成 ──

    def build_summary(self) -> dict:
        nb = len(self.blocked)
        errors: list = []
        gateways: list = []
        total = success = fail = skip = remaining = 0
        for part, entry, res in zip(self.parts, self.entries, self.results):
            total += res["total"]
            success += res["success"]
            fail += res["fail"]
            skip += res["skip"]
            remaining += res["cancelled_remaining"]
            for err in res["errors"]:
                if isinstance(err, dict):
                    err = dict(err)                       # 不改動 worker 傳回的原物件
                    err.setdefault("gateway", part.name)
                else:
                    err = {"name": str(err), "reason": "", "gateway": part.name}
                errors.append(err)
            gateways.append({"gateway_id": part.gateway_id, "name": part.name,
                             "state": entry["state"], "total": res["total"],
                             "success": res["success"], "fail": res["fail"], "skip": res["skip"]})
        for b in self.blocked:
            errors.append({"name": b.get("name", ""), "reason": b.get("reason", ""), "gateway": None})
        return {
            "total": total + nb,
            "success": success,
            "fail": fail + nb,
            "skip": skip,
            "errors": errors,
            "cancelled": bool(self.task.cancelled),
            "cancelled_remaining": remaining,
            "gateways": gateways,
            "blocked": nb,
        }

    def complete(self) -> None:
        """組裝總摘要並 push_complete，再呼叫 on_all_done。任何例外只記 log，不外洩。"""
        task = self.task
        summary = self.build_summary()
        try:
            task.push_complete(summary)
        except Exception:
            log.exception("push_complete 失敗 (task=%s)", task.id)
            if not task.done:                 # 避免輪詢端永遠看到「未完成」
                if task.summary is None:
                    task.summary = summary
                task.done = True
        if self.on_all_done is not None:
            try:
                self.on_all_done(task)
            except Exception:
                log.exception("on_all_done 失敗 (task=%s)", task.id)


# ── 對外入口 ───────────────────────────────────────────

def run_job(task: Task, parts: list, worker: Callable, queue: GatewayQueue,
            blocked: Optional[list] = None, on_all_done: Optional[Callable] = None) -> None:
    """啟動多 Gateway 作業並立刻返回（不阻塞）。

    參數：
        task: 本次請求唯一的 Task。
        parts: 每台 Gateway 一個 PartSpec；key 不可重複（否則同一個 job 的兩個 part 會共用
            同一個佇列項目而同時執行，破壞「同一台序列」）→ ValueError。
        worker: ``worker(view: PartView, part: PartSpec) -> dict``，回傳該台摘要
            ``{"total","success","fail","skip","errors":[...], "cancelled_remaining"(選填)}``；
            丟例外 → 該台全數記為失敗，其他台不受影響。不得自行 push_complete。
        queue: 呼叫端**已事先**對 parts 的所有 key 執行過 ``enqueue_many(keys, task.id, ...)``。
        blocked: 無法路由而被擋下的列 ``[{"name","reason"}]``；不開 part、不排隊，
            只計入總摘要的 total / fail / errors（errors 的 gateway 為 None）與聚合進度。
        on_all_done: 在 push_complete 之後、於最後結束的那條執行緒呼叫 ``on_all_done(task)``。

    parts 與 blocked 皆為空，或只有 blocked：不開執行緒，直接同步 push_complete。
    總摘要欄位見 _Job.build_summary（保留舊有的 total/success/fail/skip/errors，
    因此只讀這四個數字的 _append_history 不受影響）。
    """
    parts = list(parts or [])
    keys = [p.key for p in parts]
    if len(set(keys)) != len(keys):
        raise ValueError(f"parts 的 key 不可重複：{keys}")

    job = _Job(task, parts, worker, queue, blocked, on_all_done)

    if not parts:
        if job.blocked:
            with job.lock:
                job._push_aggregate_locked()
        job.completed = True
        job.complete()
        return

    # 先推一次初始聚合進度，前端立刻看得到總數（含 blocked 已算失敗）
    with job.lock:
        job._push_aggregate_locked()

    for index, part in enumerate(parts):
        thread = threading.Thread(target=job.thread_main, args=(index,),
                                  name=f"gwjob-{task.id}-{index}", daemon=True)
        try:
            thread.start()
        except Exception as e:
            # 開不了執行緒：不能讓這台的佇列項目永遠卡住，當場收尾為失敗
            log.exception("無法啟動 Gateway 執行緒 (task=%s, gateway=%s)", task.id, part.name)
            job.log(part, "error", f"無法啟動執行緒：{e}")
            job.end_part(index, STATE_FAILED, job._failed_result(part, e))
