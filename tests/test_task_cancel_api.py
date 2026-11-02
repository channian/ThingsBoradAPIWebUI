# -*- coding: utf-8 -*-
"""批次 2b：取消、任務狀態端點、佇列端點、歷史紀錄。

涵蓋：
- POST /api/tasks/{id}/cancel：排隊中取消（立刻離開、後面遞補）、執行中取消（停在目前這筆之後、
  已成功的列狀態已寫回、cancelled_remaining 正確；三個 worker 都驗）、睡眠中取消不必等整段
  delay、跨兩台時一台執行中一台排隊的取消、錯誤碼（401/403/404/409/400）與冪等、操作日誌、
  取消時一定喚醒（wake）等待者。
- GET /api/tasks/{id}：status / cancelled / type / username / gateways / position；
  回應中不含佇列 key、密碼、網址（含 URL 內夾帶的帳密）。
- GET /api/kw/queue：內容與權限。
- _append_history：多個任務同時結束不互蓋、started_at 退路、cancelled 欄位。

並行測試的原則：正確性不靠 sleep；全部用 CallGate（threading.Event）同步，所有等待都有逾時。
"""
import threading
import time

import app.main as m
from app.task_manager import Task
from tests.exec_helpers import (
    URL_A, URL_B, auth, calls_of, get_task, make_gateway, make_two_gateways,
    post_delete, post_kw, post_scale, row_id, seed_kw_row, seed_scale_row, status_of,
    upload_delete_csv,
)
from tests.fake_kepware import make_fake_kepware_client, poll_task, wait_lock_released, wait_until


def _install(monkeypatch, states):
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(states))


def _cancel(api_client, headers, task_id):
    return api_client.post(f"/api/tasks/{task_id}/cancel", headers=headers)


# ═══════════════════════════════════════════════════════════════
# 一、取消：排隊中
# ═══════════════════════════════════════════════════════════════

def test_cancel_queued_task_leaves_queue_and_next_task_moves_up(
        api_client, admin_headers, monkeypatch, make_state):
    """r1 執行中、r2 與 r3 排隊。取消 r2：它立刻離開佇列（該台 state=cancelled、完全沒碰 Kepware、
    summary.cancelled=true、cancelled_remaining=它的筆數）；r3 遞補成「前面只剩 r1」；
    放行後 r1、r3 依序完成，T2 維持 pending。"""
    gw = make_gateway("cq-gw")
    for t in ("T1", "T2", "T3"):
        seed_kw_row(t, "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)

    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")]).json()["task_id"]
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T2")]).json()["task_id"]
    r3 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T3")]).json()["task_id"]
    assert get_task(api_client, admin_headers, r3)["gateways"][0]["position"] == 2

    resp = _cancel(api_client, admin_headers, r2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True and resp.json()["status"] in ("queued", "running", "cancelled")

    res2 = poll_task(api_client, admin_headers, r2)
    assert res2["summary"]["cancelled"] is True and res2["cancelled"] is True
    assert res2["summary"]["cancelled_remaining"] == 1
    assert res2["gateways"][0]["state"] == "cancelled"
    assert res2["status"] == "cancelled"
    assert (res2["summary"]["total"], res2["summary"]["success"]) == (1, 0)
    assert get_task(api_client, admin_headers, r3)["gateways"][0]["position"] == 1, "r3 應遞補到 r1 後面"
    assert m.gateway_queue.snapshot() == {f"gw:{gw['id']}": [r1, r3]}

    gate.open()
    for tid in (r1, r3):
        assert poll_task(api_client, admin_headers, tid)["summary"]["success"] == 1
    assert calls_of(state) == ["T1", "T3"], "被取消的 r2 不可執行"
    assert status_of("T2") == "pending"


def test_cancel_wakes_waiting_threads(api_client, admin_headers, monkeypatch, make_state):
    """取消端點必須在設起 cancelled 之後呼叫 gateway_queue.wake()，排隊中的等待者才會立刻離開，
    不必等下一次輪詢（單靠輪詢也會過，所以直接監看 wake 是否被呼叫）。"""
    gw = make_gateway("wake-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)
    wakes = []
    real_wake = m.gateway_queue.wake
    monkeypatch.setattr(m.gateway_queue, "wake", lambda: (wakes.append(1), real_wake())[1])

    post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")])
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T2")]).json()["task_id"]
    assert _cancel(api_client, admin_headers, r2).status_code == 200
    assert wakes, "取消後必須呼叫 gateway_queue.wake()"
    gate.open()


# ═══════════════════════════════════════════════════════════════
# 二、取消：執行中（三個 worker 都驗）
# ═══════════════════════════════════════════════════════════════

def test_cancel_running_kw_execute_keeps_finished_rows_done(api_client, admin_headers, monkeypatch, make_state):
    """Step 3 執行中取消：T1 已成功、T2 正在 create_tag 中（被卡住）時取消 → 放行後 T2 完成、
    停在 T2 之後；T1、T2 的 tb_status 已寫回 done（取消不會丟掉已成功的列）；T3、T4 維持 pending；
    cancelled_remaining=2，總摘要標 cancelled。"""
    gw = make_gateway("cr-gw")
    for t in ("T1", "T2", "T3", "T4"):
        seed_kw_row(t, "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T2")
    _install(monkeypatch, state)

    tid = post_kw(api_client, admin_headers, gateway_id=gw["id"]).json()["task_id"]
    gate.wait_entered()
    resp = _cancel(api_client, admin_headers, tid)
    assert resp.status_code == 200 and resp.json()["status"] == "running", resp.text
    gate.open()

    result = poll_task(api_client, admin_headers, tid)
    s = result["summary"]
    assert s["cancelled"] is True and s["cancelled_remaining"] == 2, s
    assert (s["total"], s["success"], s["fail"]) == (4, 2, 0), s
    assert result["status"] == "cancelled" and result["gateways"][0]["state"] == "done"
    assert calls_of(state) == ["T1", "T2"], "取消後不可再處理後面的列"
    assert [status_of(t) for t in ("T1", "T2", "T3", "T4")] == ["done", "done", "pending", "pending"]
    assert wait_lock_released(m)


def test_cancel_running_scale_keeps_finished_rows_done(api_client, admin_headers, monkeypatch, make_state):
    """Step 5 執行中取消：已成功的列 scale_status 已寫回 done，其餘維持 pending。"""
    gw = make_gateway("cs-gw")
    for t in ("S1", "S2", "S3"):
        seed_scale_row(t, "CH1")
    state = make_state()
    gate = state.add_gate("set_tag_scaling", "2F.CHS.S2")
    _install(monkeypatch, state)

    tid = post_scale(api_client, admin_headers, gateway_id=gw["id"]).json()["task_id"]
    gate.wait_entered()
    assert _cancel(api_client, admin_headers, tid).status_code == 200
    gate.open()

    s = poll_task(api_client, admin_headers, tid)["summary"]
    assert s["cancelled"] is True and s["cancelled_remaining"] == 1, s
    assert (s["total"], s["success"]) == (3, 2), s
    assert [status_of(t, "scale_status") for t in ("S1", "S2", "S3")] == ["done", "done", "pending"]
    assert len(state.set_tag_scaling_calls) == 2


def test_cancel_running_delete_reports_remaining(api_client, admin_headers, monkeypatch, make_state):
    gw = make_gateway("cd-gw")
    uid = upload_delete_csv(api_client, admin_headers, ["D1", "D2", "D3"])
    state = make_state()
    gate = state.add_gate("delete_tag", "D2")
    _install(monkeypatch, state)

    tid = post_delete(api_client, admin_headers, uid, gateway_id=gw["id"]).json()["task_id"]
    gate.wait_entered()
    assert _cancel(api_client, admin_headers, tid).status_code == 200
    gate.open()

    s = poll_task(api_client, admin_headers, tid)["summary"]
    assert s["cancelled"] is True and s["cancelled_remaining"] == 1, s
    assert (s["total"], s["success"], s["fail"]) == (3, 2, 0), s
    assert calls_of(state, "delete_tag_calls") == ["D1", "D2"]


def test_cancel_interrupts_the_per_row_delay_sleep(api_client, admin_headers, monkeypatch, make_state):
    """逐筆限速等待（delay）可被取消打斷：delay=30 秒時，第一筆做完進入等待後取消，任務在數秒內結束，
    不必等 30 秒。（睡眠沒有切片檢查取消的話，輪詢逾時就會失敗。）"""
    gw = make_gateway("sleep-gw")
    for t in ("T1", "T2", "T3"):
        seed_kw_row(t, "CH1", device_profile="CH1-DEV1")      # 沒有 tag group → 不會先睡在建 group 之後
    state = make_state()
    _install(monkeypatch, state)

    resp = post_kw(api_client, admin_headers, gateway_id=gw["id"], delay=30)
    tid = resp.json()["task_id"]
    wait_until(lambda: get_task(api_client, admin_headers, tid)["progress"]["current"] >= 1,
               "第一筆處理完成")
    assert _cancel(api_client, admin_headers, tid).status_code == 200

    result = poll_task(api_client, admin_headers, tid, timeout=10.0)     # 遠小於 30 秒
    s = result["summary"]
    assert s["cancelled"] is True and s["cancelled_remaining"] == 2, s
    assert calls_of(state) == ["T1"]
    assert status_of("T1") == "done" and status_of("T2") == "pending"


def test_cancel_multi_gateway_one_running_one_queued(api_client, admin_headers, monkeypatch, make_state):
    """單一任務跨兩台：A 執行中、B 排隊在另一個任務後面。取消後 B 立刻 cancelled（整批算 cancelled_remaining），
    A 在目前這筆之後停止；總摘要的 cancelled_remaining 是兩台加總，gateways 明細各自正確。"""
    a, b = make_two_gateways()
    for tag, ch in (("A1", "CHA"), ("A2", "CHA"), ("B0", "CHB"), ("B1", "CHB"), ("B2", "CHB")):
        seed_kw_row(tag, ch)
    sa, sb = make_state(), make_state()
    ga, gb = sa.add_gate("create_tag", "A1"), sb.add_gate("create_tag", "B0")
    _install(monkeypatch, {URL_A: sa, URL_B: sb})

    r0 = post_kw(api_client, admin_headers, gateway_id=b["id"], ids=[row_id("B0")]).json()["task_id"]
    gb.wait_entered()                                          # B 被 r0 佔住
    r1 = post_kw(api_client, admin_headers,
                 ids=[row_id(t) for t in ("A1", "A2", "B1", "B2")])
    assert r1.status_code == 200 and r1.json()["queued"] is True, r1.text
    assert {q["gateway_name"]: q["ahead"] for q in r1.json()["queue"]} == {"GW-A": 0, "GW-B": 1}
    tid = r1.json()["task_id"]
    ga.wait_entered()                                          # A 部分已在執行中

    assert _cancel(api_client, admin_headers, tid).status_code == 200
    wait_until(lambda: {g["name"]: g["state"] for g in get_task(api_client, admin_headers, tid)["gateways"]}
               .get("GW-B") == "cancelled", "B 部分離開佇列")
    ga.open()

    result = poll_task(api_client, admin_headers, tid)
    s = result["summary"]
    gws = {g["name"]: g for g in s["gateways"]}
    assert gws["GW-A"]["state"] == "done" and gws["GW-A"]["success"] == 1
    assert gws["GW-B"]["state"] == "cancelled" and gws["GW-B"]["total"] == 2
    assert s["cancelled"] is True and s["cancelled_remaining"] == 1 + 2, s
    assert (s["total"], s["success"]) == (4, 1)
    assert calls_of(sa) == ["A1"] and calls_of(sb) == ["B0"], "B 的列不可在取消後被執行"
    assert status_of("A1") == "done" and status_of("A2") == "pending" and status_of("B1") == "pending"
    gb.open()
    assert poll_task(api_client, admin_headers, r0)["summary"]["success"] == 1
    assert wait_lock_released(m)


# ═══════════════════════════════════════════════════════════════
# 三、取消端點的錯誤碼與冪等
# ═══════════════════════════════════════════════════════════════

def test_cancel_endpoint_status_codes_and_idempotency(api_client, admin_headers, monkeypatch, make_state):
    gw = make_gateway("codes-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)
    tid = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")]).json()["task_id"]
    gate.wait_entered()

    assert api_client.post(f"/api/tasks/{tid}/cancel").status_code == 401
    assert _cancel(api_client, auth("user"), tid).status_code == 403
    assert get_task(api_client, admin_headers, tid)["cancelled"] is False, "被拒絕的請求不可有副作用"
    assert _cancel(api_client, admin_headers, "nosuchid").status_code == 404

    # operator 可取消；重複取消是冪等的（仍 200），只記一次「收到取消要求」與一筆操作日誌
    first = _cancel(api_client, auth("operator"), tid)
    again = _cancel(api_client, admin_headers, tid)
    assert first.status_code == 200 and again.status_code == 200, (first.text, again.text)
    assert again.json()["success"] is True
    task = m.task_manager.get_task(tid)
    assert [e["message"] for e in task.log_history].count("收到取消要求") == 1
    logs = m.pg_client.get_activity_logs(page=1, page_size=50, action="cancel_task")
    assert logs["total"] == 1 and tid in logs["rows"][0]["detail"] and logs["rows"][0]["username"] == "t-operator"

    gate.open()
    poll_task(api_client, admin_headers, tid)
    # 已結束（即使是被取消結束的）→ 409
    done = _cancel(api_client, admin_headers, tid)
    assert done.status_code == 409 and "任務已結束，無法取消" in done.json()["detail"]
    # 一般完成的任務也是 409
    ok = post_kw(api_client, admin_headers, gateway_id=gw["id"]).json()["task_id"]
    poll_task(api_client, admin_headers, ok)
    assert _cancel(api_client, admin_headers, ok).status_code == 409


def test_cancel_task_without_gateways_is_400(api_client, admin_headers):
    """不是本機制產生的任務（沒有 gateways）不支援取消。"""
    legacy = m.task_manager.create_task("legacy", {})
    try:
        resp = _cancel(api_client, admin_headers, legacy.id)
        assert resp.status_code == 400, resp.text
        assert legacy.cancelled is False
    finally:
        m.task_manager.tasks.pop(legacy.id, None)


# ═══════════════════════════════════════════════════════════════
# 四、GET /api/tasks/{id}、GET /api/kw/queue
# ═══════════════════════════════════════════════════════════════

_GATEWAY_KEYS = {"gateway_id", "name", "state", "total", "current", "success", "fail", "skip", "position"}


def test_task_status_endpoint_fields_and_queue_position(api_client, admin_headers, monkeypatch, make_state):
    gw = make_gateway("fields-gw")
    for t in ("T1", "T2", "T3"):
        seed_kw_row(t, "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)
    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")]).json()["task_id"]
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T2")]).json()["task_id"]
    r3 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T3")]).json()["task_id"]

    t1, t2, t3 = (get_task(api_client, admin_headers, t) for t in (r1, r2, r3))
    for t in (t1, t2, t3):
        assert {"task_id", "done", "progress", "summary", "status", "cancelled", "type",
                "username", "gateways"} <= set(t)
        assert t["done"] is False and t["summary"] is None and t["cancelled"] is False
        assert t["type"] == "kw_execute" and t["username"] == "tester"
        assert len(t["gateways"]) == 1 and set(t["gateways"][0]) == _GATEWAY_KEYS
        assert t["gateways"][0]["gateway_id"] == gw["id"] and t["gateways"][0]["name"] == "fields-gw"
        assert t["gateways"][0]["total"] == 1
    assert (t1["status"], t1["gateways"][0]["state"], t1["gateways"][0]["position"]) == ("running", "running", None)
    assert (t2["status"], t2["gateways"][0]["state"], t2["gateways"][0]["position"]) == ("queued", "queued", 1)
    assert (t3["status"], t3["gateways"][0]["state"], t3["gateways"][0]["position"]) == ("queued", "queued", 2)

    # 佇列端點：執行中在前（position 0），其後為排隊中
    q = api_client.get("/api/kw/queue", headers=admin_headers)
    assert q.status_code == 200, q.text
    assert q.json() == [{
        "gateway_id": gw["id"], "gateway_name": "fields-gw",
        "tasks": [
            {"task_id": r1, "type": "kw_execute", "username": "tester", "position": 0, "total": 1},
            {"task_id": r2, "type": "kw_execute", "username": "tester", "position": 1, "total": 1},
            {"task_id": r3, "type": "kw_execute", "username": "tester", "position": 2, "total": 1},
        ],
    }]
    gate.open()
    for tid in (r1, r2, r3):
        done = poll_task(api_client, admin_headers, tid)
        assert done["status"] == "done" and done["gateways"][0]["state"] == "done"
        assert done["gateways"][0]["position"] is None
    assert api_client.get("/api/kw/queue", headers=admin_headers).json() == []


def test_task_and_queue_endpoints_never_leak_keys_credentials_or_urls(
        api_client, admin_headers, monkeypatch, make_state):
    """回應中不可出現佇列 key（gw:/url:）、密碼、Gateway 網址；手動憑證網址若夾帶帳密（userinfo），
    顯示名稱也要去掉。任務物件本身也不留明文密碼（憑證只在 PartSpec.payload）。"""
    gw = make_gateway("leak-gw", URL_A)
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    manual_url = "http://admin:s3cret@leak.test:1/"
    _install(monkeypatch, {URL_A: state, manual_url: state})

    texts = []
    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")])
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, kw_gw_url=manual_url, kw_gw_username="mu",
                 kw_gw_password="pw-secret", ids=[row_id("T2")])
    assert r2.status_code == 200, r2.text
    texts += [r1.text, r2.text]
    tids = [r1.json()["task_id"], r2.json()["task_id"]]
    for tid in tids:
        texts.append(api_client.get(f"/api/tasks/{tid}", headers=admin_headers).text)
    q = api_client.get("/api/kw/queue", headers=admin_headers)
    texts.append(q.text)

    manual = get_task(api_client, admin_headers, tids[1])["gateways"][0]
    assert manual["gateway_id"] is None and manual["name"] == "http://leak.test:1"
    entries = {e["gateway_name"]: e for e in q.json()}
    assert entries["http://leak.test:1"]["gateway_id"] is None
    assert entries["leak-gw"]["gateway_id"] == gw["id"]

    blob = "\n".join(texts)
    for secret in ("gw:", "url:", "s3cret", "admin:", "pw-secret", "fake-pass", URL_A, "gw-a.test"):
        assert secret not in blob, f"回應不該包含 {secret!r}"
    for tid in tids:                                # 任務物件上也沒有明文密碼
        task = m.task_manager.get_task(tid)
        dump = repr(task.params) + repr(task.gateways) + repr(task.log_history)
        assert "pw-secret" not in dump and "fake-pass" not in dump
    gate.open()
    for tid in tids:
        poll_task(api_client, admin_headers, tid)


def test_queue_endpoint_requires_login_but_not_a_privileged_role(api_client, admin_headers):
    assert api_client.get("/api/kw/queue").status_code == 401
    assert api_client.get("/api/kw/queue", headers=auth("user")).status_code == 200


def test_task_status_endpoint_for_unknown_task_is_404(api_client, admin_headers):
    assert api_client.get("/api/tasks/nosuchid", headers=admin_headers).status_code == 404


# ═══════════════════════════════════════════════════════════════
# 五、歷史紀錄
# ═══════════════════════════════════════════════════════════════

def _finished_task(i, **summary_extra):
    task = Task(f"hist{i:03d}", "kw_execute", {})
    task.csv_filename = "(測試)"
    task.started_at = "2026-11-02T10:00:00"
    task.ended_at = "2026-11-02T10:00:05"
    task.summary = {"total": 3, "success": 2, "fail": 1, "skip": 0, **summary_extra}
    return task


def test_append_history_concurrent_tasks_do_not_overwrite_each_other(monkeypatch, tmp_path):
    """20 個任務同時結束：每一筆歷史紀錄都要保留（讀→改→寫整段互斥）。
    為了讓沒有鎖的實作容易現形，把 _load_history 放慢一點、用 Barrier 讓執行緒同時起跑；
    有鎖時結果與速度無關，一定是 20 筆。"""
    monkeypatch.setattr(m, "HISTORY_FILE", str(tmp_path / "history.json"))
    real_load = m._load_history

    def slow_load():
        records = real_load()
        time.sleep(0.003)
        return records

    monkeypatch.setattr(m, "_load_history", slow_load)
    n = 20
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            m._append_history(_finished_task(i))
        except Exception as e:           # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "歷史紀錄寫入卡住"
    assert not errors, errors
    records = real_load()
    assert len(records) == n
    assert {r["task_id"] for r in records} == {f"hist{i:03d}" for i in range(n)}


def test_append_history_started_at_falls_back_to_ended_at_and_records_cancelled(monkeypatch, tmp_path):
    """全部在排隊時就被取消的任務沒有 started_at（None）→ 退而用 ended_at；
    歷史紀錄多一個 cancelled 布林（取自 summary）。"""
    monkeypatch.setattr(m, "HISTORY_FILE", str(tmp_path / "history.json"))
    never_started = _finished_task(1, cancelled=True, cancelled_remaining=3)
    never_started.started_at = None
    m._append_history(never_started)
    m._append_history(_finished_task(2, cancelled=False))
    legacy = _finished_task(3)                       # 舊式摘要沒有 cancelled 欄位
    m._append_history(legacy)

    by_id = {r["task_id"]: r for r in m._load_history()}
    assert by_id["hist001"]["started_at"] == "2026-11-02T10:00:05" == by_id["hist001"]["ended_at"]
    assert by_id["hist001"]["cancelled"] is True
    assert by_id["hist002"]["started_at"] == "2026-11-02T10:00:00" and by_id["hist002"]["cancelled"] is False
    assert by_id["hist003"]["cancelled"] is False
    assert (by_id["hist002"]["total"], by_id["hist002"]["success"], by_id["hist002"]["fail"]) == (3, 2, 1)


def test_history_records_a_task_cancelled_while_queued(api_client, admin_headers, monkeypatch, make_state):
    """端到端：排隊中就被取消的任務，歷史紀錄的 started_at 不是 null、cancelled 為 true。"""
    gw = make_gateway("hist-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    _install(monkeypatch, state)
    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")]).json()["task_id"]
    gate.wait_entered()
    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T2")]).json()["task_id"]
    _cancel(api_client, admin_headers, r2)
    poll_task(api_client, admin_headers, r2)
    gate.open()
    poll_task(api_client, admin_headers, r1)

    def _history():
        return {r["task_id"]: r for r in api_client.get("/api/history", headers=admin_headers).json()}

    wait_until(lambda: {r1, r2} <= set(_history()), "兩筆歷史紀錄寫入")
    rec = _history()[r2]
    assert rec["cancelled"] is True and rec["started_at"] is not None
    assert rec["started_at"] == rec["ended_at"]
    assert _history()[r1]["cancelled"] is False
