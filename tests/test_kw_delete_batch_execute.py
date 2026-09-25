# -*- coding: utf-8 -*-
"""批次刪除（POST /api/kw/delete-batch，`dry_run=false`）的行為測試：用假的
KepwareGatewayClient 驅動 `_exec_kw_delete` 這條背景任務程式碼路徑，驗證
delete_tag 真的被呼叫、Kepware 回 404（tag 不存在）視為成功、任務結束後
Gateway 鎖確實釋放。"""
import io

import pytest

import app.main as m
from tests.fake_kepware import (
    wait_lock_released,
    FakeKepwareState,
    create_test_gateway,
    http_error,
    make_fake_kepware_client,
    poll_task,
)


@pytest.fixture(autouse=True)
def _reset_gateway_locks():
    m._gateway_locks.clear()
    yield
    m._gateway_locks.clear()


def _upload_delete_csv(api_client, admin_headers):
    """上傳一份含 name,type 欄位的 CSV，取得 /api/kw/delete-batch 需要的 upload_id。
    type 格式沿用 tb_type 的 '-' 拆解慣例：channel-device-group1.group2。"""
    csv_content = (
        "name,type\r\n"
        "DEL_OK,CH1-DEV1-2F.CHS\r\n"
        "DEL_404,CH1-DEV1-2F.CHS\r\n"
    ).encode("utf-8-sig")
    resp = api_client.post(
        "/api/csv/upload",
        files={"file": ("delete.csv", io.BytesIO(csv_content), "text/csv")},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


def test_kw_delete_batch_calls_delete_tag_and_treats_404_as_success(api_client, admin_headers, monkeypatch):
    """dry_run=false 時：delete_tag 對兩筆資料都應該被呼叫；其中一筆 Kepware 回
    404（tag 本來就不存在）要視為成功（略過），不能算失敗；任務結束後
    Gateway 鎖必須被釋放。"""
    upload_id = _upload_delete_csv(api_client, admin_headers)

    state = FakeKepwareState()
    state.queue_delete_tag_error("DEL_404", http_error(404))
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))
    gw = create_test_gateway(m.pg_client, m._encrypt_password, name="delete-batch-gw")

    resp = api_client.post(
        "/api/kw/delete-batch",
        json={
            "upload_id": upload_id, "gateway_id": gw["id"], "dry_run": False,
            "delay": 0, "batch_size": 50, "batch_pause": 0,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])

    summary = result["summary"]
    assert summary["success"] == 2, summary
    assert summary["fail"] == 0, summary

    called_tags = {c["tag_name"] for c in state.delete_tag_calls}
    assert called_tags == {"DEL_OK", "DEL_404"}, state.delete_tag_calls
    for c in state.delete_tag_calls:
        assert c["channel_name"] == "CH1"
        assert c["device_name"] == "DEV1"
        assert c["tag_group"] == "2F.CHS"

    gw_key = m._gateway_lock_key(gw["id"], gw["url"])
    assert wait_lock_released(m, gw_key), "任務結束後鎖應該已經釋放"
