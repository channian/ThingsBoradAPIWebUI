# -*- coding: utf-8 -*-
"""Gateway 自動路由（真實 DB）+ G7（kw-override 部分更新）。

涵蓋：
- gateway_route_map 對應表：schema 補建（舊資料庫）、CRUD、UNIQUE upsert、
  Gateway 刪除連帶刪對應、get_channel_index 單一查詢、count_gateways / get_only_gateway_id。
- /api/kw/routes 三個端點：權限（401/403）、參數驗證（400/404）、unmapped 清單、操作日誌。
- /api/kw/derive 與 /api/pg/derive/scale 附帶的 route 欄位：單台 → single；
  兩台 + 結構快取 + 對應表 → ok / channel_only / map_only / conflict / conflict_channel / unrouted；
  既有 validation 欄位與回傳欄位維持不變（純增量）；請求內不逐列查 DB。
- G7：POST /api/pg/staging/kw-override 改為部分更新。

註：本專案的 derive 預覽端點實際路徑是 /api/kw/derive（不是 /api/pg/derive/kw）。
"""
import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest

from tests.fake_kepware import create_test_gateway, insert_staging_row

pytestmark = pytest.mark.needs_db


# ── 共用小工具 ───────────────────────────────────────────────────

def _gw(pg, name):
    """直接用 PGClient 建一台 Gateway（密碼欄位塞假字串即可，這些測試不會連線）"""
    return create_test_gateway(pg, lambda s: "enc:" + s, name=name)["id"]


def _insert_location(pg, bu, site, zone):
    """直接 SQL 寫 location_config（測試庫的 UNIQUE 約束名稱與 upsert_location 預期的不同，
    故不走 upsert_location）"""
    with pg._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO location_config (bu, site, zone) VALUES (%s, %s, %s)",
                        (bu, site, zone))


def _tables(pg):
    with pg._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public'")
            return {r[0] for r in cur.fetchall()}


def _count_routes(pg):
    with pg._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gateway_route_map")
            return cur.fetchone()[0]


def _auth(role):
    from app.auth import create_token
    return {"Authorization": f"Bearer {create_token('t-' + role, role)}"}


@pytest.fixture
def sql_log(monkeypatch):
    """記錄測試期間所有透過 psycopg2.connect 建立的連線所執行的 SQL（用 connection_factory
    掛上會計數的 cursor）。用來驗證『單一 SQL』與『請求內不逐列查 DB』。"""
    log = []

    def counting_cursor(base):
        class _Cur(base):
            def execute(self, query, vars=None):
                log.append(str(query))
                return super().execute(query, vars)
        return _Cur

    class _Conn(psycopg2.extensions.connection):
        def cursor(self, *args, **kwargs):
            base = (kwargs.pop("cursor_factory", None) or self.cursor_factory
                    or psycopg2.extensions.cursor)
            kwargs["cursor_factory"] = counting_cursor(base)
            return super().cursor(*args, **kwargs)

    real_connect = psycopg2.connect

    def fake_connect(*args, **kwargs):
        kwargs["connection_factory"] = _Conn
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    return log


# ═══════════════════════════════════════════════════════════════
# 一、schema：ensure_schema 對舊資料庫補建 gateway_route_map
# ═══════════════════════════════════════════════════════════════

def test_ensure_schema_creates_route_map_on_fresh_db(pg_client):
    assert "gateway_route_map" not in _tables(pg_client)
    pg_client.ensure_schema()
    assert "gateway_route_map" in _tables(pg_client)


def test_ensure_schema_adds_route_map_to_legacy_db_keeping_existing_data(pg_client):
    """舊資料庫：其他表（含 kepware_gateway 與既有資料）都在，只缺 gateway_route_map →
    ensure_schema 要補建，且不能動到既有資料；重複呼叫冪等。"""
    pg_client.ensure_schema()
    gw_id = _gw(pg_client, "LEGACY-GW")
    with pg_client._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE gateway_route_map")
    assert "gateway_route_map" not in _tables(pg_client)

    pg_client.ensure_schema()

    assert "gateway_route_map" in _tables(pg_client)
    assert [g["id"] for g in pg_client.get_gateways()] == [gw_id], "既有 Gateway 資料不能被動到"
    pg_client.upsert_gateway_route("site", "K18", gw_id)   # 補建後立刻可用
    pg_client.ensure_schema()                                # 冪等：不清掉資料、不拋錯
    assert len(pg_client.list_gateway_routes()) == 1


def test_route_map_table_has_real_constraints(pg_client_migrated):
    """CHECK / UNIQUE / FK 約束要真的存在於資料庫層（不只靠應用程式碼檢查）。"""
    pg = pg_client_migrated
    gw = _gw(pg, "GW")

    def raw_insert(match_type, value, gateway_id):
        with pg._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO gateway_route_map (match_type, match_value, gateway_id) "
                            "VALUES (%s, %s, %s)", (match_type, value, gateway_id))

    with pytest.raises(psycopg2.errors.CheckViolation):
        raw_insert("building", "K18", gw)
    raw_insert("site", "K18", gw)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        raw_insert("site", "K18", gw)
    raw_insert("zone", "K18", gw)       # 同值不同 type 是不同的對應
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        raw_insert("site", "K99", gw + 999)


# ═══════════════════════════════════════════════════════════════
# 二、PGClient：對應表 CRUD
# ═══════════════════════════════════════════════════════════════

def test_upsert_and_list_routes_include_gateway_name(pg_client_migrated):
    pg = pg_client_migrated
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    r1 = pg.upsert_gateway_route("site", "K18", a)
    r2 = pg.upsert_gateway_route("zone", "Z1", b)
    assert set(r1) == {"id", "match_type", "match_value", "gateway_id", "gateway_name"}
    assert (r1["match_type"], r1["match_value"], r1["gateway_id"], r1["gateway_name"]) == \
        ("site", "K18", a, "GW-A")
    assert r2["gateway_name"] == "GW-B"

    routes = pg.list_gateway_routes()
    assert [(r["match_type"], r["match_value"], r["gateway_id"], r["gateway_name"]) for r in routes] == \
        [("site", "K18", a, "GW-A"), ("zone", "Z1", b, "GW-B")]
    assert set(routes[0]) == {"id", "match_type", "match_value", "gateway_id", "gateway_name"}


def test_upsert_same_type_and_value_updates_gateway_not_duplicates(pg_client_migrated):
    pg = pg_client_migrated
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    first = pg.upsert_gateway_route("site", "K18", a)
    second = pg.upsert_gateway_route("site", "K18", b)
    assert second["id"] == first["id"], "UNIQUE upsert：同一筆被更新而非新增"
    assert second["gateway_id"] == b and second["gateway_name"] == "GW-B"
    assert _count_routes(pg) == 1
    assert pg.list_gateway_routes()[0]["gateway_id"] == b


def test_same_value_with_different_type_are_separate_routes(pg_client_migrated):
    pg = pg_client_migrated
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    pg.upsert_gateway_route("site", "X", a)
    pg.upsert_gateway_route("zone", "X", b)
    assert _count_routes(pg) == 2
    site_map, zone_map = pg.get_route_maps()
    assert site_map == {"X": a} and zone_map == {"X": b}


def test_upsert_strips_value_and_rejects_bad_input(pg_client_migrated):
    pg = pg_client_migrated
    a = _gw(pg, "GW-A")
    assert pg.upsert_gateway_route("site", "  K18  ", a)["match_value"] == "K18"
    # 前後空白不同視為同一筆
    pg.upsert_gateway_route("site", "K18 ", a)
    assert _count_routes(pg) == 1

    for bad_value in ("", "   ", None):
        with pytest.raises(ValueError):
            pg.upsert_gateway_route("site", bad_value, a)
    for bad_type in ("building", "", None, "SITE", "Site"):
        with pytest.raises(ValueError):
            pg.upsert_gateway_route(bad_type, "K18", a)
    with pytest.raises(ValueError):
        pg.upsert_gateway_route("site", "K19", a + 999)   # Gateway 不存在
    assert _count_routes(pg) == 1, "非法輸入不可寫入任何資料"


def test_delete_gateway_route(pg_client_migrated):
    pg = pg_client_migrated
    a = _gw(pg, "GW-A")
    route = pg.upsert_gateway_route("site", "K18", a)
    assert pg.delete_gateway_route(route["id"]) is True
    assert pg.delete_gateway_route(route["id"]) is False
    assert pg.delete_gateway_route(999999) is False
    assert pg.list_gateway_routes() == []


def test_deleting_gateway_cascades_to_its_routes_only(pg_client_migrated):
    pg = pg_client_migrated
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    pg.upsert_gateway_route("site", "K18", a)
    pg.upsert_gateway_route("zone", "Z1", a)
    pg.upsert_gateway_route("site", "K19", b)
    assert pg.delete_gateway(a) is True
    routes = pg.list_gateway_routes()
    assert [(r["match_value"], r["gateway_id"]) for r in routes] == [("K19", b)]
    assert pg.get_route_maps() == ({"K19": b}, {})


def test_get_route_maps_splits_site_and_zone(pg_client_migrated):
    pg = pg_client_migrated
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    assert pg.get_route_maps() == ({}, {})
    pg.upsert_gateway_route("site", "K18", a)
    pg.upsert_gateway_route("site", "K19", b)
    pg.upsert_gateway_route("zone", "Z1", b)
    assert pg.get_route_maps() == ({"K18": a, "K19": b}, {"Z1": b})


# ═══════════════════════════════════════════════════════════════
# 三、PGClient：channel 索引 / Gateway 數量
# ═══════════════════════════════════════════════════════════════

def _seed_structures(pg):
    a, b, c = _gw(pg, "GW-A"), _gw(pg, "GW-B"), _gw(pg, "GW-C")
    pg.sync_structure(a, {"CHA": {"DEV1": ["2F.X", "3F.X"], "DEV2": []}, "CSHARED": {"D": []}})
    pg.sync_structure(b, {"CHB": {"DEV1": []}, "CSHARED": {"D": ["G"]}})
    # c 沒同步過結構
    return a, b, c


def test_get_channel_index_merges_all_gateways_and_dedupes(pg_client_migrated):
    pg = pg_client_migrated
    a, b, c = _seed_structures(pg)
    idx = pg.get_channel_index()
    # CHA 有多個 device / group 列，仍只算一台；CSHARED 在 a、b 兩台
    assert idx == {"CHA": [a], "CHB": [b], "CSHARED": [a, b]}
    assert c not in {g for gws in idx.values() for g in gws}


def test_get_channel_index_empty_when_no_structure(pg_client_migrated):
    _gw(pg_client_migrated, "GW-A")
    assert pg_client_migrated.get_channel_index() == {}


def test_get_channel_index_is_a_single_query(pg_client_migrated, sql_log):
    pg = pg_client_migrated
    _seed_structures(pg)          # 3 台 Gateway、其中 2 台有結構
    sql_log.clear()
    pg.get_channel_index()
    assert len(sql_log) == 1, f"應只下一次查詢（不可每台 Gateway 各查一次），實際：{sql_log}"
    assert "kepware_structure" in sql_log[0] and "DISTINCT" in sql_log[0].upper()


def test_count_gateways_and_get_only_gateway_id(pg_client_migrated):
    pg = pg_client_migrated
    assert pg.count_gateways() == 0
    assert pg.get_only_gateway_id() is None
    a = _gw(pg, "GW-A")
    assert pg.count_gateways() == 1
    assert pg.get_only_gateway_id() == a
    b = _gw(pg, "GW-B")
    assert pg.count_gateways() == 2
    assert pg.get_only_gateway_id() is None
    pg.delete_gateway(a)
    assert pg.get_only_gateway_id() == b, "回傳的是那台的實際 id，不是固定 1"


# ═══════════════════════════════════════════════════════════════
# 四、/api/kw/routes 端點
# ═══════════════════════════════════════════════════════════════

def test_routes_endpoints_require_login(api_client):
    assert api_client.get("/api/kw/routes").status_code == 401
    body = {"match_type": "site", "match_value": "K18", "gateway_id": 1}
    assert api_client.post("/api/kw/routes", json=body).status_code == 401
    assert api_client.delete("/api/kw/routes/1").status_code == 401


def test_routes_read_allowed_for_any_logged_in_role_write_requires_admin(api_client, admin_headers):
    import app.main as m
    gw = _gw(m.pg_client, "GW-A")
    body = {"match_type": "site", "match_value": "K18", "gateway_id": gw}
    created = api_client.post("/api/kw/routes", json=body, headers=admin_headers)
    assert created.status_code == 200, created.text
    route_id = created.json()["id"]

    for role in ("operator", "user"):
        h = _auth(role)
        assert api_client.get("/api/kw/routes", headers=h).status_code == 200, role
        assert api_client.post("/api/kw/routes", json=body, headers=h).status_code == 403, role
        assert api_client.delete(f"/api/kw/routes/{route_id}", headers=h).status_code == 403, role
    assert _count_routes(m.pg_client) == 1, "403 的請求不可有副作用"


def test_post_route_creates_updates_and_lists(api_client, admin_headers):
    import app.main as m
    a, b = _gw(m.pg_client, "GW-A"), _gw(m.pg_client, "GW-B")

    r = api_client.post("/api/kw/routes", headers=admin_headers,
                        json={"match_type": "site", "match_value": " K18 ", "gateway_id": a})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["match_value"] == "K18" and created["gateway_name"] == "GW-A"

    r = api_client.post("/api/kw/routes", headers=admin_headers,
                        json={"match_type": "site", "match_value": "K18", "gateway_id": b})
    assert r.status_code == 200
    assert r.json()["id"] == created["id"] and r.json()["gateway_id"] == b

    api_client.post("/api/kw/routes", headers=admin_headers,
                    json={"match_type": "zone", "match_value": "Z1", "gateway_id": a})

    body = api_client.get("/api/kw/routes", headers=admin_headers).json()
    assert set(body) == {"routes", "unmapped_sites", "unmapped_zones"}
    assert [(x["match_type"], x["match_value"], x["gateway_id"], x["gateway_name"])
            for x in body["routes"]] == [("site", "K18", b, "GW-B"), ("zone", "Z1", a, "GW-A")]
    assert all(set(x) == {"id", "match_type", "match_value", "gateway_id", "gateway_name"}
               for x in body["routes"])


def test_post_route_validation_errors(api_client, admin_headers):
    import app.main as m
    a = _gw(m.pg_client, "GW-A")

    def post(**override):
        body = {"match_type": "site", "match_value": "K18", "gateway_id": a}
        body.update(override)
        return api_client.post("/api/kw/routes", json=body, headers=admin_headers)

    assert post(match_type="building").status_code == 400
    assert post(match_type="").status_code == 400
    assert post(match_value="").status_code == 400
    assert post(match_value="   ").status_code == 400
    r = post(gateway_id=a + 999)
    assert r.status_code == 404 and "Gateway" in r.json()["detail"]
    assert api_client.post("/api/kw/routes", json={"match_type": "site"},
                           headers=admin_headers).status_code == 422
    assert _count_routes(m.pg_client) == 0, "任何失敗的請求都不可寫入資料"


def test_delete_route(api_client, admin_headers):
    import app.main as m
    a = _gw(m.pg_client, "GW-A")
    route = m.pg_client.upsert_gateway_route("site", "K18", a)
    r = api_client.delete(f"/api/kw/routes/{route['id']}", headers=admin_headers)
    assert r.status_code == 200 and r.json() == {"success": True}
    assert _count_routes(m.pg_client) == 0
    assert api_client.delete(f"/api/kw/routes/{route['id']}", headers=admin_headers).status_code == 404
    assert api_client.delete("/api/kw/routes/999999", headers=admin_headers).status_code == 404


def test_route_writes_are_activity_logged(api_client, admin_headers):
    import app.main as m
    a = _gw(m.pg_client, "GW-A")
    r = api_client.post("/api/kw/routes", headers=admin_headers,
                        json={"match_type": "site", "match_value": "K18", "gateway_id": a})
    assert m.pg_client.get_activity_logs(action="upsert_gateway_route")["total"] == 1
    detail = m.pg_client.get_activity_logs(action="upsert_gateway_route")["rows"][0]["detail"]
    assert "K18" in detail and "GW-A" in detail

    api_client.delete(f"/api/kw/routes/{r.json()['id']}", headers=admin_headers)
    assert m.pg_client.get_activity_logs(action="delete_gateway_route")["total"] == 1
    # 失敗的請求不記日誌
    api_client.delete("/api/kw/routes/999999", headers=admin_headers)
    assert m.pg_client.get_activity_logs(action="delete_gateway_route")["total"] == 1


def test_unmapped_sites_and_zones_listing(api_client, admin_headers):
    import app.main as m
    pg = m.pg_client
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    for site, zone in (("K18", "Z1"), ("K19", "Z1"), ("K20", "Z2"), ("K21", "Z3")):
        _insert_location(pg, "BU", site, zone)
    _insert_location(pg, "BU", "K22", "")        # 沒有 zone 的 site
    _insert_location(pg, "BU", "", "Z9")         # 沒有 site 的列：zone 仍要列入、site 不列

    def listing():
        return api_client.get("/api/kw/routes", headers=admin_headers).json()

    body = listing()
    assert body["routes"] == []
    assert body["unmapped_sites"] == ["K18", "K19", "K20", "K21", "K22"]
    assert body["unmapped_zones"] == ["Z1", "Z2", "Z3", "Z9"]

    pg.upsert_gateway_route("zone", "Z1", a)     # 涵蓋 K18、K19
    pg.upsert_gateway_route("site", "K21", b)    # K21 自己有 site 對應
    body = listing()
    assert body["unmapped_sites"] == ["K20", "K22"]
    assert body["unmapped_zones"] == ["Z2", "Z3", "Z9"], "zone 只看有沒有 zone 對應"

    pg.upsert_gateway_route("site", "K20", a)
    pg.upsert_gateway_route("site", "K22", b)
    pg.upsert_gateway_route("zone", "Z2", a)
    pg.upsert_gateway_route("zone", "Z3", a)
    pg.upsert_gateway_route("zone", "Z9", a)
    body = listing()
    assert body["unmapped_sites"] == [] and body["unmapped_zones"] == []


# ═══════════════════════════════════════════════════════════════
# 五、derive 預覽附帶 site / zone / route
# ═══════════════════════════════════════════════════════════════

KW_ORIGINAL_KEYS = {
    "id", "tag_name", "tb_type", "channel_name", "device_name", "tag_groups", "address",
    "description", "driver_type", "profile_exists", "data_type", "overridden",
}
SCALE_ORIGINAL_KEYS = {
    "id", "tag_name", "tb_type", "channel_name", "device_name", "full_tag_name",
    "scale_enabled", "scaling_type", "data_type", "scale_error",
}


def _stage(pg, tag, channel, site, **extra):
    insert_staging_row(
        pg, tag_name=tag, site=site, system_code="SYS", scada_node_name=channel,
        io_device="OPC_UA", io_address=f"A.{tag}", device_profile=f"{channel}-DEV-2F-GRP",
        **extra,
    )


def _derive_kw(api_client, headers, **body):
    body.setdefault("tb_status", "pending")
    r = api_client.post("/api/kw/derive", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return {x["tag_name"]: x for x in r.json()["data"]}


def _derive_scale(api_client, headers, **body):
    body.setdefault("scale_status", "pending")
    r = api_client.post("/api/pg/derive/scale", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return {x["tag_name"]: x for x in r.json()["data"]}


def test_derive_kw_fields_outputs_site_and_zone(pg_client_migrated):
    pg = pg_client_migrated
    _insert_location(pg, "BU", "K18", "ZoneA")
    insert_staging_row(pg, tag_name="K18_4F_CHS_A", site="  K18 ", system_code="CHS",
                       scada_node_name="K18CHS", io_device="OPC_UA", io_address="A.1")
    insert_staging_row(pg, tag_name="K99_4F_CHS_B", site="K99", system_code="CHS",
                       scada_node_name="K99CHS", io_device="OPC_UA", io_address="A.2")
    insert_staging_row(pg, tag_name="NOSITE_4F_CHS_C", site=None, system_code="CHS",
                       scada_node_name="NSCHS", io_device="OPC_UA", io_address="A.3")
    rows = pg.get_staging_list(page=0, page_size=None)["data"]
    for fn in (pg.derive_kw_fields, pg.derive_scale_fields):
        by = {e["tag_name"]: e for e in fn(rows)}
        assert (by["K18_4F_CHS_A"]["site"], by["K18_4F_CHS_A"]["zone"]) == ("K18", "ZoneA"), fn.__name__
        assert (by["K99_4F_CHS_B"]["site"], by["K99_4F_CHS_B"]["zone"]) == ("K99", ""), "對不到 location → zone 為空字串"
        assert (by["NOSITE_4F_CHS_C"]["site"], by["NOSITE_4F_CHS_C"]["zone"]) == ("", ""), "缺值給空字串"


def test_single_gateway_routes_as_single_without_structure_or_map(api_client, admin_headers):
    import app.main as m
    pg = m.pg_client
    only = _gw(pg, "ONLY-GW")
    _stage(pg, "T1", "CHA", "S1")
    _stage(pg, "T2", "WHATEVER", "")

    for gateway_id in (None, only):       # 不論請求有沒有帶 gateway_id 都要有 route
        kw = _derive_kw(api_client, admin_headers, gateway_id=gateway_id)
        for tag in ("T1", "T2"):
            route = kw[tag]["route"]
            assert route == {"gateway_id": only, "gateway_name": "ONLY-GW",
                             "status": "single", "reason": route["reason"]}, route
            assert route["reason"]
    sc = _derive_scale(api_client, admin_headers)
    assert all(x["route"]["status"] == "single" and x["route"]["gateway_id"] == only
               for x in sc.values())


def test_no_gateway_at_all_is_unrouted_not_error(api_client, admin_headers):
    import app.main as m
    _stage(m.pg_client, "T1", "CHA", "S1")
    route = _derive_kw(api_client, admin_headers)["T1"]["route"]
    assert route["status"] == "unrouted" and route["gateway_id"] is None
    assert route["gateway_name"] is None


@pytest.fixture
def two_gw_env(api_client):
    """兩台 Gateway（GW-A / GW-B）+ 結構快取 + 對應表 + 暫存資料，涵蓋所有 status。"""
    import app.main as m
    pg = m.pg_client
    a, b = _gw(pg, "GW-A"), _gw(pg, "GW-B")
    pg.sync_structure(a, {"CHA": {"DEV": ["2F.GRP"]}, "CSHARED": {"DEV": []}})
    pg.sync_structure(b, {"CHB": {"DEV": []}, "CSHARED": {"DEV": []}})

    for i in range(1, 8):
        _insert_location(pg, "BU", f"S{i}", f"Z{i}")
    pg.upsert_gateway_route("site", "S1", a)      # S1 → A
    pg.upsert_gateway_route("zone", "Z3", b)      # S3 經 zone → B
    pg.upsert_gateway_route("site", "S4", b)      # S4 → B（與 CHA 衝突）
    pg.upsert_gateway_route("site", "S6", a)      # S6：site → A
    pg.upsert_gateway_route("zone", "Z6", b)      # ……zone → B（site 優先，不該採用）

    _stage(pg, "T_OK", "CHA", "S1")
    _stage(pg, "T_CHONLY", "CHB", "S2")
    _stage(pg, "T_MAPONLY", "CNEW", "S3")
    _stage(pg, "T_CONFLICT", "CHA", "S4")
    _stage(pg, "T_UNROUTED", "CNOPE", "S5")
    _stage(pg, "T_SITEPRIO", "CHA", "S6")
    _stage(pg, "T_DUPCH", "CSHARED", "S7")
    _stage(pg, "T_NOLOC", "CHA", "SX")                         # 沒有 location 資料 → zone 空
    _stage(pg, "T_OVERRIDE", "CNEW2", "S2",                    # 推導的 channel 查無，但手動覆寫成 CHB
           kw_channel="CHB")
    return pg, a, b


def _check_two_gw_routes(by_tag, a, b):
    def route(tag):
        return by_tag[tag]["route"]

    assert (route("T_OK")["status"], route("T_OK")["gateway_id"], route("T_OK")["gateway_name"]) == ("ok", a, "GW-A")
    assert (route("T_CHONLY")["status"], route("T_CHONLY")["gateway_id"]) == ("channel_only", b)
    assert (route("T_MAPONLY")["status"], route("T_MAPONLY")["gateway_id"]) == ("map_only", b)
    assert (route("T_NOLOC")["status"], route("T_NOLOC")["gateway_id"]) == ("channel_only", a)
    assert (route("T_SITEPRIO")["status"], route("T_SITEPRIO")["gateway_id"]) == ("ok", a), "site 對應優先於 zone 對應"
    assert (route("T_OVERRIDE")["status"], route("T_OVERRIDE")["gateway_id"]) == ("channel_only", b), "路由用覆寫後的 channel"

    c = route("T_CONFLICT")
    assert c["status"] == "conflict" and c["gateway_id"] is None and c["gateway_name"] is None
    assert "GW-A" in c["reason"] and "GW-B" in c["reason"], c["reason"]
    assert "Gateway 1" not in c["reason"] and f"Gateway {a}" not in c["reason"], "reason 的 id 要換成名稱"

    d = route("T_DUPCH")
    assert d["status"] == "conflict_channel" and d["gateway_id"] is None
    assert "CSHARED" in d["reason"] and "GW-A" in d["reason"] and "GW-B" in d["reason"], d["reason"]

    u = route("T_UNROUTED")
    assert u["status"] == "unrouted" and u["gateway_id"] is None and u["reason"]

    for tag, item in by_tag.items():
        assert set(item["route"]) == {"gateway_id", "gateway_name", "status", "reason"}, tag


def test_derive_kw_route_all_statuses_with_two_gateways(two_gw_env, api_client, admin_headers):
    pg, a, b = two_gw_env
    # 不帶 gateway_id：只有 route，沒有 validation
    by_tag = _derive_kw(api_client, admin_headers)
    _check_two_gw_routes(by_tag, a, b)
    assert all("validation" not in x for x in by_tag.values())
    assert set(by_tag["T_OK"]) == KW_ORIGINAL_KEYS | {"site", "zone", "route"}, "純增量：只多 site/zone/route"
    assert (by_tag["T_OK"]["site"], by_tag["T_OK"]["zone"]) == ("S1", "Z1")
    assert (by_tag["T_NOLOC"]["site"], by_tag["T_NOLOC"]["zone"]) == ("SX", "")

    # 帶 gateway_id：route 不受影響（仍是自動路由結果），validation 仍針對請求指定的那台
    by_tag_b = _derive_kw(api_client, admin_headers, gateway_id=b)
    _check_two_gw_routes(by_tag_b, a, b)
    assert set(by_tag_b["T_OK"]) == KW_ORIGINAL_KEYS | {"site", "zone", "route", "validation"}
    # validation 對 GW-B 的結構：CHB 存在、CHA 不存在
    assert by_tag_b["T_CHONLY"]["validation"] == {
        "channel_exists": True, "device_exists": True,
        "group_exists": False, "group_auto_create": True}
    assert by_tag_b["T_OK"]["validation"] == {
        "channel_exists": False, "device_exists": False,
        "group_exists": False, "group_auto_create": False}
    by_tag_a = _derive_kw(api_client, admin_headers, gateway_id=a)
    assert by_tag_a["T_OK"]["validation"] == {
        "channel_exists": True, "device_exists": True,
        "group_exists": True, "group_auto_create": False}
    assert by_tag_a["T_CHONLY"]["validation"]["channel_exists"] is False
    # 指定的 gateway_id 不同 → route 一樣
    assert {t: x["route"] for t, x in by_tag_a.items()} == {t: x["route"] for t, x in by_tag_b.items()}


def test_derive_scale_route_all_statuses_with_two_gateways(two_gw_env, api_client, admin_headers):
    pg, a, b = two_gw_env
    by_tag = _derive_scale(api_client, admin_headers)
    _check_two_gw_routes(by_tag, a, b)
    assert set(by_tag["T_OK"]) == SCALE_ORIGINAL_KEYS | {"site", "zone", "route"}
    assert (by_tag["T_OK"]["site"], by_tag["T_OK"]["zone"]) == ("S1", "Z1")
    # Scale 與 Step 3 對同一筆的路由結果必須一致
    kw = _derive_kw(api_client, admin_headers)
    assert {t: x["route"] for t, x in by_tag.items()} == {t: x["route"] for t, x in kw.items()}


def test_route_follows_gateway_name_and_structure_changes(two_gw_env, api_client, admin_headers):
    """路由每次請求即時計算：改對應表、重新同步結構後，下一次 derive 立刻反映。"""
    pg, a, b = two_gw_env
    assert _derive_kw(api_client, admin_headers)["T_CONFLICT"]["route"]["status"] == "conflict"
    pg.upsert_gateway_route("site", "S4", a)               # 修正對應 → 與 CHA 一致
    assert _derive_kw(api_client, admin_headers)["T_CONFLICT"]["route"]["status"] == "ok"
    pg.sync_structure(b, {"CHB": {"DEV": []}})             # B 不再有 CSHARED
    r = _derive_kw(api_client, admin_headers)["T_DUPCH"]["route"]
    assert (r["status"], r["gateway_id"]) == ("channel_only", a)
    pg.update_gateway(a, name="GW-A-renamed")
    assert _derive_kw(api_client, admin_headers)["T_OK"]["route"]["gateway_name"] == "GW-A-renamed"


def test_deleting_one_gateway_makes_remaining_one_single(two_gw_env, api_client, admin_headers):
    pg, a, b = two_gw_env
    pg.delete_gateway(b)
    by_tag = _derive_kw(api_client, admin_headers)
    assert all(x["route"]["status"] == "single" and x["route"]["gateway_id"] == a
               for x in by_tag.values())


def test_derive_route_lookups_do_not_scale_with_row_count(two_gw_env, api_client, admin_headers, sql_log):
    """channel_index / site_map / zone_map / only_gateway_id 於請求開頭各取一次，
    之後逐列判斷不得再查 DB：SQL 次數與列數無關。"""
    pg, a, b = two_gw_env
    for i in range(60):
        _stage(pg, f"BULK_{i}", "CHA", "S1")
    ids = [r["id"] for r in pg.get_staging_list(page=0, page_size=None)["data"]][:2]

    def run(path, body):
        sql_log.clear()
        r = api_client.post(path, json=body, headers=admin_headers)
        assert r.status_code == 200, r.text
        return list(sql_log), r.json()["total"]

    for path, body in (("/api/kw/derive", {"tb_status": "pending"}),
                       ("/api/pg/derive/scale", {"scale_status": "pending"})):
        few, n_few = run(path, {**body, "ids": ids})
        many, n_many = run(path, body)
        assert n_few == 2 and n_many >= 60
        assert len(many) == len(few), f"{path} 的 SQL 次數隨列數增加：{len(few)} → {len(many)}"
        assert sum("kepware_structure" in q for q in many) == 1, path
        assert sum("gateway_route_map" in q for q in many) == 1, path


# ═══════════════════════════════════════════════════════════════
# 六、G7：POST /api/pg/staging/kw-override 部分更新
# ═══════════════════════════════════════════════════════════════

OLD = ("OLD_CH", "OLD_DEV", "OLD.GRP", 3)    # 預先寫入的覆寫值


def _kw_cols(pg, tag):
    with pg._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT kw_channel, kw_device, kw_tag_groups, kw_data_type "
                        "FROM scada_tag_config WHERE tag_name = %s", (tag,))
            return cur.fetchone()


def _row_id(pg, tag):
    with pg._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scada_tag_config WHERE tag_name = %s", (tag,))
            return cur.fetchone()[0]


def _seed_override_row(pg, tag, with_overrides=True):
    extra = dict(kw_channel=OLD[0], kw_device=OLD[1], kw_tag_groups=OLD[2], kw_data_type=OLD[3]) \
        if with_overrides else {}
    _stage(pg, tag, "CHA", "S1", **extra)
    return _row_id(pg, tag)


def _override(api_client, headers, body):
    return api_client.post("/api/pg/staging/kw-override", json=body, headers=headers)


def test_override_only_data_type_leaves_other_three_untouched(api_client, admin_headers):
    import app.main as m
    rid = _seed_override_row(m.pg_client, "G7_DT")
    r = _override(api_client, admin_headers, {"id": rid, "data_type": 1})
    assert r.status_code == 200 and r.json() == {"success": True}
    assert _kw_cols(m.pg_client, "G7_DT") == ("OLD_CH", "OLD_DEV", "OLD.GRP", 1)


def test_override_unsent_fields_are_not_written_even_when_row_has_no_overrides(api_client, admin_headers):
    """沒有任何覆寫的列只送 channel：device / tag_groups / data_type 必須仍是 NULL
    （舊行為會把 tag_groups 洗成空字串＝根層級覆寫）。"""
    import app.main as m
    rid = _seed_override_row(m.pg_client, "G7_NONE", with_overrides=False)
    assert _kw_cols(m.pg_client, "G7_NONE") == (None, None, None, None)
    assert _override(api_client, admin_headers, {"id": rid, "channel": "ONLY_CH"}).status_code == 200
    assert _kw_cols(m.pg_client, "G7_NONE") == ("ONLY_CH", None, None, None)
    # derive 仍用推導的 tag_groups（沒變成根層級）
    kw = _derive_kw(api_client, admin_headers)["G7_NONE"]
    assert kw["tag_groups"] == "2F.GRP" and kw["channel_name"] == "ONLY_CH"


def test_override_empty_tag_groups_means_device_root_not_null(api_client, admin_headers):
    import app.main as m
    rid = _seed_override_row(m.pg_client, "G7_TG")
    assert _override(api_client, admin_headers, {"id": rid, "tag_groups": ""}).status_code == 200
    cols = _kw_cols(m.pg_client, "G7_TG")
    assert cols == ("OLD_CH", "OLD_DEV", "", 3)
    assert cols[2] is not None and cols[2] == ""
    kw = _derive_kw(api_client, admin_headers)["G7_TG"]
    assert kw["tag_groups"] == "", "空字串 tag_groups = 覆寫到 device 根層級"
    assert kw["overridden"] is True


@pytest.mark.parametrize("value", [None, ""])
def test_override_data_type_null_or_blank_clears_only_data_type(api_client, admin_headers, value):
    import app.main as m
    rid = _seed_override_row(m.pg_client, "G7_CLR")
    r = _override(api_client, admin_headers, {"id": rid, "data_type": value})
    assert r.status_code == 200, r.text
    assert _kw_cols(m.pg_client, "G7_CLR") == ("OLD_CH", "OLD_DEV", "OLD.GRP", None)


def test_override_channel_and_device_blank_clear_to_null_independently(api_client, admin_headers):
    import app.main as m
    rid = _seed_override_row(m.pg_client, "G7_CD")
    assert _override(api_client, admin_headers, {"id": rid, "channel": ""}).status_code == 200
    assert _kw_cols(m.pg_client, "G7_CD") == (None, "OLD_DEV", "OLD.GRP", 3)
    assert _override(api_client, admin_headers, {"id": rid, "device": ""}).status_code == 200
    assert _kw_cols(m.pg_client, "G7_CD") == (None, None, "OLD.GRP", 3)
    assert _override(api_client, admin_headers, {"id": rid, "channel": "NEW", "device": "DEV9"}).status_code == 200
    assert _kw_cols(m.pg_client, "G7_CD") == ("NEW", "DEV9", "OLD.GRP", 3)


@pytest.mark.parametrize("body, expected", [
    ({"channel": "C", "device": "D", "tag_groups": "G.H", "data_type": 7}, ("C", "D", "G.H", 7)),
    ({"channel": "", "device": "", "tag_groups": "", "data_type": None}, (None, None, "", None)),
    ({"channel": "C", "device": "", "tag_groups": "", "data_type": 1}, ("C", None, "", 1)),
    ({"channel": "", "device": "D", "tag_groups": "X", "data_type": None}, (None, "D", "X", None)),
])
def test_override_full_four_fields_behaves_like_before(api_client, admin_headers, body, expected):
    """四欄全送：結果必須等同改版前的整筆覆寫（以保留不變的 update_kw_overrides 為對照）。"""
    import app.main as m
    pg = m.pg_client
    rid_api = _seed_override_row(pg, "G7_FULL_API")
    rid_old = _seed_override_row(pg, "G7_FULL_OLD")

    assert _override(api_client, admin_headers, {"id": rid_api, **body}).status_code == 200
    assert pg.update_kw_overrides(rid_old, body["channel"], body["device"],
                                  body["tag_groups"], body["data_type"]) is True

    assert _kw_cols(pg, "G7_FULL_API") == expected
    assert _kw_cols(pg, "G7_FULL_API") == _kw_cols(pg, "G7_FULL_OLD")


def test_override_empty_body_is_400_and_changes_nothing(api_client, admin_headers):
    import app.main as m
    rid = _seed_override_row(m.pg_client, "G7_EMPTY")
    for body in ({"id": rid}, {"id": rid, "unknown_field": 1}):
        r = _override(api_client, admin_headers, body)
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "至少需提供一個要更新的欄位"
    assert _kw_cols(m.pg_client, "G7_EMPTY") == OLD


def test_override_nonexistent_id_is_404_and_empty_body_check_comes_first(api_client, admin_headers):
    r = _override(api_client, admin_headers, {"id": 987654, "data_type": 8})
    assert r.status_code == 404 and "987654" in r.json()["detail"]
    r = _override(api_client, admin_headers, {"id": 987654, "channel": "X"})
    assert r.status_code == 404
    # 沒帶任何可更新欄位時，先回 400（不查 DB）
    assert _override(api_client, admin_headers, {"id": 987654}).status_code == 400


def test_override_rejects_invalid_values_and_requires_role(api_client, admin_headers):
    import app.main as m
    rid = _seed_override_row(m.pg_client, "G7_BAD")
    assert _override(api_client, admin_headers, {"id": rid, "data_type": "abc"}).status_code == 422
    assert _override(api_client, admin_headers, {"id": rid, "channel": None}).status_code == 422
    assert _override(api_client, admin_headers, {"id": rid}).status_code == 400
    assert api_client.post("/api/pg/staging/kw-override", json={"id": rid, "data_type": 1}
                           ).status_code == 401
    assert _override(api_client, _auth("user"), {"id": rid, "data_type": 1}).status_code == 403
    assert _override(api_client, _auth("operator"), {"id": rid, "data_type": 1}).status_code == 200
    assert _kw_cols(m.pg_client, "G7_BAD") == ("OLD_CH", "OLD_DEV", "OLD.GRP", 1)


def test_patch_kw_overrides_pgclient_contract(pg_client_migrated):
    pg = pg_client_migrated
    _stage(pg, "G7_PG", "CHA", "S1", kw_channel="X", kw_device="Y", kw_tag_groups="Z", kw_data_type=5)
    rid = _row_id(pg, "G7_PG")
    with pytest.raises(ValueError):
        pg.patch_kw_overrides(rid, {})
    with pytest.raises(ValueError):
        pg.patch_kw_overrides(rid, {"kw_channel": "bad-key"})
    assert _kw_cols(pg, "G7_PG") == ("X", "Y", "Z", 5), "拒絕的呼叫不得改動資料"
    assert pg.patch_kw_overrides(987654, {"data_type": 1}) is False
    # 只更新有給的鍵；tag_groups=None 才是 NULL（清除）
    assert pg.patch_kw_overrides(rid, {"tag_groups": None}) is True
    assert _kw_cols(pg, "G7_PG") == ("X", "Y", None, 5)
    assert pg.patch_kw_overrides(rid, {"data_type": "9"}) is True
    assert _kw_cols(pg, "G7_PG") == ("X", "Y", None, 9)
    # 既有的整筆覆寫 update_kw_overrides 行為不變
    assert pg.update_kw_overrides(rid, "", "", "", None) is True
    assert _kw_cols(pg, "G7_PG") == (None, None, "", None)
