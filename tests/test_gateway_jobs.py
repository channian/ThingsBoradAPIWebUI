# -*- coding: utf-8 -*-
"""多 Gateway 作業執行器 app/gateway_jobs.py（含 Task.status / Task.gateways）的行為測試。

純邏輯測試：不需要 DB，也刻意不 import app.main。

防止的退化：
- 不同台 Gateway 沒有真的並行，或同一台的兩個作業沒有嚴格依序（破壞限速假設）。
- worker 丟例外時拖垮其他 part、漏釋放佇列、或任務永遠不結束。
- push_complete 被呼叫兩次（兩個 part 同時結束時的競爭）或一次都沒有。
- 取消：排隊中的 part 沒有立刻離開、摘要沒標 cancelled / cancelled_remaining、Task.status 判斷錯。
- blocked（無法路由）的列沒有計入 total / fail / errors / 聚合進度。
- 多 part 的 log 缺 Gateway 前綴、單 part 卻多出前綴；進度沒有加總所有 part。
- started_at 被後來的 part 覆寫。

並行測試的原則：正確性不靠 sleep 判斷。全部用 Event / Barrier 同步，所有等待都有逾時，
逾時就 pytest.fail（worker 內的等待逾時則丟例外讓該 part 失敗），不會卡住整個套件。
"""
import datetime
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import gateway_jobs
from app.gateway_jobs import PartSpec, PartView, run_job
from app.gateway_queue import GatewayQueue
from app.task_manager import Task

TIMEOUT = 5.0


# ── 工具 ───────────────────────────────────────────────

@pytest.fixture
def fast_thread_switching():
    """把 GIL 切換間隔壓到極小，讓執行緒更頻繁地交錯，放大競爭條件被觸發的機率（測完還原）。"""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(old)


def make_task(tid="t1", **params):
    return Task(tid, "kw_execute", params)


def make_part(name, key=None, n=2, gid=None, payload=None):
    return PartSpec(gid, name, key or f"gw:{name}", list(range(n)), payload or {})


def count_pushes(task):
    """包裝 task.push_complete，記錄每次呼叫的摘要。"""
    calls = []
    original = task.push_complete

    def wrapper(summary):
        calls.append(summary)
        original(summary)

    task.push_complete = wrapper
    return calls


def must(event, what="事件"):
    """worker / 測試內的等待：逾時就丟例外（worker 內 → 該 part 失敗；測試內 → 測試失敗）。"""
    if not event.wait(TIMEOUT):
        raise AssertionError(f"等待{what}逾時")


def wait_for(pred, what="條件"):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.002)
    pytest.fail(f"等待{what}逾時")


class LogWatcher(logging.Handler):
    """掛在 gateway_jobs 的 logger 上，記錄訊息並在出現含指定字樣的紀錄時設起事件。

    用來同步「例外被記 log」這件事：job 的 done 事件可能早於 log 寫出（例如 on_all_done 例外時，
    helper 的 finally 先設起 done，例外才往上傳到 log.exception），直接斷言 caplog 會有競爭。
    """

    def __init__(self, needle):
        super().__init__(level=logging.ERROR)
        self.needle = needle
        self.seen = threading.Event()
        self.messages = []

    def emit(self, record):
        msg = record.getMessage()
        self.messages.append(msg)
        if self.needle in msg:
            self.seen.set()

    def __enter__(self):
        logging.getLogger("gateway_jobs").addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger("gateway_jobs").removeHandler(self)
        return False


def launch(task, parts, worker, q, blocked=None, enqueue=True, on_all_done=None):
    """（模擬端點）先同步 enqueue，再 run_job；回傳「任務完成」事件。"""
    done = threading.Event()

    def _done(t):
        try:
            if on_all_done is not None:
                on_all_done(t)
        finally:
            done.set()

    if enqueue:
        q.enqueue_many([p.key for p in parts], task.id)
    run_job(task, parts, worker, q, blocked=blocked, on_all_done=_done)
    return done


def finish(done, what="作業"):
    if not done.wait(TIMEOUT):
        pytest.fail(f"{what}逾時未完成")


def ok_worker(view, part):
    n = len(part.items)
    for i, item in enumerate(part.items, 1):
        view.add_result(str(item), "success")
        view.push_progress(i, n, i, 0, 0)
    return {"total": n, "success": n, "fail": 0, "skip": 0, "errors": []}


# ── 單 part 正常完成 ───────────────────────────────────

def test_single_part_completes_with_summary_and_progress():
    q = GatewayQueue()
    task = make_task()
    calls = count_pushes(task)
    completed_hook = []
    task.on_complete = lambda t: completed_hook.append(t.id)

    done = launch(task, [make_part("甲", n=3, gid=1)], ok_worker, q)
    finish(done)

    assert task.done is True
    s = task.summary
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (3, 3, 0, 0)
    assert s["errors"] == []
    assert s["cancelled"] is False
    assert s["cancelled_remaining"] == 0
    assert s["blocked"] == 0
    assert s["gateways"] == [{"gateway_id": 1, "name": "甲", "state": "done",
                              "total": 3, "success": 3, "fail": 0, "skip": 0}]
    assert task.progress == {"current": 3, "total": 3, "success": 3, "fail": 0, "skip": 0}
    assert task.status() == "done"
    assert len(task.results) == 3
    assert len(calls) == 1                       # push_complete 恰好一次
    assert completed_hook == [task.id]           # Task.on_complete（歷史紀錄用）也恰好一次
    assert q.snapshot() == {}                    # 佇列已釋放
    datetime.datetime.fromisoformat(task.started_at)   # 與既有 started_at 同為 ISO 字串
    assert task.ended_at is not None


def test_run_job_returns_immediately_and_runs_in_background():
    q = GatewayQueue()
    task = make_task()
    started, gate = threading.Event(), threading.Event()

    def worker(view, part):
        started.set()
        must(gate, "放行")
        return ok_worker(view, part)

    done = launch(task, [make_part("甲")], worker, q)
    must(started, "worker 啟動")
    assert task.done is False                    # run_job 已返回，worker 還卡著
    assert q.holder("gw:甲") == task.id
    gate.set()
    finish(done)
    assert task.done is True


def test_worker_receives_view_and_part():
    q = GatewayQueue()
    task = make_task(foo="bar")
    part = make_part("甲", payload={"url": "http://x"})
    seen = {}

    def worker(view, p):
        seen["view"], seen["part"] = view, p
        seen["cancelled"] = view.cancelled
        return ok_worker(view, p)

    finish(launch(task, [part], worker, q))
    view = seen["view"]
    assert isinstance(view, PartView)
    assert view.id == task.id
    assert view.params is task.params
    assert view.part is part and seen["part"] is part
    assert seen["part"].payload == {"url": "http://x"}
    assert seen["cancelled"] is False


def test_partial_worker_summary_gets_defaults():
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲", n=4)], lambda v, p: {"success": 3}, q))
    s = task.summary
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (4, 3, 0, 0)
    assert s["gateways"][0]["state"] == "done"


# ── 多 part ────────────────────────────────────────────

def test_two_parts_on_different_gateways_run_in_parallel():
    q = GatewayQueue()
    task = make_task()
    calls = count_pushes(task)
    both_inside = threading.Barrier(2, timeout=TIMEOUT)

    def worker(view, part):
        both_inside.wait()                       # 兩個 worker 必須同時在跑才會通過
        if part.name == "甲":
            view.add_result("a1", "success")
            view.add_result("a2", "success")
            return {"total": 2, "success": 2, "fail": 0, "skip": 0, "errors": []}
        view.add_result("b1", "success")
        view.add_result("b2", "fail", "壞了")
        view.add_result("b3", "skip")
        return {"total": 3, "success": 1, "fail": 1, "skip": 1,
                "errors": [{"name": "b2", "reason": "壞了"}]}

    done = launch(task, [make_part("甲", n=2, gid=1), make_part("乙", n=3, gid=2)], worker, q)
    finish(done)

    s = task.summary
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (5, 3, 1, 1)
    assert s["errors"] == [{"name": "b2", "reason": "壞了", "gateway": "乙"}]
    assert s["gateways"] == [
        {"gateway_id": 1, "name": "甲", "state": "done", "total": 2, "success": 2, "fail": 0, "skip": 0},
        {"gateway_id": 2, "name": "乙", "state": "done", "total": 3, "success": 1, "fail": 1, "skip": 1},
    ]
    assert len(calls) == 1
    assert len(task.results) == 5
    assert q.snapshot() == {}
    assert task.status() == "done"


def test_errors_get_gateway_field_without_mutating_worker_dicts():
    q = GatewayQueue()
    task = make_task()
    own = {"name": "x", "reason": "r", "gateway": "自訂"}
    plain = {"name": "y", "reason": "r2"}

    def worker(view, part):
        return {"total": 2, "success": 0, "fail": 2, "skip": 0, "errors": [own, plain]}

    finish(launch(task, [make_part("甲")], worker, q))
    errs = task.summary["errors"]
    assert errs == [{"name": "x", "reason": "r", "gateway": "自訂"},
                    {"name": "y", "reason": "r2", "gateway": "甲"}]
    assert "gateway" not in plain                # 不改動 worker 傳回的原物件


# ── details（worker 選填的顯示用清單）────────────────────

def details_worker(details):
    """回傳固定 details 的 worker（其餘欄位用 ok_worker 的）。"""
    def worker(view, part):
        summary = ok_worker(view, part)
        summary["details"] = details
        return summary
    return worker


def test_details_default_to_empty_list_when_no_worker_gives_any():
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲"), make_part("乙", gid=2)], ok_worker, q))
    assert task.summary["details"] == []


def test_details_are_kept_and_get_the_gateway_name_without_mutating_the_original():
    q = GatewayQueue()
    task = make_task()
    original = {"tag_name": "T1", "actual": 0, "expected": 8}
    worker_details = [original]
    finish(launch(task, [make_part("甲", gid=1)], details_worker(worker_details), q))
    assert task.summary["details"] == [{"tag_name": "T1", "actual": 0, "expected": 8, "gateway": "甲"}]
    assert original == {"tag_name": "T1", "actual": 0, "expected": 8}, "不改動 worker 傳回的原物件"
    assert worker_details == [original]


def test_details_keep_an_existing_gateway_field():
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲")], details_worker(
        [{"tag_name": "T1", "gateway": "自訂"}, {"tag_name": "T2"}]), q))
    assert task.summary["details"] == [{"tag_name": "T1", "gateway": "自訂"},
                                       {"tag_name": "T2", "gateway": "甲"}]


def test_details_of_several_gateways_are_concatenated_in_part_order_not_finish_order():
    """乙先結束、甲後結束，總摘要的 details 仍是甲在前、乙在後（與 gateways / errors 的順序一致）。"""
    q = GatewayQueue()
    task = make_task()
    b_done = threading.Event()

    def worker(view, part):
        if part.name == "甲":
            must(b_done, "乙先結束")
            return {"total": 2, "success": 2, "fail": 0, "skip": 0, "errors": [],
                    "details": [{"tag_name": "a1"}, {"tag_name": "a2"}]}
        summary = {"total": 1, "success": 1, "fail": 0, "skip": 0, "errors": [],
                   "details": [{"tag_name": "b1"}]}
        b_done.set()
        return summary

    finish(launch(task, [make_part("甲", n=2, gid=1), make_part("乙", n=1, gid=2)], worker, q))
    assert task.summary["details"] == [
        {"tag_name": "a1", "gateway": "甲"}, {"tag_name": "a2", "gateway": "甲"},
        {"tag_name": "b1", "gateway": "乙"},
    ]


@pytest.mark.parametrize("bad", [None, "abc", {"tag_name": "T1"}, 5, ("x",), True])
def test_details_that_is_not_a_list_is_treated_as_empty(bad):
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲")], details_worker(bad), q))
    assert task.summary["details"] == []
    assert task.summary["gateways"][0]["state"] == "done", "details 型別怪異不可讓 part 失敗"


def test_details_keeps_only_dict_elements_in_order():
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲")], details_worker(
        [1, "x", None, {"a": 1}, [2], {"b": 2}, ("c",)]), q))
    assert task.summary["details"] == [{"a": 1, "gateway": "甲"}, {"b": 2, "gateway": "甲"}]


def test_failed_and_queue_cancelled_parts_contribute_no_details():
    q = GatewayQueue()
    q.enqueue_many(["gw:2"], "blocker")           # 乙這台被別人佔住 → 取消時還在排隊
    task = make_task()
    b_waiting = threading.Event()

    def worker(view, part):
        if part.name == "甲":
            b_waiting.wait(TIMEOUT)
            raise RuntimeError("boom")
        return details_worker([{"tag_name": "never"}])(view, part)

    done = launch(task, [make_part("甲", key="gw:1"), make_part("乙", key="gw:2")], worker, q)
    wait_for(lambda: task.gateways[0]["state"] == "running", "甲開始執行")
    task.cancelled = True
    q.wake()
    wait_for(lambda: task.gateways[1]["state"] == "cancelled", "乙的排隊項目被取消")
    b_waiting.set()
    finish(done)
    assert [g["state"] for g in task.summary["gateways"]] == ["failed", "cancelled"]
    assert task.summary["details"] == []


def test_details_do_not_change_the_meaning_of_the_existing_summary_fields():
    q = GatewayQueue()
    task = make_task()
    blocked = [{"name": "X", "reason": "r"}]
    finish(launch(task, [make_part("甲", n=2, gid=1)], details_worker([{"tag_name": "T1"}]), q,
                  blocked=blocked))
    s = task.summary
    assert set(s) == {"total", "success", "fail", "skip", "errors", "cancelled", "cancelled_remaining",
                      "gateways", "blocked", "details"}
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (3, 2, 1, 0)
    assert s["blocked"] == 1 and s["cancelled"] is False and s["cancelled_remaining"] == 0
    assert s["errors"] == [{"name": "X", "reason": "r", "gateway": None}]
    assert s["gateways"] == [{"gateway_id": 1, "name": "甲", "state": "done",
                              "total": 2, "success": 2, "fail": 0, "skip": 0}]
    assert not any("details" in g for g in s["gateways"]), "details 只放在總摘要，不進每台的明細"


def test_details_in_complete_event_and_for_jobs_without_parts():
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲")], details_worker([{"tag_name": "T1"}]), q))
    events = []
    while not task.events.empty():
        events.append(task.events.get_nowait())
    complete = [e for e in events if e["type"] == "complete"]
    assert len(complete) == 1 and complete[0]["details"] == [{"tag_name": "T1", "gateway": "甲"}]

    empty = make_task("t2")
    run_job(empty, [], lambda v, p: pytest.fail("不該被呼叫"), q, blocked=[{"name": "X", "reason": "r"}])
    assert empty.summary["details"] == []


def test_same_gateway_two_jobs_run_strictly_in_order():
    q = GatewayQueue()
    t1, t2 = make_task("t1"), make_task("t2")
    p1, p2 = make_part("甲", key="gw:1"), make_part("甲", key="gw:1")
    order, order_lock = [], threading.Lock()
    started1, gate1, started2 = threading.Event(), threading.Event(), threading.Event()

    def note(tag):
        with order_lock:
            order.append(tag)

    def worker1(view, part):
        note("start1")
        started1.set()
        must(gate1, "放行 1")
        note("end1")
        return ok_worker(view, part)

    def worker2(view, part):
        note("start2")
        started2.set()
        note("end2")
        return ok_worker(view, part)

    # 先後 enqueue（請求到達順序）：t1 在前、t2 在後；但故意先啟動 t2 的 run_job
    q.enqueue_many(["gw:1"], "t1")
    q.enqueue_many(["gw:1"], "t2")
    done2 = launch(t2, [p2], worker2, q, enqueue=False)
    done1 = launch(t1, [p1], worker1, q, enqueue=False)

    must(started1, "第一個作業開始")
    assert not started2.wait(0.05)               # 反向檢查：第一個還沒結束，第二個不可開始
    assert q.position("gw:1", "t2") == 1
    assert t2.status() == "queued"
    assert t2.gateways[0]["state"] == "queued"
    assert t1.status() == "running"

    gate1.set()
    finish(done1, "第一個作業")
    finish(done2, "第二個作業")
    assert order == ["start1", "end1", "start2", "end2"]
    assert t1.summary["success"] == 2 and t2.summary["success"] == 2
    assert q.snapshot() == {}


def test_live_gateway_states_queued_running_done():
    q = GatewayQueue()
    q.enqueue_many(["gw:1"], "blocker")          # 另一個作業正佔著這台
    task = make_task()
    running, gate = threading.Event(), threading.Event()

    def worker(view, part):
        running.set()
        must(gate, "放行")
        return ok_worker(view, part)

    done = launch(task, [make_part("甲", key="gw:1", n=2, gid=9)], worker, q)
    entry = task.gateways[0]
    assert entry["state"] == "queued"
    assert entry["gateway_id"] == 9 and entry["name"] == "甲" and entry["key"] == "gw:1"
    assert entry["total"] == 2 and entry["current"] == 0
    assert task.status() == "queued"
    assert task.started_at is None               # 還沒有任何 part 開始
    assert q.position("gw:1", task.id) == 1

    q.release("gw:1", "blocker")                 # 輪到它
    must(running, "worker 開始")
    assert task.gateways[0]["state"] == "running"
    assert task.status() == "running"
    assert task.started_at is not None

    gate.set()
    finish(done)
    assert task.gateways[0]["state"] == "done"
    assert task.gateways[0]["success"] == 2 and task.gateways[0]["current"] == 2
    assert task.status() == "done"


# ── worker 例外 ────────────────────────────────────────

def test_worker_exception_fails_that_part_only():
    q = GatewayQueue()
    task = make_task()
    calls = count_pushes(task)
    barrier = threading.Barrier(2, timeout=TIMEOUT)

    def worker(view, part):
        barrier.wait()
        if part.name == "甲":
            raise RuntimeError("boom")
        return ok_worker(view, part)

    done = launch(task, [make_part("甲", n=2), make_part("乙", n=3)], worker, q)
    finish(done)

    s = task.summary
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (5, 3, 2, 0)
    assert [g["state"] for g in s["gateways"]] == ["failed", "done"]
    assert s["gateways"][0]["fail"] == 2 and s["gateways"][0]["total"] == 2
    assert len(s["errors"]) == 1
    assert s["errors"][0]["reason"] == "boom" and s["errors"][0]["gateway"] == "甲"
    assert len(calls) == 1
    assert task.done is True
    assert q.snapshot() == {}                    # 失敗的那台也釋放了
    assert any("boom" in e["message"] and "[甲]" in e["message"] for e in task.log_history)
    assert task.progress["total"] == 5 and task.progress["current"] == 5   # 失敗的列算已處理


def test_worker_exception_releases_gateway_for_next_job():
    q = GatewayQueue()
    t1, t2 = make_task("t1"), make_task("t2")

    def bad(view, part):
        raise ValueError("first fails")

    q.enqueue_many(["gw:1"], "t1")
    q.enqueue_many(["gw:1"], "t2")
    done1 = launch(t1, [make_part("甲", key="gw:1")], bad, q, enqueue=False)
    done2 = launch(t2, [make_part("甲", key="gw:1")], ok_worker, q, enqueue=False)
    finish(done1)
    finish(done2)
    assert t1.summary["fail"] == 2 and t2.summary["success"] == 2
    assert q.snapshot() == {}


def test_worker_returning_non_dict_is_treated_as_failure():
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲", n=3)], lambda v, p: None, q))
    s = task.summary
    assert s["fail"] == 3 and s["success"] == 0
    assert s["gateways"][0]["state"] == "failed"
    assert "dict" in s["errors"][0]["reason"]


def test_worker_exception_with_empty_message_still_has_a_reason():
    q = GatewayQueue()
    task = make_task()

    def worker(view, part):
        raise RuntimeError()

    finish(launch(task, [make_part("甲")], worker, q))
    assert task.summary["errors"][0]["reason"] == "RuntimeError"


# ── 取消 ───────────────────────────────────────────────

def test_cancel_while_queued_marks_part_cancelled_and_completes():
    q = GatewayQueue()
    q.enqueue_many(["gw:1"], "blocker")          # 這台被別人佔住 → 我們的 part 在排隊
    task = make_task()
    ran = []

    def worker(view, part):
        ran.append(part.name)
        return ok_worker(view, part)

    done = launch(task, [make_part("甲", key="gw:1", n=4)], worker, q)
    assert task.status() == "queued"

    task.cancelled = True                        # 取消端點：先設旗標，再 wake
    q.wake()
    finish(done)

    s = task.summary
    assert s["cancelled"] is True
    assert s["cancelled_remaining"] == 4
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (4, 0, 0, 0)
    assert s["gateways"][0]["state"] == "cancelled"
    assert task.gateways[0]["state"] == "cancelled"
    assert task.status() == "cancelled"
    assert ran == []                             # worker 完全沒被呼叫
    assert task.started_at is None
    assert q.snapshot() == {"gw:1": ["blocker"]}  # 自己離開了，別人的項目不受影響


def test_cancel_with_one_part_queued_and_another_running():
    q = GatewayQueue()
    q.enqueue_many(["gw:1"], "blocker")
    task = make_task()
    b_running, b_gate = threading.Event(), threading.Event()

    def worker(view, part):
        if part.name == "乙":
            b_running.set()
            must(b_gate, "放行")
        return ok_worker(view, part)

    done = launch(task, [make_part("甲", key="gw:1", n=3), make_part("乙", key="gw:2", n=2)],
                  worker, q)
    must(b_running, "乙開始")
    assert task.status() == "running"            # 乙在跑、甲在排隊

    task.cancelled = True
    q.wake()
    wait_for(lambda: task.gateways[0]["state"] == "cancelled", "甲的排隊項目被取消")
    assert not task.done                          # 乙還沒結束，任務就還沒完成
    b_gate.set()
    finish(done)

    s = task.summary
    assert s["cancelled"] is True and s["cancelled_remaining"] == 3
    assert [g["state"] for g in s["gateways"]] == ["cancelled", "done"]
    assert (s["total"], s["success"]) == (5, 2)
    assert task.status() == "cancelled"
    assert q.snapshot() == {"gw:1": ["blocker"]}


def test_cancel_during_run_worker_stops_early():
    q = GatewayQueue()
    task = make_task()
    first_done, proceed = threading.Event(), threading.Event()

    def worker(view, part):
        n = len(part.items)
        done_count = 0
        for i, item in enumerate(part.items, 1):
            if view.cancelled:
                return {"total": n, "success": done_count, "fail": 0, "skip": 0,
                        "errors": [], "cancelled_remaining": n - done_count}
            done_count += 1
            view.push_progress(i, n, done_count, 0, 0)
            if i == 1:
                first_done.set()
                must(proceed, "放行")
        return {"total": n, "success": done_count, "fail": 0, "skip": 0, "errors": []}

    done = launch(task, [make_part("甲", n=5)], worker, q)
    must(first_done, "第一筆完成")
    task.cancelled = True
    proceed.set()
    finish(done)

    s = task.summary
    assert s["cancelled"] is True
    assert s["cancelled_remaining"] == 4
    assert (s["total"], s["success"]) == (5, 1)
    assert s["gateways"][0]["state"] == "done"   # worker 正常返回（提早結束）
    assert task.status() == "cancelled"
    assert q.snapshot() == {}


def test_not_cancelled_summary_flag_is_false():
    q = GatewayQueue()
    task = make_task()
    finish(launch(task, [make_part("甲")], ok_worker, q))
    assert task.summary["cancelled"] is False
    assert task.summary["cancelled_remaining"] == 0


def test_part_not_enqueued_is_skipped_not_run(caplog):
    """呼叫端忘了 enqueue：該 part 不會執行（不在佇列），且任務仍會完成，不會卡住。"""
    q = GatewayQueue()
    task = make_task()
    ran = []
    with caplog.at_level(logging.WARNING, logger="gateway_jobs"):
        done = launch(task, [make_part("甲", n=2)], lambda v, p: ran.append(1) or ok_worker(v, p),
                      q, enqueue=False)
        finish(done)
    assert ran == []
    assert task.summary["gateways"][0]["state"] == "cancelled"
    assert any("不在" in r.getMessage() for r in caplog.records)


# ── blocked / 空 ───────────────────────────────────────

def test_blocked_rows_count_in_total_fail_errors_and_progress():
    q = GatewayQueue()
    task = make_task()
    initial = {}

    def worker(view, part):
        initial.update(task.progress)             # run_job 啟動前已推過初始聚合進度
        return ok_worker(view, part)

    blocked = [{"name": "T1", "reason": "無法判斷 Gateway"}, {"name": "T2", "reason": "對應表衝突"}]
    finish(launch(task, [make_part("甲", n=3)], worker, q, blocked=blocked))

    s = task.summary
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (5, 3, 2, 0)
    assert s["blocked"] == 2
    assert s["errors"] == [{"name": "T1", "reason": "無法判斷 Gateway", "gateway": None},
                           {"name": "T2", "reason": "對應表衝突", "gateway": None}]
    assert len(s["gateways"]) == 1                # blocked 不開 part
    assert initial == {"current": 2, "total": 5, "success": 0, "fail": 2, "skip": 0}
    assert task.progress == {"current": 5, "total": 5, "success": 3, "fail": 2, "skip": 0}
    assert q.snapshot() == {}


def test_blocked_only_completes_without_threads():
    q = GatewayQueue()
    task = make_task()
    calls = count_pushes(task)
    hook = []
    blocked = [{"name": "T1", "reason": "r1"}, {"name": "T2", "reason": "r2"}]
    run_job(task, [], lambda v, p: pytest.fail("不該被呼叫"), q, blocked=blocked,
            on_all_done=lambda t: hook.append(t.id))

    assert task.done is True                      # 同步完成，不需要等
    s = task.summary
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (2, 0, 2, 0)
    assert s["gateways"] == [] and s["blocked"] == 2
    assert [e["name"] for e in s["errors"]] == ["T1", "T2"]
    assert task.progress == {"current": 2, "total": 2, "success": 0, "fail": 2, "skip": 0}
    assert task.status() == "done"
    assert len(calls) == 1
    assert hook == [task.id]


def test_no_parts_and_no_blocked_still_completes_with_zero_total():
    q = GatewayQueue()
    task = make_task()
    calls = count_pushes(task)
    run_job(task, [], lambda v, p: pytest.fail("不該被呼叫"), q)
    assert task.done is True
    s = task.summary
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (0, 0, 0, 0)
    assert s["errors"] == [] and s["gateways"] == [] and s["blocked"] == 0
    assert s["cancelled"] is False
    assert len(calls) == 1


def test_duplicate_part_keys_are_rejected_before_anything_starts():
    q = GatewayQueue()
    task = make_task()
    q.enqueue_many(["gw:1"], task.id)
    ran = []
    with pytest.raises(ValueError):
        run_job(task, [make_part("甲", key="gw:1"), make_part("乙", key="gw:1")],
                lambda v, p: ran.append(1), q)
    assert ran == [] and task.gateways == [] and task.done is False


# ── PartView ───────────────────────────────────────────

def test_multi_part_logs_are_prefixed_with_gateway_name():
    q = GatewayQueue()
    task = make_task()

    def worker(view, part):
        view.push_log("info", "hello")
        return ok_worker(view, part)

    finish(launch(task, [make_part("甲"), make_part("乙")], worker, q))
    messages = sorted(e["message"] for e in task.log_history if "hello" in e["message"])
    assert messages == ["[乙] hello", "[甲] hello"]
    levels = {e["level"] for e in task.log_history if "hello" in e["message"]}
    assert levels == {"info"}


def test_single_part_logs_have_no_prefix():
    q = GatewayQueue()
    task = make_task()

    def worker(view, part):
        view.push_log("warning", "hello")
        return ok_worker(view, part)

    finish(launch(task, [make_part("甲")], worker, q))
    assert [e for e in task.log_history if "hello" in e["message"]] == [
        {"level": "warning", "message": "hello"}]


def test_partview_add_result_goes_to_task_results():
    q = GatewayQueue()
    task = make_task()

    def worker(view, part):
        view.add_result("tag1", "fail", "HTTP 500")
        view.add_result("tag2", "success")
        return {"total": 2, "success": 1, "fail": 1, "skip": 0, "errors": []}

    finish(launch(task, [make_part("甲")], worker, q))
    assert task.results == [{"name": "tag1", "status": "fail", "detail": "HTTP 500"},
                            {"name": "tag2", "status": "success", "detail": ""}]


def test_progress_is_sum_of_all_parts():
    q = GatewayQueue()
    task = make_task()
    a_pushed, b_pushed, gate = threading.Event(), threading.Event(), threading.Event()
    initial = {}

    def worker(view, part):
        if part.name == "甲":
            initial.update(task.progress)
            view.push_progress(1, 2, 1, 0, 0)
            a_pushed.set()
            must(gate, "放行")
            view.push_progress(2, 2, 2, 0, 0)
            return {"total": 2, "success": 2, "fail": 0, "skip": 0, "errors": []}
        must(a_pushed, "甲先推進度")
        view.push_progress(2, 3, 1, 1, 0)
        b_pushed.set()
        must(gate, "放行")
        view.push_progress(3, 3, 1, 1, 1)
        return {"total": 3, "success": 1, "fail": 1, "skip": 1, "errors": []}

    done = launch(task, [make_part("甲", n=2), make_part("乙", n=3)], worker, q)
    must(b_pushed, "乙推進度")
    assert initial == {"current": 0, "total": 5, "success": 0, "fail": 0, "skip": 0}
    assert task.progress == {"current": 3, "total": 5, "success": 2, "fail": 1, "skip": 0}
    gate.set()
    finish(done)
    assert task.progress == {"current": 5, "total": 5, "success": 3, "fail": 1, "skip": 1}


def test_started_at_is_set_only_by_the_first_part(monkeypatch):
    stamps = iter(f"T{i}" for i in range(1, 100))
    monkeypatch.setattr(gateway_jobs, "_now_iso", lambda: next(stamps))
    q = GatewayQueue()
    q.enqueue_many(["gw:乙"], "blocker")         # 乙會晚於甲才開始
    task = make_task()
    a_seen = {}
    b_seen = {}
    a_done = threading.Event()

    def worker(view, part):
        if part.name == "甲":
            a_seen["started_at"] = task.started_at
            a_done.set()
        else:
            b_seen["started_at"] = task.started_at
        return ok_worker(view, part)

    done = launch(task, [make_part("甲"), make_part("乙")], worker, q)
    must(a_done, "甲完成")
    q.release("gw:乙", "blocker")
    finish(done)
    assert a_seen["started_at"] == "T1"
    assert b_seen["started_at"] == "T1"           # 乙開始時不會再覆寫
    assert task.started_at == "T1"


# ── 完成的恰好一次與 hook ──────────────────────────────

class BarrierQueue(GatewayQueue):
    """release 之後讓兩個 part 在同一刻卡在收尾階段，逼出「同時結束」的競爭。"""

    def __init__(self, parties):
        super().__init__()
        self.barrier = threading.Barrier(parties, timeout=TIMEOUT)

    def release(self, key, task_id):
        super().release(key, task_id)
        self.barrier.wait()


class LockstepLock:
    """取代 _Job.lock 的鎖：part 執行緒每次「解鎖之後」都要和另一個 part 在 Barrier 會合才繼續。

    這製造出確定性的交錯：兩個 part 都完成「持鎖區段」（遞減剩餘數）之後，才一起往下走。
    若完成判斷（is_last）不是在鎖內決定而是解鎖後才讀共享狀態，兩邊就會同時看到「全部完成」→ 重複完成。
    正確實作兩邊都只會在鎖內決定，因此照樣恰好一次，也不會死結（會合點在鎖外）。
    只對名稱以 gwjob- 開頭的 part 執行緒會合，主執行緒的使用不受影響。
    """

    def __init__(self, barrier):
        self._lock = threading.Lock()
        self._barrier = barrier

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        if threading.current_thread().name.startswith("gwjob-"):
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return False


def test_completion_is_decided_atomically_when_parts_finish_in_lockstep(monkeypatch):
    for i in range(10):
        barrier = threading.Barrier(2, timeout=TIMEOUT)
        monkeypatch.setattr(gateway_jobs, "threading", SimpleNamespace(
            Lock=lambda: LockstepLock(barrier), Thread=threading.Thread))
        q = GatewayQueue()
        task = make_task(f"t{i}")
        calls = count_pushes(task)
        done = launch(task, [make_part("甲"), make_part("乙")], ok_worker, q)
        finish(done, f"第 {i} 輪")
        assert not barrier.broken, "會合點逾時：兩個 part 沒有在同一刻收尾"
        assert len(calls) == 1, f"第 {i} 輪 push_complete 被呼叫 {len(calls)} 次"
        assert task.summary["total"] == 4 and task.summary["success"] == 4
        assert q.snapshot() == {}


def test_two_parts_finishing_simultaneously_complete_exactly_once(fast_thread_switching):
    for i in range(100):
        q = BarrierQueue(2)
        task = make_task(f"t{i}")
        calls = count_pushes(task)
        start = threading.Barrier(2, timeout=TIMEOUT)

        def worker(view, part):
            start.wait()
            return ok_worker(view, part)

        done = launch(task, [make_part("甲"), make_part("乙")], worker, q)
        finish(done, f"第 {i} 輪")
        assert len(calls) == 1, f"第 {i} 輪 push_complete 被呼叫 {len(calls)} 次"
        assert task.summary["success"] == 4 and task.summary["total"] == 4
        assert [g["state"] for g in task.summary["gateways"]] == ["done", "done"]
        assert q.snapshot() == {}


def test_three_parts_with_staggered_finish_complete_exactly_once():
    q = GatewayQueue()
    task = make_task()
    calls = count_pushes(task)
    finish(launch(task, [make_part("甲"), make_part("乙"), make_part("丙")], ok_worker, q))
    assert len(calls) == 1
    assert task.summary["total"] == 6


def test_on_all_done_runs_once_after_push_complete():
    q = GatewayQueue()
    task = make_task()
    seen = []

    def hook(t):
        seen.append((t is task, t.done, t.summary is not None, q.snapshot()))

    finish(launch(task, [make_part("甲"), make_part("乙")], ok_worker, q, on_all_done=hook))
    assert seen == [(True, True, True, {})]       # push_complete 之後、佇列也已全部釋放


def test_on_all_done_exception_is_logged_not_raised():
    q = GatewayQueue()
    task = make_task()

    def hook(t):
        raise RuntimeError("hook 壞了")

    with LogWatcher("on_all_done") as watcher:
        finish(launch(task, [make_part("甲")], ok_worker, q, on_all_done=hook))
        must(watcher.seen, "on_all_done 例外被記 log")
    assert task.done is True and task.summary["success"] == 2
    assert q.snapshot() == {}


def test_push_complete_exception_is_logged_and_does_not_hang(caplog):
    q = GatewayQueue()
    task = make_task()
    hook = []

    def exploding_push_complete(summary):
        raise RuntimeError("push_complete 壞了")

    task.push_complete = exploding_push_complete
    with caplog.at_level(logging.ERROR, logger="gateway_jobs"):
        finish(launch(task, [make_part("甲"), make_part("乙")], ok_worker, q,
                      on_all_done=lambda t: hook.append(1)))
    assert q.snapshot() == {}                     # 佇列都釋放了
    assert task.done is True                      # 輪詢端不會永遠看到「未完成」
    assert task.summary is not None and task.summary["total"] == 4
    assert hook == [1]
    assert any("push_complete" in r.getMessage() for r in caplog.records)


def test_thread_start_failure_fails_that_part_and_releases_queue(monkeypatch):
    class FlakyThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self._real = threading.Thread(target=target, args=args, name=name, daemon=daemon)
            self._fail = name.endswith("-1")

        def start(self):
            if self._fail:
                raise RuntimeError("can't start new thread")
            self._real.start()

    monkeypatch.setattr(gateway_jobs, "threading",
                        SimpleNamespace(Lock=threading.Lock, Thread=FlakyThread))
    q = GatewayQueue()
    task = make_task()
    calls = count_pushes(task)
    finish(launch(task, [make_part("甲", n=2), make_part("乙", n=3)], ok_worker, q))

    s = task.summary
    assert [g["state"] for g in s["gateways"]] == ["done", "failed"]
    assert (s["total"], s["success"], s["fail"]) == (5, 2, 3)
    assert "can't start new thread" in s["errors"][0]["reason"]
    assert q.snapshot() == {}
    assert len(calls) == 1


# ── Task.status / Task.gateways ────────────────────────

def test_task_gateways_defaults_to_empty_list_and_is_not_shared():
    a, b = make_task("a"), make_task("b")
    assert a.gateways == [] and b.gateways == []
    a.gateways.append({"state": "queued"})
    assert b.gateways == []


@pytest.mark.parametrize("states, expected", [
    ([], "running"),                              # 單純背景任務（沒有 gateways）
    (["queued"], "queued"),
    (["queued", "queued"], "queued"),
    (["running"], "running"),
    (["queued", "running"], "running"),
    (["running", "running"], "running"),
    (["done", "queued"], "running"),              # 部分已完成、其餘仍排隊 → running
    (["failed", "queued"], "running"),
    (["done", "running"], "running"),
])
def test_task_status_while_not_done(states, expected):
    t = make_task()
    t.gateways = [{"state": s} for s in states]
    assert t.status() == expected


@pytest.mark.parametrize("summary, expected", [
    ({"total": 1, "cancelled": False}, "done"),
    ({"total": 1}, "done"),
    (None, "done"),
    ({"total": 1, "cancelled": True}, "cancelled"),
])
def test_task_status_when_done(summary, expected):
    t = make_task()
    t.gateways = [{"state": "done"}]
    t.summary = summary
    t.done = True
    assert t.status() == expected


def test_task_status_done_wins_over_gateway_states():
    t = make_task()
    t.gateways = [{"state": "queued"}]            # 殘留的狀態不影響已完成判斷
    t.done = True
    t.summary = {"cancelled": False}
    assert t.status() == "done"


def test_existing_task_behaviour_unchanged():
    t = make_task()
    t.push_log("info", "x")
    t.push_progress(1, 2, 1, 0, 0)
    t.push_complete({"total": 2, "success": 1, "fail": 0, "skip": 0})
    assert t.done and t.summary["total"] == 2
    assert t.progress == {"current": 1, "total": 2, "success": 1, "fail": 0, "skip": 0}
    kinds = []
    while not t.events.empty():
        kinds.append(t.events.get()["type"])
    assert kinds == ["log", "progress", "complete"]
    assert t.status() == "done"


# ── 模組獨立性 ─────────────────────────────────────────

def test_module_has_no_web_or_db_dependency():
    """app.gateway_jobs 只能依賴 app.task_manager 與 app.gateway_queue（皆為純標準函式庫）。"""
    code = (
        "import sys\n"
        "import app.gateway_jobs\n"
        "bad = [m for m in ('fastapi', 'starlette', 'psycopg2', 'app.main', 'app.pg_client') "
        "if m in sys.modules]\n"
        "sys.exit('unexpected imports: ' + ', '.join(bad) if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(gateway_jobs.__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
