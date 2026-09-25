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

背景任務在獨立執行緒呼叫這裡的方法，因此所有記錄操作都用 threading.Lock 保護，
確保 thread-safe（雖然 CPython 的 list.append 本身是原子操作，但「先查詢佇列、
再 pop 再 raise」這種複合操作不是原子的，仍需要鎖）。
"""
import threading

import requests


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


def make_fake_kepware_client(state: FakeKepwareState):
    """回傳一個類別，建構子簽章與 `app.kw_gw_client.KepwareGatewayClient` 相容
    （`KepwareGatewayClient(gw_url, verify=gw_verify)`），可直接
    `monkeypatch.setattr(app.main, "KepwareGatewayClient", make_fake_kepware_client(state))`
    取代掉背景任務內實際會呼叫的 Kepware client。"""

    class FakeKepwareClient:
        def __init__(self, base_url, verify=None, proxies=None):
            self.base_url = base_url
            self.verify = verify
            self.proxies = proxies
            self.token = None

        def login(self, username, password):
            with state._lock:
                state.login_calls.append((username, password))
            if state.login_error is not None:
                raise state.login_error
            self.token = "fake-token"
            return self.token

        def ensure_tag_groups(self, channel_name, device_name, group_path):
            with state._lock:
                state.ensure_tag_groups_calls.append({
                    "channel_name": channel_name,
                    "device_name": device_name,
                    "group_path": group_path,
                })

        def create_tag(self, channel_name, device_name, tag_name,
                        address=None, data_type=0, description=None,
                        tag_group=None):
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
            state._pop_and_raise(state._create_tag_errors, tag_name)
            return {"name": tag_name}

        def set_tag_scaling(self, config):
            with state._lock:
                state.set_tag_scaling_calls.append(dict(config))
            state._pop_and_raise(state._scaling_errors, config.get("tag_name"))
            return {"success": True}

        def delete_tag(self, channel_name, device_name, tag_name, tag_group=None):
            with state._lock:
                state.delete_tag_calls.append({
                    "channel_name": channel_name,
                    "device_name": device_name,
                    "tag_name": tag_name,
                    "tag_group": tag_group,
                })
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


def create_test_gateway(pg_client_obj, encrypt_password_fn, name="fake-gw"):
    """建立一個 DB 管理的 Gateway 紀錄。URL 是假的（不會真的被連線，因為
    KepwareGatewayClient 已被 monkeypatch 掉），密碼用呼叫端提供的加密函式
    （通常是 `app.main._encrypt_password`）加密後存入。"""
    return pg_client_obj.create_gateway(
        name=name,
        url="http://127.0.0.1:1",  # 連線埠 1：就算沒 monkeypatch 也會立刻 connection refused
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


def wait_lock_released(gw_module, lock_key, timeout=5.0, interval=0.02):
    """等待 Gateway 鎖被釋放。背景函式是先 push_complete（done=True，並執行
    on_complete 寫歷史紀錄）、之後才在 finally 釋放鎖，所以 poll_task 看到
    done 的當下鎖可能還沒放掉——直接斷言會間歇性失敗。回傳是否在時限內釋放。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lock_key not in gw_module._gateway_locks:
            return True
        time.sleep(interval)
    return False
