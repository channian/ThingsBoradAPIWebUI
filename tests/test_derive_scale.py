# -*- coding: utf-8 -*-
"""對應 orig_round2.py 測試 #1（Scale 吃覆寫）+ #2（資料不全要記錯，不能靜默降級）、
以及 orig_round3.py 的 #8（derive/scale 回傳 not_built_count，改為行為測試）。
全部需要 DB。
"""
import pytest

pytestmark = pytest.mark.needs_db


def _insert_staging_row(pg_client_obj, **kw):
    cols = ", ".join(kw.keys())
    placeholders = ", ".join(["%s"] * len(kw))
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO scada_tag_config ({cols}) VALUES ({placeholders})",
                list(kw.values()),
            )


def test_derive_scale_fields_override_and_missing_data(pg_client_migrated):
    """對應 orig_round2.py 測試 #2 + #1：
    A（四值齊全、無覆寫）→ scaling_type=1 且無 scale_error；
    B（scale_enabled=true 但缺 scaled_low）→ 必須標記 scale_error，
      指名缺少 scaled_low（不能靜默降級成『不設 Scale』而不吭聲）；
    C（scale_enabled=false，既有正常設計）→ scaling_type=0 且【不應】被誤標為錯誤；
    D（有手動 kw_channel/kw_device/kw_tag_groups 覆寫）→ channel/device/tag_groups
      要用覆寫值，且 derive_kw_fields（Step3 建點）與 derive_scale_fields（Step5 Scale）
      對同一筆資料推導出的 channel/device 必須一致，否則 Scale 設定會打到建點時
      實際沒有建立的路徑上。
    """
    client = pg_client_migrated
    _insert_staging_row(
        client, tag_name="K18_4F_CHS_OK", site="K18", system_code="CHS",
        scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.OK",
        scale_enabled=True, raw_low=0, raw_high=4000, scaled_low=0, scaled_high=100,
        device_profile="K18CHS-CHS-4F-CHS",
    )
    _insert_staging_row(
        client, tag_name="K18_4F_CHS_MISSING", site="K18", system_code="CHS",
        scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.MISS",
        scale_enabled=True, raw_low=0, raw_high=4000, scaled_high=100,
        device_profile="K18CHS-CHS-4F-CHS",
    )
    _insert_staging_row(
        client, tag_name="K18_4F_CHS_NOSCALE", site="K18", system_code="CHS",
        scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.NS",
        scale_enabled=False, device_profile="K18CHS-CHS-4F-CHS",
    )
    _insert_staging_row(
        client, tag_name="K18_4F_CHS_OVERRIDE", site="K18", system_code="CHS",
        scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.OV",
        scale_enabled=True, raw_low=0, raw_high=100, scaled_low=0, scaled_high=50,
        device_profile="K18CHS-CHS-4F-CHS",
        kw_channel="OVR_CH", kw_device="OVR_DEV", kw_tag_groups="OVR.GRP",
    )

    rows = client.get_staging_list(page=0, page_size=None)["data"]
    scale = {e["tag_name"]: e for e in client.derive_scale_fields(rows)}

    a = scale.get("K18_4F_CHS_OK")
    assert a and a["scaling_type"] == 1, a
    assert a and not a.get("scale_error"), a.get("scale_error") if a else None

    b = scale.get("K18_4F_CHS_MISSING")
    assert b and b.get("scale_error"), "B 資料不全（缺 scaled_low）應該標記 scale_error"
    assert "scaled_low" in (b.get("scale_error") or ""), b.get("scale_error")

    c = scale.get("K18_4F_CHS_NOSCALE")
    assert c and c["scaling_type"] == 0, c
    assert c and not c.get("scale_error"), (
        "scale_enabled=false 是既有正常設計，不應該被誤標為錯誤，"
        f"實際 scale_error={c.get('scale_error') if c else None}"
    )

    d = scale.get("K18_4F_CHS_OVERRIDE")
    assert d and d["channel_name"] == "OVR_CH", d
    assert d and d["device_name"] == "OVR_DEV", d
    assert d and d["full_tag_name"] == "OVR.GRP.K18_4F_CHS_OVERRIDE", d.get("full_tag_name") if d else None

    # 交叉驗證：derive_kw_fields（Step3）與 derive_scale_fields（Step5）的路徑必須一致
    kw = {e["tag_name"]: e for e in client.derive_kw_fields(rows)}
    kd = kw.get("K18_4F_CHS_OVERRIDE")
    assert kd and d
    assert kd["channel_name"] == d["channel_name"] and kd["device_name"] == d["device_name"], (
        f"Step3 與 Step5 的 channel/device 必須一致："
        f"kw={kd['channel_name']}/{kd['device_name']} scale={d['channel_name']}/{d['device_name']}"
    )


def test_derive_scale_api_reports_not_built_count(api_client, admin_headers):
    """對應 orig_round3.py #8（改為行為測試，而非只做字串比對原始碼）：
    3 筆暫存資料中，2 筆 tb_status='done'（已在 Kepware 建點）、1 筆 'pending'（未建點）。
    POST /api/pg/derive/scale 應該正確回傳 not_built_count=1——Scale 設定是 PUT
    （更新語意），若該 tag 在 Kepware 還不存在，執行時必然失敗，這個計數只是
    UI 上的提示、不會阻擋執行（合法用法：Kepware 上已手動建好點位，只想補 Scale）。
    直接用 TestClient 呼叫真正的端點驗證回應內容，比單純 regex 比對原始碼是否
    『看起來』有算這個欄位更可靠。"""
    import app.main as m

    for name, tb in (("T_BUILT_1", "done"), ("T_BUILT_2", "done"), ("T_NOTBUILT", "pending")):
        _insert_staging_row(
            m.pg_client, tag_name=name, site="K18", system_code="CHS",
            scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.1",
            scale_enabled=True, raw_low=0, raw_high=100, scaled_low=0, scaled_high=50,
            device_profile="K18CHS-CHS-4F-CHS", tb_status=tb,
        )

    # 測試資料就緒的前置確認：3 筆中應該有 1 筆未建點
    rows = m.pg_client.get_staging_list(page=0, page_size=None, scale_status="pending")["data"]
    assert len(rows) == 3, rows
    assert sum(1 for r in rows if r.get("tb_status") != "done") == 1, rows

    resp = api_client.post(
        "/api/pg/derive/scale", json={"scale_status": "pending"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3, body
    assert body["not_built_count"] == 1, body
