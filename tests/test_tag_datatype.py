# -*- coding: utf-8 -*-
"""app/tag_datatype.py（資料型態的純邏輯）與 KepwareGatewayClient.set_tag_data_type 的測試。

純邏輯測試：不需要 DB。

防止的退化：
- extract_tag_data_type 亂猜：bool 變成 1、超出 0–15 照收、多個候選不一致時任取一個、
  不認識的值被當成 0——對一個會「寫入正式環境」的功能，判錯比判不出來危險得多。
- 候選鍵／包裝層／大小寫／數字字串／型態名稱這些寬鬆解析被改窄，導致真實回應判不出來。
- diff_tag_payloads 漏掉巢狀欄位的變動、新增／消失的鍵，或把 allowed 以外的鍵也忽略，
  導致「PUT 影響了其他欄位」的斷路器失靈。
- set_tag_data_type 的 PUT body 夾帶 scaling 等其他欄位，或接受不合法的型態代碼。
"""
import subprocess
import sys
from pathlib import Path

import pytest

from app import tag_datatype
from app.kw_gw_client import KepwareGatewayClient
from app.tag_datatype import (
    DATA_TYPE_NAMES, data_type_label, describe_payload_keys, diff_tag_payloads,
    extract_tag_data_type, is_writable_data_type,
)


# ═══════════════════════════════════════════════════════════════
# DATA_TYPE_NAMES / data_type_label
# ═══════════════════════════════════════════════════════════════

def test_data_type_names_table():
    assert DATA_TYPE_NAMES[0] == "未指定(0)"
    assert {k: DATA_TYPE_NAMES[k] for k in range(1, 16)} == {
        1: "Boolean", 2: "Char", 3: "Byte", 4: "Short", 5: "Word", 6: "Long", 7: "DWord", 8: "Float",
        9: "Double", 10: "String", 11: "BCD", 12: "LBCD", 13: "Date", 14: "LLong", 15: "QWord",
    }
    assert sorted(DATA_TYPE_NAMES) == list(range(16))


def test_data_type_label_known_unknown_and_none():
    assert data_type_label(0) == "未指定(0)"
    assert data_type_label(8) == "Float"
    assert data_type_label(15) == "QWord"
    assert data_type_label(None) == "—"
    assert data_type_label(16) == "未知(16)"
    assert data_type_label(-1) == "未知(-1)"
    assert data_type_label("8") == "未知(8)", "字串不是代碼，不可被當成 8"
    assert data_type_label(True) == "未知(True)", "bool 不可被當成代碼 1"


@pytest.mark.parametrize("value,expected", [
    (1, True), (8, True), (15, True), (0, False), (16, False), (-1, False),
    (True, False), (8.0, False), ("8", False), (None, False),
])
def test_is_writable_data_type(value, expected):
    assert is_writable_data_type(value) is expected


# ═══════════════════════════════════════════════════════════════
# extract_tag_data_type：各種形狀
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("key", ["data_type", "dataType", "datatype", "tag_data_type",
                                 "servermain.TAG_DATA_TYPE"])
def test_extract_each_candidate_key_at_top_level(key):
    assert extract_tag_data_type({"name": "T1", key: 8}) == 8


@pytest.mark.parametrize("key", ["DATA_TYPE", "DataType", "DATATYPE", "Tag_Data_Type",
                                 "SERVERMAIN.tag_data_type"])
def test_extract_candidate_keys_are_case_insensitive(key):
    assert extract_tag_data_type({key: 4}) == 4


@pytest.mark.parametrize("wrapper", ["tag", "data", "result", "definition", "properties"])
def test_extract_one_level_under_each_wrapper_key(wrapper):
    assert extract_tag_data_type({wrapper: {"name": "T1", "data_type": 9}}) == 9


def test_extract_wrapper_key_is_case_insensitive():
    assert extract_tag_data_type({"Tag": {"dataType": 3}}) == 3


def test_extract_does_not_look_deeper_than_one_wrapper_level():
    assert extract_tag_data_type({"data": {"tag": {"data_type": 8}}}) is None
    assert extract_tag_data_type({"other": {"data_type": 8}}) is None, "不是包裝層的鍵底下不找"
    assert extract_tag_data_type({"tag": [{"data_type": 8}]}) is None, "包裝層必須是 dict"


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (8, 8), (15, 15),
    ("0", 0), ("8", 8), (" 12 ", 12), ("08", 8),
])
def test_extract_int_and_numeric_string(value, expected):
    assert extract_tag_data_type({"data_type": value}) == expected


@pytest.mark.parametrize("name,expected", [
    ("Boolean", 1), ("boolean", 1), ("CHAR", 2), ("Byte", 3), ("Short", 4), ("Word", 5), ("Long", 6),
    ("DWord", 7), ("dword", 7), ("Float", 8), ("fLoAt", 8), ("Double", 9), ("String", 10), ("BCD", 11),
    ("LBCD", 12), ("Date", 13), ("LLong", 14), ("QWord", 15), (" Float ", 8),
    ("Default", 0), ("default", 0), ("未指定(0)", 0),
])
def test_extract_type_name_strings(name, expected):
    assert extract_tag_data_type({"data_type": name}) == expected


@pytest.mark.parametrize("value", [
    True, False,                 # bool 不是數字（True 不可變成 1）
    16, 99, -1, "16", "-1",      # 超出 0–15
    8.0, 8.5,                    # float 不收
    None, "", "  ", "Floaty", "Float Array", "abc", "１２",     # 無法辨識（含全形數字）
    [8], {"v": 8},               # 容器
])
def test_extract_unrecognized_values_return_none(value):
    assert extract_tag_data_type({"data_type": value}) is None


def test_extract_bool_inside_wrapper_is_also_rejected():
    assert extract_tag_data_type({"tag": {"data_type": True}}) is None


@pytest.mark.parametrize("payload", [None, [], [{"data_type": 8}], "8", 8, {}, {"name": "T1"},
                                     {"scaling_type": 8}])
def test_extract_no_candidate_returns_none(payload):
    assert extract_tag_data_type(payload) is None


def test_extract_conflicting_candidates_return_none_not_the_first():
    # 同一層兩個候選鍵數值不一致
    assert extract_tag_data_type({"data_type": 8, "dataType": 0}) is None
    # 第一層與包裝層不一致
    assert extract_tag_data_type({"data_type": 8, "tag": {"data_type": 1}}) is None
    # 兩個包裝層不一致
    assert extract_tag_data_type({"tag": {"data_type": 8}, "properties": {"datatype": 9}}) is None
    # 數字與名稱表示同一個值 → 一致
    assert extract_tag_data_type({"data_type": 8, "tag": {"dataType": "Float"}}) == 8
    assert extract_tag_data_type({"data_type": "8", "datatype": 8}) == 8


def test_extract_one_unrecognized_candidate_poisons_the_result():
    """一個候選認得、另一個認不得：不能挑認得的那個——整體無法確定。"""
    assert extract_tag_data_type({"data_type": 8, "tag": {"data_type": "weird"}}) is None
    assert extract_tag_data_type({"data_type": 8, "dataType": None}) is None
    assert extract_tag_data_type({"data_type": 8, "tag": {"data_type": True}}) is None


def test_extract_ignores_non_candidate_keys_with_similar_names():
    assert extract_tag_data_type({"data_types": 8, "type": 8, "scaled_data_type": 8,
                                  "scaling_scaled_data_type": 8}) is None


def test_extract_realistic_shapes():
    assert extract_tag_data_type({"name": "T1", "address": "ns=2;s=x", "data_type": 8,
                                  "scaling_type": 0}) == 8
    assert extract_tag_data_type({"tag": {"name": "T1", "dataType": "Float"}, "meta": {"v": 1}}) == 8


# ═══════════════════════════════════════════════════════════════
# describe_payload_keys
# ═══════════════════════════════════════════════════════════════

def test_describe_payload_keys_lists_first_two_levels_without_values():
    text = describe_payload_keys({"name": "SECRET-VALUE", "tag": {"address": "x", "data_type": 8},
                                  "scaling_type": 0, "empty": {}})
    assert text == "name, tag{address,data_type}, scaling_type, empty"
    assert "SECRET-VALUE" not in text, "只列鍵名，不可洩漏值"


def test_describe_payload_keys_does_not_go_below_second_level():
    assert describe_payload_keys({"a": {"b": {"c": 1}}}) == "a{b}"


def test_describe_payload_keys_non_dict_and_empty():
    assert "list" in describe_payload_keys([1, 2])
    assert "NoneType" in describe_payload_keys(None)
    assert describe_payload_keys({}) == "(空物件)"


def test_describe_payload_keys_truncates_to_200_chars():
    payload = {f"key_{i:04d}": i for i in range(200)}
    text = describe_payload_keys(payload)
    assert len(text) == 200 and text.endswith("…")
    assert text.startswith("key_0000, key_0001")
    short = describe_payload_keys({"a": 1, "b": 2})
    assert short == "a, b" and "…" not in short


def test_describe_payload_keys_exact_boundary_is_not_truncated():
    payload = {"k" * 200: 1}
    assert describe_payload_keys(payload) == "k" * 200            # 剛好 200 字：不截斷、不加刪節號
    assert describe_payload_keys({"k" * 201: 1}) == "k" * 199 + "…"


# ═══════════════════════════════════════════════════════════════
# diff_tag_payloads
# ═══════════════════════════════════════════════════════════════

def test_diff_identical_is_empty():
    p = {"name": "T1", "data_type": 8, "nested": {"address": "x", "deep": {"v": [1, 2]}}}
    assert diff_tag_payloads(p, {"name": "T1", "data_type": 8,
                                 "nested": {"address": "x", "deep": {"v": [1, 2]}}}) == []


def test_diff_ignores_allowed_data_type_keys_only():
    before = {"name": "T1", "data_type": 0}
    after = {"name": "T1", "data_type": 8}
    assert diff_tag_payloads(before, after) == []
    for key in ("data_type", "dataType", "datatype", "tag_data_type", "servermain.TAG_DATA_TYPE", "DATA_TYPE"):
        assert diff_tag_payloads({key: 0, "x": 1}, {key: 8, "x": 1}) == [], key
    assert diff_tag_payloads({"data_type": 0, "scaling_type": 0},
                             {"data_type": 8, "scaling_type": 1}) == ["scaling_type"]


def test_diff_detects_changed_top_level_value():
    assert diff_tag_payloads({"scaling_type": 0, "name": "T"}, {"scaling_type": 1, "name": "T"}) == ["scaling_type"]


def test_diff_nested_paths():
    before = {"name": "T", "nested": {"address": "a", "deep": {"v": 1}}}
    after = {"name": "T", "nested": {"address": "b", "deep": {"v": 2}}}
    assert diff_tag_payloads(before, after) == ["nested.address", "nested.deep.v"]


def test_diff_added_and_removed_keys():
    assert diff_tag_payloads({"a": 1}, {"a": 1, "b": 2}) == ["b"]
    assert diff_tag_payloads({"a": 1, "b": 2}, {"a": 1}) == ["b"]
    assert diff_tag_payloads({"n": {"a": 1}}, {"n": {"a": 1, "z": 0}}) == ["n.z"]
    assert diff_tag_payloads({"n": {"a": 1, "z": 0}}, {"n": {"a": 1}}) == ["n.z"]


def test_diff_allowed_key_added_or_removed_is_ignored_but_siblings_are_not():
    assert diff_tag_payloads({"a": 1}, {"a": 1, "data_type": 8}) == []
    assert diff_tag_payloads({"a": 1, "data_type": 8}, {"a": 2}) == ["a"]


def test_diff_allowed_key_is_ignored_at_any_depth():
    assert diff_tag_payloads({"tag": {"data_type": 0, "x": 1}}, {"tag": {"data_type": 8, "x": 1}}) == []
    assert diff_tag_payloads({"tag": {"data_type": 0, "x": 1}}, {"tag": {"data_type": 8, "x": 2}}) == ["tag.x"]


def test_diff_type_changes_are_not_hidden():
    assert diff_tag_payloads({"a": 1}, {"a": True}) == ["a"], "1 與 true 不是同一個值"
    assert diff_tag_payloads({"a": 1}, {"a": 1.0}) == ["a"]
    assert diff_tag_payloads({"a": {"b": 1}}, {"a": 5}) == ["a"]
    assert diff_tag_payloads({"a": None}, {"a": 0}) == ["a"]
    assert diff_tag_payloads({"a": "1"}, {"a": 1}) == ["a"]


def test_diff_lists_are_compared_as_a_whole():
    assert diff_tag_payloads({"l": [1, 2]}, {"l": [1, 2]}) == []
    assert diff_tag_payloads({"l": [1, 2]}, {"l": [1, 3]}) == ["l"]
    assert diff_tag_payloads({"l": [1, 2]}, {"l": [1, 2, 3]}) == ["l"]
    assert diff_tag_payloads({"l": [{"a": 1}]}, {"l": [{"a": True}]}) == ["l"]


def test_diff_result_is_sorted_and_custom_allowed_is_honoured():
    before = {"z": 1, "a": 1, "m": {"y": 1, "b": 1}, "skip_me": 1}
    after = {"z": 2, "a": 2, "m": {"y": 2, "b": 2}, "skip_me": 2}
    assert diff_tag_payloads(before, after) == ["a", "m.b", "m.y", "skip_me", "z"]
    assert diff_tag_payloads(before, after, allowed=("skip_me", "m")) == ["a", "z"]
    assert diff_tag_payloads(before, after, allowed=()) == ["a", "m.b", "m.y", "skip_me", "z"]


def test_diff_non_dict_roots():
    assert diff_tag_payloads([1], [1]) == []
    assert diff_tag_payloads([1], [2]) == ["<root>"]
    assert diff_tag_payloads({"a": 1}, [1]) == ["<root>"]


def test_diff_does_not_mutate_inputs():
    before = {"a": {"b": 1}, "data_type": 0}
    after = {"a": {"b": 2}, "data_type": 8}
    snap_b, snap_a = {"a": {"b": 1}, "data_type": 0}, {"a": {"b": 2}, "data_type": 8}
    diff_tag_payloads(before, after)
    assert before == snap_b and after == snap_a


# ═══════════════════════════════════════════════════════════════
# 模組獨立性
# ═══════════════════════════════════════════════════════════════

def test_module_has_no_web_or_db_dependency():
    """app.tag_datatype 必須是純邏輯：import 它不可連帶載入 FastAPI / psycopg2 / requests / app.main 等。"""
    code = (
        "import sys\n"
        "import app.tag_datatype\n"
        "bad = [m for m in ('fastapi', 'starlette', 'psycopg2', 'requests', 'app.main', 'app.pg_client') "
        "if m in sys.modules]\n"
        "sys.exit('unexpected imports: ' + ', '.join(bad) if bad else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            cwd=str(Path(tag_datatype.__file__).resolve().parent.parent),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ═══════════════════════════════════════════════════════════════
# KepwareGatewayClient.set_tag_data_type：PUT body 與驗證
# ═══════════════════════════════════════════════════════════════

class _FakeHttpResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {"ok": True} if body is None else body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(f"{self.status_code} Error")
            err.response = self
            raise err

    def json(self):
        return self._body


@pytest.fixture
def put_calls(monkeypatch):
    """攔截 app.kw_gw_client 內的 requests.put，記錄呼叫（url / body / 其餘參數）。"""
    import app.kw_gw_client as mod
    calls = []

    def fake_put(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _FakeHttpResponse()

    monkeypatch.setattr(mod.requests, "put", fake_put)
    return calls


def _logged_in_client(**kwargs):
    client = KepwareGatewayClient("http://gw.test:1/", **kwargs)
    client.token = "tok"
    return client


def test_set_tag_data_type_body_has_only_the_four_fields(put_calls):
    client = _logged_in_client(verify=False, proxies={"http": None, "https": None})
    result = client.set_tag_data_type("CH1", "DEV1", "T1", 8)
    assert result == {"ok": True}
    assert len(put_calls) == 1
    call = put_calls[0]
    assert call["url"] == "http://gw.test:1/api/config/tags"
    assert call["json"] == {"channel_name": "CH1", "device_name": "DEV1", "tag_name": "T1", "data_type": 8}
    assert set(call["json"]) == {"channel_name", "device_name", "tag_name", "data_type"}
    assert not any("scal" in k for k in call["json"]), "不得夾帶任何 scaling 欄位"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["timeout"] == 15 and call["verify"] is False and call["proxies"] == {"http": None, "https": None}


def test_set_tag_data_type_with_tag_group_adds_exactly_one_more_field(put_calls):
    _logged_in_client().set_tag_data_type("CH1", "DEV1", "T1", 1, tag_group="2F.CHS")
    body = put_calls[0]["json"]
    assert body == {"channel_name": "CH1", "device_name": "DEV1", "tag_name": "T1",
                    "data_type": 1, "tag_group": "2F.CHS"}


@pytest.mark.parametrize("group", [None, ""])
def test_set_tag_data_type_without_group_has_no_tag_group_key(put_calls, group):
    _logged_in_client().set_tag_data_type("CH1", "DEV1", "T1", 8, tag_group=group)
    assert "tag_group" not in put_calls[0]["json"]


@pytest.mark.parametrize("bad", [0, 16, -1, True, False, None, "8", 8.0, 8.5])
def test_set_tag_data_type_rejects_invalid_codes_without_calling_gateway(put_calls, bad):
    with pytest.raises(ValueError):
        _logged_in_client().set_tag_data_type("CH1", "DEV1", "T1", bad)
    assert put_calls == [], "不合法的型態代碼不可送出任何請求"


def test_set_tag_data_type_requires_login(put_calls):
    client = KepwareGatewayClient("http://gw.test:1")
    with pytest.raises(RuntimeError):
        client.set_tag_data_type("CH1", "DEV1", "T1", 8)
    assert put_calls == []


def test_set_tag_data_type_http_error_propagates(monkeypatch):
    import app.kw_gw_client as mod
    import requests
    monkeypatch.setattr(mod.requests, "put", lambda url, **kw: _FakeHttpResponse(500))
    with pytest.raises(requests.HTTPError):
        _logged_in_client().set_tag_data_type("CH1", "DEV1", "T1", 8)


def test_fake_client_signatures_match_the_real_client():
    """假 Kepware client 的 get_tag / set_tag_data_type 簽章必須與真實 client 相同，
    否則測試會在假 client 上通過、真實呼叫卻因參數不符而壞掉。"""
    import inspect
    from tests.fake_kepware import FakeKepwareState, make_fake_kepware_client
    fake_cls = make_fake_kepware_client(FakeKepwareState())
    for name in ("get_tag", "set_tag_data_type"):
        real = list(inspect.signature(getattr(KepwareGatewayClient, name)).parameters.items())
        fake = list(inspect.signature(getattr(fake_cls, name)).parameters.items())
        assert [n for n, _ in real] == [n for n, _ in fake], name
        assert [p.default for _, p in real] == [p.default for _, p in fake], name
