# -*- coding: utf-8 -*-
"""pytest 全域設定與 fixture。

★★★ 安全性最重要的一段：必須在 import 任何 app.* 模組之前，把所有連線相關的
環境變數明確設定成測試專用值 ★★★

app/main.py 模組頂層會呼叫 `load_dotenv(os.path.join(BASE_DIR, ".env"))`。
python-dotenv 的 load_dotenv() 預設 override=False，意即「.env 檔裡的值不會覆蓋
os.environ 中已經存在的變數」。所以只要我們在這支 conftest.py 的最上層（模組被
匯入的當下，早於任何測試檔的 `import app.xxx`）就把 PG_DATABASE 等變數塞進
os.environ，之後不管 main.py 何時呼叫 load_dotenv()，讀到的都會是這裡設定的
測試用值，不會被 .env（可能指向正式資料庫！）覆蓋掉。

conftest.py 是 pytest 在蒐集任何測試之前一定會最先匯入的檔案，因此把這段放在這裡
（而非放在某個 fixture 函式內）可以確保順序正確。
"""
import os
import sys

import pytest

# ── 1. 決定測試用 PostgreSQL 連線資訊（可用環境變數覆蓋）───────────
#    連線資訊統一用 KIS_TEST_PG_* 系列變數指定，預設對應 CLAUDE.md 所述的 kis_test 角色。
_KIS_TEST_PG_HOST = os.environ.get("KIS_TEST_PG_HOST", "localhost")
_KIS_TEST_PG_PORT = os.environ.get("KIS_TEST_PG_PORT", "5432")
_KIS_TEST_PG_USER = os.environ.get("KIS_TEST_PG_USER", "kis_test")
_KIS_TEST_PG_PASSWORD = os.environ.get("KIS_TEST_PG_PASSWORD", "kis_test_pw")

# 測試專用的資料庫名稱，兩者都以 kis_pytest 開頭，讓下面的防呆檢查可以生效。
PG_TEST_DB = "kis_pytest"
COLLECTOR_TEST_DB = "kis_pytest_collector"

# ── 2. 在任何 app.* 模組被匯入之前，直接覆寫（而非 setdefault）以下環境變數 ──
#    用直接指定（=）而不是 setdefault，是刻意的：不管執行 pytest 的 shell /
#    容器原本殘留了什麼 PG_DATABASE 之類的變數，一律強制蓋成這裡的測試值，
#    絕不讓測試有機會連到任何非 kis_pytest 開頭的資料庫。
os.environ["PG_HOST"] = _KIS_TEST_PG_HOST
os.environ["PG_PORT"] = _KIS_TEST_PG_PORT
os.environ["PG_USER"] = _KIS_TEST_PG_USER
os.environ["PG_PASSWORD"] = _KIS_TEST_PG_PASSWORD
os.environ["PG_DATABASE"] = PG_TEST_DB
os.environ["COLLECTOR_DB_DATABASE"] = COLLECTOR_TEST_DB
os.environ["JWT_SECRET_KEY"] = "pytest-only-jwt-secret-not-for-production"
os.environ["KW_ENCRYPT_KEY"] = "pytest-only-kw-encrypt-key-not-for-production"

# ── 3. 最後一道防護：確認最終生效的資料庫名稱真的是測試庫 ─────────
#    就算上面的邏輯以後被改壞（例如誤用了 setdefault），這裡仍會擋下來，
#    直接中止整個 pytest session，不讓任何測試有機會執行 DROP/CREATE 等破壞性語句。
if not os.environ["PG_DATABASE"].startswith("kis_pytest"):
    pytest.exit(
        f"拒絕執行：PG_DATABASE={os.environ['PG_DATABASE']!r} 不是以 'kis_pytest' 開頭，"
        "可能誤連到正式/開發資料庫，已中止測試。"
    )
if not os.environ["COLLECTOR_DB_DATABASE"].startswith("kis_pytest"):
    pytest.exit(
        f"拒絕執行：COLLECTOR_DB_DATABASE={os.environ['COLLECTOR_DB_DATABASE']!r} "
        "不是以 'kis_pytest' 開頭，可能誤連到正式/開發資料庫，已中止測試。"
    )

# ── 4. 確保專案根目錄在 sys.path 上，讓 `import app.xxx` / `import tests.xxx` 都能找到 ──
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import psycopg2  # noqa: E402  (必須排在環境變數設定之後，但 psycopg2 本身跟 app.* 無關，提前 import 沒問題)


def _maintenance_conn_params():
    """連到 postgres 維護庫用的連線參數（用來 CREATE/DROP 測試資料庫本身）。"""
    return dict(
        host=_KIS_TEST_PG_HOST,
        port=int(_KIS_TEST_PG_PORT),
        user=_KIS_TEST_PG_USER,
        password=_KIS_TEST_PG_PASSWORD,
        dbname="postgres",
        connect_timeout=5,
    )


def _test_db_conn_params(dbname: str):
    return dict(
        host=_KIS_TEST_PG_HOST,
        port=int(_KIS_TEST_PG_PORT),
        user=_KIS_TEST_PG_USER,
        password=_KIS_TEST_PG_PASSWORD,
        dbname=dbname,
        connect_timeout=10,
    )


# ── 主庫 scada_tag_config「正式環境本來就預先存在」的最小欄位版本 ───────
# 刻意不含 kw_channel/kw_device/kw_tag_groups/kw_data_type/scale_status/scan_group，
# 這些欄位／參照表由 PGClient.ensure_schema() 以 ALTER TABLE / CREATE TABLE IF NOT EXISTS
# 補上，測試才能驗證「遷移」這件事本身。tag_name 的 UNIQUE 約束依 CLAUDE.md 所述，
# 是 import_staging() 的 ON CONFLICT (tag_name) 能運作的前提。
STAGING_BASE_TABLE_SQL = """
    CREATE TABLE scada_tag_config (
        id SERIAL PRIMARY KEY,
        site VARCHAR(100),
        system_code VARCHAR(100),
        scada_node_name VARCHAR(255),
        tag_name VARCHAR(255) NOT NULL,
        io_device VARCHAR(100),
        io_address VARCHAR(500),
        scale_enabled BOOLEAN DEFAULT FALSE,
        raw_low DOUBLE PRECISION,
        raw_high DOUBLE PRECISION,
        scaled_low DOUBLE PRECISION,
        scaled_high DOUBLE PRECISION,
        description TEXT,
        project_name VARCHAR(255),
        data_owner VARCHAR(255),
        device_profile VARCHAR(255),
        tb_status VARCHAR(20) DEFAULT 'pending',
        pg_status VARCHAR(20) DEFAULT 'pending',
        processed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT NOW(),
        CONSTRAINT scada_tag_config_tag_name_key UNIQUE (tag_name)
    )
"""

# ── Collector 庫 tags 表：enable 刻意用 SMALLINT（複製正式環境曾出現的型別），
# 用來重現／驗證「enable 為 smallint 而非 boolean」時仍能正確寫入的 bug 修正。
COLLECTOR_BASE_TABLE_SQL = """
    CREATE TABLE tags (
        tag_id SERIAL PRIMARY KEY,
        tagname VARCHAR(255) NOT NULL UNIQUE,
        tag_address VARCHAR(500),
        scan_group VARCHAR(255) DEFAULT '',
        unit VARCHAR(100) DEFAULT '',
        description TEXT,
        enable SMALLINT DEFAULT 1,
        target_table VARCHAR(255),
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )
"""


def _reset_schema(conn_params: dict, create_table_sql: str):
    """DROP SCHEMA public CASCADE + CREATE SCHEMA public，再重建一張「正式環境本來就
    預先存在」的表。用 autocommit，避免 DDL 卡在未提交的交易裡。"""
    conn = psycopg2.connect(**conn_params)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute(create_table_sql)
    finally:
        conn.close()


# ── session 層級：檢查 PostgreSQL 是否可連線 ────────────────────────

@pytest.fixture(scope="session")
def _pg_unavailable_reason():
    """回傳 None 表示 PostgreSQL 可連線；否則回傳不可連線的原因字串。
    只在 session 開始時檢查一次，避免每個測試都重新嘗試連線拖慢整體執行時間。"""
    try:
        conn = psycopg2.connect(**_maintenance_conn_params())
        conn.close()
        return None
    except Exception as e:
        return f"無法連線到測試用 PostgreSQL（host={_KIS_TEST_PG_HOST}, user={_KIS_TEST_PG_USER}): {e}"


@pytest.fixture(scope="session")
def test_databases(_pg_unavailable_reason):
    """session 層級：建立（先 DROP IF EXISTS）kis_pytest 與 kis_pytest_collector 兩個
    測試資料庫，結束時刪除。PostgreSQL 不可連線時，直接 skip 所有依賴這個 fixture
    （進而依賴 clean_db / pg_client / api_client）的測試，並附上清楚原因；
    不需要 DB 的純邏輯測試不會用到這個 fixture，完全不受影響、照常執行。"""
    if _pg_unavailable_reason:
        pytest.skip(_pg_unavailable_reason)

    conn = psycopg2.connect(**_maintenance_conn_params())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for db in (PG_TEST_DB, COLLECTOR_TEST_DB):
                # WITH (FORCE)：PG13+ 支援，會順便把該庫上任何殘留連線斷掉再刪，
                # 避免因為前一次測試意外留下的連線導致 DROP DATABASE 卡住。
                cur.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
                cur.execute(f'CREATE DATABASE "{db}"')
    finally:
        conn.close()

    yield

    try:
        conn = psycopg2.connect(**_maintenance_conn_params())
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for db in (PG_TEST_DB, COLLECTOR_TEST_DB):
                    cur.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        finally:
            conn.close()
    except Exception:
        # 收尾清理失敗不應該讓整個測試 session 回報失敗
        pass


# ── function 層級：每個測試前重置兩庫的 schema，確保測試彼此獨立 ────────

@pytest.fixture
def clean_db(test_databases):
    """function 層級：把 kis_pytest / kis_pytest_collector 兩庫的 public schema 整個
    重建，並重新建立「正式環境本來就預先存在」的最小表結構。每個測試各自呼叫一次，
    確保測試之間互不依賴、與執行順序無關（這正是原驗收腳本吃過的虧：因為前一輪已經
    對同一個庫跑過 ensure_schema()，導致『欄位尚未存在』的前置斷言失敗）。"""
    _reset_schema(_test_db_conn_params(PG_TEST_DB), STAGING_BASE_TABLE_SQL)
    _reset_schema(_test_db_conn_params(COLLECTOR_TEST_DB), COLLECTOR_BASE_TABLE_SQL)
    yield


@pytest.fixture
def pg_client(clean_db):
    """乾淨 DB + 一個全新的 PGClient 實例。scada_tag_config 只有基本欄位
    （不含 kw_*/scale_status/scan_group），適合用來測試「還沒 migrate」的狀態，
    或是不需要那些欄位的功能（如 import_formal、Collector 寫入）。"""
    from app.pg_client import PGClient
    return PGClient()


@pytest.fixture
def pg_client_migrated(pg_client):
    """乾淨 DB，且已經呼叫過 ensure_schema()：kw_*/scale_status/scan_group 等欄位與
    6 張參照表都已存在。供需要這些欄位的測試使用（例如 import_staging 的 INSERT
    會用到 scan_group、derive_scale_fields 的覆寫測試會用到 kw_channel 等）。"""
    pg_client.ensure_schema()
    return pg_client


# ── 認證 / API 測試共用 fixture ─────────────────────────────────────

@pytest.fixture
def admin_headers():
    """回傳帶 admin 角色 Bearer Token 的 HTTP headers，供 TestClient 呼叫需要
    require_role("admin", "operator") 的端點使用。純 JWT 簽章/驗證，不需要 DB
    （不必真的在 kepitsimple_user 表建立這個帳號）。"""
    from app.auth import create_token
    token = create_token("tester", "admin")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api_client(clean_db):
    """FastAPI TestClient。以 `with` 進入時會觸發 app 的 on_startup（呼叫
    pg_client.ensure_schema()、建立初始 admin 帳號、啟動每週背景清理排程執行緒），
    離開時觸發 on_shutdown（設定 _cleanup_stop，讓背景排程執行緒盡快結束）。
    也就是說：測試主體開始執行時，schema 已經 migrate 完成，可以直接對
    app.main.pg_client 寫測試資料。"""
    from fastapi.testclient import TestClient
    import app.main as main_module
    with TestClient(main_module.app) as client:
        yield client
