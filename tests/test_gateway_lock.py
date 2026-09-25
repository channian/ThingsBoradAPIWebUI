# -*- coding: utf-8 -*-
"""對應 orig_round3.py 的 #10 系列：Gateway 併發鎖。

前半段（#10-1 ~ #10-5）為純邏輯測試，不需要 DB。
後半段是任務規格新增的行為測試：實際用 TestClient 打 /api/kw/execute 驗證 409 併發鎖行為，
以及「不同 Gateway 可並行」，需要 DB（走 api_client fixture）。
"""
import ast
import threading

import pytest

import app.main as m
from tests.ast_utils import (
    body_without_docstring,
    calls_named_in_nodes,
    find_nested_function,
    get_main_ast,
)


@pytest.fixture(autouse=True)
def _reset_gateway_locks():
    """鎖表是 app.main 模組內的全域 dict，跨測試共用同一個 process，
    每個測試前後都清空，避免測試互相汙染（例如上一個測試忘記釋放鎖）。"""
    m._gateway_locks.clear()
    yield
    m._gateway_locks.clear()


# ── #10-1：鎖的基本互斥行為 ─────────────────────────────────────

def test_acquire_lock_basic_mutual_exclusion():
    """A 首次搶鎖成功（回 None）；B 搶同一 Gateway 被擋，且回報目前佔用者 task-A；
    不同 Gateway（gw:2）互不影響，仍可正常搶到。"""
    assert m._acquire_gateway_lock("gw:1", "task-A") is None
    assert m._acquire_gateway_lock("gw:1", "task-B") == "task-A"
    assert m._acquire_gateway_lock("gw:2", "task-C") is None


# ── #10-2：只有持有者能釋放（防誤放）────────────────────────────

def test_release_lock_only_by_holder():
    """非持有者呼叫釋放應該無效（避免任務 B 誤放任務 A 手上的鎖）；
    持有者釋放後鎖確實解除，其他任務才能搶到。"""
    m._acquire_gateway_lock("gw:1", "task-A")

    m._release_gateway_lock("gw:1", "task-X")  # 非持有者
    assert m._gateway_locks.get("gw:1") == "task-A", "非持有者釋放不應該生效"

    m._release_gateway_lock("gw:1", "task-A")  # 持有者
    assert "gw:1" not in m._gateway_locks, "持有者釋放後鎖應該已解除"

    assert m._acquire_gateway_lock("gw:1", "task-B") is None, "解鎖後應該可以被其他任務搶到"


# ── #10-3：lock key 規則 ───────────────────────────────────────

def test_gateway_lock_key_rules():
    """有 gateway_id 時用 gw: 前綴（DB 管理的 Gateway）；
    無 gateway_id（手動輸入憑證）時用 url: 前綴，以 URL 本身當 key。"""
    assert m._gateway_lock_key(5, "http://x") == "gw:5"
    assert m._gateway_lock_key(None, "http://kepware:57412") == "url:http://kepware:57412"


# ── #10-4：多執行緒競爭下只有一個能拿到鎖（真並發）───────────────

def test_concurrent_lock_only_one_winner():
    """20 條執行緒用 Barrier 同時起跑搶同一把鎖，驗證鎖的實作在真並發下仍然只有
    一個贏家（防止退化成非原子操作，例如先 get 再 set 之間出現競態）。"""
    winners = []
    barrier = threading.Barrier(20)

    def racer(i):
        barrier.wait()
        if m._acquire_gateway_lock("gw:race", f"task-{i}") is None:
            winners.append(i)

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"20 條執行緒同時搶鎖應該只有 1 個成功，實際 winners={winners}"


# ── #10-5（強化版）：背景函式必須用 try/finally 釋放鎖 ─────────────

@pytest.mark.parametrize("fn_name", ["_exec_kw_execute", "_exec_pg_execute_scale", "_exec_kw_delete"])
def test_exec_functions_release_lock_via_try_finally(fn_name):
    """靜態檢查（AST，取代原本的 regex）：三個背景執行函式的主體（略過 docstring）
    「唯一」語句必須是 try，且 finally 區塊內確實呼叫 _release_gateway_lock。

    原驗收腳本用 regex 找 'finally:' 字串，只能證明檔案裡某處出現過這個字串，
    無法保證它真的包住整個函式主體——例如若有人在 try 之外、函式最前面加一段
    「提早 return」的邏輯，regex 檢查不出來，但那條路徑就會漏掉釋放鎖。
    AST 檢查函式主體「僅有一個 try 陳述式」則能排除這種退化。
    """
    tree = get_main_ast()
    node = find_nested_function(tree, fn_name)
    assert node is not None, f"在 app/main.py 中找不到巢狀函式 {fn_name}"

    body = body_without_docstring(node)
    assert len(body) == 1 and isinstance(body[0], ast.Try), (
        f"{fn_name} 的函式主體必須『僅有』一個 try 陳述式，"
        f"否則可能有邏輯寫在 try 之外、繞過鎖釋放。實際主體節點: {[type(s).__name__ for s in body]}"
    )

    try_node = body[0]
    assert try_node.finalbody, f"{fn_name} 的 try 必須要有 finally 區塊"

    called_names = calls_named_in_nodes(try_node.finalbody)
    assert "_release_gateway_lock" in called_names, (
        f"{fn_name} 的 finally 區塊必須呼叫 _release_gateway_lock，"
        f"實際 finally 內呼叫的函式: {called_names}"
    )


# ── 新增：409 併發鎖行為測試（需要 DB，走真正的 API）──────────────

def _insert_pending_staging_row(pg_client_obj, tag_name: str):
    with pg_client_obj._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO scada_tag_config
                   (tag_name, site, system_code, scada_node_name, io_device, io_address, tb_status)
                   VALUES (%s, 'K18', 'CHS', 'K18CHS', 'OPC_UA', 'A.1', 'pending')""",
                (tag_name,),
            )


def test_execute_kw_api_returns_409_when_gateway_locked(api_client, admin_headers):
    """新增的行為測試：手動佔用某個 Gateway 的鎖後，POST /api/kw/execute 應該回 409，
    且 detail 帶出佔用者的 task_id。

    這個測試特意不需要真正連得到 Kepware：/api/kw/execute 端點內，鎖的檢查
    （_acquire_gateway_lock）發生在「解析憑證、推導欄位」之後、但在「建立背景任務、
    實際呼叫 Kepware 登入」之前，因此即使 Gateway URL 是假的，只要暫存資料與 Gateway
    設定齊全，就能同步拿到 409，不必等待或 mock 任何網路呼叫。
    """
    # api_client 進入 with 區塊時已觸發 on_startup → ensure_schema()，
    # 這裡直接沿用 app.main 自己的 pg_client 實例操作同一個已 migrate 的 DB。
    gw = m.pg_client.create_gateway(
        name="lock-test-gw",
        url="http://127.0.0.1:1",  # 連線埠 1：立刻 connection refused，不會真的呼叫到
        username="u",
        password_enc=m._encrypt_password("pw"),
        verify_ssl=False,
    )
    _insert_pending_staging_row(m.pg_client, "LOCK_T1")

    lock_key = m._gateway_lock_key(gw["id"], gw["url"])
    assert m._acquire_gateway_lock(lock_key, "task-held") is None  # 前置：鎖確實搶到了

    try:
        resp = api_client.post(
            "/api/kw/execute", json={"gateway_id": gw["id"]}, headers=admin_headers
        )
        assert resp.status_code == 409, resp.text
        assert "task-held" in resp.json().get("detail", ""), resp.json()
    finally:
        m._release_gateway_lock(lock_key, "task-held")


def test_execute_kw_api_different_gateway_not_blocked(api_client, admin_headers):
    """新增：不同 Gateway 應可並行——鎖住 Gateway A 時，對 Gateway B 的
    /api/kw/execute 呼叫應該通過鎖檢查（不是 409）。Gateway B 之後會在背景任務中
    因連不上假的 URL 而失敗，但那是背景任務自己的事，這裡只驗證『鎖檢查』階段
    確實沒有被 Gateway A 的鎖誤擋。"""
    gw_a = m.pg_client.create_gateway(
        name="gw-a", url="http://127.0.0.1:1", username="u",
        password_enc=m._encrypt_password("pw"), verify_ssl=False,
    )
    gw_b = m.pg_client.create_gateway(
        name="gw-b", url="http://127.0.0.1:1", username="u",
        password_enc=m._encrypt_password("pw"), verify_ssl=False,
    )
    _insert_pending_staging_row(m.pg_client, "LOCK_T2")

    key_a = m._gateway_lock_key(gw_a["id"], gw_a["url"])
    assert m._acquire_gateway_lock(key_a, "task-A-held") is None

    try:
        resp = api_client.post(
            "/api/kw/execute", json={"gateway_id": gw_b["id"]}, headers=admin_headers
        )
        assert resp.status_code != 409, resp.text
        assert "task_id" in resp.json(), resp.json()
    finally:
        m._release_gateway_lock(key_a, "task-A-held")
        # gw_b 的鎖會由它自己觸發的背景任務在登入失敗後於 finally 中釋放；
        # 就算那條背景執行緒還沒跑完，_reset_gateway_locks 這個 autouse fixture
        # 也會在測試結束時把整個鎖表清空，不會影響後續測試。
