# -*- coding: utf-8 -*-
"""POST /api/kw/datatype-check（Kepware data_type 檢查／校正）的行為測試：真實 DB、假 Kepware client。

這個功能會**寫入正式環境**，所以測試重點是「出錯時寧可停下來」：
- 檢查模式（預設）絕對唯讀：只發 GET，零 PUT；
- 校正模式必須明確給 ids，只對 actual≠expected 的點發 PUT，PUT 的參數只有五個欄位、沒有 scaling；
- 讀不到、讀不懂（含 404／500／欄位缺失／有歧義）一律該筆失敗，絕不猜測、絕不寫入；
- 電路斷路器：前 3 筆成功的 PUT 要讀回驗證（data_type 變成預期值、其他欄位沒動），任一不符就中止這台其餘的筆數；
- 只處理 tb_status=done 的列，且校正前會重新確認；不改任何暫存表狀態；
- 沿用 Step 3 的路由／排隊／取消／安全鎖／歷史紀錄。

並行測試的原則：正確性不靠 sleep；全部用 CallGate（threading.Event）同步，所有等待都有逾時。
"""
import pytest
import requests

import app.main as m
from tests.exec_helpers import (
    URL_A, URL_B, auth, get_task, make_gateway, make_two_gateways, post_datatype, post_kw, row_id,
    seed_kw_row, status_of,
)
from tests.fake_kepware import http_error, make_fake_kepware_client, poll_task, wait_lock_released, wait_until

pytestmark = pytest.mark.needs_db

CH, DEV, GRP = "CH1", "DEV1", "2F.CHS"


def _install(monkeypatch, states):
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(states))


def _seed(state, tag, actual=0, channel=CH, payload=None, **extra):
    """一筆已建點（tb_status=done）的暫存列＋Kepware 上對應的 Tag。
    actual 是 Kepware 上目前的 data_type（None 且沒給 payload＝Kepware 上沒有這個 Tag）；回傳暫存列 id。"""
    extra.setdefault("tb_status", "done")
    seed_kw_row(tag, channel, **extra)
    if actual is not None or payload is not None:
        state.set_remote_tag(channel, DEV, GRP, tag, data_type=actual, payload=payload)
    return row_id(tag)


def _run(api_client, headers, timeout=10.0, **body):
    """送出請求並等任務結束；回傳 (回應 JSON, 任務最終狀態)。"""
    resp = post_datatype(api_client, headers, **body)
    assert resp.status_code == 200, resp.text
    return resp.json(), poll_task(api_client, headers, resp.json()["task_id"], timeout=timeout)


def _args(call):
    """PUT 呼叫記錄（假 client 記錄的參數）。"""
    return dict(call)


def _errors(summary):
    return {e["tag_name"]: e for e in summary["errors"]}


def _task_logs(task_id, level=None):
    task = m.task_manager.get_task(task_id)
    return [e["message"] for e in task.log_history if level is None or e["level"] == level]


def _assert_nothing_written(state):
    assert state.set_tag_data_type_calls == []
    assert state.set_tag_scaling_calls == []
    assert state.create_tag_calls == []
    assert state.delete_tag_calls == []


def _put_tags(state):
    return [c["tag_name"] for c in state.set_tag_data_type_calls]


def _get_tags(state):
    return [c["tag_name"] for c in state.get_tag_calls]


# ═══════════════════════════════════════════════════════════════
# 一、檢查模式：唯讀
# ═══════════════════════════════════════════════════════════════

def test_check_mode_lists_mismatches_including_overrides_and_never_writes(
        api_client, admin_headers, monkeypatch, make_state):
    """檢查模式：找出實際≠預期的點（預期含逐筆覆寫，如覆寫成 Boolean=1），已正確的計入 success 但不在 details；
    整個過程只發 GET：零 PUT、零 scaling／建點／刪除，Kepware 上的資料原封不動，暫存表狀態也不變。"""
    make_gateway("chk-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T_OK", actual=8)                              # 預期 8（預設），實際 8 → 正確
    id_zero = _seed(state, "T_ZERO", actual=0)                  # 預期 8，實際 0 → 需校正
    id_ovr = _seed(state, "T_OVR", actual=8, kw_data_type=1)    # 覆寫成 Boolean(1)，實際 8 → 需校正
    _seed(state, "T_OVR_OK", actual=1, kw_data_type=1)          # 覆寫成 1，實際 1 → 正確
    before = state.remote_snapshot()

    body, result = _run(api_client, admin_headers)
    assert body["queued"] is False and body["blocked"] == 0

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (4, 4, 0, 0), s
    assert s["errors"] == [] and s["cancelled"] is False and s["cancelled_remaining"] == 0
    details = {d["tag_name"]: d for d in s["details"]}
    assert set(details) == {"T_ZERO", "T_OVR"}, "已正確的點不可出現在 details"
    assert details["T_ZERO"] == {
        "id": id_zero, "tag_name": "T_ZERO", "channel_name": CH, "device_name": DEV, "tag_groups": GRP,
        "gateway": "chk-gw", "actual": 0, "expected": 8,
        "actual_label": "未指定(0)", "expected_label": "Float",
    }
    assert details["T_OVR"]["id"] == id_ovr
    assert (details["T_OVR"]["actual"], details["T_OVR"]["expected"]) == (8, 1), "預期值要用逐筆覆寫的值"
    assert (details["T_OVR"]["actual_label"], details["T_OVR"]["expected_label"]) == ("Float", "Boolean")

    assert result["type"] == "kw_datatype_check"
    _assert_nothing_written(state)
    assert sorted(_get_tags(state)) == ["T_OK", "T_OVR", "T_OVR_OK", "T_ZERO"]
    assert state.remote_snapshot() == before, "檢查模式不可改動 Kepware 上的任何資料"
    for tag in ("T_OK", "T_ZERO", "T_OVR", "T_OVR_OK"):
        assert status_of(tag) == "done"
        assert status_of(tag, "pg_status") == "pending" and status_of(tag, "scale_status") == "pending"


def test_check_mode_with_ids_narrows_to_those_rows(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("ids-gw")
    state = make_state()
    _install(monkeypatch, state)
    a = _seed(state, "A", actual=0)
    _seed(state, "B", actual=0)
    c = _seed(state, "C", actual=0)
    _, result = _run(api_client, admin_headers, ids=[a, c])
    assert result["summary"]["total"] == 2
    assert sorted(_get_tags(state)) == ["A", "C"]
    assert sorted(d["tag_name"] for d in result["summary"]["details"]) == ["A", "C"]


def test_check_mode_uses_derived_channel_device_group_including_overrides(
        api_client, admin_headers, monkeypatch, make_state):
    """GET 讀的是推導（含覆寫）出的 channel / device / tag_group；沒有群組＝不帶 tag_group。"""
    make_gateway("path-gw")
    state = make_state()
    _install(monkeypatch, state)
    seed_kw_row("T_DEFAULT", CH, tb_status="done")
    state.set_remote_tag(CH, DEV, GRP, "T_DEFAULT", data_type=8)
    seed_kw_row("T_OVR", CH, tb_status="done", kw_channel="OCH", kw_device="ODEV", kw_tag_groups="X.Y")
    state.set_remote_tag("OCH", "ODEV", "X.Y", "T_OVR", data_type=0)
    seed_kw_row("T_ROOT", CH, device="DEV1", tb_status="done", device_profile="CH1-DEV1")   # 沒有 tag group
    state.set_remote_tag(CH, DEV, "", "T_ROOT", data_type=0)

    _, result = _run(api_client, admin_headers)
    s = result["summary"]
    assert (s["success"], s["fail"]) == (3, 0), s
    calls = {c["tag_name"]: c for c in state.get_tag_calls}
    assert (calls["T_DEFAULT"]["channel_name"], calls["T_DEFAULT"]["device_name"],
            calls["T_DEFAULT"]["tag_group"]) == (CH, DEV, GRP)
    assert (calls["T_OVR"]["channel_name"], calls["T_OVR"]["device_name"],
            calls["T_OVR"]["tag_group"]) == ("OCH", "ODEV", "X.Y")
    assert calls["T_ROOT"]["tag_group"] is None
    assert {d["tag_name"] for d in s["details"]} == {"T_OVR", "T_ROOT"}


# ═══════════════════════════════════════════════════════════════
# 二、讀不到／讀不懂：逐筆失敗並帶清楚原因
# ═══════════════════════════════════════════════════════════════

def test_check_mode_unreadable_rows_fail_with_clear_reasons(api_client, admin_headers, monkeypatch, make_state):
    """404、回應缺欄位、有歧義、HTTP 500 各自失敗並帶清楚原因；不影響其他列；失敗的點不在 details。"""
    make_gateway("err-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T_GOOD", actual=0)
    _seed(state, "T_404", actual=None)                                  # Kepware 上沒有這個 Tag
    _seed(state, "T_NOFIELD", actual=None, payload={"name": "T_NOFIELD", "tag": {"meta": 1}})
    _seed(state, "T_AMBIG", actual=None, payload={"data_type": 8, "tag": {"data_type": 1}})
    _seed(state, "T_500", actual=0)
    state.queue_get_tag_error("T_500", http_error(500))

    _, result = _run(api_client, admin_headers)
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (5, 1, 4, 0), s
    errs = _errors(s)
    assert set(errs) == {"T_404", "T_NOFIELD", "T_AMBIG", "T_500"}
    assert "Kepware 上找不到此 Tag" in errs["T_404"]["reason"] and "channel/device/群組" in errs["T_404"]["reason"]
    assert "無法判讀資料型態" in errs["T_NOFIELD"]["reason"]
    assert "回應欄位" in errs["T_NOFIELD"]["reason"] and "tag{meta}" in errs["T_NOFIELD"]["reason"]
    assert "無法判讀資料型態" in errs["T_AMBIG"]["reason"], "多個候選不一致不可挑一個"
    assert "HTTP 500" in errs["T_500"]["reason"] and "讀取" in errs["T_500"]["reason"]
    assert all(e["gateway"] == "err-gw" for e in s["errors"])
    assert [d["tag_name"] for d in s["details"]] == ["T_GOOD"]
    _assert_nothing_written(state)


def test_check_mode_404_is_not_counted_as_success_nor_in_details(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("nf-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T_404", actual=None)
    _, result = _run(api_client, admin_headers)
    s = result["summary"]
    assert (s["success"], s["fail"]) == (0, 1) and s["details"] == []


def test_check_mode_connection_error_on_read_fails_that_row(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("ce-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T1", actual=0)
    _seed(state, "T2", actual=0)
    state.queue_get_tag_error("T1", requests.exceptions.ConnectionError("連不上"))
    _, result = _run(api_client, admin_headers)
    s = result["summary"]
    assert (s["success"], s["fail"]) == (1, 1)
    assert "讀取失敗" in _errors(s)["T1"]["reason"] and "連不上" in _errors(s)["T1"]["reason"]


def test_expected_data_type_outside_1_to_15_is_failed_without_calling_kepware(
        api_client, admin_headers, monkeypatch, make_state):
    """預期值本身不合法（覆寫成 0 或亂填）：不讀、更不寫；原因指向 Step 3 的覆寫。其他列照常。"""
    make_gateway("inv-gw")
    state = make_state()
    _install(monkeypatch, state)
    id0 = _seed(state, "T_ZERO_OVR", actual=8, kw_data_type=0)
    id99 = _seed(state, "T_99_OVR", actual=8, kw_data_type=99)
    id_ok = _seed(state, "T_FINE", actual=0)

    for body in ({}, {"apply": True, "ids": [id0, id99, id_ok]}):
        state.get_tag_calls.clear()
        state.set_tag_data_type_calls.clear()
        _, result = _run(api_client, admin_headers, **body)
        s = result["summary"]
        errs = _errors(s)
        assert set(errs) == {"T_ZERO_OVR", "T_99_OVR"}, s
        assert "預期資料型態" in errs["T_ZERO_OVR"]["reason"] and "無效" in errs["T_ZERO_OVR"]["reason"]
        assert "Step 3" in errs["T_ZERO_OVR"]["reason"]
        assert set(_get_tags(state)) == {"T_FINE"}, "不合法的預期值那幾列根本不該打 Kepware"
    assert _put_tags(state) == ["T_FINE"], "只有合法的那筆被校正（第二輪的校正模式）"
    assert state.remote_data_type(CH, DEV, GRP, "T_ZERO_OVR") == 8
    assert state.remote_data_type(CH, DEV, GRP, "T_99_OVR") == 8


# ═══════════════════════════════════════════════════════════════
# 三、校正模式：必須有 ids、只寫該寫的
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("ids_field", [None, [], "omit"])
def test_apply_requires_explicit_non_empty_ids(
        ids_field, api_client, admin_headers, monkeypatch, make_state):
    """校正（apply=true）沒有 ids（缺、null、空 list）一律 400，不建任務、不排隊、不碰 Kepware、不寫活動日誌。
    檢查模式不給 ids 則照常（對照組）。"""
    make_gateway("need-ids-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T1", actual=0)
    body = {"apply": True}
    if ids_field != "omit":
        body["ids"] = ids_field
    tasks_before = len(m.task_manager.tasks)

    resp = post_datatype(api_client, admin_headers, **body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "校正必須明確指定要校正的點位（ids）"
    assert len(m.task_manager.tasks) == tasks_before and m.gateway_queue.snapshot() == {}
    assert not state.login_calls and state.get_tag_calls == []
    _assert_nothing_written(state)
    assert m.pg_client.get_activity_logs(action="datatype_fix")["total"] == 0

    ok = post_datatype(api_client, admin_headers)
    assert ok.status_code == 200, "檢查模式不需要 ids"
    poll_task(api_client, admin_headers, ok.json()["task_id"])


def test_apply_only_puts_selected_mismatching_rows_with_exact_body(
        api_client, admin_headers, monkeypatch, make_state):
    """校正：只對「在 ids 內且 actual≠expected」的點發 PUT；已正確的 skip（不發 PUT）；不在 ids 內的完全不碰。
    PUT 的參數恰為 channel/device/tag/data_type/tag_group 五個，沒有任何 scaling；暫存表狀態不變。"""
    make_gateway("fix-gw")
    state = make_state()
    _install(monkeypatch, state)
    a = _seed(state, "A", actual=0)
    b = _seed(state, "B", actual=0, kw_data_type=1)         # 預期 Boolean(1)
    c = _seed(state, "C", actual=8)                         # 已是預期 → skip
    _seed(state, "D", actual=0)                             # 不在 ids 內 → 不碰

    body, result = _run(api_client, admin_headers, apply=True, ids=[a, b, c])
    assert body["queued"] is False
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (3, 2, 0, 1), s
    assert s["errors"] == [] and s["cancelled"] is False and s["cancelled_remaining"] == 0
    assert s["details"] == [], "details 只有檢查模式才有"
    assert result["type"] == "kw_datatype_fix"

    assert _put_tags(state) == ["A", "B"], "已正確的 C、不在 ids 內的 D 都不可 PUT"
    assert _args(state.set_tag_data_type_calls[0]) == {
        "channel_name": CH, "device_name": DEV, "tag_name": "A", "data_type": 8, "tag_group": GRP}
    assert _args(state.set_tag_data_type_calls[1]) == {
        "channel_name": CH, "device_name": DEV, "tag_name": "B", "data_type": 1, "tag_group": GRP}
    for call in state.set_tag_data_type_calls:
        assert set(call) == {"channel_name", "device_name", "tag_name", "data_type", "tag_group"}
        assert not any("scal" in k for k in call)
    assert state.set_tag_scaling_calls == [] and state.create_tag_calls == [] and state.delete_tag_calls == []
    assert "C" not in [t for kind, t in state.events if kind == "PUT"]
    assert [state.remote_data_type(CH, DEV, GRP, t) for t in "ABCD"] == [8, 1, 8, 0]
    assert any(r["name"] == "C" and r["status"] == "skip" and "已是預期型態" in r["detail"]
               for r in m.task_manager.get_task(body["task_id"]).results)
    for tag in "ABCD":
        assert status_of(tag) == "done"
        assert status_of(tag, "pg_status") == "pending" and status_of(tag, "scale_status") == "pending"


def test_apply_put_for_rows_without_group_has_no_tag_group(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("root-gw")
    state = make_state()
    _install(monkeypatch, state)
    seed_kw_row("T_ROOT", CH, tb_status="done", device_profile="CH1-DEV1")
    state.set_remote_tag(CH, DEV, "", "T_ROOT", data_type=0)
    _run(api_client, admin_headers, apply=True, ids=[row_id("T_ROOT")])
    assert _args(state.set_tag_data_type_calls[0]) == {
        "channel_name": CH, "device_name": DEV, "tag_name": "T_ROOT", "data_type": 8, "tag_group": None}
    assert state.remote_data_type(CH, DEV, "", "T_ROOT") == 8


def test_apply_reads_before_writing_each_row_and_verifies_after(api_client, admin_headers, monkeypatch, make_state):
    """每筆都是「先 GET 再 PUT」（不是不看就寫），前幾筆寫完再 GET 讀回驗證。"""
    make_gateway("order-gw")
    state = make_state()
    _install(monkeypatch, state)
    a = _seed(state, "A", actual=0)
    b = _seed(state, "B", actual=0)
    _run(api_client, admin_headers, apply=True, ids=[a, b])
    assert state.events == [("GET", "A"), ("PUT", "A"), ("GET", "A"),
                            ("GET", "B"), ("PUT", "B"), ("GET", "B")]


def test_apply_never_writes_unreadable_rows(api_client, admin_headers, monkeypatch, make_state):
    """actual 判讀不出來（缺欄位／有歧義／HTTP 錯誤／404）：該筆失敗，絕不猜測、絕不寫入。"""
    make_gateway("nowrite-gw")
    state = make_state()
    _install(monkeypatch, state)
    ids = [
        _seed(state, "T_NOFIELD", actual=None, payload={"name": "T_NOFIELD"}),
        _seed(state, "T_AMBIG", actual=None, payload={"data_type": 0, "dataType": 8}),
        _seed(state, "T_BOOL", actual=None, payload={"data_type": True}),
        _seed(state, "T_500", actual=0),
        _seed(state, "T_404", actual=None),
    ]
    state.queue_get_tag_error("T_500", http_error(500))

    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (5, 0, 5, 0), s
    assert state.set_tag_data_type_calls == [], "判讀不出來的點不可寫入"
    assert state.events == [("GET", t) for t in ("T_NOFIELD", "T_AMBIG", "T_BOOL", "T_500", "T_404")]
    assert s["cancelled_remaining"] == 0, "單筆讀取失敗不會觸發斷路器"


def test_apply_put_http_error_fails_only_that_row(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("put-err-gw")
    state = make_state()
    _install(monkeypatch, state)
    a = _seed(state, "A", actual=0)
    b = _seed(state, "B", actual=0)
    state.queue_set_data_type_error("A", http_error(500))
    _, result = _run(api_client, admin_headers, apply=True, ids=[a, b])
    s = result["summary"]
    assert (s["success"], s["fail"], s["cancelled_remaining"]) == (1, 1, 0), s
    assert "寫入失敗" in _errors(s)["A"]["reason"] and "HTTP 500" in _errors(s)["A"]["reason"]
    assert state.remote_data_type(CH, DEV, GRP, "A") == 0 and state.remote_data_type(CH, DEV, GRP, "B") == 8


def test_apply_rows_are_all_correct_means_nothing_is_written(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("allok-gw")
    state = make_state()
    _install(monkeypatch, state)
    ids = [_seed(state, "A", actual=8), _seed(state, "B", actual=8)]
    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["success"], s["fail"], s["skip"]) == (0, 0, 2)
    assert state.set_tag_data_type_calls == []


# ═══════════════════════════════════════════════════════════════
# 四、電路斷路器
# ═══════════════════════════════════════════════════════════════

def _seed_many(state, n, prefix="R", actual=0):
    return [_seed(state, f"{prefix}{i}", actual=actual) for i in range(1, n + 1)]


def test_breaker_readback_mismatch_aborts_the_rest_of_the_gateway(
        api_client, admin_headers, monkeypatch, make_state):
    """第一筆 PUT 成功但讀回的 data_type 不是預期值：該筆記失敗（原因含「寫入後讀回不符」）、
    立即中止這台其餘筆數（cancelled_remaining＝未處理筆數）、之後的列連 GET 都沒發、只有 1 次 PUT、log 一行 error。"""
    make_gateway("brk1-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.put_applies_data_type = False
    ids = _seed_many(state, 5)

    body, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (5, 0, 1, 0), s
    assert s["cancelled_remaining"] == 4
    assert s["cancelled"] is False, "斷路器中止不是使用者取消"
    assert result["status"] == "done"
    reason = _errors(s)["R1"]["reason"]
    assert "寫入後讀回不符" in reason and "Float" in reason and "未指定(0)" in reason
    assert "影響了其他欄位" not in reason
    assert state.events == [("GET", "R1"), ("PUT", "R1"), ("GET", "R1")], "R2 之後完全沒被處理"
    assert [state.remote_data_type(CH, DEV, GRP, f"R{i}") for i in range(1, 6)] == [0] * 5
    aborts = [msg for msg in _task_logs(body["task_id"], "error") if "中止" in msg]
    assert len(aborts) == 1 and "4" in aborts[0] and "讀回不符" in aborts[0], aborts


def test_breaker_aborts_when_put_changes_other_fields_and_names_them(
        api_client, admin_headers, monkeypatch, make_state):
    """PUT 順便動到 scaling_type 與巢狀欄位：該筆失敗、中止其餘、原因列出（排序後的）鍵路徑。"""
    make_gateway("brk2-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.put_side_effects = {"scaling_type": 1}

    def hook(remote, call, n):
        remote["nested"]["address"] = "changed"
    state.put_hook = hook
    ids = []
    for i in range(1, 5):
        ids.append(_seed(state, f"R{i}", actual=0, payload={
            "name": f"R{i}", "data_type": 0, "scaling_type": 0, "nested": {"address": "orig"}}))

    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["success"], s["fail"], s["cancelled_remaining"]) == (0, 1, 3), s
    reason = _errors(s)["R1"]["reason"]
    assert "PUT 影響了其他欄位：nested.address、scaling_type" in reason
    assert "寫入後讀回不符" not in reason, "data_type 本身是對的，不該說讀回不符"
    assert _put_tags(state) == ["R1"]


def test_breaker_reports_both_problems_when_data_type_and_other_fields_are_wrong(
        api_client, admin_headers, monkeypatch, make_state):
    make_gateway("brk-both-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.put_applies_data_type = False
    state.put_side_effects = {"scaling_type": 1}
    ids = _seed_many(state, 2)
    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    reason = _errors(result["summary"])["R1"]["reason"]
    assert "寫入後讀回不符" in reason and "PUT 影響了其他欄位：scaling_type" in reason


def test_breaker_lists_at_most_10_changed_keys(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("brk-cap-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.put_side_effects = {f"k{i:02d}": 1 for i in range(1, 13)}
    ids = _seed_many(state, 2)
    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    reason = _errors(result["summary"])["R1"]["reason"]
    for i in range(1, 11):
        assert f"k{i:02d}" in reason
    assert "k11" not in reason and "k12" not in reason, "最多列 10 個鍵路徑"
    assert "共 12 個" in reason


def test_breaker_also_guards_the_second_and_third_write(api_client, admin_headers, monkeypatch, make_state):
    """前 3 筆成功的 PUT 都要驗證：第 3 筆才出問題也要抓到；之前通過的 2 筆保持成功，其餘 3 筆沒被處理。"""
    make_gateway("brk3-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.put_hook = lambda remote, call, n: remote.update(scaling_type=9) if n == 3 else None
    ids = _seed_many(state, 6)

    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"]) == (6, 2, 1), s
    assert s["cancelled_remaining"] == 3
    assert "scaling_type" in _errors(s)["R3"]["reason"]
    assert _put_tags(state) == ["R1", "R2", "R3"]
    assert [kind for kind, _ in state.events].count("GET") == 6, "3 筆各 2 次 GET（讀＋讀回）"
    assert [state.remote_data_type(CH, DEV, GRP, f"R{i}") for i in range(1, 7)] == [8, 8, 8, 0, 0, 0]


def test_breaker_stops_verifying_after_the_first_three_writes(api_client, admin_headers, monkeypatch, make_state):
    """通過前 3 筆驗證後不再逐筆讀回（避免 3 倍呼叫量）：6 筆校正共 6+3 次 GET；
    第 4 筆起即使 PUT 動到其他欄位也不會被察覺（這是規格內的取捨，用來釘住『只驗證前 3 筆』）。"""
    assert m._DATATYPE_VERIFY_FIRST == 3
    make_gateway("noverify-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.put_hook = lambda remote, call, n: remote.update(scaling_type=9) if n >= 4 else None
    ids = _seed_many(state, 6)

    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["cancelled_remaining"]) == (6, 6, 0, 0), s
    assert len(state.get_tag_calls) == 9
    assert state.events == [
        ("GET", "R1"), ("PUT", "R1"), ("GET", "R1"),
        ("GET", "R2"), ("PUT", "R2"), ("GET", "R2"),
        ("GET", "R3"), ("PUT", "R3"), ("GET", "R3"),
        ("GET", "R4"), ("PUT", "R4"),
        ("GET", "R5"), ("PUT", "R5"),
        ("GET", "R6"), ("PUT", "R6"),
    ]


def test_breaker_counts_only_successful_puts_toward_the_verification_quota(
        api_client, admin_headers, monkeypatch, make_state):
    """PUT 失敗（HTTP 錯誤）的不算在「前 3 筆成功的 PUT」內：R1 的 PUT 失敗，R2、R3、R4 成功的才是前 3 筆。"""
    make_gateway("quota-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.queue_set_data_type_error("R1", http_error(500))
    ids = _seed_many(state, 5)
    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["success"], s["fail"]) == (4, 1), s
    readback_gets = [t for (kind, t), nxt in zip(state.events, state.events[1:] + [("", "")])
                     if kind == "PUT" and nxt == ("GET", t)]
    assert readback_gets == ["R2", "R3", "R4"], "R5 是第 4 筆成功的 PUT，不再讀回"


def test_breaker_readback_http_error_aborts_because_the_write_cannot_be_confirmed(
        api_client, admin_headers, monkeypatch, make_state):
    make_gateway("brk-rb-gw")
    state = make_state()
    _install(monkeypatch, state)
    state.queue_get_tag_error("R1", None, http_error(500))      # 第一次讀成功、寫完讀回時失敗
    ids = _seed_many(state, 3)
    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    assert (s["success"], s["fail"], s["cancelled_remaining"]) == (0, 1, 2), s
    assert "寫入後讀回失敗" in _errors(s)["R1"]["reason"] and "無法確認" in _errors(s)["R1"]["reason"]
    assert _put_tags(state) == ["R1"]


def test_breaker_on_one_gateway_does_not_stop_the_other(api_client, admin_headers, monkeypatch, make_state):
    """斷路器只中止「這台」：A 台的寫入有副作用而中止，B 台照常把自己的列校正完。"""
    make_two_gateways()
    sa, sb = make_state(), make_state()
    _install(monkeypatch, {URL_A: sa, URL_B: sb})
    sa.put_side_effects = {"scaling_type": 1}
    ids = [_seed(sa, "A1", actual=0, channel="CHA"), _seed(sa, "A2", actual=0, channel="CHA"),
           _seed(sb, "B1", actual=0, channel="CHB"), _seed(sb, "B2", actual=0, channel="CHB")]
    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    s = result["summary"]
    gws = {g["name"]: g for g in s["gateways"]}
    assert (gws["GW-A"]["success"], gws["GW-A"]["fail"]) == (0, 1)
    assert (gws["GW-B"]["success"], gws["GW-B"]["fail"]) == (2, 0)
    assert s["cancelled_remaining"] == 1
    assert _put_tags(sa) == ["A1"] and _put_tags(sb) == ["B1", "B2"]


# ═══════════════════════════════════════════════════════════════
# 五、只處理已建點（tb_status=done）的列
# ═══════════════════════════════════════════════════════════════

def test_only_done_rows_are_eligible_and_ineligible_ids_are_excluded(
        api_client, admin_headers, monkeypatch, make_state):
    make_gateway("elig-gw")
    state = make_state()
    _install(monkeypatch, state)
    d1 = _seed(state, "D1", actual=0)
    p1 = _seed(state, "P1", actual=0, tb_status="pending")
    s1 = _seed(state, "S1", actual=0, tb_status="skip")
    _seed(state, "D2", actual=0)

    # 沒給 ids：只看 done 的
    _, result = _run(api_client, admin_headers)
    assert result["summary"]["total"] == 2
    assert sorted(_get_tags(state)) == ["D1", "D2"]
    assert sorted(d["tag_name"] for d in result["summary"]["details"]) == ["D1", "D2"]

    # ids 指到 pending / skip 的列：被排除，不計入 total
    state.get_tag_calls.clear()
    _, result = _run(api_client, admin_headers, ids=[d1, p1, s1, 999999])
    assert result["summary"]["total"] == 1 and _get_tags(state) == ["D1"]

    # 校正：ids 內有 pending 的列 → 只校正 done 的，pending 的絕不碰
    state.get_tag_calls.clear()
    body, result = _run(api_client, admin_headers, apply=True, ids=[d1, p1, s1])
    assert result["summary"]["total"] == 1 and result["summary"]["success"] == 1
    assert _put_tags(state) == ["D1"]
    assert state.remote_data_type(CH, DEV, GRP, "P1") == 0 and state.remote_data_type(CH, DEV, GRP, "S1") == 0
    assert state.remote_data_type(CH, DEV, GRP, "D2") == 0
    assert any("已排除" in msg for msg in _task_logs(body["task_id"], "warning")), "要告知有幾筆 ids 被排除"


def test_no_eligible_rows_returns_400(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("none-gw")
    state = make_state()
    _install(monkeypatch, state)
    resp = post_datatype(api_client, admin_headers)
    assert resp.status_code == 400 and resp.json()["detail"] == "沒有已建點的資料可檢查"

    p1 = _seed(state, "P1", actual=0, tb_status="pending")
    for body in ({}, {"ids": [p1]}, {"apply": True, "ids": [p1]}, {"ids": [424242]}):
        resp = post_datatype(api_client, admin_headers, **body)
        assert resp.status_code == 400, (body, resp.text)
        assert resp.json()["detail"] == "沒有已建點的資料可檢查"
    assert not state.login_calls and m.gateway_queue.snapshot() == {}


def test_ids_must_be_integers(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("int-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T1", actual=0)
    resp = post_datatype(api_client, admin_headers, apply=True, ids=["abc"])
    assert resp.status_code == 422, resp.text
    assert not state.login_calls
    _assert_nothing_written(state)


def test_apply_rechecks_tb_status_when_its_turn_comes(api_client, admin_headers, monkeypatch, make_state):
    """校正請求排隊期間，某列被改回 pending、某列被刪除：輪到時重新確認，這兩列略過（skip）、不 GET 也不 PUT；
    仍是 done 的那列照常校正。"""
    make_gateway("recheck-gw")
    state = make_state()
    _install(monkeypatch, state)
    blocker = _seed(state, "BLOCK", actual=8)
    x = _seed(state, "X", actual=0)
    y = _seed(state, "Y", actual=0)
    z = _seed(state, "Z", actual=0)
    gate = state.add_gate("get_tag", "BLOCK")

    r1 = post_datatype(api_client, admin_headers, ids=[blocker])              # 先佔住這台
    gate.wait_entered()
    r2 = post_datatype(api_client, admin_headers, apply=True, ids=[x, y, z])
    assert r2.status_code == 200 and r2.json()["queued"] is True, r2.text
    m.pg_client.update_staging_status([x], "tb_status", "pending")             # 排隊期間：X 被改回 pending
    m.pg_client.delete_staging([z])                                            # Z 被刪除
    gate.open()
    poll_task(api_client, admin_headers, r1.json()["task_id"])
    result = poll_task(api_client, admin_headers, r2.json()["task_id"])

    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (3, 1, 0, 2), s
    assert _put_tags(state) == ["Y"]
    assert "X" not in _get_tags(state) and "Z" not in _get_tags(state), "略過的列連 GET 都不該發"
    assert state.remote_data_type(CH, DEV, GRP, "X") == 0 and state.remote_data_type(CH, DEV, GRP, "Z") == 0
    assert status_of("Y") == "done", "校正流程不改狀態欄位"


# ═══════════════════════════════════════════════════════════════
# 六、Gateway：自動路由／篩選／手動憑證／排隊
# ═══════════════════════════════════════════════════════════════

def test_auto_routing_two_gateways_each_checks_its_own_rows_in_parallel(
        api_client, admin_headers, monkeypatch, make_state):
    """兩台 Gateway 自動路由：各檢查自己的列、兩台同時執行（兩個 gate 都 entered 才放行）；
    summary.details 每筆都帶所屬 Gateway 名稱，依 Gateway 順序串接。"""
    a, b = make_two_gateways()
    sa, sb = make_state(), make_state()
    _seed(sa, "A1", actual=0, channel="CHA")
    _seed(sa, "A2", actual=8, channel="CHA")
    _seed(sa, "A3", actual=0, channel="CHA")
    _seed(sb, "B1", actual=0, channel="CHB")
    _seed(sb, "B2", actual=0, channel="CHB")
    ga, gb = sa.add_gate("get_tag"), sb.add_gate("get_tag")
    _install(monkeypatch, {URL_A: sa, URL_B: sb})

    resp = post_datatype(api_client, admin_headers)
    assert resp.status_code == 200, resp.text
    assert {q["gateway_name"]: q["ahead"] for q in resp.json()["queue"]} == {"GW-A": 0, "GW-B": 0}
    task_id = resp.json()["task_id"]
    ga.wait_entered()
    gb.wait_entered()
    running = get_task(api_client, admin_headers, task_id)
    assert {g["name"]: g["state"] for g in running["gateways"]} == {"GW-A": "running", "GW-B": "running"}
    ga.open()
    gb.open()

    result = poll_task(api_client, admin_headers, task_id)
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"]) == (5, 5, 0), s
    assert sorted(_get_tags(sa)) == ["A1", "A2", "A3"] and sorted(_get_tags(sb)) == ["B1", "B2"]
    assert {(d["tag_name"], d["gateway"]) for d in s["details"]} == {
        ("A1", "GW-A"), ("A3", "GW-A"), ("B1", "GW-B"), ("B2", "GW-B")}
    assert [d["gateway"] for d in s["details"]] == ["GW-A", "GW-A", "GW-B", "GW-B"]
    gws = {g["name"]: g for g in s["gateways"]}
    assert gws["GW-A"]["gateway_id"] == a["id"] and gws["GW-B"]["gateway_id"] == b["id"]
    assert len(sa.login_calls) == 1 and len(sb.login_calls) == 1
    _assert_nothing_written(sa)
    _assert_nothing_written(sb)
    assert wait_lock_released(m)


def test_auto_routing_apply_writes_each_gateway_only_its_own_rows(
        api_client, admin_headers, monkeypatch, make_state):
    make_two_gateways()
    sa, sb = make_state(), make_state()
    _install(monkeypatch, {URL_A: sa, URL_B: sb})
    ids = [_seed(sa, "A1", actual=0, channel="CHA"), _seed(sb, "B1", actual=0, channel="CHB"),
           _seed(sa, "A2", actual=0, channel="CHA")]
    _, result = _run(api_client, admin_headers, apply=True, ids=ids)
    assert result["summary"]["success"] == 3
    assert sorted(_put_tags(sa)) == ["A1", "A2"] and _put_tags(sb) == ["B1"]
    assert sorted(_get_tags(sa)) == ["A1", "A1", "A2", "A2"], "每筆讀＋讀回各一次，且只打自己那台"


def test_unroutable_rows_are_blocked_and_never_read(api_client, admin_headers, monkeypatch, make_state):
    make_two_gateways()
    sa, sb = make_state(), make_state()
    _install(monkeypatch, {URL_A: sa, URL_B: sb})
    _seed(sa, "A1", actual=0, channel="CHA")
    _seed(sa, "X1", actual=0, channel="CHX")           # 哪台 Gateway 都找不到
    body, result = _run(api_client, admin_headers)
    assert body["blocked"] == 1
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["blocked"]) == (2, 1, 1, 1), s
    blocked = [e for e in s["errors"] if e["gateway"] is None]
    assert [e["name"] for e in blocked] == ["X1"]
    assert _get_tags(sa) == ["A1"] and _get_tags(sb) == []


def test_filter_mode_with_gateway_id_only_processes_that_gateways_rows(
        api_client, admin_headers, monkeypatch, make_state):
    a, b = make_two_gateways()
    sa, sb = make_state(), make_state()
    _install(monkeypatch, {URL_A: sa, URL_B: sb})
    _seed(sa, "A1", actual=0, channel="CHA")
    _seed(sa, "A2", actual=0, channel="CHA")
    _seed(sb, "B1", actual=0, channel="CHB")
    _seed(sb, "B2", actual=0, channel="CHB")
    _seed(sb, "B3", actual=0, channel="CHB")
    body, result = _run(api_client, admin_headers, gateway_id=a["id"])
    assert body["excluded"] == {"other_gateway": 3, "blocked": 0}
    s = result["summary"]
    assert (s["total"], s["success"]) == (2, 2)
    assert sorted(_get_tags(sa)) == ["A1", "A2"] and sb.get_tag_calls == [] and not sb.login_calls
    assert {d["gateway"] for d in s["details"]} == {"GW-A"}

    resp = post_datatype(api_client, admin_headers, gateway_id=9999)
    assert resp.status_code == 404, resp.text


def test_manual_credentials_mode_does_not_route(api_client, admin_headers, monkeypatch, make_state):
    make_two_gateways()
    s_manual, sa = make_state(), make_state()
    _install(monkeypatch, {"http://manual.test:1": s_manual, URL_A: sa, URL_B: make_state()})
    _seed(s_manual, "A1", actual=0, channel="CHA")
    _seed(s_manual, "X1", actual=0, channel="CHX")
    body, result = _run(api_client, admin_headers, kw_gw_url="http://manual.test:1",
                        kw_gw_username="mu", kw_gw_password="mp")
    assert body["queue"] == [{"gateway_id": None, "gateway_name": "http://manual.test:1", "ahead": 0}]
    assert (result["summary"]["total"], result["summary"]["success"]) == (2, 2)
    assert sorted(_get_tags(s_manual)) == ["A1", "X1"] and s_manual.login_calls == [("mu", "mp")]
    assert not sa.login_calls
    assert {d["gateway"] for d in result["summary"]["details"]} == {"http://manual.test:1"}


def test_second_request_on_the_same_gateway_queues_with_the_normal_key(
        api_client, admin_headers, monkeypatch, make_state):
    """同一台 Gateway 的第二個請求排隊（queued=true），用真正的 Gateway 佇列 key（不是 dry: 專屬 key），
    與建點等其他作業共用同一個佇列；放行後依序完成。"""
    gw = make_gateway("queue-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "Q1", actual=0)
    _seed(state, "Q2", actual=0)
    seed_kw_row("P1", CH, tb_status="pending")
    gate = state.add_gate("get_tag", "Q1")

    r1 = post_datatype(api_client, admin_headers, ids=[row_id("Q1")])
    gate.wait_entered()
    r2 = post_datatype(api_client, admin_headers, ids=[row_id("Q2")])
    assert r2.status_code == 200 and r2.json()["queued"] is True, r2.text
    assert r2.json()["queue"][0]["ahead"] == 1
    assert "已有任務進行中" in r2.json()["message"]
    assert get_task(api_client, admin_headers, r2.json()["task_id"])["status"] == "queued"
    assert state.get_tag_calls[0]["tag_name"] == "Q1" and len(state.get_tag_calls) == 1, "第二個還沒開始"

    # 與 Step 3 建點共用同一個佇列：建點請求也排在後面
    r3 = post_kw(api_client, admin_headers, gateway_id=gw["id"])
    assert r3.status_code == 200 and r3.json()["queued"] is True and r3.json()["queue"][0]["ahead"] == 2
    keys = list(m.gateway_queue.snapshot())
    assert keys == [f"gw:{gw['id']}"] and not any(k.startswith("dry:") for k in keys)

    gate.open()
    for r in (r1, r2, r3):
        poll_task(api_client, admin_headers, r.json()["task_id"])
    assert _get_tags(state) == ["Q1", "Q2"]
    assert wait_lock_released(m)


# ═══════════════════════════════════════════════════════════════
# 七、取消
# ═══════════════════════════════════════════════════════════════

def test_cancel_running_apply_keeps_written_rows_and_reports_remaining(
        api_client, admin_headers, monkeypatch, make_state):
    """校正執行中取消：已 PUT 的列保持（Kepware 上已是 8）、處理完目前這一筆後停止、剩餘筆數正確、後面的列沒碰。"""
    make_gateway("cancel-gw")
    state = make_state()
    _install(monkeypatch, state)
    ids = _seed_many(state, 4)
    gate = state.add_gate("get_tag", "R2")

    resp = post_datatype(api_client, admin_headers, apply=True, ids=ids)
    tid = resp.json()["task_id"]
    gate.wait_entered()
    assert api_client.post(f"/api/tasks/{tid}/cancel", headers=admin_headers).status_code == 200
    gate.open()

    result = poll_task(api_client, admin_headers, tid)
    s = result["summary"]
    assert s["cancelled"] is True and s["cancelled_remaining"] == 2, s
    assert (s["total"], s["success"], s["fail"]) == (4, 2, 0), s
    assert result["status"] == "cancelled"
    assert _put_tags(state) == ["R1", "R2"], "取消後不可再處理後面的列"
    assert [state.remote_data_type(CH, DEV, GRP, f"R{i}") for i in range(1, 5)] == [8, 8, 0, 0], "已寫入的保持"
    assert wait_lock_released(m)


def test_cancel_running_check_returns_partial_details(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("cancel-chk-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed_many(state, 4)
    gate = state.add_gate("get_tag", "R2")
    tid = post_datatype(api_client, admin_headers).json()["task_id"]
    gate.wait_entered()
    assert api_client.post(f"/api/tasks/{tid}/cancel", headers=admin_headers).status_code == 200
    gate.open()
    s = poll_task(api_client, admin_headers, tid)["summary"]
    assert s["cancelled"] is True and s["cancelled_remaining"] == 2 and s["success"] == 2
    assert [d["tag_name"] for d in s["details"]] == ["R1", "R2"], "取消前已檢查的結果仍保留"
    assert _get_tags(state) == ["R1", "R2"]


def test_cancel_while_queued_never_touches_kepware(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("cancel-q-gw")
    state = make_state()
    _install(monkeypatch, state)
    blocker = _seed(state, "BLOCK", actual=8)
    ids = _seed_many(state, 2)
    gate = state.add_gate("get_tag", "BLOCK")
    r1 = post_datatype(api_client, admin_headers, ids=[blocker])
    gate.wait_entered()
    r2 = post_datatype(api_client, admin_headers, apply=True, ids=ids)
    assert r2.json()["queued"] is True
    assert api_client.post(f"/api/tasks/{r2.json()['task_id']}/cancel", headers=admin_headers).status_code == 200
    gate.open()
    poll_task(api_client, admin_headers, r1.json()["task_id"])
    s = poll_task(api_client, admin_headers, r2.json()["task_id"])["summary"]
    assert s["cancelled"] is True and s["cancelled_remaining"] == 2
    assert _get_tags(state) == ["BLOCK"] and state.set_tag_data_type_calls == []


# ═══════════════════════════════════════════════════════════════
# 八、安全鎖、429、歷史、權限、登入失敗、活動日誌
# ═══════════════════════════════════════════════════════════════

def test_batch_over_limit_returns_422_in_both_modes_and_equal_to_limit_is_allowed(
        api_client, admin_headers, monkeypatch, make_state, safety_env):
    """沿用 KIS_LIMIT_KW_EXECUTE：N（套用 ids 篩選之後的列數）> 上限 → 422（不建任務、不排隊、不碰 Kepware、
    不寫活動日誌）；剛好等於上限允許。檢查與校正都受限。"""
    safety_env(KIS_LIMIT_KW_EXECUTE=2)
    make_gateway("limit-gw")
    state = make_state()
    _install(monkeypatch, state)
    ids = [_seed(state, f"T{i}", actual=0) for i in range(1, 4)]
    before = len(m.task_manager.tasks)

    for body in ({}, {"ids": ids}, {"apply": True, "ids": ids}):
        resp = post_datatype(api_client, admin_headers, **body)
        assert resp.status_code == 422, (body, resp.text)
        detail = resp.json()["detail"]
        assert "Kepware 資料型態校正" in detail and "最多處理 2 點" in detail and "3 點" in detail
        assert "KIS_LIMIT_KW_EXECUTE" in detail
    assert len(m.task_manager.tasks) == before and m.gateway_queue.snapshot() == {}
    assert not state.login_calls and state.get_tag_calls == []
    _assert_nothing_written(state)
    assert m.pg_client.get_activity_logs(action="datatype_check")["total"] == 0
    assert m.pg_client.get_activity_logs(action="datatype_fix")["total"] == 0

    _, result = _run(api_client, admin_headers, ids=ids[:2])
    assert result["summary"]["total"] == 2
    _, result = _run(api_client, admin_headers, apply=True, ids=ids[:2])
    assert result["summary"]["success"] == 2


def test_429_on_read_is_retried_after_backoff(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("r429-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T_429", actual=0)
    state.queue_get_tag_error("T_429", http_error(429, retry_after=1))
    _, result = _run(api_client, admin_headers, timeout=15.0)
    s = result["summary"]
    assert (s["success"], s["fail"]) == (1, 0), s
    assert _get_tags(state) == ["T_429", "T_429"], "429 之後重試一次就成功"
    assert [d["tag_name"] for d in s["details"]] == ["T_429"]


def test_429_on_write_is_retried_after_backoff(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("w429-gw")
    state = make_state()
    _install(monkeypatch, state)
    t = _seed(state, "T_429", actual=0)
    state.queue_set_data_type_error("T_429", http_error(429, retry_after=1))
    _, result = _run(api_client, admin_headers, timeout=15.0, apply=True, ids=[t])
    s = result["summary"]
    assert (s["success"], s["fail"]) == (1, 0), s
    assert _put_tags(state) == ["T_429", "T_429"]
    assert state.remote_data_type(CH, DEV, GRP, "T_429") == 8


def test_history_records_both_modes(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("hist-gw")
    state = make_state()
    _install(monkeypatch, state)
    t1 = _seed(state, "T1", actual=0)
    _seed(state, "T2", actual=8)

    _, check = _run(api_client, admin_headers)
    wait_until(lambda: len(api_client.get("/api/history", headers=admin_headers).json()) >= 1, "檢查的歷史紀錄")
    _, fix = _run(api_client, admin_headers, apply=True, ids=[t1])
    wait_until(lambda: len(api_client.get("/api/history", headers=admin_headers).json()) >= 2, "校正的歷史紀錄")

    history = api_client.get("/api/history", headers=admin_headers).json()
    newest, older = history[0], history[1]
    assert newest["task_id"] == fix["task_id"] and older["task_id"] == check["task_id"]
    assert newest["type"] == "kw_datatype_fix" and older["type"] == "kw_datatype_check"
    assert newest["csv_filename"] == "(Kepware 資料型態校正)"
    assert older["csv_filename"] == "(Kepware 資料型態檢查)"
    assert older["dry_run"] is True and newest["dry_run"] is False
    assert (older["total"], older["success"], older["fail"], older["skip"]) == (2, 2, 0, 0)
    assert (newest["total"], newest["success"], newest["fail"], newest["skip"]) == (1, 1, 0, 0)
    assert newest["started_at"] and newest["ended_at"]


@pytest.mark.parametrize("body", [{}, {"apply": True, "ids": [1]}, {"apply": True}])
def test_user_role_is_forbidden_and_nothing_happens(body, api_client, monkeypatch, make_state):
    make_gateway("role-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T1", actual=0)
    before = len(m.task_manager.tasks)
    resp = api_client.post("/api/kw/datatype-check", json=body, headers=auth("user"))
    assert resp.status_code == 403, resp.text
    assert len(m.task_manager.tasks) == before and not state.login_calls
    assert api_client.post("/api/kw/datatype-check", json=body).status_code in (401, 403)
    _assert_nothing_written(state)


def test_operator_role_is_allowed(api_client, monkeypatch, make_state):
    make_gateway("op-gw")
    state = make_state()
    _install(monkeypatch, state)
    t = _seed(state, "T1", actual=0)
    headers = auth("operator")
    resp = post_datatype(api_client, headers)
    assert resp.status_code == 200, resp.text
    assert poll_task(api_client, headers, resp.json()["task_id"])["summary"]["success"] == 1
    resp = post_datatype(api_client, headers, apply=True, ids=[t])
    assert resp.status_code == 200, resp.text
    assert poll_task(api_client, headers, resp.json()["task_id"])["summary"]["success"] == 1


@pytest.mark.parametrize("apply", [False, True])
def test_login_failure_fails_every_row_and_releases_the_queue(
        apply, api_client, admin_headers, monkeypatch, make_state):
    gw = make_gateway("login-gw")
    state = make_state()
    state.login_error = requests.exceptions.ConnectionError("模擬連線失敗")
    _install(monkeypatch, state)
    ids = [_seed(state, f"T{i}", actual=0) for i in range(1, 4)]
    body = {"apply": True, "ids": ids} if apply else {}
    _, result = _run(api_client, admin_headers, **body)
    s = result["summary"]
    assert (s["total"], s["success"], s["fail"], s["skip"]) == (3, 0, 3, 0), s
    assert "模擬連線失敗" in s["errors"][0]["reason"]
    assert s["details"] == []
    assert len(state.login_calls) == 1 and state.get_tag_calls == []
    _assert_nothing_written(state)
    assert wait_lock_released(m, f"gw:{gw['id']}")


def test_activity_log_has_one_entry_per_accepted_request(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("log-gw")
    state = make_state()
    _install(monkeypatch, state)
    a = _seed(state, "A", actual=0)
    _seed(state, "B", actual=0)

    _run(api_client, admin_headers)
    logs = m.pg_client.get_activity_logs(action="datatype_check")
    assert logs["total"] == 1
    detail = logs["rows"][0]["detail"]
    assert "檢查" in detail and "2 點" in detail and "log-gw" in detail
    assert logs["rows"][0]["username"] == "tester"
    assert m.pg_client.get_activity_logs(action="datatype_fix")["total"] == 0

    _run(api_client, admin_headers, apply=True, ids=[a])
    logs = m.pg_client.get_activity_logs(action="datatype_fix")
    assert logs["total"] == 1
    assert "校正" in logs["rows"][0]["detail"] and "1 點" in logs["rows"][0]["detail"]
    assert "log-gw" in logs["rows"][0]["detail"]
    assert m.pg_client.get_activity_logs(action="datatype_check")["total"] == 1


def test_task_status_endpoint_exposes_type_and_details(api_client, admin_headers, monkeypatch, make_state):
    make_gateway("api-gw")
    state = make_state()
    _install(monkeypatch, state)
    _seed(state, "T1", actual=0)
    _, result = _run(api_client, admin_headers)
    assert result["type"] == "kw_datatype_check" and result["username"] == "tester"
    assert result["summary"]["details"][0]["tag_name"] == "T1"
    assert result["gateways"][0]["name"] == "api-gw" and result["gateways"][0]["state"] == "done"


def test_check_after_fix_finds_nothing_left_to_correct(api_client, admin_headers, monkeypatch, make_state):
    """端到端：檢查 → 依 details 勾選校正 → 再檢查，details 為空。"""
    make_gateway("e2e-gw")
    state = make_state()
    _install(monkeypatch, state)
    for i in range(1, 5):
        _seed(state, f"T{i}", actual=0 if i % 2 else 8)
    _, check = _run(api_client, admin_headers)
    picked = [d["id"] for d in check["summary"]["details"]]
    assert len(picked) == 2
    _, fix = _run(api_client, admin_headers, apply=True, ids=picked)
    assert fix["summary"]["success"] == 2
    _, again = _run(api_client, admin_headers)
    assert again["summary"]["details"] == [] and again["summary"]["success"] == 4
