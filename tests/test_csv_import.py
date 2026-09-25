# -*- coding: utf-8 -*-
"""對應 orig_round1.py：
測試 1 → _row_get_ci 純邏輯（不需要 DB）。
測試 2、3 → import_staging 表頭大小寫變體 / 重複匯入 skip 警示（需要 DB）。

import_staging() 的 INSERT 會用到 scan_group 欄位，而 scan_group 是由
ensure_schema() 才補上的欄位（見 CLAUDE.md 與 tests/conftest.py 的
STAGING_BASE_TABLE_SQL 說明），所以這裡的 DB 測試一律用 pg_client_migrated
fixture（先呼叫過 ensure_schema()），而不是裸的 pg_client。
"""
import pytest

from app.pg_client import _row_get_ci

# ── 測試 1：_row_get_ci 表頭大小寫變體（純邏輯，不需要 DB）──────────

def test_row_get_ci_title_case_header_maps_to_io_address():
    """標頭 'I/O Address'（Title Case）應能對到 io_address，
    修正前大小寫敏感比對會讓它被靜默對成空字串。"""
    row = {"I/O Address": " ns=2;s=PLC1.T1 ", "Tag Name": "T1", "Site": "K18"}
    assert _row_get_ci(row, "io_address", "I/O ADDRESS") == "ns=2;s=PLC1.T1"


def test_row_get_ci_tag_name_variant_header():
    row = {"I/O Address": " ns=2;s=PLC1.T1 ", "Tag Name": "T1", "Site": "K18"}
    assert _row_get_ci(row, "tag_name", "Tag Name") == "T1"


def test_row_get_ci_missing_key_returns_none():
    row = {"I/O Address": " ns=2;s=PLC1.T1 ", "Tag Name": "T1", "Site": "K18"}
    assert _row_get_ci(row, "raw_low", "Raw Low") is None


def test_row_get_ci_all_caps_header_also_matches():
    assert _row_get_ci({"IO_ADDRESS": "x"}, "io_address", "I/O ADDRESS") == "x"


# ── 測試 2：import_staging 用表頭變體匯入，io_address 等欄位應正確入庫（需要 DB）──

@pytest.mark.needs_db
def test_import_staging_with_header_case_variants(pg_client_migrated):
    """模擬同事習慣用 Title Case（或混用大小寫）填寫 CSV 表頭，
    io_address / scale_enabled / raw_high 等欄位都要正確入庫，
    而不是被表頭比對邏輯靜默轉成空值或預設值。"""
    client = pg_client_migrated
    csv_rows = [
        {
            "Tag Name": "K18_4F_CHS_CH03_KW_RT",
            "Site": "K18", "System": "CHS",
            "SCADA Node Name": "K18CHS",
            "I/O Device": "OPC_UA",              # 變體：非全大寫
            "I/O Address": "PLC1.CH03.KW_RT",     # 變體：Title Case（修正前會靜默變空）
            "Scale Enabled": "true",
            "Raw Low": "0", "Raw High": "4000",
            "Scaled Low": "0", "Scaled High": "100",
            "Description": "冰水系統03 KW_RT",
        },
        {  # 正常 snake_case
            "tag_name": "K18_4F_CHS_CH03_RT",
            "site": "K18", "system_code": "CHS",
            "io_device": "OPC_UA", "io_address": "PLC1.CH03.RT",
            "scale_enabled": "true",
            "raw_low": "0", "raw_high": "100", "scaled_low": "0", "scaled_high": "50",
            "description": "冰水系統03 RT",
        },
    ]
    r = client.import_staging(csv_rows)
    assert r["inserted"] == 2, r

    with client._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT io_address, scale_enabled, raw_high FROM scada_tag_config "
                "WHERE tag_name='K18_4F_CHS_CH03_KW_RT'"
            )
            io_addr, se, rh = cur.fetchone()

    assert io_addr == "PLC1.CH03.KW_RT", f"Title Case 表頭的 io_address 應正確入庫，got {io_addr!r}"
    assert se is True and rh == 4000.0, f"Scale Enabled/Raw High 變體表頭也應正確，se={se} rh={rh}"


# ── 測試 3：重複匯入 → 跳過且回應含警示 message（需要 DB）───────────

@pytest.mark.needs_db
def test_import_staging_duplicate_rows_are_skipped_with_warning_message(pg_client_migrated):
    """tag_name 重複的資料重新匯入時應該被跳過（ON CONFLICT DO NOTHING，
    既有資料內容不會被更新），且回應要帶出清楚的警示訊息，避免使用者誤以為
    『重新匯入』等於『更新既有資料』。沒有任何筆被跳過時，訊息則是單純的成功訊息。"""
    client = pg_client_migrated
    csv_rows = [
        {"tag_name": "DUP_TAG_01", "site": "K18", "system_code": "CHS", "io_device": "OPC_UA",
         "io_address": "PLC1.A", "scale_enabled": "true", "raw_low": "0", "raw_high": "100",
         "scaled_low": "0", "scaled_high": "50", "description": "d"},
        {"tag_name": "DUP_TAG_02", "site": "K18", "system_code": "CHS", "io_device": "OPC_UA",
         "io_address": "PLC1.B", "scale_enabled": "true", "raw_low": "0", "raw_high": "100",
         "scaled_low": "0", "scaled_high": "50", "description": "d"},
    ]
    r1 = client.import_staging(csv_rows)
    assert r1["inserted"] == 2, r1

    r2 = client.import_staging(csv_rows)
    assert r2["inserted"] == 0 and r2["skipped"] == 2, r2
    assert "message" in r2 and "不會被更新" in r2.get("message", ""), r2.get("message", "(無)")

    r3 = client.import_staging([{"tag_name": "NEW_TAG_01", "io_address": "A.B"}])
    assert r3.get("message", "").startswith("新增 1"), r3.get("message", "(無)")
