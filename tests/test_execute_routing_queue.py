# -*- coding: utf-8 -*-
"""批次 2b：Step 3 / Step 5 / 批次刪除接上「自動路由 × 每台排隊 × 安全鎖」的行為測試。

涵蓋：
- 自動模式（兩台 Gateway 混合列：各收各的、兩台並行、單一 task_id、摘要 gateways 明細）
- blocked（無法判斷的列不送 Kepware、狀態維持 pending、計入摘要；全部 blocked → 400）
- 篩選模式（指定 gateway_id 只處理屬於它的列、excluded 計數；單台 Gateway 同舊行為）
- 手動憑證模式（不路由）
- 輪到時重新檢查狀態（第二個處理同一批列的任務不再呼叫 Kepware）
- 安全鎖：佇列滿 → 429（不殘留 task / 佇列）、批次超過上限 → 422、刪除預演不受限也不排隊
- 憑證解密失敗 → 400 且沒有任何 enqueue 殘留；run_job 前例外 → 清理 enqueue 與 task

並行測試的原則：正確性不靠 sleep；全部用 CallGate（threading.Event）同步，所有等待都有逾時。
"""
import pytest

import app.main as m
from tests.exec_helpers import (
    URL_A, URL_B, calls_of, get_task, make_gateway, make_two_gateways,
    post_delete, post_kw, post_scale, row_id, seed_kw_row, seed_scale_row, status_of,
    upload_delete_csv,
)
from tests.fake_kepware import make_fake_kepware_client, poll_task, wait_lock_released

pytestmark = pytest.mark.needs_db


def _install(monkeypatch, states):
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(states))


# ═══════════════════════════════════════════════════════════════
# 一、自動模式
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind", ["kw_execute", "scale", "delete"])
def test_auto_mode_splits_rows_by_gateway_and_runs_in_parallel(
        kind, api_client, admin_headers, monkeypatch, make_state):
    """沒有 gateway_id 也沒有 kw_gw_url＝自動：混合列依 channel 分給各自的 Gateway，
    各自只收到自己的列；兩台同時執行（兩個 gate 都 entered 才放行，不靠睡眠）；
    單一 task_id；摘要 gateways 明細正確。三個端點都適用。"""
    a, b = make_two_gateways()
    rows = [("A1", "CHA"), ("B1", "CHB"), ("A2", "CHA"), ("B2", "CHB"), ("B3", "CHB")]
    sa, sb = make_state(), make_state()
    if kind == "kw_execute":
        for tag, ch in rows:
            seed_kw_row(tag, ch)
        method, send = "create_tag", lambda: post_kw(api_client, admin_headers)
        calls = lambda st: calls_of(st)                           # noqa: E731
    elif kind == "scale":
        for tag, ch in rows:
            seed_scale_row(tag, ch)
        method, send = "set_tag_scaling", lambda: post_scale(api_client, admin_headers)
        calls = lambda st: [c["tag_name"].rsplit(".", 1)[-1] for c in st.set_tag_scaling_calls]  # noqa: E731
    else:
        # 刪除的 type 就是 channel-device-group，混合兩個 channel 用 (名稱, type) 元組
        uid = upload_delete_csv(api_client, admin_headers, [(t, f"{ch}-DEV1-2F.CHS") for t, ch in rows])
        method, send = "delete_tag", lambda: post_delete(api_client, admin_headers, uid)
        calls = lambda st: calls_of(st, "delete_tag_calls")       # noqa: E731
    ga, gb = sa.add_gate(method), sb.add_gate(method)
    _install(monkeypatch, {URL_A: sa, URL_B: sb})

    resp = send()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued"] is False and body["blocked"] == 0 and "excluded" not in body
    assert {q["gateway_name"]: q["ahead"] for q in body["queue"]} == {"GW-A": 0, "GW-B": 0}
    task_id = body["task_id"]

    ga.wait_entered()
    gb.wait_entered()                         # 兩台同時在執行；序列執行的話這裡會逾時
    running = get_task(api_client, admin_headers, task_id)
    assert running["status"] == "running" and running["done"] is False
    assert {g["name"]: g["state"] for g in running["gateways"]} == {"GW-A": "running", "GW-B": "running"}
    ga.open()
    gb.open()

    result = poll_task(api_client, admin_headers, task_id)
    assert sorted(calls(sa)) == ["A1", "A2"], "GW-A 只該收到 channel CHA 的列"
    assert sorted(calls(sb)) == ["B1", "B2", "B3"], "GW-B 只該收到 channel CHB 的列"
    summary = result["summary"]
    assert (summary["total"], summary["success"], summary["fail"], summary["blocked"]) == (5, 5, 0, 0)
    gws = {g["name"]: g for g in summary["gateways"]}
    assert (gws["GW-A"]["total"], gws["GW-A"]["success"], gws["GW-A"]["state"]) == (2, 2, "done")
    assert (gws["GW-B"]["total"], gws["GW-B"]["success"], gws["GW-B"]["state"]) == (3, 3, "done")
    assert gws["GW-A"]["gateway_id"] == a["id"] and gws["GW-B"]["gateway_id"] == b["id"]
    assert len(sa.login_calls) == 1 and len(sb.login_calls) == 1
    assert wait_lock_released(m)
    if kind == "kw_execute":
        assert all(status_of(t) == "done" for t, _ in rows)
        assert {c["channel_name"] for c in sa.create_tag_calls} == {"CHA"}


def test_auto_mode_with_single_gateway_needs_no_structure(api_client, admin_headers, monkeypatch, make_state):
    """只有 1 台 Gateway：自動模式直接用它（route.status=single），不要求同步結構、不要求對應表。"""
    gw = make_gateway("only-gw")
    seed_kw_row("T1", "WHATEVER")
    seed_kw_row("T2", "OTHER")
    state = make_state()
    _install(monkeypatch, state)
    resp = post_kw(api_client, admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["queue"] == [{"gateway_id": gw["id"], "gateway_name": "only-gw", "ahead": 0}]
    summary = poll_task(api_client, admin_headers, resp.json()["task_id"])["summary"]
    assert (summary["success"], summary["fail"], summary["blocked"]) == (2, 0, 0)
    assert sorted(calls_of(state)) == ["T1", "T2"]


# ═══════════════════════════════════════════════════════════════
# 二、blocked：無法判斷的列不送出
# ═══════════════════════════════════════════════════════════════

def test_blocked_rows_are_not_sent_and_stay_pending(api_client, admin_headers, monkeypatch, make_state):
    """unrouted（channel 不在任何結構、也沒有對應表）與 conflict（結構指向 A、對應表指向 B）
    的列：不送 Kepware、tb_status 維持 pending、出現在摘要 errors 並計入 blocked／total／fail。"""
    a, b = make_two_gateways()
    m.pg_client.upsert_gateway_route("site", "K19", b["id"])      # K19 → GW-B
    seed_kw_row("A1", "CHA")
    seed_kw_row("X1", "CHX")                                      # unrouted
    seed_kw_row("C1", "CHA", site="K19")                          # conflict（CHA 在 A，但 K19 → B）
    sa, sb = make_state(), make_state()
    _install(monkeypatch, {URL_A: sa, URL_B: sb})

    resp = post_kw(api_client, admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["blocked"] == 2
    assert "無法判斷所屬 Gateway" in resp.json()["message"]
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])

    assert calls_of(sa) == ["A1"] and calls_of(sb) == []
    assert status_of("A1") == "done"
    assert status_of("X1") == "pending" and status_of("C1") == "pending"
    summary = result["summary"]
    assert (summary["total"], summary["success"], summary["fail"], summary["blocked"]) == (3, 1, 2, 2)
    blocked = {e["name"]: e for e in summary["errors"] if e.get("gateway") is None}
    assert set(blocked) == {"X1", "C1"}
    assert "CHX" in blocked["X1"]["reason"]
    assert "GW-A" in blocked["C1"]["reason"] and "GW-B" in blocked["C1"]["reason"], "reason 應為 Gateway 名稱而非 id"
    assert result["progress"]["total"] == 3 and result["progress"]["fail"] == 2


def test_all_rows_blocked_returns_400_without_side_effects(api_client, admin_headers, monkeypatch, make_state):
    """全部都無法判斷：400，detail 說明筆數、各狀態與前 3 筆原因；沒有建立 task、佇列沒有殘留。"""
    make_two_gateways()
    for i in range(1, 5):
        seed_kw_row(f"X{i}", "CHX")
    state = make_state()
    _install(monkeypatch, state)
    before = len(m.task_manager.tasks)

    resp = post_kw(api_client, admin_headers)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "全部 4 筆都無法判斷所屬 Gateway" in detail
    assert "找不到 channel 也沒有對應表（unrouted）4 筆" in detail
    assert "X1" in detail and "X3" in detail and "X4" not in detail, "只列前 3 筆原因"
    assert len(m.task_manager.tasks) == before
    assert m.gateway_queue.snapshot() == {}
    assert not state.login_calls
    assert all(status_of(f"X{i}") == "pending" for i in range(1, 5))


def test_auto_mode_without_any_gateway_says_so(api_client, admin_headers):
    seed_kw_row("T1", "CH1")
    resp = post_kw(api_client, admin_headers)
    assert resp.status_code == 400
    assert "尚未設定任何 Gateway" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════
# 三、篩選模式（指定 gateway_id）
# ═══════════════════════════════════════════════════════════════

def test_filter_mode_only_processes_rows_belonging_to_the_gateway(api_client, admin_headers, monkeypatch, make_state):
    """指定 gateway_id＝只處理屬於它的列：其他台的、無法判斷的都排除在任務之外
    （不送、不計入 total、狀態維持 pending），excluded 回報各自的筆數。"""
    a, b = make_two_gateways()
    seed_kw_row("A1", "CHA")
    seed_kw_row("A2", "CHA")
    seed_kw_row("B1", "CHB")
    seed_kw_row("B2", "CHB")
    seed_kw_row("B3", "CHB")
    seed_kw_row("X1", "CHX")                 # 其他台 3 筆、無法判斷 1 筆：數字刻意不對稱
    sa, sb = make_state(), make_state()
    _install(monkeypatch, {URL_A: sa, URL_B: sb})

    resp = post_kw(api_client, admin_headers, gateway_id=a["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["excluded"] == {"other_gateway": 3, "blocked": 1}
    assert body["blocked"] == 0
    assert [q["gateway_name"] for q in body["queue"]] == ["GW-A"]
    assert "已排除屬於其他 Gateway 的 3 筆、無法判斷的 1 筆" in body["message"]
    summary = poll_task(api_client, admin_headers, body["task_id"])["summary"]
    assert (summary["total"], summary["success"], summary["fail"], summary["blocked"]) == (2, 2, 0, 0)
    assert sorted(calls_of(sa)) == ["A1", "A2"] and calls_of(sb) == []
    assert all(status_of(t) == "pending" for t in ("B1", "B2", "B3", "X1"))


def test_filter_mode_with_nothing_for_the_gateway_returns_400(api_client, admin_headers, monkeypatch, make_state):
    a, b = make_two_gateways()
    seed_kw_row("B1", "CHB")
    seed_kw_row("B2", "CHB")
    seed_kw_row("X1", "CHX")
    state = make_state()
    _install(monkeypatch, state)
    before = len(m.task_manager.tasks)

    resp = post_kw(api_client, admin_headers, gateway_id=a["id"])
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "沒有屬於 Gateway「GW-A」的待處理資料" in detail
    assert "屬於其他 Gateway 的 2 筆" in detail and "無法判斷所屬 Gateway 的 1 筆" in detail
    assert len(m.task_manager.tasks) == before and m.gateway_queue.snapshot() == {}
    assert not state.login_calls


def test_filter_mode_unknown_gateway_is_404(api_client, admin_headers):
    make_gateway("only")
    seed_kw_row("T1", "CH1")
    resp = post_kw(api_client, admin_headers, gateway_id=9999)
    assert resp.status_code == 404, resp.text


def test_filter_mode_with_single_gateway_behaves_like_before(api_client, admin_headers, monkeypatch, make_state):
    """只有 1 台 Gateway：指定 gateway_id 時所有列都屬於它（single 旁路），不需要結構快取，
    行為與改版前完全相同；excluded 為 0。"""
    gw = make_gateway("solo")
    seed_kw_row("T1", "ANY1")
    seed_kw_row("T2", "ANY2")
    state = make_state()
    _install(monkeypatch, state)
    resp = post_kw(api_client, admin_headers, gateway_id=gw["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["excluded"] == {"other_gateway": 0, "blocked": 0}
    assert poll_task(api_client, admin_headers, resp.json()["task_id"])["summary"]["success"] == 2
    assert sorted(calls_of(state)) == ["T1", "T2"]


# ═══════════════════════════════════════════════════════════════
# 四、手動憑證模式：不路由
# ═══════════════════════════════════════════════════════════════

def test_manual_credentials_mode_does_not_route(api_client, admin_headers, monkeypatch, make_state):
    """帶 kw_gw_url（沒有 gateway_id）＝單一 part、整批送往該 URL，不做路由：
    即使兩台 DB Gateway 都有結構，channel 屬於 A、B 的列也全部送給手動指定的那台。"""
    make_two_gateways()
    seed_kw_row("A1", "CHA")
    seed_kw_row("B1", "CHB")
    seed_kw_row("X1", "CHX")
    s_manual, sa = make_state(), make_state()
    _install(monkeypatch, {"http://manual.test:1": s_manual, URL_A: sa, URL_B: make_state()})

    resp = post_kw(api_client, admin_headers, kw_gw_url="http://manual.test:1",
                   kw_gw_username="mu", kw_gw_password="mp")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queue"] == [{"gateway_id": None, "gateway_name": "http://manual.test:1", "ahead": 0}]
    assert "excluded" not in body and body["blocked"] == 0
    summary = poll_task(api_client, admin_headers, body["task_id"])["summary"]
    assert (summary["total"], summary["success"]) == (3, 3)
    assert sorted(calls_of(s_manual)) == ["A1", "B1", "X1"]
    assert s_manual.login_calls == [("mu", "mp")]
    assert not sa.login_calls


# ═══════════════════════════════════════════════════════════════
# 五、輪到時重新檢查狀態
# ═══════════════════════════════════════════════════════════════

def test_queued_task_rechecks_status_and_skips_rows_done_by_previous_task(
        api_client, admin_headers, monkeypatch, make_state):
    """兩個任務送出時處理同一批列：第二個排隊；輪到時重新檢查，列已被第一個完成 → 不再呼叫
    Kepware（假 client 的呼叫次數證明）、摘要 skip 正確；第二個甚至不需要登入。"""
    gw = make_gateway("recheck-gw")
    for t in ("T1", "T2", "T3"):
        seed_kw_row(t, "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)

    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"])
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"])
    assert r2.json()["queued"] is True
    gate.open()
    s1 = poll_task(api_client, admin_headers, r1.json()["task_id"])["summary"]
    res2 = poll_task(api_client, admin_headers, r2.json()["task_id"])
    s2 = res2["summary"]

    assert (s1["success"], s1["skip"]) == (3, 0)
    assert (s2["total"], s2["success"], s2["fail"], s2["skip"]) == (3, 0, 0, 3), s2
    assert sorted(calls_of(state)) == ["T1", "T2", "T3"], "每個 tag 只該被建立一次"
    assert len(state.login_calls) == 1, "全部略過的任務不需要登入 Kepware"
    assert any("已被先前任務處理" in e["message"] for e in m.task_manager.get_task(r2.json()["task_id"]).log_history)
    assert res2["gateways"][0]["skip"] == 3


def test_queued_task_processes_only_rows_still_pending(api_client, admin_headers, monkeypatch, make_state):
    """第二個任務的快照有 T1、T2、T3，但輪到時 T1、T2 已被第一個任務完成：只處理 T3，skip=2。"""
    gw = make_gateway("partial-gw")
    for t in ("T1", "T2", "T3"):
        seed_kw_row(t, "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)

    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1"), row_id("T2")])
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"])     # 快照：T1、T2、T3 都還是 pending
    assert r2.json()["queued"] is True
    gate.open()
    poll_task(api_client, admin_headers, r1.json()["task_id"])
    s2 = poll_task(api_client, admin_headers, r2.json()["task_id"])["summary"]
    assert (s2["total"], s2["success"], s2["skip"]) == (3, 1, 2), s2
    assert calls_of(state) == ["T1", "T2", "T3"]
    assert status_of("T3") == "done"


def test_scale_queued_task_rechecks_scale_status(api_client, admin_headers, monkeypatch, make_state):
    """Step 5 同樣在輪到時以 scale_status 重新檢查（不是 tb_status）。"""
    gw = make_gateway("scale-recheck-gw")
    seed_scale_row("S1", "CH1")
    seed_scale_row("S2", "CH1")
    state = make_state()
    gate = state.add_gate("set_tag_scaling")
    _install(monkeypatch, state)

    r1 = post_scale(api_client, admin_headers, gateway_id=gw["id"])
    gate.wait_entered()
    r2 = post_scale(api_client, admin_headers, gateway_id=gw["id"])
    assert r2.json()["queued"] is True
    gate.open()
    poll_task(api_client, admin_headers, r1.json()["task_id"])
    s2 = poll_task(api_client, admin_headers, r2.json()["task_id"])["summary"]
    assert (s2["success"], s2["skip"]) == (0, 2), s2
    assert len(state.set_tag_scaling_calls) == 2
    assert status_of("S1", "scale_status") == "done"


def test_scale_skip_count_adds_up_already_processed_and_scale_disabled_rows(
        api_client, admin_headers, monkeypatch, make_state):
    """Step 5 的 skip＝「輪到時發現已被先前任務處理」＋「未啟用 Scale」兩者合計。
    r2 的快照有 S1、S2（要設定）、S3（scale_enabled=false）：輪到時 S1 已被 r1 完成 → skip；
    S3 未啟用 → skip；只有 S2 真的呼叫 Kepware。"""
    gw = make_gateway("scale-skip-gw")
    seed_scale_row("S1", "CH1")
    seed_scale_row("S2", "CH1")
    seed_scale_row("S3", "CH1", scale_enabled=False)
    state = make_state()
    gate = state.add_gate("set_tag_scaling", "2F.CHS.S1")
    _install(monkeypatch, state)

    r1 = post_scale(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("S1")])
    gate.wait_entered()
    r2 = post_scale(api_client, admin_headers, gateway_id=gw["id"])
    assert r2.json()["queued"] is True
    gate.open()
    poll_task(api_client, admin_headers, r1.json()["task_id"])
    res2 = poll_task(api_client, admin_headers, r2.json()["task_id"])
    s2 = res2["summary"]
    assert (s2["total"], s2["success"], s2["fail"], s2["skip"]) == (3, 1, 0, 2), s2
    assert res2["gateways"][0]["skip"] == 2 and res2["progress"]["skip"] == 2
    assert [c["tag_name"] for c in state.set_tag_scaling_calls] == ["2F.CHS.S1", "2F.CHS.S2"]
    assert status_of("S3", "scale_status") == "skip" and status_of("S2", "scale_status") == "done"


def test_queued_task_uses_submission_time_snapshot(api_client, admin_headers, monkeypatch, make_state):
    """已知且刻意的限制：排隊中的任務用「送出當下」的推導快照，輪到時不重新推導。
    排隊期間才修改 Step 3 手動覆寫（kw-override），排隊中的任務仍沿用舊值；要套用新值得取消後重送。"""
    gw = make_gateway("snap-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)

    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")])
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T2")])
    assert r2.json()["queued"] is True
    ov = api_client.post("/api/pg/staging/kw-override",
                         json={"id": row_id("T2"), "data_type": 1}, headers=admin_headers)
    assert ov.status_code == 200, ov.text
    gate.open()
    poll_task(api_client, admin_headers, r1.json()["task_id"])
    poll_task(api_client, admin_headers, r2.json()["task_id"])
    t2 = next(c for c in state.create_tag_calls if c["tag_name"] == "T2")
    assert t2["data_type"] == 8, "排隊期間的覆寫不會套用到已送出的任務（快照語意）"


# ═══════════════════════════════════════════════════════════════
# 六、安全鎖：429（佇列滿）／422（批次超限）
# ═══════════════════════════════════════════════════════════════

def test_queue_full_returns_429_and_leaves_nothing_behind(
        api_client, admin_headers, monkeypatch, make_state, safety_env):
    """佇列深度上限 2：第三個請求回 429，detail 用 Gateway「名稱」（不是 key）並含上限與環境變數；
    剛建立的 task 被移除、佇列裡沒有它。已在排隊／執行的任務不受影響，放行後正常完成。"""
    safety_env(KIS_LIMIT_QUEUE_PER_GATEWAY=2)
    gw = make_gateway("full-gw")
    for t in ("T1", "T2", "T3"):
        seed_kw_row(t, "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)

    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")])
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T2")])
    assert r2.status_code == 200 and r2.json()["queued"] is True
    tasks_before = set(m.task_manager.tasks)
    queue_before = m.gateway_queue.snapshot()

    r3 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T3")])
    assert r3.status_code == 429, r3.text
    detail = r3.json()["detail"]
    assert "Gateway「full-gw」" in detail and "gw:" not in detail
    assert "上限 2 個" in detail and "KIS_LIMIT_QUEUE_PER_GATEWAY" in detail
    assert set(m.task_manager.tasks) == tasks_before, "被拒絕的請求建立的 task 不可殘留"
    assert m.gateway_queue.snapshot() == queue_before, "被拒絕的請求不可留在佇列"

    gate.open()
    for r in (r1, r2):
        assert poll_task(api_client, admin_headers, r.json()["task_id"])["summary"]["success"] == 1
    assert calls_of(state) == ["T1", "T2"]
    assert status_of("T3") == "pending"


def test_queue_full_on_one_gateway_rejects_whole_multi_gateway_request(
        api_client, admin_headers, monkeypatch, make_state, safety_env):
    """一個請求跨兩台，其中一台佇列已滿：整個請求回 429，另一台也不會被 enqueue（原子）。"""
    safety_env(KIS_LIMIT_QUEUE_PER_GATEWAY=1)
    a, b = make_two_gateways()
    seed_kw_row("A1", "CHA")
    seed_kw_row("A2", "CHA")
    seed_kw_row("B1", "CHB")
    sa, sb = make_state(), make_state()
    ga = sa.add_gate("create_tag", "A1")
    _install(monkeypatch, {URL_A: sa, URL_B: sb})

    r1 = post_kw(api_client, admin_headers, gateway_id=a["id"], ids=[row_id("A1")])
    ga.wait_entered()
    tasks_before = set(m.task_manager.tasks)
    r2 = post_kw(api_client, admin_headers, ids=[row_id("A2"), row_id("B1")])     # 自動：跨 A、B
    assert r2.status_code == 429, r2.text
    assert "GW-A" in r2.json()["detail"]
    assert set(m.task_manager.tasks) == tasks_before
    assert list(m.gateway_queue.snapshot()) == [f"gw:{a['id']}"], "B 的佇列不可被半途加入"
    ga.open()
    poll_task(api_client, admin_headers, r1.json()["task_id"])
    assert not sb.login_calls


@pytest.mark.parametrize("kind,env", [
    ("kw_execute", "KIS_LIMIT_KW_EXECUTE"), ("scale", "KIS_LIMIT_SCALE"), ("delete", "KIS_LIMIT_DELETE"),
])
def test_batch_over_limit_returns_422_and_creates_no_task(
        kind, env, api_client, admin_headers, monkeypatch, make_state, safety_env):
    """批次點數超過上限 → 422（detail 含上限、實際點數與環境變數）、不建立 task、不碰佇列與 Kepware；
    剛好等於上限允許（> 才拒絕）；N 是套用 ids 篩選之後的列數（不是全部待處理列數）。"""
    safety_env(**{env: 2})
    gw = make_gateway("limit-gw")
    state = make_state()
    _install(monkeypatch, state)
    if kind == "kw_execute":
        for t in ("T1", "T2", "T3"):
            seed_kw_row(t, "CH1")
        send = lambda ids=None: post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=ids)   # noqa: E731
        ids_two = [row_id("T1"), row_id("T2")]
    elif kind == "scale":
        for t in ("T1", "T2", "T3"):
            seed_scale_row(t, "CH1")
        send = lambda ids=None: post_scale(api_client, admin_headers, gateway_id=gw["id"], ids=ids)  # noqa: E731
        ids_two = [row_id("T1"), row_id("T2")]
    else:
        uid3 = upload_delete_csv(api_client, admin_headers, ["T1", "T2", "T3"])
        uid2 = upload_delete_csv(api_client, admin_headers, ["T1", "T2"])
        send = lambda ids=None: post_delete(api_client, admin_headers, uid3 if ids is None else uid2,   # noqa: E731
                                            gateway_id=gw["id"])
        ids_two = [1]       # 刪除沒有 ids：用 2 列的 CSV 代表「剛好等於上限」
    before = len(m.task_manager.tasks)

    resp = send()
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "最多處理 2 點" in detail and "3 點" in detail and env in detail
    assert len(m.task_manager.tasks) == before and m.gateway_queue.snapshot() == {}
    assert not state.login_calls

    ok = send(ids_two)
    assert ok.status_code == 200, "剛好等於上限必須允許（N 為 ids 篩選之後的列數）：" + ok.text
    assert poll_task(api_client, admin_headers, ok.json()["task_id"])["summary"]["success"] == 2


def test_delete_dry_run_ignores_batch_limit_and_never_queues(
        api_client, admin_headers, monkeypatch, make_state, safety_env):
    """刪除預演不碰 Kepware：不受批次上限限制、不排隊（用專屬 key，即使該台佇列已滿也照樣 200 直接跑）、
    也不會出現在 /api/kw/queue；仍會顯示路由結果。真正的刪除則照常受限制。"""
    safety_env(KIS_LIMIT_DELETE=1, KIS_LIMIT_QUEUE_PER_GATEWAY=1)
    gw = make_gateway("dry-gw")
    uid = upload_delete_csv(api_client, admin_headers, ["D1", "D2", "D3"])
    state = make_state()
    gate = state.add_gate("delete_tag", "D1")
    _install(monkeypatch, state)

    # 先放一個真正的刪除（1 筆）佔住這台並把佇列塞滿
    uid1 = upload_delete_csv(api_client, admin_headers, ["D1"])
    busy = post_delete(api_client, admin_headers, uid1, gateway_id=gw["id"])
    assert busy.status_code == 200, busy.text
    gate.wait_entered()

    real = post_delete(api_client, admin_headers, uid, gateway_id=gw["id"])
    assert real.status_code == 422, "真正的刪除 3 筆超過上限 1"
    real2 = post_delete(api_client, admin_headers, uid1, gateway_id=gw["id"])
    assert real2.status_code == 429, "佇列上限 1，已滿"

    dry = post_delete(api_client, admin_headers, uid, gateway_id=gw["id"], dry_run=True)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["queued"] is False and body["queue"][0]["ahead"] == 0
    assert body["queue"][0]["gateway_name"] == "dry-gw", "預演仍顯示路由結果"
    summary = poll_task(api_client, admin_headers, body["task_id"])["summary"]
    assert (summary["total"], summary["success"]) == (3, 3)
    assert not state.delete_tag_calls[1:], "預演不呼叫 Kepware"
    assert all(not k.startswith("dry:") for k in m.gateway_queue.snapshot()), "預演的專屬 key 用完要釋放"

    # 佇列端點只列真正的 Gateway 佇列，不列預演的專屬 key
    assert m._queue_key_info("dry:abc12345:0", {}) is None
    q = api_client.get("/api/kw/queue", headers=admin_headers).json()
    assert [t["task_id"] for e in q for t in e["tasks"]] == [busy.json()["task_id"]]
    gate.open()
    poll_task(api_client, admin_headers, busy.json()["task_id"])


# ═══════════════════════════════════════════════════════════════
# 七、憑證解密失敗／run_job 前例外：不殘留 enqueue
# ═══════════════════════════════════════════════════════════════

def _corrupt_password(gateway_id):
    with m.pg_client._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE kepware_gateway SET password_enc = 'not-a-valid-token' WHERE id = %s",
                        (gateway_id,))


def test_undecryptable_password_returns_400_before_any_enqueue(api_client, admin_headers, monkeypatch, make_state):
    """自動模式跨兩台，其中 B 的密碼無法解密：400（密碼解密失敗訊息原樣），而且 A 也沒有被 enqueue
    ——憑證解析發生在任何 enqueue 之前；沒有 task、沒有佇列殘留、沒有碰 Kepware、列狀態不變。"""
    a, b = make_two_gateways()
    seed_kw_row("A1", "CHA")
    seed_kw_row("B1", "CHB")
    _corrupt_password(b["id"])
    sa, sb = make_state(), make_state()
    _install(monkeypatch, {URL_A: sa, URL_B: sb})
    before = len(m.task_manager.tasks)

    resp = post_kw(api_client, admin_headers)
    assert resp.status_code == 400, resp.text
    assert "無法解密" in resp.json()["detail"] and "GW-B" in resp.json()["detail"]
    assert len(m.task_manager.tasks) == before
    assert m.gateway_queue.snapshot() == {}
    assert not sa.login_calls and not sb.login_calls
    assert status_of("A1") == "pending" and status_of("B1") == "pending"

    # 指定 gateway_id 的篩選模式同樣：400、無殘留
    resp = post_kw(api_client, admin_headers, gateway_id=b["id"])
    assert resp.status_code == 400 and "無法解密" in resp.json()["detail"]
    assert m.gateway_queue.snapshot() == {}


def test_exception_between_enqueue_and_run_job_cleans_up(api_client, admin_headers, monkeypatch, make_state):
    """enqueue 成功後、run_job 啟動前若出任何例外：必須對每個 key release 並移除 task，
    否則佇列頭永遠卡死、同一台之後的任務全部排不到。"""
    a, b = make_two_gateways()
    seed_kw_row("A1", "CHA")
    seed_kw_row("B1", "CHB")
    state = make_state()
    _install(monkeypatch, {URL_A: state, URL_B: make_state()})
    before = set(m.task_manager.tasks)

    def boom(*args, **kwargs):
        raise RuntimeError("run_job 啟動失敗")

    real_run_job = m.run_job
    monkeypatch.setattr(m, "run_job", boom)
    with pytest.raises(RuntimeError, match="run_job 啟動失敗"):
        post_kw(api_client, admin_headers)
    assert m.gateway_queue.snapshot() == {}, "兩台的佇列項目都必須被清掉"
    assert set(m.task_manager.tasks) == before

    monkeypatch.setattr(m, "run_job", real_run_job)
    resp = post_kw(api_client, admin_headers, gateway_id=a["id"])
    assert resp.status_code == 200 and resp.json()["queued"] is False, "佇列沒被卡死"
    poll_task(api_client, admin_headers, resp.json()["task_id"])


# ═══════════════════════════════════════════════════════════════
# 八、PGClient.get_staging_statuses（輪到時重新檢查用）
# ═══════════════════════════════════════════════════════════════

def test_get_staging_statuses_single_query_whitelist_and_edge_cases(api_client):
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1", tb_status="done")
    seed_scale_row("S1", "CH1")
    pg = m.pg_client
    i1, i2, s1 = row_id("T1"), row_id("T2"), row_id("S1")

    assert pg.get_staging_statuses([i1, i2], "tb_status") == {i1: "pending", i2: "done"}
    assert pg.get_staging_statuses([s1], "scale_status") == {s1: "pending"}
    assert pg.get_staging_statuses([i1], "pg_status") == {i1: "pending"}
    assert pg.get_staging_statuses([], "tb_status") == {}, "ids 為空回 {}，不查資料庫"
    assert pg.get_staging_statuses([i1, 999999], "tb_status") == {i1: "pending"}, "查無的 id 不會出現"
    with pytest.raises(ValueError):
        pg.get_staging_statuses([i1], "tag_name")
    with pytest.raises(ValueError):
        pg.get_staging_statuses([i1], "tb_status; DROP TABLE scada_tag_config")
    with pytest.raises(ValueError):       # 欄位白名單檢查要在「ids 為空就回 {}」之前
        pg.get_staging_statuses([], "bogus")
