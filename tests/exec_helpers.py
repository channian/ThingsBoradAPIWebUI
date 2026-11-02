# -*- coding: utf-8 -*-
"""批次 2b（路由 × 佇列 × 取消）測試共用的小工具。不是測試檔（檔名不是 test_*.py）。

提供：兩台假 Gateway 的建置、各端點的暫存列/CSV 種子資料、送出請求與讀取狀態的捷徑。
所有「等」都走 tests.fake_kepware 的 poll_task / wait_until（有逾時保護）。
"""
import io

import app.main as m
from tests.fake_kepware import create_test_gateway, insert_staging_row

URL_A = "http://gw-a.test:1"
URL_B = "http://gw-b.test:1"


def auth(role):
    from app.auth import create_token
    return {"Authorization": f"Bearer {create_token('t-' + role, role)}"}


def fast_body(**kw):
    """不做限速等待的請求內容（delay=0、batch_pause=0）"""
    body = {"delay": 0, "batch_size": 50, "batch_pause": 0}
    body.update(kw)
    return body


# ── Gateway ──────────────────────────────────────────────────

def make_gateway(name="GW-A", url=URL_A, structure=None):
    """建一台 Gateway；structure 為 {channel: {device: [group]}}，有給就寫入結構快取。"""
    gw = create_test_gateway(m.pg_client, m._encrypt_password, name=name, url=url)
    if structure:
        m.pg_client.sync_structure(gw["id"], structure)
    return gw


def make_two_gateways(structure_a=None, structure_b=None):
    """GW-A 擁有 channel CHA、GW-B 擁有 channel CHB（皆 DEV1 / 2F.CHS）。"""
    a = make_gateway("GW-A", URL_A, structure_a or {"CHA": {"DEV1": ["2F.CHS"]}})
    b = make_gateway("GW-B", URL_B, structure_b or {"CHB": {"DEV1": ["2F.CHS"]}})
    return a, b


# ── 暫存列 ───────────────────────────────────────────────────

def seed_kw_row(tag, channel="CH1", device="DEV1", **extra):
    """一筆 Step 3 待建點（tb_status=pending）的暫存列；device_profile 決定 channel。"""
    row = dict(
        tag_name=tag, site="K18", system_code="CHS", scada_node_name="K18CHS",
        io_device="OPC_UA", io_address=f"PLC.{tag}",
        device_profile=f"{channel}-{device}-2F-CHS", tb_status="pending",
    )
    row.update(extra)
    insert_staging_row(m.pg_client, **row)


def seed_scale_row(tag, channel="CH1", device="DEV1", **extra):
    """一筆 Step 5 待設定 Scale（tb_status=done、scale_status=pending、scale_enabled）的暫存列。"""
    row = dict(
        tag_name=tag, site="K18", system_code="CHS", scada_node_name="K18CHS",
        io_device="OPC_UA", io_address=f"PLC.{tag}",
        device_profile=f"{channel}-{device}-2F-CHS", tb_status="done", scale_status="pending",
        scale_enabled=True, raw_low=0, raw_high=4000, scaled_low=0, scaled_high=100,
    )
    row.update(extra)
    insert_staging_row(m.pg_client, **row)


def row_id(tag):
    with m.pg_client._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scada_tag_config WHERE tag_name = %s", (tag,))
            return cur.fetchone()[0]


def status_of(tag, field="tb_status"):
    assert field in ("tb_status", "pg_status", "scale_status")
    with m.pg_client._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {field} FROM scada_tag_config WHERE tag_name = %s", (tag,))
            row = cur.fetchone()
            return row[0] if row else None


# ── 批次刪除用的 CSV ─────────────────────────────────────────

def upload_delete_csv(api_client, headers, names, type_="CH1-DEV1-2F.CHS"):
    """上傳 name,type 的 CSV，回傳 upload_id。names 的每一項是 tag 名稱（用共同的 type_），
    或 (名稱, type) 元組（需要混合不同 channel 時）。"""
    lines = []
    for item in names:
        name, typ = item if isinstance(item, tuple) else (item, type_)
        lines.append(f"{name},{typ}\r\n")
    body = "name,type\r\n" + "".join(lines)
    resp = api_client.post(
        "/api/csv/upload",
        files={"file": ("delete.csv", io.BytesIO(body.encode("utf-8-sig")), "text/csv")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


# ── 請求捷徑 ─────────────────────────────────────────────────

def post_kw(api_client, headers, **body):
    return api_client.post("/api/kw/execute", json=fast_body(**body), headers=headers)


def post_scale(api_client, headers, **body):
    return api_client.post("/api/pg/execute/scale", json=fast_body(**body), headers=headers)


def post_delete(api_client, headers, upload_id, **body):
    payload = fast_body(upload_id=upload_id, dry_run=False)
    payload.update(body)
    return api_client.post("/api/kw/delete-batch", json=payload, headers=headers)


def get_task(api_client, headers, task_id):
    resp = api_client.get(f"/api/tasks/{task_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def calls_of(state, kind="create_tag_calls"):
    """假 Kepware 狀態上某種呼叫的 tag 名稱清單（依呼叫順序）。"""
    key = "tag_name"
    return [c[key] for c in getattr(state, kind)]
