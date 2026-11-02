# -*- coding: utf-8 -*-
"""POST /api/kw/derive 的 validation（✓路徑存在／+G 群組會自動建／▲ channel 或 device 缺）依路由計算。

規格：
- 請求**有帶** gateway_id：行為完全不變——每一筆都對該台的結構快取驗證（不論它路由到哪台、能不能路由）。
- 請求**沒帶** gateway_id（自動模式）：每一筆對「它自己 route.gateway_id 所屬那台」的結構快取驗證；
  無法判斷所屬 Gateway 的列（route.gateway_id 為 None）不給 validation 欄位。
- 每台 Gateway 的結構快取每個請求只讀一次（不逐列查 DB），沒有任何列路由到的 Gateway 不讀。
- 系統只有 1 台 Gateway 時，自動模式的 validation 與「明確帶 gateway_id」的舊行為逐筆一致。
"""
import pytest

from tests.fake_kepware import create_test_gateway, insert_staging_row
from tests.test_route_map_db import _insert_location, sql_log  # noqa: F401  (sql_log 是 fixture)

pytestmark = pytest.mark.needs_db

# validation 四種典型結果
V_ALL_OK = {"channel_exists": True, "device_exists": True, "group_exists": True, "group_auto_create": False}
V_NEW_GROUP = {"channel_exists": True, "device_exists": True, "group_exists": False, "group_auto_create": True}
V_NO_DEVICE = {"channel_exists": True, "device_exists": False, "group_exists": False, "group_auto_create": False}
V_NO_CHANNEL = {"channel_exists": False, "device_exists": False, "group_exists": False, "group_auto_create": False}


def _gw(pg, name):
    return create_test_gateway(pg, lambda s: "enc:" + s, name=name)["id"]


def _stage(pg, tag, channel, site, profile=None, **extra):
    """一筆待建點暫存列；device_profile 預設 {channel}-DEV-2F-GRP（device=DEV、groups=2F.GRP）"""
    insert_staging_row(
        pg, tag_name=tag, site=site, system_code="SYS", scada_node_name=channel,
        io_device="OPC_UA", io_address=f"A.{tag}",
        device_profile=profile or f"{channel}-DEV-2F-GRP", **extra,
    )


def _derive(api_client, headers, **body):
    body.setdefault("tb_status", "pending")
    r = api_client.post("/api/kw/derive", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return {x["tag_name"]: x for x in r.json()["data"]}


@pytest.fixture
def two_gw(api_client):
    """兩台 Gateway，結構快取各不相同，涵蓋各種路由結果的暫存列。
    A: CHA/DEV/[2F.GRP]、CSHARED/DEV；B: CHB/DEV/[]（device 在、群組不在）、CSHARED/DEV"""
    import app.main as m
    pg = m.pg_client
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    pg.sync_structure(a, {"CHA": {"DEV": ["2F.GRP"]}, "CSHARED": {"DEV": []}})
    pg.sync_structure(b, {"CHB": {"DEV": []}, "CSHARED": {"DEV": []}})
    for i in range(1, 7):
        _insert_location(pg, "BU", f"S{i}", f"Z{i}")
    pg.upsert_gateway_route("site", "S1", a)       # S1 → A
    pg.upsert_gateway_route("zone", "Z3", b)       # S3 經 zone → B
    pg.upsert_gateway_route("site", "S4", b)       # S4 → B（與 CHA 衝突）

    _stage(pg, "V_A_FULL", "CHA", "S1")                           # ok → A，路徑全在
    _stage(pg, "V_B_NEWGRP", "CHB", "S2")                         # channel_only → B，群組不在（+G）
    _stage(pg, "V_A_NODEV", "CHA", "S1", profile="CHA-DEV9-2F-GRP")   # ok → A，device 不在（▲）
    _stage(pg, "V_B_NOGROUP", "CHB", "S2", profile="CHB-DEV")     # channel_only → B，沒有 tag group
    _stage(pg, "V_MAPONLY", "CNEW", "S3")                         # map_only → B，channel 不在任何結構快取
    _stage(pg, "V_OVERRIDE", "CNEW2", "S2", kw_channel="CHA")     # 覆寫成 CHA → channel_only → A
    _stage(pg, "V_CONFLICT", "CHA", "S4")                         # conflict（結構 A、對應表 B）
    _stage(pg, "V_DUPCH", "CSHARED", "S5")                        # conflict_channel
    _stage(pg, "V_UNROUTED", "CNOPE", "S6")                       # unrouted
    return pg, a, b


ROUTED = {  # tag → (預期 route.status, 預期 gateway 名稱, 預期 validation)
    "V_A_FULL": ("ok", "GW-A", V_ALL_OK),
    "V_B_NEWGRP": ("channel_only", "GW-B", V_NEW_GROUP),
    "V_A_NODEV": ("ok", "GW-A", V_NO_DEVICE),
    "V_B_NOGROUP": ("channel_only", "GW-B", V_ALL_OK),      # tag_groups 為空 → group_exists 視為 True
    "V_MAPONLY": ("map_only", "GW-B", V_NO_CHANNEL),
    "V_OVERRIDE": ("channel_only", "GW-A", V_ALL_OK),       # 驗證用的是覆寫後的 channel
}
UNROUTABLE = {"V_CONFLICT": "conflict", "V_DUPCH": "conflict_channel", "V_UNROUTED": "unrouted"}


# ═══════════════════════════════════════════════════════════════
# 自動模式（沒帶 gateway_id）
# ═══════════════════════════════════════════════════════════════

def test_auto_mode_validates_each_row_against_its_own_routed_gateway(two_gw, api_client, admin_headers):
    by_tag = _derive(api_client, admin_headers)
    assert set(by_tag) == set(ROUTED) | set(UNROUTABLE)
    for tag, (status, gw_name, validation) in ROUTED.items():
        item = by_tag[tag]
        assert (item["route"]["status"], item["route"]["gateway_name"]) == (status, gw_name), tag
        assert item["validation"] == validation, f"{tag} 應對 {gw_name} 的結構快取驗證"


def test_auto_mode_unroutable_rows_have_no_validation(two_gw, api_client, admin_headers):
    by_tag = _derive(api_client, admin_headers)
    for tag, status in UNROUTABLE.items():
        item = by_tag[tag]
        assert item["route"]["status"] == status and item["route"]["gateway_id"] is None, tag
        assert "validation" not in item, f"{tag} 無法判斷所屬 Gateway，不該有 validation"


def test_auto_mode_equals_explicit_call_for_the_routed_gateway(two_gw, api_client, admin_headers):
    """自動模式某列的 validation，必須等於「明確帶 gateway_id=它路由到的那台」時的結果；
    且同一列對另一台驗證會得到不同結果（證明是真的依路由挑結構，而不是全部用同一台）。"""
    pg, a, b = two_gw
    auto = _derive(api_client, admin_headers)
    explicit = {a: _derive(api_client, admin_headers, gateway_id=a),
                b: _derive(api_client, admin_headers, gateway_id=b)}
    checked = {a: 0, b: 0}
    for tag, item in auto.items():
        gid = item["route"]["gateway_id"]
        if gid is None:
            continue
        assert item["validation"] == explicit[gid][tag]["validation"], tag
        checked[gid] += 1
    assert checked[a] >= 2 and checked[b] >= 2, f"兩台都要有被比對到的列：{checked}"
    # 同一列換另一台驗證 → 結果不同（V_B_NEWGRP 在 A 上連 channel 都沒有）
    assert explicit[a]["V_B_NEWGRP"]["validation"] == V_NO_CHANNEL
    assert auto["V_B_NEWGRP"]["validation"] == V_NEW_GROUP


def test_auto_mode_follows_latest_structure_each_request(two_gw, api_client, admin_headers):
    """結構快取不跨請求保留：重新同步後，下一次 derive 立刻反映。"""
    pg, a, b = two_gw
    assert _derive(api_client, admin_headers)["V_B_NEWGRP"]["validation"] == V_NEW_GROUP
    pg.sync_structure(b, {"CHB": {"DEV": ["2F.GRP"]}})
    assert _derive(api_client, admin_headers)["V_B_NEWGRP"]["validation"] == V_ALL_OK
    pg.sync_structure(b, {})                      # 清空 → B 不再有任何 channel
    item = _derive(api_client, admin_headers)["V_B_NEWGRP"]
    assert item["route"]["status"] == "unrouted" and "validation" not in item


# ═══════════════════════════════════════════════════════════════
# 有帶 gateway_id：行為完全不變
# ═══════════════════════════════════════════════════════════════

def test_explicit_gateway_id_still_validates_every_row_against_that_gateway(two_gw, api_client, admin_headers):
    pg, a, b = two_gw
    by_b = _derive(api_client, admin_headers, gateway_id=b)
    assert set(by_b) == set(ROUTED) | set(UNROUTABLE)
    assert all("validation" in x for x in by_b.values()), "帶 gateway_id 時每一筆都有 validation，含無法判斷者"
    # 全部對 GW-B（CHB/DEV/[]、CSHARED/DEV）驗證，不論該列路由到哪台
    assert by_b["V_B_NEWGRP"]["validation"] == V_NEW_GROUP
    assert by_b["V_B_NOGROUP"]["validation"] == V_ALL_OK
    assert by_b["V_A_FULL"]["validation"] == V_NO_CHANNEL, "路由到 A 的列，在指定 B 時仍對 B 驗證"
    assert by_b["V_CONFLICT"]["validation"] == V_NO_CHANNEL
    assert by_b["V_DUPCH"]["validation"] == V_NEW_GROUP, "CSHARED 在 B 有 channel/device，群組缺"
    assert by_b["V_UNROUTED"]["validation"] == V_NO_CHANNEL

    by_a = _derive(api_client, admin_headers, gateway_id=a)
    assert by_a["V_A_FULL"]["validation"] == V_ALL_OK
    assert by_a["V_A_NODEV"]["validation"] == V_NO_DEVICE
    assert by_a["V_CONFLICT"]["validation"] == V_ALL_OK, "V_CONFLICT 的 channel CHA 在 A 的結構中存在"
    assert by_a["V_OVERRIDE"]["validation"] == V_ALL_OK
    # route 與指定的 gateway_id 無關
    assert {t: x["route"] for t, x in by_a.items()} == {t: x["route"] for t, x in by_b.items()}


def test_explicit_gateway_id_reads_only_that_gateways_structure(two_gw, api_client, admin_headers, sql_log):
    pg, a, b = two_gw
    sql_log.clear()
    _derive(api_client, admin_headers, gateway_id=a)
    reads = [q for q in sql_log if "kepware_structure" in q]
    assert len(reads) == 2, "channel 索引 1 次 + 指定的那台 1 次"
    assert sum("WHERE gateway_id" in q for q in reads) == 1


# ═══════════════════════════════════════════════════════════════
# 只有一台 Gateway
# ═══════════════════════════════════════════════════════════════

def test_single_gateway_auto_validation_matches_explicit_legacy_call(api_client, admin_headers):
    import app.main as m
    pg = m.pg_client
    only = _gw(pg, "ONLY")
    pg.sync_structure(only, {"CHA": {"DEV": ["2F.GRP"]}, "CHB": {"DEV": []}})
    _stage(pg, "S_FULL", "CHA", "")                       # 全在
    _stage(pg, "S_NEWGRP", "CHB", "")                     # +G
    _stage(pg, "S_NODEV", "CHA", "", profile="CHA-DEV9-2F-GRP")
    _stage(pg, "S_UNSEEN", "NEVERSYNCED", "X")            # 未同步的 channel：single 仍放行，validation 為 ▲

    auto = _derive(api_client, admin_headers)
    legacy = _derive(api_client, admin_headers, gateway_id=only)
    assert set(auto) == set(legacy) == {"S_FULL", "S_NEWGRP", "S_NODEV", "S_UNSEEN"}
    for tag in auto:
        assert auto[tag]["route"]["status"] == "single" and auto[tag]["route"]["gateway_id"] == only
        assert auto[tag]["validation"] == legacy[tag]["validation"], tag
    assert auto["S_FULL"]["validation"] == V_ALL_OK
    assert auto["S_NEWGRP"]["validation"] == V_NEW_GROUP
    assert auto["S_NODEV"]["validation"] == V_NO_DEVICE
    assert auto["S_UNSEEN"]["validation"] == V_NO_CHANNEL


def test_single_gateway_never_synced_gives_all_unseen_validation(api_client, admin_headers):
    import app.main as m
    pg = m.pg_client
    _gw(pg, "ONLY")                                      # 沒有同步過結構
    _stage(pg, "S1", "CHA", "")
    item = _derive(api_client, admin_headers)["S1"]
    assert item["route"]["status"] == "single"
    assert item["validation"] == V_NO_CHANNEL


def test_no_gateway_at_all_returns_rows_without_validation(api_client, admin_headers):
    import app.main as m
    _stage(m.pg_client, "T1", "CHA", "S1")
    item = _derive(api_client, admin_headers)["T1"]
    assert item["route"]["status"] == "unrouted"
    assert "validation" not in item


# ═══════════════════════════════════════════════════════════════
# 每台 Gateway 只讀一次，不逐列查 DB
# ═══════════════════════════════════════════════════════════════

def _structure_reads(sql_log):
    reads = [q for q in sql_log if "kepware_structure" in q]
    per_gateway = [q for q in reads if "WHERE gateway_id" in q]
    return len(reads), len(per_gateway)


def test_structure_is_read_once_per_gateway_not_per_row(two_gw, api_client, admin_headers, sql_log):
    pg, a, b = two_gw
    # 每台各再灌 40 筆，列數大增、SQL 次數不得跟著增加
    for i in range(40):
        _stage(pg, f"BULK_A_{i}", "CHA", "S1")
        _stage(pg, f"BULK_B_{i}", "CHB", "S2")
    ids = [r["id"] for r in pg.get_staging_list(page=0, page_size=None)["data"]][:2]   # V_A_FULL、V_B_NEWGRP

    sql_log.clear()
    few = _derive(api_client, admin_headers, ids=ids)
    sql_few = list(sql_log)
    sql_log.clear()
    many = _derive(api_client, admin_headers)
    sql_many = list(sql_log)

    assert len(few) == 2 and len(many) >= 80
    assert len(sql_many) == len(sql_few), f"SQL 次數隨列數增加：{len(sql_few)} → {len(sql_many)}"
    total, per_gw = _structure_reads(sql_many)
    assert (total, per_gw) == (3, 2), "channel 索引 1 次 + 用到的 GW-A、GW-B 各 1 次"
    assert all(x["validation"] == V_ALL_OK for t, x in many.items() if t.startswith("BULK_A_"))
    assert all(x["validation"] == V_NEW_GROUP for t, x in many.items() if t.startswith("BULK_B_"))


def test_gateway_with_no_routed_rows_is_not_read(two_gw, api_client, admin_headers, sql_log):
    """只有路由到 GW-A 的列時，GW-B 的結構快取完全不讀。"""
    pg, a, b = two_gw
    only_a = [r["id"] for r in pg.get_staging_list(page=0, page_size=None)["data"]
              if r["tag_name"] in ("V_A_FULL", "V_A_NODEV")]
    sql_log.clear()
    by_tag = _derive(api_client, admin_headers, ids=only_a)
    assert set(by_tag) == {"V_A_FULL", "V_A_NODEV"}
    assert _structure_reads(sql_log) == (2, 1), "channel 索引 1 次 + GW-A 1 次，沒有 GW-B"

    # 全部都無法判斷 → 只有 channel 索引那 1 次，不讀任何一台的結構
    unroutable = [r["id"] for r in pg.get_staging_list(page=0, page_size=None)["data"]
                  if r["tag_name"] in UNROUTABLE]
    sql_log.clear()
    by_tag = _derive(api_client, admin_headers, ids=unroutable)
    assert all("validation" not in x for x in by_tag.values())
    assert _structure_reads(sql_log) == (1, 0)


def test_derive_requires_admin_or_operator(two_gw, api_client):
    from tests.test_route_map_db import _auth
    assert api_client.post("/api/kw/derive", json={}).status_code == 401
    assert api_client.post("/api/kw/derive", json={}, headers=_auth("user")).status_code == 403
    assert api_client.post("/api/kw/derive", json={}, headers=_auth("operator")).status_code == 200
