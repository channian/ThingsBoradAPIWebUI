# -*- coding: utf-8 -*-
"""對應 orig_429.py（429 退避，2026-07-14）：_retry_after_seconds 與
_kw_call_with_429_retry。純邏輯測試，不需要 DB（app.main 模組頂層 import 不會連線）。

防止的退化：Kepware API Gateway 是滑動視窗限速（60 次/分鐘），固定等待固定秒數
重試幾乎必定再次撞到限速；這裡驗證退避時間是依 429 回應的 Retry-After 算出來的，
而不是寫死的常數。
"""
import time

import pytest
import requests

import app.main as m

# app/main.py 頂層用 `import time` 後在函式內呼叫 time.sleep(...)，
# 所以直接 monkeypatch 這裡 import 的 time 模組（同一個模組物件）即可生效，
# monkeypatch fixture 會在測試結束後自動還原，不會影響其他測試。


class _FakeResp:
    """模擬 requests.Response 429 回應的最小介面：status_code / headers / json()"""

    def __init__(self, status_code=429, headers=None, json_body=None, json_raises=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_body = json_body or {}
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("invalid json")
        return self._json_body


def test_retry_after_seconds_reads_header():
    """優先讀取 Retry-After 標頭（與 Kepware GW 端 RateLimitMiddleware 的行為一致）"""
    resp = _FakeResp(headers={"Retry-After": "2"}, json_body={"detail": "請求過於頻繁", "retry_after": 2})
    assert m._retry_after_seconds(resp, 5.0) == 2.0


def test_retry_after_seconds_json_body_fallback():
    """沒有 Retry-After 標頭時，退而求其次讀 JSON body 的 retry_after 欄位"""
    resp = _FakeResp(headers={}, json_body={"retry_after": 7})
    assert m._retry_after_seconds(resp, 5.0) == 7.0


def test_retry_after_seconds_default_when_unparseable():
    """標頭與 JSON body 都拿不到值時（json() 直接拋錯），退回呼叫端提供的 default_wait"""
    resp = _FakeResp(headers={}, json_raises=True)
    assert m._retry_after_seconds(resp, 5.0) == 5.0


def test_retry_after_seconds_capped_at_120():
    """Retry-After 標頭給出誇張大的值時，要裁切到 120 秒上限，避免任務停滯過久"""
    resp = _FakeResp(headers={"Retry-After": "9999"}, json_body={})
    assert m._retry_after_seconds(resp, 5.0) == 120.0


class _FakeTask:
    """模擬 task_manager.Task 的最小介面：push_log 記錄呼叫"""

    def __init__(self):
        self.logs = []

    def push_log(self, level, msg):
        self.logs.append((level, msg))


def test_kw_call_with_429_retry_retries_then_succeeds(monkeypatch):
    """429 兩次後第三次成功：應重試、每次依 Retry-After 等待，且成功時回傳結果、
    記錄對應的警告 log。用 monkeypatch time.sleep 加速測試，但仍保留「確實依
    Retry-After 算出的秒數呼叫了 sleep」這個斷言，不讓測試因為加速而失去意義。"""
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleep_calls.append(secs))

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            err = requests.HTTPError("429")
            err.response = _FakeResp(headers={"Retry-After": "1"}, json_body={"retry_after": 1})
            raise err
        return "ok"

    task = _FakeTask()
    result = m._kw_call_with_429_retry(task, "Tag TEST", flaky, max_retries=3, default_wait=1)

    assert result == "ok"
    assert calls["n"] == 3
    assert len(task.logs) == 2, "應該記錄兩次 429 重試的警告 log"
    # Retry-After=1 秒，重試 2 次，確認等待秒數確實是依 Retry-After 算出的 1.0 秒，而非其他常數
    assert sleep_calls == [1.0, 1.0], f"重試等待秒數應依 Retry-After 計算，實際呼叫: {sleep_calls}"


def test_kw_call_with_429_retry_reraises_non_429(monkeypatch):
    """非 429 的錯誤（例如 409 已存在）要原樣往外拋，交由呼叫端處理，不能被吞掉或誤判成限速"""
    monkeypatch.setattr(time, "sleep", lambda secs: None)

    def conflict():
        err = requests.HTTPError("409")
        err.response = _FakeResp(status_code=409, headers={}, json_body={})
        raise err

    with pytest.raises(requests.HTTPError) as exc_info:
        m._kw_call_with_429_retry(_FakeTask(), "Tag X", conflict)
    assert exc_info.value.response.status_code == 409


def test_request_models_default_delay_within_rate_limit():
    """三個背景任務請求模型的 delay 預設值皆為 1.1 秒（約 48 筆/分鐘 < 60 次/分鐘限額），
    避免有人改動其中一個時忘了讓三者保持一致。"""
    assert m.ExecuteKwRequest().delay == 1.1
    assert m.ExecuteScaleRequest().delay == 1.1
    assert m.KwBatchDeleteRequest(upload_id="x").delay == 1.1
