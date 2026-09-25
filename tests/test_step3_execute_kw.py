# -*- coding: utf-8 -*-
"""Step 3（POST /api/kw/execute）背景任務的**行為測試**：用假的
KepwareGatewayClient 取代真實 client，驅動 `app.main` 內 `_exec_kw_execute`
這條真正會被執行到的程式碼路徑——建點迴圈、tag group 預建、409/429/500 錯誤處理、
tb_status 狀態更新、429 重試在真實流程中是否真的有接上、login 失敗時的
try/finally 鎖釋放。

在此之前，Step 3 完全沒有任何行為測試：只有 `tests/test_data_type.py` 對
`create_tag(...)` 呼叫是否帶 `data_type` 關鍵字做 AST 靜態檢查，以及
`tests/test_gateway_lock.py` 對 try/finally 結構做 AST 靜態檢查，兩者都不會
實際執行迴圈本體。
"""
import pytest
import requests

import app.main as m
from tests.fake_kepware import (
    wait_lock_released,
    FakeKepwareState,
    create_test_gateway,
    http_error,
    insert_staging_row,
    make_fake_kepware_client,
    poll_task,
)


@pytest.fixture(autouse=True)
def _reset_gateway_locks():
    m._gateway_locks.clear()
    yield
    m._gateway_locks.clear()


def _tb_status_of(pg_client_obj, tag_name):
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tb_status FROM scada_tag_config WHERE tag_name = %s",
                (tag_name,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _insert_pending_row(pg_client_obj, tag_name, io_address, **overrides):
    common = dict(
        tag_name=tag_name, site="K18", system_code="CHS", scada_node_name="K18CHS",
        io_device="OPC_UA", io_address=io_address,
        device_profile="CH1-DEV1-2F-CHS",  # → channel=CH1 device=DEV1 group=2F.CHS
        tb_status="pending",
    )
    common.update(overrides)
    insert_staging_row(pg_client_obj, **common)


def _base_execute_body(gateway_id):
    return {"gateway_id": gateway_id, "delay": 0, "batch_size": 50, "batch_pause": 0}


# ── 6 + 7：data_type / address / tag_group 推導、ensure_tag_groups 去重 ──

def test_step3_create_tag_payload_and_group_dedup(api_client, admin_headers, monkeypatch):
    """驗證：

    6. 每筆 create_tag 都帶 data_type（未覆寫用預設 8，覆寫的用覆寫值）；
       OPC_UA 的 address 會轉成 `ns=2;s=<io_address>`；tag_group 符合推導結果。
    7. ensure_tag_groups 對不重複的 (channel, device, group) 只各呼叫一次
       ——三筆 tag 共用 (CH1, DEV1, 2F.CHS)，只應該呼叫一次；
       另一筆用不同 group (CH1, DEV1, 3F.CHS)，該組合也只呼叫一次。
    """
    _insert_pending_row(m.pg_client, "T1", "PLC1.T1")
    _insert_pending_row(m.pg_client, "T2", "PLC1.T2")
    _insert_pending_row(m.pg_client, "T3", "PLC1.T3", device_profile="CH1-DEV1-3F-CHS")
    _insert_pending_row(m.pg_client, "T4", "PLC1.T4", kw_data_type=1)

    state = FakeKepwareState()
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))
    gw = create_test_gateway(m.pg_client, m._encrypt_password, name="step3-gw-1")

    resp = api_client.post("/api/kw/execute", json=_base_execute_body(gw["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])

    assert result["summary"]["success"] == 4, result["summary"]
    assert result["summary"]["fail"] == 0, result["summary"]

    calls = {c["tag_name"]: c for c in state.create_tag_calls}
    assert calls["T1"]["data_type"] == 8
    assert calls["T2"]["data_type"] == 8
    assert calls["T3"]["data_type"] == 8
    assert calls["T4"]["data_type"] == 1, "T4 有 kw_data_type 覆寫，應該帶覆寫值 1"

    assert calls["T1"]["address"] == "ns=2;s=PLC1.T1", "OPC_UA 應轉成 ns=2;s=<io_address> 格式"
    assert calls["T1"]["tag_group"] == "2F.CHS"
    assert calls["T3"]["tag_group"] == "3F.CHS"

    # 7. ensure_tag_groups 去重：只有 2 個不重複的 (channel, device, group) 組合
    group_keys = {
        (c["channel_name"], c["device_name"], c["group_path"])
        for c in state.ensure_tag_groups_calls
    }
    assert group_keys == {("CH1", "DEV1", "2F.CHS"), ("CH1", "DEV1", "3F.CHS")}, (
        f"ensure_tag_groups 呼叫的 (channel, device, group) 組合不符預期: {group_keys}"
    )
    assert len(state.ensure_tag_groups_calls) == 2, (
        f"ensure_tag_groups 應該對不重複組合各只呼叫一次，實際呼叫次數: "
        f"{len(state.ensure_tag_groups_calls)}，內容: {state.ensure_tag_groups_calls}"
    )

    for tag in ("T1", "T2", "T3", "T4"):
        assert _tb_status_of(m.pg_client, tag) == "done"


# ── 8：409 視為成功、500 視為失敗 ──

def test_step3_409_treated_as_success_500_treated_as_failure(api_client, admin_headers, monkeypatch):
    """8. Kepware 對某筆回 409（已存在）→ 視為成功、tb_status=done；
    對某筆回 500 → 記為失敗、tb_status 維持 pending、summary.errors 含該筆。"""
    _insert_pending_row(m.pg_client, "T_409", "PLC1.T409")
    _insert_pending_row(m.pg_client, "T_500", "PLC1.T500")

    state = FakeKepwareState()
    state.queue_create_tag_error("T_409", http_error(409))
    state.queue_create_tag_error("T_500", http_error(500))
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))
    gw = create_test_gateway(m.pg_client, m._encrypt_password, name="step3-gw-2")

    resp = api_client.post("/api/kw/execute", json=_base_execute_body(gw["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])

    summary = result["summary"]
    assert summary["success"] == 1, summary
    assert summary["fail"] == 1, summary
    assert any(e["name"] == "T_500" for e in summary["errors"]), summary["errors"]

    assert _tb_status_of(m.pg_client, "T_409") == "done", "409（已存在）應視為成功"
    assert _tb_status_of(m.pg_client, "T_500") == "pending", "500 失敗不該把 tb_status 標成 done"


# ── 9：429 重試一次後成功 ──

def test_step3_429_retry_then_success(api_client, admin_headers, monkeypatch):
    """9. 第一次呼叫回 429（帶 Retry-After: 1），第二次成功 → 最終視為成功，
    驗證 `_kw_call_with_429_retry` 在真實 Step 3 流程中真的有接上（而不是只在
    `tests/test_retry_429.py` 裡對這個函式本身做單元測試）。"""
    _insert_pending_row(m.pg_client, "T_429", "PLC1.T429")

    state = FakeKepwareState()
    state.queue_create_tag_error("T_429", http_error(429, retry_after=1))
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))
    gw = create_test_gateway(m.pg_client, m._encrypt_password, name="step3-gw-3")

    resp = api_client.post("/api/kw/execute", json=_base_execute_body(gw["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    # 429 重試會真的 sleep 1 秒（Retry-After），逾時保護拉長一些
    result = poll_task(api_client, admin_headers, resp.json()["task_id"], timeout=15.0)

    summary = result["summary"]
    assert summary["success"] == 1, summary
    assert summary["fail"] == 0, summary
    # create_tag 應該被呼叫兩次：第一次 429、第二次成功
    calls = [c for c in state.create_tag_calls if c["tag_name"] == "T_429"]
    assert len(calls) == 2, f"429 重試後應該再呼叫一次 create_tag，實際呼叫次數: {len(calls)}"
    assert _tb_status_of(m.pg_client, "T_429") == "done"


# ── 10：login 失敗時任務仍正常結束、鎖有釋放 ──

def test_step3_login_failure_completes_task_and_releases_lock(api_client, admin_headers, monkeypatch):
    """10. Kepware 登入失敗時：任務仍應正常結束（done=True、全部計為失敗），
    且 Gateway 的鎖必須被釋放——這是 try/finally 的**執行期**驗證，
    與 `tests/test_gateway_lock.py` 裡的 AST 靜態檢查互補（AST 只能確認程式碼
    「長得像」try/finally，無法確認 finally 真的在例外路徑上被執行到）。"""
    _insert_pending_row(m.pg_client, "T_LOGIN_FAIL", "PLC1.LF")

    state = FakeKepwareState()
    state.login_error = requests.exceptions.ConnectionError("模擬連線失敗")
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))
    gw = create_test_gateway(m.pg_client, m._encrypt_password, name="step3-gw-4")

    resp = api_client.post("/api/kw/execute", json=_base_execute_body(gw["id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])

    assert result["done"] is True
    summary = result["summary"]
    assert summary["success"] == 0, summary
    assert summary["fail"] == 1, summary
    assert _tb_status_of(m.pg_client, "T_LOGIN_FAIL") == "pending"
    assert len(state.login_calls) == 1, "login 應該被呼叫過一次（雖然失敗）"
    assert not state.create_tag_calls, "登入都失敗了，不該有任何 create_tag 呼叫"

    gw_key = m._gateway_lock_key(gw["id"], gw["url"])
    assert wait_lock_released(m, gw_key), "login 失敗的例外路徑上，鎖也必須被釋放"
