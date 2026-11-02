# -*- coding: utf-8 -*-
"""Gateway 併發控制（批次 2b 之後）：同一台排隊、不同台並行、手動憑證 URL 正規化、
任何路徑都會釋放佇列。

舊機制（`_gateway_locks` + 409 拒絕 + 三個背景函式的 try/finally AST 檢查）已整個移除，
這裡改為驗證新行為：
- 同一台 Gateway 的第二個請求不再回 409，而是 200 + queued=true，輪到時依先進先出自動執行；
- 不同台互不等待（用 Event 證明同時在執行，不靠睡眠）；
- 沒有 gateway_id 的手動憑證，`http://x:1/` 與 `http://X:1` 共用同一個佇列；
- 登入失敗／worker 例外：整台記為失敗，且佇列一定被釋放（後面排隊的任務不會卡死）。

並行測試的原則：正確性不靠 sleep；全部用 CallGate（threading.Event）同步，所有等待都有逾時。
"""
import pytest

import app.main as m
from app.gateway_queue import gateway_key
from tests.exec_helpers import (
    URL_A, URL_B, calls_of, get_task, make_gateway, make_two_gateways,
    post_delete, post_kw, post_scale, row_id, seed_kw_row, seed_scale_row, status_of,
    upload_delete_csv,
)
from tests.fake_kepware import make_fake_kepware_client, poll_task, wait_lock_released

pytestmark = pytest.mark.needs_db


# ── 舊機制已移除 ─────────────────────────────────────────────────

def test_old_lock_mechanism_is_gone():
    """舊的 409 拒絕式鎖整個移除；所有 gateway_key 的使用都改走 app.gateway_queue。"""
    for name in ("_gateway_locks", "_gateway_lock_mutex", "_gateway_lock_key",
                 "_acquire_gateway_lock", "_release_gateway_lock"):
        assert not hasattr(m, name), f"app.main 不該再有 {name}"
    assert m.gateway_key is gateway_key, "佇列 key 必須用 app.gateway_queue.gateway_key"


# ── 同一台：排隊而不是 409 ───────────────────────────────────────

def test_same_gateway_requests_queue_fifo_instead_of_409(api_client, admin_headers, monkeypatch, make_state):
    """三個請求打同一台：第一個執行中（卡在 create_tag）；第二、三個回 200 + queued=true
    （不是 409），message 含「已進入佇列」，ahead 為 1、2；放行後依 1→2→3 的順序執行。"""
    gw = make_gateway("queue-gw")
    for tag in ("T1", "T2", "T3"):
        seed_kw_row(tag, "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))

    r1 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T1")])
    assert r1.status_code == 200, r1.text
    assert r1.json()["queued"] is False and r1.json()["queue"][0]["ahead"] == 0
    gate.wait_entered()                      # 第一個確定已在執行中

    r2 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T2")])
    r3 = post_kw(api_client, admin_headers, gateway_id=gw["id"], ids=[row_id("T3")])
    assert r2.status_code == 200 and r3.status_code == 200, (r2.text, r3.text)
    b2, b3 = r2.json(), r3.json()
    assert b2["queued"] is True and b3["queued"] is True
    assert b2["queue"] == [{"gateway_id": gw["id"], "gateway_name": "queue-gw", "ahead": 1}]
    assert b3["queue"][0]["ahead"] == 2
    assert "已進入佇列" in b2["message"] and "queue-gw" in b2["message"] and "前面 1 個" in b2["message"]
    assert "前面 2 個" in b3["message"]
    assert len({r1.json()["task_id"], b2["task_id"], b3["task_id"]}) == 3

    # 排隊中的任務尚未碰 Kepware
    assert calls_of(state) == ["T1"]
    assert get_task(api_client, admin_headers, b2["task_id"])["status"] == "queued"

    gate.open()
    for resp in (r1, r2, r3):
        summary = poll_task(api_client, admin_headers, resp.json()["task_id"])["summary"]
        assert summary["success"] == 1 and summary["fail"] == 0, summary
    assert calls_of(state) == ["T1", "T2", "T3"], "同一台必須先進先出"
    assert wait_lock_released(m)


# ── 不同台：互不等待 ─────────────────────────────────────────────

def test_different_gateways_run_concurrently(api_client, admin_headers, monkeypatch, make_state):
    """兩個請求各打一台：兩台的 create_tag 同時被卡住（兩個 gate 都 entered）才放行，
    證明沒有互相等待；兩個請求都不是 queued。"""
    a, b = make_two_gateways()
    seed_kw_row("A1", "CHA")
    seed_kw_row("B1", "CHB")
    sa, sb = make_state(), make_state()
    ga, gb = sa.add_gate("create_tag", "A1"), sb.add_gate("create_tag", "B1")
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client({URL_A: sa, URL_B: sb}))

    ra = post_kw(api_client, admin_headers, gateway_id=a["id"])
    rb = post_kw(api_client, admin_headers, gateway_id=b["id"])
    assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
    assert ra.json()["queued"] is False and rb.json()["queued"] is False
    ga.wait_entered()
    gb.wait_entered()                        # 若 B 被 A 擋住，這裡會逾時失敗
    assert calls_of(sa) == ["A1"] and calls_of(sb) == ["B1"]
    ga.open()
    gb.open()
    for resp in (ra, rb):
        assert poll_task(api_client, admin_headers, resp.json()["task_id"])["summary"]["success"] == 1
    assert wait_lock_released(m)


# ── 手動憑證：URL 正規化 ─────────────────────────────────────────

def test_manual_credentials_share_queue_across_url_spellings(api_client, admin_headers, monkeypatch, make_state):
    """沒有 gateway_id 的手動憑證：`http://x:1/` 與 `http://X:1` 是同一台，共用同一個佇列
    （第二個要排隊），不再各有一把鎖；不經過路由。"""
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    gate = state.add_gate("create_tag", "T1")
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))

    r1 = post_kw(api_client, admin_headers, kw_gw_url="http://x:1/", kw_gw_username="u",
                 kw_gw_password="p", ids=[row_id("T1")])
    assert r1.status_code == 200, r1.text
    gate.wait_entered()
    assert list(m.gateway_queue.snapshot()) == ["url:http://x:1"]

    r2 = post_kw(api_client, admin_headers, kw_gw_url="http://X:1", kw_gw_username="u",
                 kw_gw_password="p", ids=[row_id("T2")])
    assert r2.status_code == 200, r2.text
    assert r2.json()["queued"] is True and r2.json()["queue"][0]["ahead"] == 1
    assert r2.json()["queue"][0]["gateway_id"] is None
    assert r2.json()["queue"][0]["gateway_name"] == "http://x:1"
    assert list(m.gateway_queue.snapshot()) == ["url:http://x:1"], "兩種寫法必須是同一個佇列 key"

    gate.open()
    for resp in (r1, r2):
        assert poll_task(api_client, admin_headers, resp.json()["task_id"])["summary"]["success"] == 1
    assert state.login_calls == [("u", "p"), ("u", "p")]
    assert wait_lock_released(m)


# ── 任何路徑都會釋放佇列 ─────────────────────────────────────────

def _seed_for(kind):
    """三個端點各自的待處理資料 + 發送函式；每個都兩筆，登入失敗時 fail 應等於 2。"""
    if kind == "kw_execute":
        seed_kw_row("L1", "CH1")
        seed_kw_row("L2", "CH1")
        return lambda api, h, gid: post_kw(api, h, gateway_id=gid)
    if kind == "scale":
        seed_scale_row("L1", "CH1")
        seed_scale_row("L2", "CH1")
        return lambda api, h, gid: post_scale(api, h, gateway_id=gid)

    def _send(api, h, gid):
        return post_delete(api, h, upload_delete_csv(api, h, ["L1", "L2"]), gateway_id=gid)
    return _send


@pytest.mark.parametrize("kind", ["kw_execute", "scale", "delete"])
def test_login_failure_fails_whole_part_and_releases_queue(kind, api_client, admin_headers, monkeypatch, make_state):
    """登入失敗＝這台全數記為失敗（fail=total，三個端點一致，含刪除——舊版刪除是 fail=0），
    errors 帶原因；佇列釋放，後面的任務可以馬上執行。"""
    gw = make_gateway("login-gw")
    send = _seed_for(kind)
    state = make_state()
    state.login_error = RuntimeError("模擬登入失敗")
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))

    resp = send(api_client, admin_headers, gw["id"])
    assert resp.status_code == 200, resp.text
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])
    summary = result["summary"]
    assert (summary["total"], summary["success"], summary["fail"]) == (2, 0, 2), summary
    assert any("模擬登入失敗" in e["reason"] for e in summary["errors"]), summary["errors"]
    assert result["gateways"][0]["fail"] == 2
    assert not state.create_tag_calls and not state.set_tag_scaling_calls and not state.delete_tag_calls
    assert wait_lock_released(m, gateway_key(gw["id"], gw["url"]))

    # 佇列確實空了：登入恢復後再送一次，直接開始、不排隊
    state.login_error = None
    resp2 = send(api_client, admin_headers, gw["id"])
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["queued"] is False
    poll_task(api_client, admin_headers, resp2.json()["task_id"])


def test_worker_exception_fails_part_and_releases_queue(api_client, admin_headers, monkeypatch, make_state):
    """worker 丟出未預期的例外（這裡讓重新檢查狀態的 SQL 失敗）：該台整個記為失敗、任務仍正常結束，
    佇列被釋放，不會讓後面的任務永遠排不到。"""
    gw = make_gateway("boom-gw")
    seed_kw_row("T1", "CH1")
    seed_kw_row("T2", "CH1")
    state = make_state()
    monkeypatch.setattr(m, "KepwareGatewayClient", make_fake_kepware_client(state))

    def boom(ids, field):
        raise RuntimeError("資料庫爆炸")

    monkeypatch.setattr(m.pg_client, "get_staging_statuses", boom)
    resp = post_kw(api_client, admin_headers, gateway_id=gw["id"])
    assert resp.status_code == 200, resp.text
    result = poll_task(api_client, admin_headers, resp.json()["task_id"])
    assert result["summary"]["fail"] == 2 and result["gateways"][0]["state"] == "failed", result
    assert any("資料庫爆炸" in e["reason"] for e in result["summary"]["errors"])
    assert not state.create_tag_calls
    assert wait_lock_released(m, gateway_key(gw["id"], gw["url"]))
    assert status_of("T1") == "pending"
