# -*- coding: utf-8 -*-
"""對應 orig_round2.py 測試 #11（ensure_schema 遷移）、orig_round4.py 的 kw_data_type
前置檢查，以及任務規格額外要求的「啟動流程」驗收：在乾淨 DB 上跑
asyncio.run(app.main.on_startup()) 後，不呼叫任何 API，schema 也要自動 migrate 完成。
"""
import asyncio

import pytest

pytestmark = pytest.mark.needs_db

REF_TABLES = (
    "location_config", "ownership_config", "device_config",
    "system_config", "tb_device_profile", "kepware_gateway",
)


def _columns_of(pg_client_obj, table_name: str) -> set:
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
                (table_name,),
            )
            return {r[0] for r in cur.fetchall()}


def _tables_in_public_schema(pg_client_obj) -> set:
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
            return {r[0] for r in cur.fetchall()}


def test_ensure_schema_adds_missing_columns_and_ref_tables(pg_client):
    """#11：ensure_schema() 應該把 kw_channel/kw_device/kw_tag_groups/scale_status/
    scan_group 欄位補齊到 scada_tag_config，並建立 6 張參照表。呼叫前先確認
    kw_channel『確實還不存在』——這正是原驗收腳本吃過的虧：如果前一個測試已經對
    同一個 DB 跑過 ensure_schema()，這個前置條件會失敗、讓整個測試失去意義；
    這裡靠 pg_client fixture 每次給一個乾淨、未 migrate 的 DB 來避免。"""
    before = _columns_of(pg_client, "scada_tag_config")
    assert "kw_channel" not in before, f"前置條件失敗：kw_channel 不應該存在，實際欄位={sorted(before)}"

    assert hasattr(pg_client, "ensure_schema"), "PGClient 應該要有公開的 ensure_schema 方法"
    pg_client.ensure_schema()

    after = _columns_of(pg_client, "scada_tag_config")
    tables = _tables_in_public_schema(pg_client)

    for col in ("kw_channel", "kw_device", "kw_tag_groups", "scale_status", "scan_group"):
        assert col in after, f"ensure_schema 後欄位 {col} 應該已存在，實際欄位={sorted(after)}"
    for t in REF_TABLES:
        assert t in tables, f"參照表 {t} 應該已建立，實際表={sorted(tables)}"

    # 冪等性：重複呼叫不應該拋錯（CREATE TABLE IF NOT EXISTS / 有條件的 ALTER）
    pg_client.ensure_schema()


def test_ensure_schema_adds_kw_data_type_column_as_nullable_integer(pg_client):
    """對應 orig_round4.py 前置檢查：ensure_schema() 應建立 kw_data_type 欄位，
    型別為 INTEGER 且可為 NULL（NULL 代表『未覆寫，使用預設 Float(8)』）。"""
    pg_client.ensure_schema()
    with pg_client._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT data_type, is_nullable FROM information_schema.columns
                   WHERE table_name='scada_tag_config' AND column_name='kw_data_type'"""
            )
            col = cur.fetchone()

    assert col is not None, "kw_data_type 欄位不存在"
    assert col[0] == "integer", f"kw_data_type 型別應為 integer，實際={col[0]}"
    assert col[1] == "YES", f"kw_data_type 應可為 NULL（代表未覆寫），實際 is_nullable={col[1]}"


def test_on_startup_migrates_clean_db_without_calling_any_api(clean_db):
    """啟動流程驗收：完全不呼叫 /api/pg/test 等任何 API，只在乾淨 DB 上跑
    asyncio.run(app.main.on_startup())，之後 scada_tag_config 就應該具備
    kw_channel/kw_device/kw_tag_groups/kw_data_type/scale_status/scan_group，
    且 6 張參照表存在。防止『schema migration 只綁在某個手動測試端點上，
    正常啟動流程完全不會跑到』這種退化。"""
    import app.main as m

    try:
        asyncio.run(m.on_startup())

        after = _columns_of(m.pg_client, "scada_tag_config")
        tables = _tables_in_public_schema(m.pg_client)

        for col in ("kw_channel", "kw_device", "kw_tag_groups", "kw_data_type",
                    "scale_status", "scan_group"):
            assert col in after, f"on_startup 後欄位 {col} 應該存在，實際欄位={sorted(after)}"
        for t in REF_TABLES:
            assert t in tables, f"on_startup 後參照表 {t} 應該存在，實際表={sorted(tables)}"
    finally:
        # on_startup 會啟動一個每週清理排程的背景 daemon 執行緒，測試結束後
        # 通知它停止，避免殘留到下一個測試或行程結束時仍在背景跑。
        m._cleanup_stop.set()
