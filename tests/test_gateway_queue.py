# -*- coding: utf-8 -*-
"""Gateway 作業佇列 app/gateway_queue.py 的行為測試。

純邏輯測試：不需要 DB，也刻意不 import app.main。

防止的退化：
- 佇列不再先進先出、position/depth/holder 數字算錯（含「depth 不含執行中」這類 off-by-one）。
- enqueue_many 變成非原子（某個 key 已滿卻仍把其他 key 加進去），或佇列深度檢查被兩個請求同時繞過。
- release 不驗 task_id 而誤放他人、不冪等、忘了通知等待者、空佇列的 key 洩漏。
- wait_turn 不檢查取消、取消時沒把自己移出佇列、wake() 沒有真的喚醒（取消要等 poll 才生效）。
- 同一台 Gateway 兩個作業同時進入臨界區（破壞限速假設），或不同台被誤串成一條。
- 手動 URL 多一個斜線 / 大小寫不同就變成另一把鎖。

並行測試的原則：正確性絕不靠 sleep 判斷。全部用 Event / Barrier 同步，所有等待都有逾時，
逾時就 pytest.fail，不會卡住整個套件。少數「確認尚未放行」的反向檢查只用很短的 Event.wait，
正確實作下它不可能誤報，只是讓有 bug 的實作更容易被抓到。
"""
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import gateway_queue
from app.gateway_queue import (
    GatewayQueue,
    QueueFullError,
    gateway_key,
    normalize_gateway_url,
)

TIMEOUT = 5.0      # 所有等待的逾時秒數
BIG_POLL = 60.0    # 刻意設很大：證明靠 release()/wake() 的通知喚醒，而不是靠輪詢逾時


@pytest.fixture
def fast_thread_switching():
    """把 GIL 切換間隔壓到極小，讓執行緒更頻繁地交錯，放大競爭條件被觸發的機率（測完還原）。"""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(old)


# ── 工具 ───────────────────────────────────────────────

def _spawn(fn, *args) -> threading.Thread:
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def _join_all(threads, what="執行緒"):
    deadline = time.monotonic() + TIMEOUT
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    alive = [t for t in threads if t.is_alive()]
    if alive:
        pytest.fail(f"{what}逾時未結束（{len(alive)} 條仍在執行）")


class Waiter:
    """在背景執行緒呼叫 wait_turn，並記錄結果。

    waiting 事件在「第一次呼叫 cancelled()」時設起——此時執行緒持有佇列鎖、即將進入等待；
    之後主執行緒呼叫 release()/wake() 一定得等到對方真的進入 wait 才拿得到鎖，
    因此「通知是否真的有喚醒等待者」可以被確定性地驗證（不會因搶先而漏測）。
    """

    def __init__(self, q, key, task_id, poll=BIG_POLL, cancelled=None):
        self.q, self.key, self.task_id, self.poll = q, key, task_id, poll
        self.cancel_flag = threading.Event()
        self._cancelled = cancelled
        self.waiting = threading.Event()
        self.finished = threading.Event()
        self._ready = threading.Event()      # 開始檢查 或 已返回（不在佇列時根本不會呼叫 cancelled）
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _is_cancelled(self):
        self.waiting.set()
        self._ready.set()
        if self._cancelled is not None:
            return self._cancelled()
        return self.cancel_flag.is_set()

    def _run(self):
        try:
            self.result = self.q.wait_turn(self.key, self.task_id,
                                           cancelled=self._is_cancelled, poll=self.poll)
        except BaseException as e:      # noqa: BLE001 - 把例外帶回主執行緒
            self.error = e
        finally:
            self.finished.set()
            self._ready.set()

    def start(self):
        self.thread.start()
        if not self._ready.wait(TIMEOUT):
            pytest.fail(f"{self.task_id} 的 wait_turn 沒有開始檢查")
        return self

    def outcome(self):
        if not self.finished.wait(TIMEOUT):
            pytest.fail(f"{self.task_id} 的 wait_turn 逾時未返回（沒被喚醒？）")
        if self.error is not None:
            raise self.error
        return self.result

    def assert_not_returned_yet(self):
        # 反向檢查：正確實作下它不可能失敗，只用來抓「沒等到輪到就放行」的 bug
        assert not self.finished.wait(0.05), f"{self.task_id} 不該在輪到之前就返回"


# ── FIFO 與查詢 ────────────────────────────────────────

def test_fifo_order_positions_depth_holder():
    q = GatewayQueue()
    assert q.enqueue_many(["k"], "A") == {"k": 0}
    assert q.enqueue_many(["k"], "B") == {"k": 1}
    assert q.enqueue_many(["k"], "C") == {"k": 2}

    assert q.depth("k") == 3                      # 執行中 + 排隊中
    assert q.holder("k") == "A"                   # 頭＝執行中
    assert [q.position("k", t) for t in "ABC"] == [0, 1, 2]
    assert q.snapshot() == {"k": ["A", "B", "C"]}

    q.release("k", "A")
    assert q.holder("k") == "B"
    assert q.depth("k") == 2
    assert [q.position("k", t) for t in "BC"] == [0, 1]
    assert q.position("k", "A") is None


def test_queries_on_unknown_key_and_task():
    q = GatewayQueue()
    assert q.depth("nope") == 0
    assert q.holder("nope") is None
    assert q.position("nope", "A") is None
    assert q.snapshot() == {}
    q.enqueue_many(["k"], "A")
    assert q.position("k", "ghost") is None


def test_enqueue_many_returns_ahead_per_key():
    q = GatewayQueue()
    q.enqueue_many(["k1"], "x")
    q.enqueue_many(["k1"], "y")
    q.enqueue_many(["k3"], "z")
    ahead = q.enqueue_many(["k1", "k2", "k3"], "T")
    assert ahead == {"k1": 2, "k2": 0, "k3": 1}
    assert q.position("k1", "T") == 2
    assert q.position("k2", "T") == 0
    assert q.position("k3", "T") == 1


def test_enqueue_many_empty_keys_is_noop():
    q = GatewayQueue()
    assert q.enqueue_many([], "T") == {}
    assert q.snapshot() == {}


def test_snapshot_is_a_copy():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.enqueue_many(["k"], "B")
    snap = q.snapshot()
    snap["k"].append("EVIL")
    snap["other"] = ["X"]
    assert q.snapshot() == {"k": ["A", "B"]}
    assert q.depth("k") == 2


# ── enqueue_many：原子性、上限、重複 ────────────────────

def test_enqueue_many_is_atomic_when_one_key_is_full():
    q = GatewayQueue()
    q.enqueue_many(["k1"], "old1")
    q.enqueue_many(["k2"], "old2")
    q.enqueue_many(["k2"], "old3")          # k2 深度 2，已達上限
    before = q.snapshot()

    with pytest.raises(QueueFullError) as ei:
        q.enqueue_many(["k1", "k2", "k3"], "T", max_depth=2)

    assert ei.value.key == "k2"
    assert ei.value.depth == 2
    assert ei.value.limit == 2
    assert q.snapshot() == before            # k1、k3 都沒有被加入
    assert q.position("k1", "T") is None
    assert q.depth("k3") == 0


def test_max_depth_boundary_is_inclusive_of_running():
    q = GatewayQueue()
    # depth 1 < 2：可以再加一個（加完 depth 2）
    q.enqueue_many(["k"], "A", max_depth=2)
    q.enqueue_many(["k"], "B", max_depth=2)
    assert q.depth("k") == 2
    # depth 2 >= 2：已滿
    with pytest.raises(QueueFullError):
        q.enqueue_many(["k"], "C", max_depth=2)
    # 釋放一個後又能加
    q.release("k", "A")
    q.enqueue_many(["k"], "C", max_depth=2)
    assert q.snapshot() == {"k": ["B", "C"]}


def test_queue_full_error_reports_actual_depth_and_limit_separately():
    q = GatewayQueue()
    for i in range(4):
        q.enqueue_many(["k"], f"t{i}")           # 不設上限加到 4 個
    with pytest.raises(QueueFullError) as ei:
        q.enqueue_many(["k"], "T", max_depth=2)  # 上限調低到 2：depth 與 limit 不同
    assert (ei.value.key, ei.value.depth, ei.value.limit) == ("k", 4, 2)
    assert q.depth("k") == 4


def test_max_depth_none_means_unlimited():
    q = GatewayQueue()
    for i in range(50):
        q.enqueue_many(["k"], f"t{i}")
    assert q.depth("k") == 50


def test_duplicate_enqueue_raises_value_error_and_adds_nothing():
    q = GatewayQueue()
    q.enqueue_many(["k1"], "T")
    with pytest.raises(ValueError):
        q.enqueue_many(["k2", "k1"], "T")     # k1 重複
    assert q.snapshot() == {"k1": ["T"]}       # k2 沒有被加入


def test_duplicate_keys_in_list_are_deduped():
    q = GatewayQueue()
    ahead = q.enqueue_many(["k", "k", "k2", "k"], "T", max_depth=1)
    assert ahead == {"k": 0, "k2": 0}
    assert q.snapshot() == {"k": ["T"], "k2": ["T"]}
    assert q.depth("k") == 1


def test_queue_full_error_attributes():
    err = QueueFullError("gw:7", 5, 5)
    assert isinstance(err, Exception)
    assert (err.key, err.depth, err.limit) == ("gw:7", 5, 5)
    assert "gw:7" in str(err)


def test_concurrent_enqueue_many_respects_max_depth_exactly(fast_thread_switching):
    """30 個請求同時搶 2 個 key、上限 5：恰好 5 個全部成功，其餘全部整批失敗（兩個 key 一致）。"""
    q = GatewayQueue()
    start = threading.Barrier(30, timeout=TIMEOUT)
    outcome = {}
    lock = threading.Lock()

    def attempt(i):
        tid = f"t{i}"
        start.wait()
        try:
            q.enqueue_many(["a", "b"], tid, max_depth=5)
            res = "ok"
        except QueueFullError:
            res = "full"
        with lock:
            outcome[tid] = res

    _join_all([_spawn(attempt, i) for i in range(30)], "enqueue 競爭")
    winners = sorted(t for t, r in outcome.items() if r == "ok")
    assert len(winners) == 5
    assert len(outcome) == 30
    snap = q.snapshot()
    assert sorted(snap["a"]) == winners and sorted(snap["b"]) == winners
    assert snap["a"] == snap["b"]               # 兩條佇列的順序也一致（原子地一起加入）


# ── release ────────────────────────────────────────────

def test_release_is_idempotent_and_tolerates_unknowns():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.release("k", "A")
    q.release("k", "A")                 # 第二次：不報錯
    q.release("nope", "A")              # 不存在的 key
    q.release("k", "ghost")             # 不存在的 task
    assert q.snapshot() == {}


def test_release_by_non_owner_does_not_touch_others():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.enqueue_many(["k"], "B")
    q.release("k", "C")                 # C 不在佇列：不可誤放 A
    assert q.holder("k") == "A"
    assert q.snapshot() == {"k": ["A", "B"]}
    q.release("other", "A")             # 別的 key：不影響 k
    assert q.snapshot() == {"k": ["A", "B"]}


def test_release_queued_item_lets_followers_advance():
    q = GatewayQueue()
    for t in "ABC":
        q.enqueue_many(["k"], t)
    q.release("k", "B")                 # 排隊中的中間項目
    assert q.holder("k") == "A"         # 頭不變
    assert q.snapshot() == {"k": ["A", "C"]}
    assert q.position("k", "C") == 1    # C 遞補到 B 的位置
    q.release("k", "A")
    assert q.holder("k") == "C"


def test_release_removes_empty_key():
    q = GatewayQueue()
    q.enqueue_many(["k1", "k2"], "A")
    q.release("k1", "A")
    assert "k1" not in q.snapshot()
    assert q.snapshot() == {"k2": ["A"]}
    q.release("k2", "A")
    assert q.snapshot() == {}


def test_release_head_wakes_waiter_without_waiting_for_poll():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.enqueue_many(["k"], "B")
    w = Waiter(q, "k", "B").start()     # poll=60：只能靠 release 的通知喚醒
    w.assert_not_returned_yet()
    q.release("k", "A")
    assert w.outcome() is True
    assert q.holder("k") == "B"


def test_release_of_queued_item_wakes_nobody_wrongly():
    """排隊中的第三者離開，不能讓第二者提前變成頭。"""
    q = GatewayQueue()
    for t in "ABC":
        q.enqueue_many(["k"], t)
    wb = Waiter(q, "k", "B").start()
    q.release("k", "C")
    wb.assert_not_returned_yet()
    q.release("k", "A")
    assert wb.outcome() is True


# ── wait_turn ──────────────────────────────────────────

def test_wait_turn_returns_true_immediately_for_head():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    assert Waiter(q, "k", "A").start().outcome() is True
    assert q.holder("k") == "A"          # 回 True 不會動到佇列


def test_wait_turn_default_cancelled_argument_never_cancels():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    out = []

    def run():
        out.append(q.wait_turn("k", "A"))

    _join_all([_spawn(run)], "預設參數的 wait_turn")
    assert out == [True]


def test_wait_turn_for_missing_entry_returns_false():
    q = GatewayQueue()
    assert Waiter(q, "k", "A").start().outcome() is False            # key 不存在
    q.enqueue_many(["k"], "A")
    assert Waiter(q, "k", "ghost").start().outcome() is False        # key 在、task 不在
    q.release("k", "A")
    assert Waiter(q, "k", "A").start().outcome() is False            # 已被 release


def test_wait_turn_queued_waiter_proceeds_after_predecessor_releases():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.enqueue_many(["k"], "B")
    order = []
    w = Waiter(q, "k", "B").start()
    order.append("release")
    q.release("k", "A")
    assert w.outcome() is True
    order.append("acquired")
    assert order == ["release", "acquired"]


def test_wait_turn_cancelled_removes_self_and_returns_false_via_wake():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")           # 頭（持有中）
    q.enqueue_many(["k"], "B")
    w = Waiter(q, "k", "B").start()      # poll=60：沒有 wake 就會卡住
    w.assert_not_returned_yet()
    w.cancel_flag.set()                  # 呼叫端的順序：先設旗標，再 wake
    q.wake()
    assert w.outcome() is False
    assert q.snapshot() == {"k": ["A"]}   # B 已自行移出
    assert q.holder("k") == "A"


def test_wait_turn_cancelled_is_noticed_by_polling_even_without_wake():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.enqueue_many(["k"], "B")
    w = Waiter(q, "k", "B", poll=0.01).start()
    w.cancel_flag.set()                  # 故意不呼叫 wake()
    assert w.outcome() is False
    assert q.snapshot() == {"k": ["A"]}


def test_wait_turn_cancelled_head_is_removed_and_returns_false():
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    w = Waiter(q, "k", "A", cancelled=lambda: True).start()
    assert w.outcome() is False          # 即使已是頭，已取消就不執行
    assert q.snapshot() == {}


def test_cancel_removal_notifies_waiters_behind():
    """A 是頭但尚未開始等待就取消；B 在後面等，A 離開後 B 必須被通知而變成頭。"""
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.enqueue_many(["k"], "B")
    wb = Waiter(q, "k", "B").start()                               # poll=60
    wb.assert_not_returned_yet()
    wa = Waiter(q, "k", "A", cancelled=lambda: True).start()
    assert wa.outcome() is False
    assert wb.outcome() is True
    assert q.holder("k") == "B"


def test_wait_turn_cancel_check_is_atomic_with_wake():
    """cancelled 在持鎖時評估：重複 50 次「先設旗標再 wake」，等待者不可漏掉喚醒（poll=60）。"""
    for i in range(50):
        q = GatewayQueue()
        q.enqueue_many(["k"], "A")
        q.enqueue_many(["k"], "B")
        w = Waiter(q, "k", "B")
        w.thread.start()                 # 不等 waiting：讓旗標與等待者進入 wait 的時機隨機交錯
        w.cancel_flag.set()
        q.wake()
        assert w.outcome() is False, f"第 {i} 次漏掉喚醒"


# ── 並行正確性 ─────────────────────────────────────────

def test_stress_mutual_exclusion_and_fifo_on_one_key(fast_thread_switching):
    n = 20
    q = GatewayQueue()
    ids = [f"t{i}" for i in range(n)]
    for tid in ids:                       # 依序 enqueue＝請求到達順序
        q.enqueue_many(["k"], tid)

    guard = threading.Lock()
    inside = 0
    max_inside = 0
    entered = []
    results = {}

    def worker(tid):
        nonlocal inside, max_inside
        got = q.wait_turn("k", tid, poll=BIG_POLL)
        try:
            if got:
                with guard:
                    inside += 1
                    max_inside = max(max_inside, inside)
                    entered.append(tid)
                time.sleep(0)             # 讓出時間片，放大競爭；正確性不依賴它
                with guard:
                    inside -= 1
        finally:
            q.release("k", tid)
        results[tid] = got

    order = ids[:]
    random.Random(12345).shuffle(order)   # 執行緒啟動順序打亂：進入順序仍必須是 enqueue 順序
    _join_all([_spawn(worker, tid) for tid in order], "壓力測試")

    assert all(results[t] is True for t in ids)
    assert max_inside == 1                # 任一時刻最多 1 個在臨界區
    assert entered == ids                 # 嚴格 FIFO
    assert q.snapshot() == {}


def test_different_keys_enter_critical_section_concurrently():
    q = GatewayQueue()
    q.enqueue_many(["k1"], "A")
    q.enqueue_many(["k2"], "B")
    meet = threading.Barrier(2, timeout=TIMEOUT)    # 兩邊都在臨界區內才會通過
    results = {}

    def worker(key, tid):
        got = q.wait_turn(key, tid, poll=BIG_POLL)
        try:
            meet.wait()
            results[tid] = got
        except threading.BrokenBarrierError:
            results[tid] = "serialized"
        finally:
            q.release(key, tid)

    _join_all([_spawn(worker, "k1", "A"), _spawn(worker, "k2", "B")], "跨 key 並行")
    assert results == {"A": True, "B": True}
    assert q.snapshot() == {}


def test_same_key_does_not_enter_concurrently():
    """對照組：同一個 key 的兩個作業不能同時在臨界區（Barrier 一定會逾時）。"""
    q = GatewayQueue()
    q.enqueue_many(["k"], "A")
    q.enqueue_many(["k"], "B")
    meet = threading.Barrier(2, timeout=0.3)
    results = {}

    def worker(tid):
        got = q.wait_turn("k", tid, poll=BIG_POLL)
        try:
            meet.wait()
            results[tid] = "concurrent"
        except threading.BrokenBarrierError:
            results[tid] = "serialized"
        finally:
            q.release("k", tid)

    _join_all([_spawn(worker, "A"), _spawn(worker, "B")], "同 key 序列")
    assert results == {"A": "serialized", "B": "serialized"}


# ── URL 正規化與 key ───────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("http://gw:8080/", "http://gw:8080"),
    ("http://gw:8080", "http://gw:8080"),
    ("http://gw:8080///", "http://gw:8080"),
    ("HTTP://GW.Example.COM:8080", "http://gw.example.com:8080"),
    ("  http://gw  ", "http://gw"),
    ("\thttps://GW/\n", "https://gw"),
    ("http://GW/Api/V1/", "http://gw/Api/V1"),                      # path 保持原樣（大小寫）
    ("https://GW:57412/Path/To?Q=Abc", "https://gw:57412/Path/To?Q=Abc"),
    ("http://User:PaSs@GW:80/", "http://User:PaSs@gw:80"),          # 帳密保持原樣
    ("http://[::1]:8080/", "http://[::1]:8080"),
    ("GW:8080/", "gw:8080"),                                        # 無 scheme：host 轉小寫
    ("GW/Api", "gw/Api"),
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("/", ""),
])
def test_normalize_gateway_url(raw, expected):
    assert normalize_gateway_url(raw) == expected
    # 冪等
    assert normalize_gateway_url(expected) == expected


def test_gateway_key_prefers_gateway_id():
    assert gateway_key(3, "http://whatever") == "gw:3"
    assert gateway_key("7", None) == "gw:7"
    assert gateway_key(3, None) == gateway_key(3, "http://other")


def test_gateway_key_falls_back_to_normalized_url():
    assert gateway_key(None, "http://GW:8080/") == "url:http://gw:8080"
    assert gateway_key(None, "http://GW:8080/") == gateway_key(None, " http://gw:8080 ")
    assert gateway_key(0, "http://gw") == "url:http://gw"          # 0 為 falsy → 視為沒有 id
    assert gateway_key("", "http://gw") == "url:http://gw"
    assert gateway_key(None, None) == "url:"
    assert gateway_key(None, "http://a") != gateway_key(None, "http://b")


def test_url_spelling_variants_share_one_queue():
    """手動憑證的 URL 多一個斜線或大小寫不同，必須排在同一條佇列，而不是變成另一把鎖。"""
    q = GatewayQueue()
    k1 = gateway_key(None, "http://GW:8080/")
    k2 = gateway_key(None, "http://gw:8080")
    assert k1 == k2
    q.enqueue_many([k1], "A")
    assert q.enqueue_many([k2], "B") == {k2: 1}


# ── 模組獨立性 ─────────────────────────────────────────

def test_module_has_no_web_or_db_dependency():
    """app.gateway_queue 必須是純標準函式庫：import 它不可連帶載入 FastAPI / psycopg2 / app.main 等。

    在乾淨的子程序檢查（pytest 本身的 conftest 已 import 了 psycopg2，不能在本程序檢查）。
    """
    code = (
        "import sys\n"
        "import app.gateway_queue\n"
        "bad = [m for m in ('fastapi', 'starlette', 'psycopg2', 'app.main', 'app.pg_client') "
        "if m in sys.modules]\n"
        "sys.exit('unexpected imports: ' + ', '.join(bad) if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(gateway_queue.__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
