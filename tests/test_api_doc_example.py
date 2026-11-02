# -*- coding: utf-8 -*-
"""API_SCHEMA.md 的 Python 輪詢範例 `run_and_wait` 實際執行驗證。

文件範例若寫錯（例如又加回 409 分支、少處理 429 / 422），整合方照抄就會踩雷，所以直接從
API_SCHEMA.md 把範例程式碼抽出來執行：
- 對真正的應用程式（TestClient + 假 Kepware）跑：一般完成、排隊（印出提示後照常輪詢）、
  422（直接報錯）、429（有限次重送後報錯）。
- 對罐頭回應跑：429 兩次後成功、輪詢遇到 404（任務遺失）。
等待時間（poll_interval / full_wait）都傳小值或換成假的 sleep，不會真的睡。
"""
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.main as m
from tests.exec_helpers import (
    make_gateway, post_kw, row_id, seed_kw_row,
)
from tests.fake_kepware import make_fake_kepware_client, wait_until

DOC = Path(__file__).resolve().parent.parent / "API_SCHEMA.md"
BASE = "http://testserver"


def _load_example():
    """從 API_SCHEMA.md 抽出 `run_and_wait` 範例並編譯成函式；回傳 (函式, 命名空間)。"""
    text = DOC.read_text(encoding="utf-8")
    anchor = text.index("**Python 輪詢範例**")
    match = re.search(r"```python\n(.*?)```", text[anchor:], re.S)
    assert match, "API_SCHEMA.md 找不到 Python 輪詢範例"
    code = match.group(1)
    assert "def run_and_wait(" in code
    namespace: dict = {}
    exec(compile(code, "API_SCHEMA.md:run_and_wait", "exec"), namespace)    # noqa: S102
    return namespace["run_and_wait"], namespace


def test_doc_example_has_no_409_branch():
    """範例不再處理 409 / max_busy_retries（已改為排隊），改處理 queued / 429 / 422。"""
    text = DOC.read_text(encoding="utf-8")
    code = re.search(r"```python\n(.*?)```", text[text.index("**Python 輪詢範例**"):], re.S).group(1)
    assert "== 409" not in code and "max_busy_retries" not in code and "busy_task_id" not in code
    for needle in ("queued", "429", "422", "404"):
        assert needle in code, f"範例應處理 {needle}"


class _TestClientRequests:
    """把 TestClient 包成 `requests` 的最小介面（post / get），讓範例對真的應用程式執行。"""

    def __init__(self, client):
        self._client = client

    @staticmethod
    def _path(url):
        return "/api/" + url.split("/api/", 1)[1]

    def post(self, url, headers=None, json=None):
        return self._client.post(self._path(url), headers=headers, json=json)

    def get(self, url, headers=None):
        return self._client.get(self._path(url), headers=headers)


def _bind(namespace, client, **sleeps):
    namespace["requests"] = _TestClientRequests(client)
    real_sleep = time.sleep
    namespace["time"] = SimpleNamespace(sleep=lambda s: real_sleep(min(s, 0.01)))


def _body(**kw):
    body = {"delay": 0, "batch_size": 50, "batch_pause": 0}
    body.update(kw)
    return body


def test_example_completes_a_normal_task(api_client, admin_headers, monkeypatch, make_state):
    run_and_wait, ns = _load_example()
    _bind(ns, api_client)
    make_gateway("doc-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))

    summary = run_and_wait(f"{BASE}/api/kw/execute", _body(), admin_headers, poll_interval=0.01)
    assert (summary["success"], summary["fail"], summary["total"]) == (2, 0, 2)
    assert summary["cancelled"] is False and summary["gateways"][0]["name"] == "doc-gw"


def test_example_waits_in_queue_and_prints_notice(api_client, admin_headers, monkeypatch, make_state, capsys):
    """該 Gateway 忙碌：範例收到 200 + queued=true，印出提示，照常輪詢到完成（不重送、不報錯）。"""
    run_and_wait, ns = _load_example()
    _bind(ns, api_client)
    gw = make_gateway("doc-queue-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))
    first = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")])
    assert first.status_code == 200
    gate.wait_entered()

    # 第二個請求由範例送出（會排隊）。另一條執行緒只負責「等佇列裡出現第二個任務後放行」，
    # 不碰 TestClient（只操作 Event 與記憶體佇列），所以不會與主執行緒搶用用戶端。
    key = f"gw:{gw['id']}"
    opener = threading.Thread(
        target=lambda: (wait_until(lambda: len(m.gateway_queue.snapshot().get(key, [])) == 2,
                                   "第二個任務進入佇列"), gate.open()),
        daemon=True)
    opener.start()
    summary = run_and_wait(f"{BASE}/api/kw/execute", _body(gateway_id=gw["id"], ids=[row_id("T2")]),
                           admin_headers, poll_interval=0.01)
    opener.join(timeout=10)
    assert summary["success"] == 1 and summary["fail"] == 0, summary
    assert "已進入佇列" in capsys.readouterr().out
    assert [c["tag_name"] for c in state.create_tag_calls] == ["T1", "T2"]


def test_example_raises_on_422_without_retrying(api_client, admin_headers, monkeypatch, make_state, safety_env):
    run_and_wait, ns = _load_example()
    _bind(ns, api_client)
    safety_env(KIS_LIMIT_KW_EXECUTE=1)
    make_gateway("doc-422-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    before = len(m.task_manager.tasks)
    with pytest.raises(RuntimeError, match="批次超過安全鎖上限"):
        run_and_wait(f"{BASE}/api/kw/execute", _body(), admin_headers, poll_interval=0.01)
    assert len(m.task_manager.tasks) == before, "422 不重試、也不建立任務"


def test_example_gives_up_after_limited_429_retries(api_client, admin_headers, monkeypatch, make_state, safety_env):
    run_and_wait, ns = _load_example()
    _bind(ns, api_client)
    safety_env(KIS_LIMIT_QUEUE_PER_GATEWAY=1)
    gw = make_gateway("doc-429-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    state.add_gate("create_tag", "T1")                 # 讓第一個任務一直佔住佇列（teardown 會放行）
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))
    first = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")])
    assert first.status_code == 200

    posts = []
    real_post = ns["requests"].post

    def counting_post(url, headers=None, json=None):
        resp = real_post(url, headers=headers, json=json)
        posts.append(resp.status_code)
        return resp

    ns["requests"].post = counting_post
    with pytest.raises(RuntimeError, match="佇列持續已滿"):
        run_and_wait(f"{BASE}/api/kw/execute", _body(gateway_id=gw["id"], ids=[row_id("T2")]),
                     admin_headers, poll_interval=0.01, full_wait=0, max_full_retries=3)
    assert posts == [429, 429, 429, 429], "最多送 1 + max_full_retries 次，不是無上限重試"


# ── 罐頭回應：不需要資料庫 ────────────────────────────────────────

class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Scripted:
    """依序回放罐頭回應的假 requests"""

    def __init__(self, posts, gets):
        self.posts, self.gets = list(posts), list(gets)
        self.post_count = 0

    def post(self, url, headers=None, json=None):
        self.post_count += 1
        return self.posts.pop(0)

    def get(self, url, headers=None):
        return self.gets.pop(0)


def test_example_retries_429_then_succeeds_and_reports_blocked(capsys):
    run_and_wait, ns = _load_example()
    sleeps = []
    ns["time"] = SimpleNamespace(sleep=sleeps.append)
    ok = {"task_id": "abc", "queued": False, "blocked": 2, "message": "m"}
    ns["requests"] = _Scripted(
        posts=[_Resp(429, {"detail": "滿"}), _Resp(429, {"detail": "滿"}), _Resp(200, ok)],
        gets=[_Resp(200, {"done": False}), _Resp(200, {"done": True, "summary": {"fail": 2}})])
    summary = run_and_wait("http://x/api/kw/execute", {}, {}, poll_interval=1, full_wait=7)
    assert summary == {"fail": 2}
    assert ns["requests"].post_count == 3
    assert sleeps == [7, 7, 1], "429 各等 full_wait 秒再重送，輪詢間隔用 poll_interval"
    assert "2 筆無法判斷" in capsys.readouterr().out


def test_example_reports_lost_task_when_polling_returns_404():
    run_and_wait, ns = _load_example()
    ns["time"] = SimpleNamespace(sleep=lambda s: None)
    ns["requests"] = _Scripted(posts=[_Resp(200, {"task_id": "gone", "queued": False})],
                               gets=[_Resp(404)])
    with pytest.raises(RuntimeError, match="已遺失"):
        run_and_wait("http://x/api/kw/execute", {}, {}, poll_interval=1)


def test_example_times_out_if_the_task_never_finishes():
    run_and_wait, ns = _load_example()
    ns["time"] = SimpleNamespace(sleep=lambda s: None)
    ns["requests"] = _Scripted(posts=[_Resp(200, {"task_id": "slow", "queued": True, "message": "q"})],
                               gets=[_Resp(200, {"done": False})] * 10)
    with pytest.raises(TimeoutError):
        run_and_wait("http://x/api/kw/execute", {}, {}, poll_interval=1, timeout=3)
