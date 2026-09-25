# -*- coding: utf-8 -*-
"""對應 orig_round4.py：data_type 覆寫機制。

- _resolve_kw_data_type 解析規則（純邏輯，不需要 DB）。
- derive_kw_fields / derive_scale_fields 對 data_type 的處理與一致性（需要 DB）。
- update_kw_overrides 儲存與清除（需要 DB）。
- Step 3 建點必須帶 data_type、Step 5 跳過未啟用 Scale 的點（AST／原始碼片段靜態檢查，
  不需要 DB）。
"""
import re

import pytest

import app.pg_client as pgc
from tests.ast_utils import (
    call_has_keyword,
    find_all_calls_named,
    get_function_source,
    get_main_ast,
)


# ── _resolve_kw_data_type 解析規則（純邏輯）──────────────────────

def test_kw_data_type_default_constant_is_float():
    """預設常數為 8（Float）：實務上九成點位皆為此型別，故作為預設值"""
    assert pgc.KW_DATA_TYPE_DEFAULT == 8


def test_resolve_kw_data_type_none_falls_back_to_default():
    assert pgc._resolve_kw_data_type({"kw_data_type": None}) == 8


def test_resolve_kw_data_type_missing_key_falls_back_to_default():
    assert pgc._resolve_kw_data_type({}) == 8


def test_resolve_kw_data_type_boolean_override():
    assert pgc._resolve_kw_data_type({"kw_data_type": 1}) == 1


def test_resolve_kw_data_type_dword_override():
    assert pgc._resolve_kw_data_type({"kw_data_type": 7}) == 7


def test_resolve_kw_data_type_invalid_value_falls_back_to_default():
    assert pgc._resolve_kw_data_type({"kw_data_type": "abc"}) == 8


# ── derive_kw_fields / derive_scale_fields 的 data_type 行為（需要 DB）───

def _insert_staging_row(pg_client_obj, **kw):
    cols = ", ".join(kw.keys())
    placeholders = ", ".join(["%s"] * len(kw))
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO scada_tag_config ({cols}) VALUES ({placeholders})",
                list(kw.values()),
            )


@pytest.fixture
def data_type_rows(pg_client_migrated):
    """建立三筆測試資料：
    T_DEFAULT  未覆寫、需要 Scale；
    T_BOOL     覆寫成 Boolean(1)、不需要 Scale；
    T_NOSCALE  未覆寫、不需要 Scale（用來驗證 data_type 與 scale_enabled 無關）。
    """
    client = pg_client_migrated
    _insert_staging_row(
        client, tag_name="T_DEFAULT", site="K18", system_code="CHS",
        scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.1",
        scale_enabled=True, raw_low=0, raw_high=4000, scaled_low=0, scaled_high=100,
        device_profile="K18CHS-CHS-4F-CHS",
    )
    _insert_staging_row(
        client, tag_name="T_BOOL", site="K18", system_code="CHS",
        scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.2",
        scale_enabled=False, device_profile="K18CHS-CHS-4F-CHS", kw_data_type=1,
    )
    _insert_staging_row(
        client, tag_name="T_NOSCALE", site="K18", system_code="CHS",
        scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.3",
        scale_enabled=False, device_profile="K18CHS-CHS-4F-CHS",
    )
    return client


@pytest.mark.needs_db
def test_derive_kw_fields_carries_data_type_and_override_flag(data_type_rows):
    """derive_kw_fields（Step 3 建點用）要帶出正確的 data_type：
    未覆寫 → 預設 Float(8)；覆寫過 → 用覆寫值；不需要 Scale 的點一樣要有 data_type
    （data_type 是否要覆寫，跟這個點需不需要 Scale 是兩件互不相干的事）。
    同時 overridden 旗標要正確反映『是否有任何欄位被手動覆寫』。"""
    client = data_type_rows
    rows = client.get_staging_list(page=0, page_size=None)["data"]
    kw = {e["tag_name"]: e for e in client.derive_kw_fields(rows)}

    assert kw["T_DEFAULT"]["data_type"] == 8, kw["T_DEFAULT"]
    assert kw["T_BOOL"]["data_type"] == 1, kw["T_BOOL"]
    assert kw["T_NOSCALE"]["data_type"] == 8, kw["T_NOSCALE"]
    assert kw["T_BOOL"].get("overridden") is True, kw["T_BOOL"]
    assert kw["T_DEFAULT"].get("overridden") is False, kw["T_DEFAULT"]


@pytest.mark.needs_db
def test_derive_scale_fields_uses_same_data_type_as_kw_fields(data_type_rows):
    """Step3（derive_kw_fields）與 Step5（derive_scale_fields）對同一筆資料算出的
    data_type 必須一致；scaling_scaled_data_type（縮放『輸出值』的型別）則要固定是 8，
    不受 data_type 覆寫影響（它跟 tag 本身的 data_type 是兩個獨立的設定）。"""
    client = data_type_rows
    rows = client.get_staging_list(page=0, page_size=None)["data"]
    kw = {e["tag_name"]: e for e in client.derive_kw_fields(rows)}
    sc = {e["tag_name"]: e for e in client.derive_scale_fields(rows)}

    assert kw["T_DEFAULT"]["data_type"] == sc["T_DEFAULT"]["data_type"] == 8
    assert kw["T_BOOL"]["data_type"] == sc["T_BOOL"]["data_type"] == 1
    assert sc["T_DEFAULT"].get("scaling_scaled_data_type") == 8, sc["T_DEFAULT"]


@pytest.mark.needs_db
def test_update_kw_overrides_save_and_clear(data_type_rows):
    """update_kw_overrides 儲存覆寫值、以及用 None／空字串清除覆寫都要正確寫回
    kw_data_type 欄位（清除後應為 NULL，代表回到預設 Float(8)）。"""
    client = data_type_rows
    rows = client.get_staging_list(page=0, page_size=None)["data"]
    target = next(r["id"] for r in rows if r["tag_name"] == "T_DEFAULT")

    def _get_kw_data_type():
        with client._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT kw_data_type FROM scada_tag_config WHERE id=%s", (target,))
                return cur.fetchone()[0]

    client.update_kw_overrides(target, "", "", "", 7)  # 覆寫成 DWord
    assert _get_kw_data_type() == 7

    client.update_kw_overrides(target, "", "", "", None)  # 清除覆寫
    assert _get_kw_data_type() is None, "傳 None 應該清除覆寫（回到 NULL/預設）"

    client.update_kw_overrides(target, "", "", "", "")  # 空字串也應視為清除
    assert _get_kw_data_type() is None, "傳空字串也應該視為清除覆寫"


# ── Step 3 / Step 5 靜態一致性檢查（AST／原始碼片段，不需要 DB）──────

def test_create_tag_calls_always_pass_data_type():
    """Step 3 建點：所有 create_tag(...) 呼叫都必須帶 data_type 關鍵字參數，
    否則建出來的 Kepware tag 會用 Kepware 自己的預設型別，可能跟 Step5 Scale
    設定用的型別對不上。用 AST 找出所有 obj.create_tag(...) 呼叫節點，取代原本
    用括號配對手動擷取字串再 regex 比對的作法——原本非貪婪 regex 一旦遇到巢狀
    括號就會提早截斷字串、造成『明明有帶 data_type 卻被判定沒帶』的誤判。"""
    tree = get_main_ast()
    calls = find_all_calls_named(tree, "create_tag")
    assert len(calls) >= 1, "找不到任何 create_tag(...) 呼叫"

    missing_at_lines = [c.lineno for c in calls if not call_has_keyword(c, "data_type")]
    assert not missing_at_lines, (
        f"以下行號的 create_tag 呼叫缺少 data_type 關鍵字參數: {missing_at_lines}"
    )


def test_step5_skips_tags_with_scale_disabled():
    """Step 5（_exec_pg_execute_scale）應該跳過 scale_enabled=false 的點，
    不浪費 Kepware 限速額度去呼叫它們：要有 skip_ids 收集清單、要有『不呼叫 Kepware
    直接 continue』的分支、跳過的點要把 scale_status 標成 'skip'，且完成時的
    push_complete 要回報『實際』跳過的筆數（skipped_count），而不是寫死 0。"""
    tree = get_main_ast()
    src = get_function_source(tree, "_exec_pg_execute_scale")
    assert src, "找不到 _exec_pg_execute_scale 函式原始碼"

    assert "skip_ids" in src, "應該有 skip_ids 收集清單"
    assert "continue" in src and "skip" in src, "應該有跳過不呼叫 Kepware 的分支（continue）"
    assert re.search(r'update_staging_status\(\s*skip_ids\s*,\s*"scale_status"\s*,\s*"skip"\s*\)', src), (
        "跳過的點應該要更新 scale_status='skip'"
    )
    assert re.search(r'"skip":\s*skipped_count', src), (
        "push_complete 應該回報實際 skip 數（skipped_count），而不是寫死 0"
    )
