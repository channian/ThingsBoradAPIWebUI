# -*- coding: utf-8 -*-
"""對應 orig_round2.py 測試 #3：import_formal() 逐筆 SAVEPOINT。

import_formal() 操作的是 tags 正式表，由 _ensure_formal_table() 在方法內自行
CREATE TABLE IF NOT EXISTS 建立，不依賴 scada_tag_config 的任何額外欄位，
所以用裸的 pg_client fixture 即可，不需要先呼叫 ensure_schema()。
"""
import pytest

pytestmark = pytest.mark.needs_db


def _derived_row(tagname: str, **extra) -> dict:
    base = {
        "id": 0, "tagname": tagname, "description": "d", "node_name": "N",
        "driver_type": "OPC", "address": "a", "tabname": "t", "zone": "z", "bu": "b",
        "site": "s", "floor": "4F", "system": "CHS", "owner": "", "department": "",
        "data_type": "float",
    }
    base.update(extra)
    return base


def test_import_formal_savepoint_isolates_bad_row(pg_client):
    """同一批次中一筆 tagname 超過 255 字元的資料必定 INSERT 失敗；
    沒有逐筆 SAVEPOINT 時，PostgreSQL 同一個交易內一筆出錯後，
    後續所有指令都會收到 'current transaction is aborted' 而連環失敗。
    驗證：錯誤筆之後仍能寫入 3 筆正常資料，且只回報 1 筆『真』錯誤（不是連環的假錯誤）。"""
    derived = [
        _derived_row("FORMAL_OK_1"),
        _derived_row("X" * 300),  # 超長 tagname，必爆錯
        _derived_row("FORMAL_OK_2"),
        _derived_row("FORMAL_OK_3"),
    ]
    r = pg_client.import_formal(derived)

    assert r["inserted"] == 3, {k: v for k, v in r.items() if k != "errors"}
    assert len(r["errors"]) == 1, [e["reason"][:80] for e in r["errors"]]
    assert "transaction is aborted" not in r["errors"][0]["reason"], r["errors"][0]["reason"]

    with pg_client._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tagname FROM tags WHERE tagname LIKE 'FORMAL_OK%' ORDER BY tagname")
            got = [x[0] for x in cur.fetchall()]

    assert got == ["FORMAL_OK_1", "FORMAL_OK_2", "FORMAL_OK_3"], got
