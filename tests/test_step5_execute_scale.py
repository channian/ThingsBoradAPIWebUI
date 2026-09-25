# -*- coding: utf-8 -*-
"""Step 5（POST /api/pg/execute/scale）背景任務的**行為測試**：用假的
KepwareGatewayClient 取代真實 client，驅動 `app.main` 內 `_exec_pg_execute_scale`
這條真正會被執行到的程式碼路徑（而不是只靜態比對原始碼字串）。

補的缺口：把 `_exec_pg_execute_scale` 內
    if item.get("scaling_type") == 0 and not item.get("scale_error"):
改成 `if False:`（Step 5 不再跳過未啟用 Scale 的點）後，原本
`tests/test_data_type.py::test_step5_skips_tags_with_scale_disabled` 這個純
AST／字串靜態檢查完全抓不到（`skip_ids`、`continue`、`skip_status` 等字串仍原封
不動留在程式碼裡，只是變成永遠不會走到的死路徑）。這裡改用「插入真實資料 →
真的跑一次背景任務 → 檢查 Kepware 端實際收到哪些呼叫、DB 實際被改成什麼狀態」
才能抓到這種邏輯性退化。
"""
import pytest

import app.main as m
from tests.fake_kepware import (
    wait_lock_released,
    FakeKepwareState,
    create_test_gateway,
    insert_staging_row,
    make_fake_kepware_client,
    poll_task,
)


@pytest.fixture(autouse=True)
def _reset_gateway_locks():
    """避免這個檔案裡的測試互相汙染鎖表，也避免汙染其他測試檔。"""
    m._gateway_locks.clear()
    yield
    m._gateway_locks.clear()


# 四筆測試資料共用的推導基礎：device_profile 直接指定 tb_type，
# 拆解後 channel_name=CH1 / device_name=DEV1 / tag_groups=2F.CHS
_DEVICE_PROFILE = "CH1-DEV1-2F-CHS"


def _seed_four_rows(pg_client_obj):
    """插入 A/B/C/D 四筆 tb_status='done'、scale_status='pending' 的暫存資料：
    A：scale_enabled=true，範圍齊全，無覆寫 → 應該呼叫 Kepware 設定 Scale。
    B：scale_enabled=false → 不該呼叫 Kepware，直接標記 skip。
    C：scale_enabled=true 但 scaled_low 缺漏 → 不該呼叫 Kepware，標記 fail、
       scale_status 維持 pending（等修正後可重跑）。
    D：scale_enabled=true，範圍齊全，且有 kw_channel/kw_device/kw_tag_groups/
       kw_data_type 覆寫 → 應該呼叫 Kepware，且用覆寫值而非推導值。
    """
    common = dict(
        site="K18", system_code="CHS", scada_node_name="K18CHS",
        io_device="OPC_UA", io_address="A.1", device_profile=_DEVICE_PROFILE,
        tb_status="done", scale_status="pending",
    )
    insert_staging_row(
        pg_client_obj, tag_name="STEP5_A", scale_enabled=True,
        raw_low=0, raw_high=4000, scaled_low=0, scaled_high=100, **common,
    )
    insert_staging_row(
        pg_client_obj, tag_name="STEP5_B", scale_enabled=False, **common,
    )
    insert_staging_row(
        pg_client_obj, tag_name="STEP5_C", scale_enabled=True,
        raw_low=0, raw_high=4000, scaled_high=100,  # 故意缺 scaled_low
        **common,
    )
    insert_staging_row(
        pg_client_obj, tag_name="STEP5_D", scale_enabled=True,
        raw_low=0, raw_high=4000, scaled_low=0, scaled_high=100,
        kw_channel="OVR_CH", kw_device="OVR_DEV", kw_tag_groups="OVR.GRP",
        kw_data_type=7, **common,
    )


def _scale_status_of(pg_client_obj, tag_name):
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scale_status FROM scada_tag_config WHERE tag_name = %s",
                (tag_name,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def test_step5_execute_scale_end_to_end(api_client, admin_headers, monkeypatch):
    """完整跑一次 Step 5 背景任務，逐項驗證：

    1. set_tag_scaling 只被呼叫於 A、D（B、C 完全不打 Kepware）——
       這項正是原本被 `if False:` 突變逃過去的行為。
    2. D 的 payload 使用覆寫值（channel/device/tag_group/data_type）；
       A 的 payload 使用推導值，且帶正確的 scaling_raw_*/scaling_scaled_*。
    3. DB 的 scale_status：A、D → done；B → skip；C → 維持 pending。
    4. summary：success=2、skip=1、fail=1，且 C 的失敗原因提到 scaled_low。
    5. 任務結束後該 Gateway 的鎖已釋放，且立刻再打一次 execute/scale 不會回 409。
    """
    _seed_four_rows(m.pg_client)

    state = FakeKepwareState()
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))

    gw = create_test_gateway(m.pg_client, m._encrypt_password, name="step5-gw")

    resp = api_client.post(
        "/api/pg/execute/scale",
        json={"gateway_id": gw["id"], "delay": 0, "batch_size": 50, "batch_pause": 0},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    result = poll_task(api_client, admin_headers, task_id)

    # ── 1. set_tag_scaling 呼叫對象 ──
    called_tag_names = {c["tag_name"] for c in state.set_tag_scaling_calls}
    assert called_tag_names == {"2F.CHS.STEP5_A", "OVR.GRP.STEP5_D"}, (
        f"set_tag_scaling 應該只被 A、D 觸發，實際呼叫: {state.set_tag_scaling_calls}"
    )

    calls_by_tag = {c["tag_name"]: c for c in state.set_tag_scaling_calls}

    # ── 2a. D：覆寫值優先 ──
    d_call = calls_by_tag["OVR.GRP.STEP5_D"]
    assert d_call["channel_name"] == "OVR_CH"
    assert d_call["device_name"] == "OVR_DEV"
    assert d_call["data_type"] == 7
    assert d_call["scaling_scaled_data_type"] == 8, (
        "scaling_scaled_data_type（輸出值型別）固定是 8，不受 data_type 覆寫影響"
    )

    # ── 2b. A：推導值 + 完整 Linear scaling 參數 ──
    a_call = calls_by_tag["2F.CHS.STEP5_A"]
    assert a_call["channel_name"] == "CH1"
    assert a_call["device_name"] == "DEV1"
    assert a_call["data_type"] == 8
    assert a_call["scaling_type"] == 1
    assert a_call["scaling_raw_low"] == 0.0
    assert a_call["scaling_raw_high"] == 4000.0
    assert a_call["scaling_scaled_low"] == 0.0
    assert a_call["scaling_scaled_high"] == 100.0

    # ── 3. DB scale_status ──
    assert _scale_status_of(m.pg_client, "STEP5_A") == "done"
    assert _scale_status_of(m.pg_client, "STEP5_D") == "done"
    assert _scale_status_of(m.pg_client, "STEP5_B") == "skip"
    assert _scale_status_of(m.pg_client, "STEP5_C") == "pending", (
        "資料不全的 C 不該被標記完成，維持 pending 才能在修正後重跑"
    )

    # ── 4. summary ──
    summary = result["summary"]
    assert summary["success"] == 2, summary
    assert summary["skip"] == 1, summary
    assert summary["fail"] == 1, summary
    c_errors = [e for e in summary["errors"] if e["tag_name"] == "STEP5_C"]
    assert len(c_errors) == 1, summary["errors"]
    assert "scaled_low" in c_errors[0]["reason"], c_errors[0]

    # ── 5. Gateway 鎖已釋放，立刻再呼叫一次不會 409 ──
    gw_key = m._gateway_lock_key(gw["id"], gw["url"])
    assert wait_lock_released(m, gw_key), "任務結束後鎖應該已經釋放"

    resp2 = api_client.post(
        "/api/pg/execute/scale",
        json={"gateway_id": gw["id"], "delay": 0, "batch_size": 50, "batch_pause": 0},
        headers=admin_headers,
    )
    assert resp2.status_code != 409, resp2.text
    # 把第二次任務（只剩 C 一筆，仍會失敗）跑完，避免殘留背景執行緒影響後續測試
    poll_task(api_client, admin_headers, resp2.json()["task_id"])
