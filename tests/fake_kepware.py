# -*- coding: utf-8 -*-
"""假的 Kepware API Gateway Client，供 Step 3 (`/api/kw/execute`)、
Step 5 (`/api/pg/execute/scale`) 與批次刪除 (`/api/kw/delete-batch`) 的
**行為測試**使用：取代 `app.main.KepwareGatewayClient`，讓背景任務的真實程式碼
（建點迴圈、Scale 迴圈、狀態欄更新、429 重試、Gateway 鎖釋放）實際被執行到，
而不是像 `tests/test_data_type.py::test_step5_skips_tags_with_scale_disabled`
那樣只靜態比對原始碼字串（改成 `if False:` 之類的邏輯性退化，靜態字串比對抓不到）。

用法：
    state = FakeKepwareState()
    monkeypatch.setattr(app.main, "KepwareGatewayClient", make_fake_kepware_client(state))
    # ... 呼叫 /api/kw/execute 或 /api/pg/execute/scale ...
    assert state.create_tag_calls == [...]

多台 Gateway（依 URL 區分各自的狀態）：
    monkeypatch.setattr(app.main, "KepwareGatewayClient",
                        make_fake_kepware_client({"http://gw-a.test:1": state_a,
                                                  "http://gw-b.test:1": state_b}))

製造「執行中」狀態（用 threading.Event 卡住某個呼叫，不靠睡眠時間）：
    gate = state.add_gate("create_tag", tag_name="T2")   # 或 "delete_tag" / "set_tag_scaling"
    ... 送出請求 ...
    gate.wait_entered()      # 背景任務已經走進 create_tag("T2")，確定處於「執行中」
    ...                      # 此時可以送第二個請求（會排隊）、取消、查詢佇列...
    gate.open()              # 放行；之後的呼叫不再卡住
測試一定要在 finally 裡 gate.open()，否則背景執行緒會一直卡到逾時。

背景任務在獨立執行緒呼叫這裡的方法，因此所有記錄操作都用 threading.Lock 保護，
確保 thread-safe（雖然 CPython 的 list.append 本身是原子操作，但「先查詢佇列、
再 pop 再 raise」這種複合操作不是原子的，仍需要鎖）。
"""
import threading
import time

import pytest
import requests

from app.gateway_queue import normalize_gateway_url


# ── 模擬 Kepware API 錯誤回應 ─────────────────────────────────────

class _FakeResponse:
    """模擬 requests.Response 的最小介面：status_code / headers / json() / text。"""

    def __init__(self, status_code, headers=None, json_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_body = json_body if json_body is not None else {}
        self.text = str(self._json_body)

    def json(self):
        return self._json_body


def http_error(status_code, retry_after=None, detail=None):
    """建立一個帶 `.response` 的 `requests.HTTPError`，模擬 Kepware API Gateway
    回應 409（已存在）/ 429（限速，可帶 Retry-After）/ 500（伺服器錯誤）等狀況。"""
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    body = {"detail": detail or f"HTTP {status_code}"}
    if retry_after is not None:
        body["retry_after"] = retry_after
    err = requests.HTTPError(f"{status_code} Error")
    err.response = _FakeResponse(status_code, headers=headers, json_body=body)
    return err


# ── 可卡住的呼叫（製造「執行中」狀態）─────────────────────────────

class CallGate:
    """卡住假 client 的某個呼叫。entered 在背景執行緒走進該呼叫時設起；
    open() 之後所有（含之後的）呼叫都直接通過。背景執行緒內的等待有逾時，
    逾時丟 AssertionError（該筆記為失敗，不會讓測試套件掛住）。"""

    def __init__(self, hold_timeout=10.0):
        self.entered = threading.Event()
        self._open = threading.Event()
        self._hold_timeout = hold_timeout
        self.hits = 0
        self._hits_lock = threading.Lock()

    def wait_entered(self, timeout=5.0):
        """測試端等待背景執行緒走進被卡住的呼叫；逾時直接讓測試失敗。"""
        if not self.entered.wait(timeout):
            pytest.fail(f"等待背景任務走進被卡住的呼叫逾時（{timeout} 秒）")

    def open(self):
        self._open.set()

    def _pass(self):
        with self._hits_lock:
            self.hits += 1
        self.entered.set()
        if not self._open.wait(self._hold_timeout):
            raise AssertionError(f"CallGate 超過 {self._hold_timeout} 秒仍未被 open()")


# ── 共用狀態容器：記錄呼叫 + 可佇列要拋出的例外 ─────────────────────

class FakeKepwareState:
    """一次測試共用的狀態：記錄所有方法呼叫的參數，並可依 key（通常是 tag_name）
    佇列要依序拋出的例外（例如：第一次 429、第二次成功）。佇列耗盡或從未設定時，
    呼叫一律視為成功。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.login_calls = []
        self.login_error = None  # 設定後，login() 一律拋出這個例外
        self.ensure_tag_groups_calls = []
        self.create_tag_calls = []
        self.set_tag_scaling_calls = []
        self.delete_tag_calls = []
        self._create_tag_errors = {}
        self._scaling_errors = {}
        self._delete_tag_errors = {}
        self._gates = {}

    # ── 卡住呼叫（製造「執行中」）──

    def add_gate(self, method, tag_name=None, hold_timeout=10.0):
        """卡住 method（login / create_tag / set_tag_scaling / delete_tag）的呼叫。
        tag_name 為 None＝任何 tag 都卡（login 只能用 None）；set_tag_scaling 的 tag_name
        是 Kepware 全名（含 tag group，例如 "2F.CHS.STEP5_A"）。回傳 CallGate。"""
        gate = CallGate(hold_timeout)
        with self._lock:
            self._gates[(method, tag_name)] = gate
        return gate

    def open_all_gates(self):
        """放行這份狀態上所有的 CallGate（測試收尾用，避免失敗的測試把背景執行緒卡在 gate 上）。"""
        with self._lock:
            gates = list(self._gates.values())
        for gate in gates:
            gate.open()

    def _wait_gate(self, method, key):
        with self._lock:
            gate = self._gates.get((method, key)) or self._gates.get((method, None))
        if gate is not None:
            gate._pass()

    # ── 設定要拋出的例外序列 ──

    def queue_create_tag_error(self, tag_name, *errors):
        """為指定 tag_name 的 create_tag() 呼叫依序佇列例外；用完後改回成功。"""
        with self._lock:
            self._create_tag_errors.setdefault(tag_name, []).extend(errors)

    def queue_scaling_error(self, tag_name, *errors):
        with self._lock:
            self._scaling_errors.setdefault(tag_name, []).extend(errors)

    def queue_delete_tag_error(self, tag_name, *errors):
        with self._lock:
            self._delete_tag_errors.setdefault(tag_name, []).extend(errors)

    # ── 內部：依序取出並拋出（若佇列已空則視為成功、不拋出）──

    def _pop_and_raise(self, table, key):
        with self._lock:
            queue = table.get(key)
            err = queue.pop(0) if queue else None
        if err is not None:
            raise err


def make_fake_kepware_client(state_or_map):
    """回傳一個類別，建構子簽章與 `app.kw_gw_client.KepwareGatewayClient` 相容
    （`KepwareGatewayClient(gw_url, verify=gw_verify)`），可直接
    `monkeypatch.setattr(app.main, "KepwareGatewayClient", make_fake_kepware_client(state))`
    取代掉背景任務內實際會呼叫的 Kepware client。

    state_or_map：
      - 單一 FakeKepwareState：所有 URL 共用同一份狀態（單台 Gateway 的測試）。
      - dict {url: FakeKepwareState}：依 base_url 區分（兩台以上 Gateway 的測試）；
        比對前用 normalize_gateway_url 正規化（尾斜線、大小寫不影響）；
        找不到的 URL 在建構時丟 ConnectionError（等同連不上該 Gateway）。
    """
    if isinstance(state_or_map, FakeKepwareState):
        resolve = lambda url: state_or_map  # noqa: E731
    else:
        by_url = {normalize_gateway_url(u): st for u, st in state_or_map.items()}

        def resolve(url):
            st = by_url.get(normalize_gateway_url(url))
            if st is None:
                raise requests.exceptions.ConnectionError(f"假 Kepware：未知的 Gateway URL {url}")
            return st

    class FakeKepwareClient:
        def __init__(self, base_url, verify=None, proxies=None):
            self.base_url = base_url
            self.verify = verify
            self.proxies = proxies
            self.token = None
            self.state = resolve(base_url)

        def login(self, username, password):
            state = self.state
            with state._lock:
                state.login_calls.append((username, password))
            state._wait_gate("login", None)
            if state.login_error is not None:
                raise state.login_error
            self.token = "fake-token"
            return self.token

        def ensure_tag_groups(self, channel_name, device_name, group_path):
            state = self.state
            with state._lock:
                state.ensure_tag_groups_calls.append({
                    "channel_name": channel_name,
                    "device_name": device_name,
                    "group_path": group_path,
                })

        def create_tag(self, channel_name, device_name, tag_name,
                        address=None, data_type=0, description=None,
                        tag_group=None):
            state = self.state
            with state._lock:
                state.create_tag_calls.append({
                    "channel_name": channel_name,
                    "device_name": device_name,
                    "tag_name": tag_name,
                    "address": address,
                    "data_type": data_type,
                    "description": description,
                    "tag_group": tag_group,
                })
            state._wait_gate("create_tag", tag_name)
            state._pop_and_raise(state._create_tag_errors, tag_name)
            return {"name": tag_name}

        def set_tag_scaling(self, config):
            state = self.state
            with state._lock:
                state.set_tag_scaling_calls.append(dict(config))
            state._wait_gate("set_tag_scaling", config.get("tag_name"))
            state._pop_and_raise(state._scaling_errors, config.get("tag_name"))
            return {"success": True}

        def delete_tag(self, channel_name, device_name, tag_name, tag_group=None):
            state = self.state
            with state._lock:
                state.delete_tag_calls.append({
                    "channel_name": channel_name,
                    "device_name": device_name,
                    "tag_name": tag_name,
                    "tag_group": tag_group,
                })
            state._wait_gate("delete_tag", tag_name)
            state._pop_and_raise(state._delete_tag_errors, tag_name)
            return {"success": True}

    return FakeKepwareClient


# ── 共用小工具 ─────────────────────────────────────────────────

def insert_staging_row(pg_client_obj, **kw):
    """依關鍵字參數動態組出 INSERT 語句寫入 scada_tag_config，供各測試依需求
    只指定自己在意的欄位（其餘欄位吃資料庫預設值）。"""
    cols = ", ".join(kw.keys())
    placeholders = ", ".join(["%s"] * len(kw))
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO scada_tag_config ({cols}) VALUES ({placeholders})",
                list(kw.values()),
            )


def create_test_gateway(pg_client_obj, encrypt_password_fn, name="fake-gw",
                        url="http://127.0.0.1:1"):
    """建立一個 DB 管理的 Gateway 紀錄。URL 是假的（不會真的被連線，因為
    KepwareGatewayClient 已被 monkeypatch 掉），密碼用呼叫端提供的加密函式
    （通常是 `app.main._encrypt_password`）加密後存入。兩台以上 Gateway 的測試要傳
    不同的 url，才能讓假 client 依 URL 區分各自的 FakeKepwareState。"""
    return pg_client_obj.create_gateway(
        name=name,
        url=url,  # 預設連線埠 1：就算沒 monkeypatch 也會立刻 connection refused
        username="fake-user",
        password_enc=encrypt_password_fn("fake-pass"),
        verify_ssl=False,
    )


def poll_task(api_client, headers, task_id, timeout=10.0, interval=0.05):
    """輪詢 GET /api/tasks/{task_id} 直到 done=True 或逾時（預設 10 秒保護，
    避免背景執行緒卡住時測試無限期掛住）。回傳最後一次的回應 JSON。"""
    import time
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = api_client.get(f"/api/tasks/{task_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["done"]:
            return last
        time.sleep(interval)
    raise AssertionError(f"任務 {task_id} 在 {timeout} 秒內未完成，最後狀態: {last}")


def wait_lock_released(gw_module, lock_key=None, timeout=5.0, interval=0.02):
    """（沿用舊名）確認 Gateway 佇列已釋放；回傳是否在時限內釋放。

    新設計（app/gateway_jobs.py）是每個 part 結束時「先 release 佇列、最後一個 part 才
    push_complete」，所以 poll_task 看到 done 的當下佇列必定已空，第一次檢查就會成立，
    不再像舊的 `_gateway_locks`（先 push_complete、finally 才釋放）需要輪詢等待。
    保留短逾時只是為了萬一實作退化時失敗而不是卡死。
    lock_key 為 None＝整個佇列都要是空的；否則只看該 key（例如 gateway_key(gid, url)）。"""
    deadline = time.time() + timeout
    while True:
        snapshot = gw_module.gateway_queue.snapshot()
        released = (not snapshot) if lock_key is None else (lock_key not in snapshot)
        if released:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def wait_until(predicate, what="條件", timeout=5.0, interval=0.005):
    """輪詢直到 predicate() 為真；逾時直接讓測試失敗（所有並行等待都要有逾時保護）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"等待{what}逾時（{timeout} 秒）")
