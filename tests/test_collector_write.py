# -*- coding: utf-8 -*-
"""對應 orig_round1.py 測試 4、5：Collector DB enable 欄位型別相容（SMALLINT / BOOLEAN）
與 import_collector_tags 的逐筆 SAVEPOINT。全部需要 DB。
"""
import pytest

pytestmark = pytest.mark.needs_db


def test_collector_smallint_enable_type_and_savepoint_isolation(pg_client):
    """Collector 的 tags 表可能由外部系統預先建立，enable 欄位型別是 SMALLINT
    而非 BOOLEAN（tests/conftest.py 的 COLLECTOR_BASE_TABLE_SQL 刻意複製這個型別）。
    驗證：
    1) smallint 型別不再報型別錯誤，True/False 能正確轉成 1/0 寫入；
    2) 其中一筆 tagname 超過 255 字元必定爆錯，靠逐筆 SAVEPOINT，
       這筆錯誤不會讓同一個交易內其餘資料也連環失敗
       （沒有 SAVEPOINT 時，之後每一筆都會收到 'current transaction is aborted'）。
    """
    rows = [
        {"tagname": "OK_TAG_1", "tag_address": "a1", "scan_group": "g", "description": "d",
         "enable": True, "target_table": "t"},
        # 這筆 tagname 超長（>255）會爆錯，用來驗證 SAVEPOINT 之後其餘筆不受影響
        {"tagname": "X" * 300, "tag_address": "bad", "scan_group": "g", "description": "d",
         "enable": True, "target_table": "t"},
        {"tagname": "OK_TAG_2", "tag_address": "a2", "scan_group": "g", "description": "d",
         "enable": False, "target_table": "t"},
        {"tagname": "OK_TAG_3", "tag_address": "a3", "scan_group": "g", "description": "d",
         "enable": True, "target_table": "t"},
    ]
    cr = pg_client.import_collector_tags(rows)

    assert cr["inserted"] == 3, cr
    assert len(cr["errors"]) == 1, f"只應該有 1 筆真錯誤（無連環 aborted），實際: {cr['errors']}"
    assert "transaction is aborted" not in cr["errors"][0]["reason"], cr["errors"][0]["reason"]

    with pg_client._get_collector_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tagname, enable FROM tags ORDER BY tagname")
            got = dict(cur.fetchall())

    assert got.get("OK_TAG_1") == 1 and got.get("OK_TAG_2") == 0 and got.get("OK_TAG_3") == 1, (
        f"enable 應以 smallint 正確寫入（True→1, False→0），實際: {got}"
    )


def test_collector_boolean_enable_still_works(pg_client):
    """回歸測試：若 Collector tags 表的 enable 本來就是 BOOLEAN（非本專案刻意重現的
    smallint 情境），寫入仍要正常運作——型別偵測必須兩種都相容，
    不能只修好 smallint 卻打壞原本就能動的 boolean 情境。"""
    with pg_client._get_collector_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE tags")
            cur.execute("""
                CREATE TABLE tags (
                    tag_id SERIAL PRIMARY KEY, tagname VARCHAR(255) NOT NULL UNIQUE,
                    tag_address VARCHAR(500), scan_group VARCHAR(255) DEFAULT '',
                    unit VARCHAR(100) DEFAULT '', description TEXT,
                    enable BOOLEAN DEFAULT TRUE, target_table VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW())
            """)

    cr2 = pg_client.import_collector_tags([
        {"tagname": "BOOL_TAG", "tag_address": "a", "scan_group": "g", "description": "d",
         "enable": True, "target_table": "t"},
    ])
    assert cr2["inserted"] == 1 and not cr2["errors"], cr2

    with pg_client._get_collector_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT enable FROM tags WHERE tagname='BOOL_TAG'")
            assert cur.fetchone()[0] is True, "boolean 表的 enable 應寫入為 true"
