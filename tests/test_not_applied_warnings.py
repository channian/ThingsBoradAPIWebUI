# -*- coding: utf-8 -*-
"""Step 3（建點）／Step 5（Scale）對 Kepware 回應 `data.not_applied` 的「只記錄、不改判定」警告。

背景：Kepware 逐屬性套用——HTTP 200 / success:true 時，`data.not_applied`（屬性名→目前值）仍可能列出
「嘗試要改、但沒套用成功」的屬性。data_type 校正已把它當失敗；Step 3 / Step 5 目前**只記錄警告**
（使用者決定：先收集一陣子真實資料，再決定要不要改成失敗），所以：

- 警告放進該台 worker 摘要的 `details`（`kind=not_applied`），gateway_jobs 會補上 `gateway`；
  同時 `view.push_log("warning", ...)` 一行、`log.warning(...)` 寫進伺服器日誌；
- **絕不改變**成功／失敗／skip 的判定、tb_status / scale_status 的更新、add_result 的狀態、
  摘要的 total / success / fail / skip 計數；
- 格式不符（空 dict、None、list、data 不是 dict…）不產生警告；
- 警告記錄本身出錯，也不能把已成功的列翻成失敗。

涵蓋：not_applied_keys / not_applied_message 單元測試（含舊名別名）、_record_not_applied 單元測試、
Step 3 與 Step 5 的端到端（真實 DB + 假 Kepware）、兩台 Gateway 自動路由、取消與 429 重試下摘要欄位不變、
data_type 檢查的 details 沒有被加上 kind。
"""
import logging
from types import SimpleNamespace

import pytest

import app.main as m
from app.kw_gw_client import _not_applied_keys, not_applied_keys, not_applied_message
from tests.exec_helpers import (
    URL_A, URL_B, make_gateway, make_two_gateways, post_datatype, post_kw, post_scale,
    row_id, seed_kw_row, seed_scale_row, status_of,
)
from tests.fake_kepware import http_error, make_fake_kepware_client, poll_task, wait_lock_released

# 使用者提供的真實回應訊息
REAL_MESSAGE = ("Not all properties were applied. This could be due to active client reference "
                "or property is disallowed/disabled/read-only.")


def na_response(not_applied, message=REAL_MESSAGE, **extra_data):
    """仿真實回應：success=true + data.not_applied（+ data.message / code）。message=None＝不放 message。"""
    data = {"not_applied": not_applied, "code": 200}
    if message is not None:
        data["message"] = message
    data.update(extra_data)
    return {"success": True, "message": "Tag 更新成功", "data": data}


# 刻意讓 dict 的插入順序「不是」字母順序：RAW_HIGH 應排在 UNITS 前面（驗證 keys 有排序）
REAL_RESPONSE = na_response({"servermain.TAG_SCALING_UNITS": "", "servermain.TAG_SCALING_RAW_HIGH": 1000})
REAL_KEYS = ["servermain.TAG_SCALING_RAW_HIGH", "servermain.TAG_SCALING_UNITS"]


def detail(step, tag, keys, message, gateway, id_):
    """摘要 details 裡一筆 not_applied 警告的完整預期（欄位固定，多一個少一個都要失敗）。"""
    return {"kind": "not_applied", "step": step, "tag_name": tag, "keys": keys,
            "message": message, "id": id_, "gateway": gateway}


# 各種「格式不符、不該產生警告」的回應
MALFORMED_RESPONSES = [
    {"success": True, "data": {"not_applied": {}, "code": 200}},            # 空 dict
    {"success": True, "data": {"not_applied": None}},                       # None
    {"success": True, "data": {"not_applied": ["servermain.X"]}},           # list（不是 dict）
    {"success": True, "data": {"not_applied": "servermain.X"}},             # 字串
    {"success": True, "data": "oops"},                                      # data 不是 dict
    {"success": True, "data": ["servermain.X"]},                            # data 是 list
    {"success": True, "data": None},
    {"success": True, "not_applied": {"servermain.X": 1}},                  # 不在 data 底下
    {"success": True},                                                      # 沒有 data
    None,                                                                   # 回應本身是 None
    ["not_applied"],                                                        # 回應本身是 list
]


def _install(monkeypatch, states):
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(states))


def _run_kw(api_client, headers, timeout=10.0, **body):
    resp = post_kw(api_client, headers, **body)
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"], poll_task(api_client, headers, resp.json()["task_id"], timeout=timeout)


def _run_scale(api_client, headers, timeout=10.0, **body):
    resp = post_scale(api_client, headers, **body)
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"], poll_task(api_client, headers, resp.json()["task_id"], timeout=timeout)


def _task(task_id):
    return m.task_manager.get_task(task_id)


def _warning_logs(task_id):
    """任務日誌中 level=warning 的訊息。"""
    return [e["message"] for e in _task(task_id).log_history if e["level"] == "warning"]


def _na_server_logs(caplog):
    """伺服器日誌（logger "main"）中的 not_applied 警告訊息。"""
    return [r.getMessage() for r in caplog.records
            if r.name == "main" and r.levelno == logging.WARNING and "[not_applied]" in r.getMessage()]


def _results(task_id):
    """Task.add_result 的紀錄 {tag: (status, detail)}。"""
    return {r["name"]: (r["status"], r["detail"]) for r in _task(task_id).results}


def _by_tag(details):
    return {d["tag_name"]: d for d in details}


# ═══════════════════════════════════════════════════════════════
# 一、not_applied_keys / not_applied_message（純邏輯）
# ═══════════════════════════════════════════════════════════════

def test_not_applied_keys_returns_sorted_property_names():
    assert not_applied_keys(REAL_RESPONSE) == REAL_KEYS
    assert not_applied_keys({"data": {"not_applied": {"b": 1, "c": 3, "a": 2}}}) == ["a", "b", "c"]


def test_not_applied_keys_coerces_names_to_str():
    assert not_applied_keys({"data": {"not_applied": {2: "x", "10": "y"}}}) == ["10", "2"]


def test_underscore_alias_is_the_same_function():
    """舊名 _not_applied_keys 是別名（既有程式與測試仍在用），行為必須與公開版完全相同。"""
    assert _not_applied_keys is not_applied_keys
    assert _not_applied_keys(REAL_RESPONSE) == REAL_KEYS
    assert _not_applied_keys({"data": {"not_applied": {}}}) == []


@pytest.mark.parametrize("bad", MALFORMED_RESPONSES + [42, "x", {}, {"data": {}}, {"data": {"message": "m"}}])
def test_not_applied_keys_ignores_malformed_or_empty(bad):
    assert not_applied_keys(bad) == []
    assert _not_applied_keys(bad) == []


def test_not_applied_message_returns_data_message():
    assert not_applied_message(REAL_RESPONSE) == REAL_MESSAGE
    # 只負責取說明文字：不判斷 not_applied 是否非空
    assert not_applied_message({"data": {"message": "只有說明"}}) == "只有說明"


@pytest.mark.parametrize("bad", [
    None, [], "x", 42, {}, {"data": None}, {"data": "oops"}, {"data": []}, {"data": {}},
    {"data": {"message": None}}, {"data": {"message": ""}}, {"message": "外層的 message 不算"},
])
def test_not_applied_message_missing_returns_empty_string(bad):
    assert not_applied_message(bad) == ""


def test_not_applied_message_is_truncated_to_200_chars():
    assert not_applied_message({"data": {"message": "x" * 200}}) == "x" * 200
    assert not_applied_message({"data": {"message": "y" * 201}}) == "y" * 200
    long = not_applied_message({"data": {"message": "字" * 500}})
    assert long == "字" * 200 and len(long) == 200


# ═══════════════════════════════════════════════════════════════
# 二、_record_not_applied（純邏輯：用假的 view / part）
# ═══════════════════════════════════════════════════════════════

class _FakeView:
    def __init__(self, fail_push_log=False):
        self.logs = []
        self.fail_push_log = fail_push_log

    def push_log(self, level, message):
        if self.fail_push_log:
            raise RuntimeError("push_log 壞了")
        self.logs.append((level, message))


PART = SimpleNamespace(name="GW-X")
ITEM = {"id": 7, "tag_name": "TAG7"}


def test_record_not_applied_appends_one_detail_and_one_warning_log(caplog):
    caplog.set_level(logging.WARNING, logger="main")
    view, details = _FakeView(), []
    assert m._record_not_applied(view, PART, details, "create", ITEM, REAL_RESPONSE) is None
    assert details == [{"kind": "not_applied", "step": "create", "tag_name": "TAG7",
                        "keys": REAL_KEYS, "message": REAL_MESSAGE, "id": 7}], "gateway 由 gateway_jobs 補，這裡不能自己加"
    assert len(view.logs) == 1 and view.logs[0][0] == "warning"
    assert "\n" not in view.logs[0][1] and "TAG7" in view.logs[0][1]
    server = _na_server_logs(caplog)
    assert len(server) == 1 and "GW-X" in server[0] and "TAG7" in server[0] and REAL_KEYS[0] in server[0]


@pytest.mark.parametrize("bad", MALFORMED_RESPONSES)
def test_record_not_applied_ignores_malformed_responses(bad, caplog):
    caplog.set_level(logging.WARNING, logger="main")
    view, details = _FakeView(), []
    assert m._record_not_applied(view, PART, details, "scale", ITEM, bad) is None
    assert details == [] and view.logs == [] and _na_server_logs(caplog) == []


@pytest.mark.parametrize("view,item", [
    (_FakeView(fail_push_log=True), ITEM),     # 任務日誌寫入出錯
    (_FakeView(), {"tag_name": "TAG7"}),       # 缺 id（不該發生，但不能炸）
])
def test_record_not_applied_never_raises(view, item, caplog):
    """呼叫端是在「這筆已記成功」之後才呼叫它：這裡的任何例外都不可外洩，否則成功的列會被翻成失敗。"""
    caplog.set_level(logging.WARNING, logger="main")
    assert m._record_not_applied(view, PART, [], "create", item, REAL_RESPONSE) is None
    assert any("記錄警告失敗" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════════
# 三、Step 3（建點）
# ═══════════════════════════════════════════════════════════════

@pytest.mark.needs_db
def test_step3_not_applied_is_recorded_but_rows_still_succeed(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    """NA_T1（真實回應）與 NA_T3（單一屬性、超長說明）回應含 not_applied：**仍計入 success、tb_status 仍為 done**，
    摘要 details 各一筆（keys 已排序、message 正確、id 正確、gateway 已補）；沒有 not_applied 的 OK_T2 不產生。"""
    gw = make_gateway("GW-A")
    for tag in ("NA_T1", "OK_T2", "NA_T3"):
        seed_kw_row(tag, "CH1")
    state = make_state()
    state.set_create_response("NA_T1", REAL_RESPONSE)
    state.set_create_response("NA_T3", na_response({"servermain.TAG_DESCRIPTION": "舊說明"}, message="Z" * 300))
    _install(monkeypatch, state)
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_kw(api_client, admin_headers, gateway_id=gw["id"])

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"], s["blocked"]) == (3, 3, 0, 0, 0), s
    assert s["errors"] == [] and s["cancelled"] is False and s["cancelled_remaining"] == 0
    assert (s["gateways"][0]["total"], s["gateways"][0]["success"], s["gateways"][0]["fail"]) == (3, 3, 0)
    assert result["progress"]["success"] == 3 and result["progress"]["fail"] == 0
    for tag in ("NA_T1", "OK_T2", "NA_T3"):
        assert status_of(tag) == "done", f"{tag}：有 not_applied 也只是警告，tb_status 必須照常 done"
    assert _results(task_id) == {t: ("success", "") for t in ("NA_T1", "OK_T2", "NA_T3")}, \
        "add_result 的狀態與說明不可因警告而改變"

    assert len(s["details"]) == 2
    by_tag = _by_tag(s["details"])
    assert by_tag["NA_T1"] == detail("create", "NA_T1", REAL_KEYS, REAL_MESSAGE, "GW-A", row_id("NA_T1"))
    assert by_tag["NA_T3"] == detail("create", "NA_T3", ["servermain.TAG_DESCRIPTION"], "Z" * 200,
                                     "GW-A", row_id("NA_T3")), "說明要截斷到 200 字"
    assert "OK_T2" not in by_tag

    # 任務日誌：每筆警告一行，沒有警告的列不產生
    warns = _warning_logs(task_id)
    assert len(warns) == 2 and all("\n" not in w for w in warns), warns
    w1 = next(w for w in warns if "NA_T1" in w)
    assert all(k in w1 for k in REAL_KEYS) and REAL_MESSAGE in w1
    assert not any("OK_T2" in w for w in warns), "沒有 not_applied 的列不該有警告日誌"
    # 伺服器日誌：含 Gateway 名稱、tag、屬性清單
    server = _na_server_logs(caplog)
    assert len(server) == 2, server
    s1 = next(x for x in server if "NA_T1" in x)
    assert "GW-A" in s1 and all(k in s1 for k in REAL_KEYS)
    assert wait_lock_released(m)


@pytest.mark.needs_db
def test_step3_all_clean_means_empty_details_and_no_warnings(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    """全部都沒有 not_applied：摘要 details == []（gateway_jobs 既有行為），沒有任何警告日誌。"""
    gw = make_gateway("GW-A")
    for tag in ("C1", "C2"):
        seed_kw_row(tag, "CH1")
    state = make_state()
    state.set_create_response("C2", {"success": True, "data": {"not_applied": {}}})
    _install(monkeypatch, state)
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_kw(api_client, admin_headers, gateway_id=gw["id"])
    s = result["summary"]
    assert s["details"] == []
    assert (s["success"], s["fail"]) == (2, 0)
    assert _warning_logs(task_id) == [] and _na_server_logs(caplog) == []


@pytest.mark.needs_db
def test_step3_malformed_not_applied_produces_no_warning(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    """空 dict／None／list／字串／data 不是 dict／回應本身是 None 或 list…：不產生警告，列照常成功。"""
    gw = make_gateway("GW-A")
    state = make_state()
    tags = []
    for i, payload in enumerate(MALFORMED_RESPONSES):
        tag = f"MF{i:02d}"
        tags.append(tag)
        seed_kw_row(tag, "CH1")
        state.set_create_response(tag, payload)
    _install(monkeypatch, state)
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_kw(api_client, admin_headers, gateway_id=gw["id"])
    s = result["summary"]
    assert s["details"] == [], s["details"]
    assert (s["total"], s["success"], s["fail"]) == (len(tags), len(tags), 0)
    assert all(status_of(t) == "done" for t in tags)
    assert _warning_logs(task_id) == [] and _na_server_logs(caplog) == []


@pytest.mark.needs_db
def test_step3_two_gateways_each_warning_carries_its_own_gateway_name(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    """自動路由兩台：GW-A 的警告帶 GW-A、GW-B 的帶 GW-B（摘要 details 與日誌皆然）；沒警告的列不產生。"""
    make_two_gateways()
    for tag, ch in (("A1", "CHA"), ("A2", "CHA"), ("B1", "CHB"), ("B2", "CHB")):
        seed_kw_row(tag, ch)
    sa, sb = make_state(), make_state()
    sa.set_create_response("A1", na_response({"servermain.TAG_ADDRESS": "x"}, message="說明 A"))
    sb.set_create_response("B2", na_response({"servermain.TAG_B": 1, "servermain.TAG_A": 2}, message="說明 B"))
    _install(monkeypatch, {URL_A: sa, URL_B: sb})
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_kw(api_client, admin_headers)    # 省略 gateway_id＝自動

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["blocked"]) == (4, 4, 0, 0)
    by_tag = _by_tag(s["details"])
    assert set(by_tag) == {"A1", "B2"} and len(s["details"]) == 2
    assert by_tag["A1"] == detail("create", "A1", ["servermain.TAG_ADDRESS"], "說明 A", "GW-A", row_id("A1"))
    assert by_tag["B2"] == detail("create", "B2", ["servermain.TAG_A", "servermain.TAG_B"], "說明 B",
                                  "GW-B", row_id("B2"))
    assert all(status_of(t) == "done" for t in ("A1", "A2", "B1", "B2"))
    # 兩台以上：任務日誌會自動加 [Gateway 名稱] 前綴；伺服器日誌也帶各自的 Gateway 名稱
    warns = _warning_logs(task_id)
    assert len(warns) == 2
    assert next(w for w in warns if "A1" in w).startswith("[GW-A] ")
    assert next(w for w in warns if "B2" in w).startswith("[GW-B] ")
    server = _na_server_logs(caplog)
    assert "GW-A" in next(x for x in server if "A1" in x) and "GW-B" not in next(x for x in server if "A1" in x)
    assert "GW-B" in next(x for x in server if "B2" in x) and "GW-A" not in next(x for x in server if "B2" in x)


@pytest.mark.needs_db
def test_step3_failed_and_already_exists_rows_never_produce_warnings(
        api_client, admin_headers, monkeypatch, make_state):
    """409「已存在」沒有回傳值、500 失敗：都不處理 not_applied（即使假 Kepware 預設了含 not_applied 的回傳，
    例外也先於回傳生效）；它們的判定不變：409＝成功、500＝失敗。只有真正成功且帶 not_applied 的列才有警告。"""
    gw = make_gateway("GW-A")
    for tag in ("N_OK", "N_409", "N_500"):
        seed_kw_row(tag, "CH1")
    state = make_state()
    for tag in ("N_OK", "N_409", "N_500"):
        state.set_create_response(tag, REAL_RESPONSE)
    state.queue_create_tag_error("N_409", http_error(409))
    state.queue_create_tag_error("N_500", http_error(500))
    _install(monkeypatch, state)

    task_id, result = _run_kw(api_client, admin_headers, gateway_id=gw["id"])
    s = result["summary"]
    assert (s["success"], s["fail"], s["skip"]) == (2, 1, 0), s
    assert [e["name"] for e in s["errors"]] == ["N_500"]
    assert [d["tag_name"] for d in s["details"]] == ["N_OK"]
    assert (status_of("N_OK"), status_of("N_409"), status_of("N_500")) == ("done", "done", "pending")
    assert _results(task_id)["N_409"] == ("success", "已存在")


@pytest.mark.needs_db
def test_step3_warning_failure_never_flips_a_successful_row_to_failed(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    """警告記錄本身出錯（這裡讓取說明文字的函式丟例外）：該列仍是成功、tb_status=done、沒有錯誤，只留伺服器日誌。"""
    gw = make_gateway("GW-A")
    seed_kw_row("R1", "CH1")
    seed_kw_row("R2", "CH1")
    state = make_state()
    state.set_create_response("R1", REAL_RESPONSE)

    def boom(_result):
        raise RuntimeError("取說明文字壞了")
    monkeypatch.setattr(m, "not_applied_message", boom)
    _install(monkeypatch, state)
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_kw(api_client, admin_headers, gateway_id=gw["id"])
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"]) == (2, 2, 0), s
    assert s["errors"] == [] and s["details"] == []
    assert status_of("R1") == "done" and status_of("R2") == "done"
    assert _results(task_id) == {"R1": ("success", ""), "R2": ("success", "")}
    assert any("記錄警告失敗" in r.getMessage() for r in caplog.records)


@pytest.mark.needs_db
def test_step3_cancel_keeps_warnings_of_processed_rows_and_summary_fields_unchanged(
        api_client, admin_headers, monkeypatch, make_state):
    """執行中取消：T1、T2（正在建點時被取消、放行後完成）的警告都保留，T3（有 not_applied 但未處理）不會出現；
    摘要的計數與取消欄位與沒有警告時完全一致（對照既有 test_cancel_running_kw_execute_keeps_finished_rows_done）。"""
    gw = make_gateway("GW-A")
    for tag in ("T1", "T2", "T3", "T4"):
        seed_kw_row(tag, "CH1")
    state = make_state()
    for tag in ("T1", "T2", "T3"):
        state.set_create_response(tag, REAL_RESPONSE)
    gate = state.add_gate("create_tag", "T2")
    _install(monkeypatch, state)

    resp = post_kw(api_client, admin_headers, gateway_id=gw["id"])
    task_id = resp.json()["task_id"]
    gate.wait_entered()
    assert api_client.post(f"/api/tasks/{task_id}/cancel", headers=admin_headers).status_code == 200
    gate.open()
    result = poll_task(api_client, admin_headers, task_id)

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (4, 2, 0, 0), s
    assert s["cancelled"] is True and s["cancelled_remaining"] == 2 and s["errors"] == []
    assert result["status"] == "cancelled"
    assert (status_of("T1"), status_of("T2"), status_of("T3"), status_of("T4")) == ("done", "done", "pending", "pending")
    assert sorted(d["tag_name"] for d in s["details"]) == ["T1", "T2"]
    assert all(d["gateway"] == "GW-A" and d["step"] == "create" for d in s["details"])
    assert wait_lock_released(m)


@pytest.mark.needs_db
def test_step3_429_retry_records_a_single_warning_and_counts_unchanged(
        api_client, admin_headers, monkeypatch, make_state):
    """第一次 429（Retry-After 1）、重試成功且回應含 not_applied：只記一筆警告（不是兩筆），
    create_tag 呼叫兩次，success=1、fail=0，與既有 test_step3_429_retry_then_success 的計數一致。"""
    gw = make_gateway("GW-A")
    seed_kw_row("T_429", "CH1")
    state = make_state()
    state.queue_create_tag_error("T_429", http_error(429, retry_after=1))
    state.set_create_response("T_429", REAL_RESPONSE)
    _install(monkeypatch, state)

    task_id, result = _run_kw(api_client, admin_headers, timeout=15.0, gateway_id=gw["id"])
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (1, 1, 0, 0), s
    assert len([c for c in state.create_tag_calls if c["tag_name"] == "T_429"]) == 2
    assert s["details"] == [detail("create", "T_429", REAL_KEYS, REAL_MESSAGE, "GW-A", row_id("T_429"))]
    assert status_of("T_429") == "done"
    assert len([w for w in _warning_logs(task_id) if "未套用屬性" in w]) == 1


# ═══════════════════════════════════════════════════════════════
# 四、Step 5（Scale）
# ═══════════════════════════════════════════════════════════════

@pytest.mark.needs_db
def test_step5_not_applied_is_recorded_but_rows_still_succeed(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    """SC_A（真實回應）／SC_E（沒有 data.message）回應含 not_applied：**仍計入 success、scale_status 仍為 done**，
    details 為 step=scale、tag_name 是暫存列的 tag 名稱（不是 Kepware 全名）；SC_D 乾淨；
    scale_enabled=false 的 SC_B 被跳過（即使 Kepware 若被呼叫會回 not_applied）、資料不全的 SC_C 失敗：都不產生警告。"""
    gw = make_gateway("GW-A")
    seed_scale_row("SC_A", "CH1")
    seed_scale_row("SC_B", "CH1", scale_enabled=False)
    seed_scale_row("SC_C", "CH1", scaled_low=None)            # scale_enabled 但缺 scaled_low → scale_error
    seed_scale_row("SC_D", "CH1")
    seed_scale_row("SC_E", "CH1")
    state = make_state()
    state.set_scaling_response("2F.CHS.SC_A", REAL_RESPONSE)
    state.set_scaling_response("2F.CHS.SC_B", REAL_RESPONSE)
    state.set_scaling_response("2F.CHS.SC_C", REAL_RESPONSE)
    state.set_scaling_response("2F.CHS.SC_E", na_response({"servermain.TAG_SCALING_UNITS": ""}, message=None))
    _install(monkeypatch, state)
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_scale(api_client, admin_headers, gateway_id=gw["id"])

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"], s["blocked"]) == (5, 3, 1, 1, 0), s
    assert [e["tag_name"] for e in s["errors"]] == ["SC_C"]
    assert (status_of("SC_A", "scale_status"), status_of("SC_B", "scale_status"),
            status_of("SC_C", "scale_status"), status_of("SC_D", "scale_status"),
            status_of("SC_E", "scale_status")) == ("done", "skip", "pending", "done", "done")
    assert sorted(c["tag_name"] for c in state.set_tag_scaling_calls) == [
        "2F.CHS.SC_A", "2F.CHS.SC_D", "2F.CHS.SC_E"], "B（未啟用）與 C（資料不全）本來就不打 Kepware"
    res = _results(task_id)
    assert res["SC_A"] == ("success", "") and res["SC_E"] == ("success", "")
    assert res["SC_B"][0] == "skip" and res["SC_C"][0] == "fail"

    assert len(s["details"]) == 2
    by_tag = _by_tag(s["details"])
    assert by_tag["SC_A"] == detail("scale", "SC_A", REAL_KEYS, REAL_MESSAGE, "GW-A", row_id("SC_A"))
    assert by_tag["SC_E"] == detail("scale", "SC_E", ["servermain.TAG_SCALING_UNITS"], "", "GW-A", row_id("SC_E"))

    warns = [w for w in _warning_logs(task_id) if "未套用屬性" in w]
    assert len(warns) == 2
    assert not any("SC_B" in w or "SC_C" in w or "SC_D" in w for w in warns)
    assert "Kepware 說明" in next(w for w in warns if "SC_A" in w)
    assert "Kepware 說明" not in next(w for w in warns if "SC_E" in w), "沒有 data.message 就不要印空的說明"
    server = _na_server_logs(caplog)
    assert len(server) == 2 and all("GW-A" in x for x in server)
    assert any("SC_A" in x and all(k in x for k in REAL_KEYS) for x in server)


@pytest.mark.needs_db
def test_step5_all_clean_or_malformed_means_empty_details(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    """全部乾淨：details == []。格式不符的回應（含 None／list）一樣不產生警告、列照常成功。"""
    gw = make_gateway("GW-A")
    state = make_state()
    tags = ["CL_0"] + [f"MF{i:02d}" for i in range(len(MALFORMED_RESPONSES))]
    for tag in tags:
        seed_scale_row(tag, "CH1")
    for i, payload in enumerate(MALFORMED_RESPONSES):
        state.set_scaling_response(f"2F.CHS.MF{i:02d}", payload)
    _install(monkeypatch, state)
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_scale(api_client, admin_headers, gateway_id=gw["id"])
    s = result["summary"]
    assert s["details"] == [], s["details"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (len(tags), len(tags), 0, 0)
    assert all(status_of(t, "scale_status") == "done" for t in tags)
    assert _warning_logs(task_id) == [] and _na_server_logs(caplog) == []


@pytest.mark.needs_db
def test_step5_two_gateways_each_warning_carries_its_own_gateway_name(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    make_two_gateways()
    for tag, ch in (("A1", "CHA"), ("A2", "CHA"), ("B1", "CHB"), ("B2", "CHB")):
        seed_scale_row(tag, ch)
    sa, sb = make_state(), make_state()
    sa.set_scaling_response("2F.CHS.A2", na_response({"servermain.TAG_SCALING_RAW_LOW": 0}, message="說明 A"))
    sb.set_scaling_response("2F.CHS.B1", na_response({"servermain.TAG_Z": 1, "servermain.TAG_Y": 2}, message="說明 B"))
    _install(monkeypatch, {URL_A: sa, URL_B: sb})
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_scale(api_client, admin_headers)   # 自動

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"], s["blocked"]) == (4, 4, 0, 0, 0)
    by_tag = _by_tag(s["details"])
    assert set(by_tag) == {"A2", "B1"} and len(s["details"]) == 2
    assert by_tag["A2"] == detail("scale", "A2", ["servermain.TAG_SCALING_RAW_LOW"], "說明 A", "GW-A", row_id("A2"))
    assert by_tag["B1"] == detail("scale", "B1", ["servermain.TAG_Y", "servermain.TAG_Z"], "說明 B",
                                  "GW-B", row_id("B1"))
    warns = _warning_logs(task_id)
    assert next(w for w in warns if "A2" in w).startswith("[GW-A] ")
    assert next(w for w in warns if "B1" in w).startswith("[GW-B] ")
    server = _na_server_logs(caplog)
    assert "GW-A" in next(x for x in server if "A2" in x) and "GW-B" not in next(x for x in server if "A2" in x)
    assert "GW-B" in next(x for x in server if "B1" in x) and "GW-A" not in next(x for x in server if "B1" in x)


@pytest.mark.needs_db
def test_step5_warning_failure_never_flips_a_successful_row_to_failed(
        api_client, admin_headers, monkeypatch, make_state, caplog):
    gw = make_gateway("GW-A")
    seed_scale_row("R1", "CH1")
    seed_scale_row("R2", "CH1")
    state = make_state()
    state.set_scaling_response("2F.CHS.R1", REAL_RESPONSE)

    def boom(_result):
        raise RuntimeError("取屬性清單壞了")
    monkeypatch.setattr(m, "not_applied_keys", boom)
    _install(monkeypatch, state)
    caplog.set_level(logging.WARNING, logger="main")

    task_id, result = _run_scale(api_client, admin_headers, gateway_id=gw["id"])
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (2, 2, 0, 0), s
    assert s["errors"] == [] and s["details"] == []
    assert status_of("R1", "scale_status") == "done" and status_of("R2", "scale_status") == "done"
    assert _results(task_id) == {"R1": ("success", ""), "R2": ("success", "")}
    assert any("記錄警告失敗" in r.getMessage() for r in caplog.records)


@pytest.mark.needs_db
def test_step5_cancel_keeps_warnings_of_processed_rows_and_summary_fields_unchanged(
        api_client, admin_headers, monkeypatch, make_state):
    gw = make_gateway("GW-A")
    for tag in ("S1", "S2", "S3", "S4"):
        seed_scale_row(tag, "CH1")
    state = make_state()
    for tag in ("S1", "S2", "S3"):
        state.set_scaling_response(f"2F.CHS.{tag}", REAL_RESPONSE)
    gate = state.add_gate("set_tag_scaling", "2F.CHS.S2")
    _install(monkeypatch, state)

    task_id = post_scale(api_client, admin_headers, gateway_id=gw["id"]).json()["task_id"]
    gate.wait_entered()
    assert api_client.post(f"/api/tasks/{task_id}/cancel", headers=admin_headers).status_code == 200
    gate.open()
    result = poll_task(api_client, admin_headers, task_id)

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (4, 2, 0, 0), s
    assert s["cancelled"] is True and s["cancelled_remaining"] == 2 and s["errors"] == []
    assert [status_of(t, "scale_status") for t in ("S1", "S2", "S3", "S4")] == ["done", "done", "pending", "pending"]
    assert sorted(d["tag_name"] for d in s["details"]) == ["S1", "S2"]
    assert all(d["gateway"] == "GW-A" and d["step"] == "scale" for d in s["details"])
    assert wait_lock_released(m)


@pytest.mark.needs_db
def test_step5_429_retry_records_a_single_warning_and_counts_unchanged(
        api_client, admin_headers, monkeypatch, make_state):
    gw = make_gateway("GW-A")
    seed_scale_row("S_429", "CH1")
    state = make_state()
    state.queue_scaling_error("2F.CHS.S_429", http_error(429, retry_after=1))
    state.set_scaling_response("2F.CHS.S_429", REAL_RESPONSE)
    _install(monkeypatch, state)

    task_id, result = _run_scale(api_client, admin_headers, timeout=15.0, gateway_id=gw["id"])
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (1, 1, 0, 0), s
    assert len(state.set_tag_scaling_calls) == 2
    assert s["details"] == [detail("scale", "S_429", REAL_KEYS, REAL_MESSAGE, "GW-A", row_id("S_429"))]
    assert status_of("S_429", "scale_status") == "done"


@pytest.mark.needs_db
def test_step5_http_failure_is_still_a_failure_without_warning(
        api_client, admin_headers, monkeypatch, make_state):
    """Scale 對 Kepware 呼叫失敗（500）：仍是失敗、scale_status 維持 pending、不產生警告。"""
    gw = make_gateway("GW-A")
    seed_scale_row("S_500", "CH1")
    seed_scale_row("S_OK", "CH1")
    state = make_state()
    state.set_scaling_response("2F.CHS.S_500", REAL_RESPONSE)
    state.queue_scaling_error("2F.CHS.S_500", http_error(500))
    _install(monkeypatch, state)

    task_id, result = _run_scale(api_client, admin_headers, gateway_id=gw["id"])
    s = result["summary"]
    assert (s["success"], s["fail"]) == (1, 1) and [e["tag_name"] for e in s["errors"]] == ["S_500"]
    assert s["details"] == []
    assert status_of("S_500", "scale_status") == "pending" and status_of("S_OK", "scale_status") == "done"


# ═══════════════════════════════════════════════════════════════
# 五、不動 data_type 檢查
# ═══════════════════════════════════════════════════════════════

@pytest.mark.needs_db
def test_datatype_check_details_do_not_get_a_kind_field(api_client, admin_headers, monkeypatch, make_state):
    """data_type 檢查的 details（需校正清單）沒有 kind／step 欄位，維持原狀；
    Step 3 / 5 的警告（kind=not_applied）靠 kind 區分，兩者不可混淆。"""
    gw = make_gateway("GW-A")
    seed_kw_row("DT1", "CH1", tb_status="done")
    state = make_state()
    state.set_remote_tag("CH1", "DEV1", "2F.CHS", "DT1", data_type=0)
    _install(monkeypatch, state)

    resp = post_datatype(api_client, admin_headers, gateway_id=gw["id"])
    assert resp.status_code == 200, resp.text
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])
    details = result["summary"]["details"]
    assert len(details) == 1
    assert "kind" not in details[0] and "step" not in details[0], details[0]
    assert set(details[0]) == {"id", "tag_name", "channel_name", "device_name", "tag_groups", "gateway",
                               "actual", "expected", "actual_label", "expected_label"}
