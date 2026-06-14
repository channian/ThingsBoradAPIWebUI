# -*- coding: utf-8 -*-
"""
PostgreSQL 資料庫客戶端
管理所有 PG 表的 CRUD 操作與 Tag 推導邏輯
"""

import os
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

log = logging.getLogger("pg_client")


class PGClient:
    """PostgreSQL 連線與操作封裝"""

    def __init__(self):
        self.conn_params = {
            "host": os.getenv("PG_HOST", "localhost"),
            "port": int(os.getenv("PG_PORT", 5432)),
            "database": os.getenv("PG_DATABASE", ""),
            "user": os.getenv("PG_USER", ""),
            "password": os.getenv("PG_PASSWORD", ""),
        }

    @contextmanager
    def _get_conn(self):
        conn = psycopg2.connect(**self.conn_params)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_ref_tables(self, conn):
        """確保參照表存在"""
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS location_config (
                    id SERIAL PRIMARY KEY,
                    bu VARCHAR(100),
                    site VARCHAR(100),
                    zone VARCHAR(100),
                    UNIQUE(bu, site, zone)
                );
                CREATE TABLE IF NOT EXISTS ownership_config (
                    id SERIAL PRIMARY KEY,
                    department VARCHAR(255),
                    owner VARCHAR(255),
                    UNIQUE(department, owner)
                );
                CREATE TABLE IF NOT EXISTS device_config (
                    id SERIAL PRIMARY KEY,
                    device_name VARCHAR(255) NOT NULL UNIQUE,
                    driver_type VARCHAR(100),
                    site VARCHAR(100),
                    system_code VARCHAR(100),
                    ip_address VARCHAR(100),
                    description TEXT
                );
                CREATE TABLE IF NOT EXISTS system_config (
                    id SERIAL PRIMARY KEY,
                    system_code VARCHAR(100) NOT NULL UNIQUE,
                    system_name VARCHAR(255),
                    description TEXT
                );
                CREATE TABLE IF NOT EXISTS tb_device_profile (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    description TEXT
                );
                CREATE TABLE IF NOT EXISTS kepitsimple_user (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    display_name VARCHAR(255),
                    role VARCHAR(20) NOT NULL DEFAULT 'operator',
                    perm_group VARCHAR(50) NOT NULL DEFAULT 'viewer',
                    is_active BOOLEAN DEFAULT true,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) NOT NULL,
                    action VARCHAR(100) NOT NULL,
                    detail TEXT,
                    ip_addr VARCHAR(45),
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_activity_log_created
                    ON activity_log (created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_log_user
                    ON activity_log (username);
                CREATE TABLE IF NOT EXISTS kepware_gateway (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    url VARCHAR(500) NOT NULL,
                    username VARCHAR(255) NOT NULL,
                    password_enc VARCHAR(500) NOT NULL,
                    verify_ssl BOOLEAN DEFAULT TRUE,
                    zone VARCHAR(100),
                    is_default BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)

    def _ensure_extra_columns(self, conn):
        """確保各表有必要的額外欄位"""
        with conn.cursor() as cur:
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'scada_tag_config'
                        AND column_name = 'scale_status'
                    ) THEN
                        ALTER TABLE scada_tag_config
                        ADD COLUMN scale_status VARCHAR(20) DEFAULT 'pending';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'scada_tag_config'
                        AND column_name = 'scan_group'
                    ) THEN
                        ALTER TABLE scada_tag_config
                        ADD COLUMN scan_group VARCHAR(255) DEFAULT '';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'kepitsimple_user'
                        AND column_name = 'perm_group'
                    ) THEN
                        ALTER TABLE kepitsimple_user
                        ADD COLUMN perm_group VARCHAR(50) NOT NULL DEFAULT 'viewer';
                    END IF;
                END $$;
            """)

    def test_connection(self) -> dict:
        """測試 PG 連線並確保參照表存在"""
        with self._get_conn() as conn:
            self._ensure_ref_tables(conn)
            self._ensure_extra_columns(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                return {"success": True, "version": version}

    # ── scada_tag_config (暫存表) ─────────────────────

    def import_staging(self, rows: list) -> dict:
        """將 CSV 資料匯入暫存表，利用 ON CONFLICT 提升效能與穩定性"""
        if not rows:
            return {"inserted": 0, "skipped": 0, "errors": [], "total": 0}

        inserted = 0
        skipped = 0
        errors = []
        
        # 準備資料與清洗
        clean_data = []
        for row in rows:
            tag_name = (row.get("tag_name") or row.get("Tag Name") or "").strip()
            if not tag_name:
                skipped += 1
                continue
            
            try:
                # 預處理資料格式
                data_tuple = (
                    _get(row, "site", "Site"),
                    _get(row, "system_code", "System"),
                    _get(row, "scada_node_name", "SCADA Node Name"),
                    tag_name,
                    _get(row, "io_device", "I/O DEVICE"),
                    _get(row, "io_address", "I/O ADDRESS"),
                    _parse_bool(row.get("scale_enabled") or row.get("SCALE Enabled") or ""),
                    _parse_num(row.get("raw_low") or row.get("Raw Low")),
                    _parse_num(row.get("raw_high") or row.get("Raw High")),
                    _parse_num(row.get("scaled_low") or row.get("Scaled Low")),
                    _parse_num(row.get("scaled_high") or row.get("Scaled High")),
                    _get(row, "description", "Description"),
                    _get(row, "project_name", "專案名稱"),
                    _get(row, "data_owner", "DataOwner"),
                    _get(row, "device_profile", "device_profile"),
                    _get(row, "scan_group", "Scan Group"),
                )
                clean_data.append(data_tuple)
            except Exception as e:
                errors.append({"tag_name": tag_name, "reason": f"資料預處理失敗: {e}"})

        # 批次寫入資料庫
        if clean_data:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    # 使用 PostgreSQL 的 ON CONFLICT DO NOTHING (前提是 tag_name 有 Unique Constraint)
                    # 如果 tag_name 重複，資料庫會自動跳過該筆而不報錯
                    query = """
                        INSERT INTO scada_tag_config
                        (site, system_code, scada_node_name, tag_name,
                         io_device, io_address, scale_enabled,
                         raw_low, raw_high, scaled_low, scaled_high,
                         description, project_name, data_owner, device_profile,
                         scan_group)
                        VALUES %s
                        ON CONFLICT (tag_name) DO NOTHING
                        RETURNING id;
                    """
                    try:
                        # 使用 psycopg2 的 fast execution 擴展
                        from psycopg2.extras import execute_values
                        execute_values(cur, query, clean_data)
                        
                        # 在 DO NOTHING 模式下，只有真正新增的會回傳，這可以用來計算數量
                        inserted = cur.rowcount 
                        skipped += (len(clean_data) - inserted)
                        
                    except Exception as e:
                        # 這裡的錯誤通常是表格結構問題（欄位長度、型態不合）
                        errors.append({"tag_name": "BULK_INSERT", "reason": str(e)})
                        # 由於使用了 contextmanager，這裡 raise 會自動 rollback
                        raise 

        return {
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors,
            "total": len(rows),
        }

    def get_staging_list(
        self,
        page: int = 0,
        page_size: int = 50,
        tb_status: str = None,
        pg_status: str = None,
        scale_status: str = None,
    ) -> dict:
        """分頁查詢暫存表"""
        conditions = []
        params = []

        if tb_status:
            conditions.append("tb_status = %s")
            params.append(tb_status)
        if pg_status:
            conditions.append("pg_status = %s")
            params.append(pg_status)
        if scale_status:
            conditions.append("scale_status = %s")
            params.append(scale_status)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = page * page_size

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"SELECT COUNT(*) as cnt FROM scada_tag_config {where}", params
                )
                total = cur.fetchone()["cnt"]

                cur.execute(
                    f"""SELECT * FROM scada_tag_config {where}
                    ORDER BY id ASC LIMIT %s OFFSET %s""",
                    params + [page_size, offset],
                )
                rows = cur.fetchall()

        return {
            "data": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
        }

    def update_staging_status(self, ids: list, field: str, status: str):
        """批次更新暫存表狀態"""
        if field not in ("tb_status", "pg_status", "scale_status"):
            raise ValueError("field must be tb_status/pg_status/scale_status")
        if status not in ("pending", "done", "skip"):
            raise ValueError("status must be pending/done/skip")

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE scada_tag_config
                    SET {field} = %s, processed_at = NOW()
                    WHERE id = ANY(%s)""",
                    (status, ids),
                )
                return cur.rowcount

    def delete_staging(self, ids: list) -> int:
        """刪除暫存表資料"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM scada_tag_config WHERE id = ANY(%s)", (ids,)
                )
                return cur.rowcount

    def clear_staging(self) -> int:
        """清空暫存表"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM scada_tag_config")
                return cur.rowcount

    # ── 參照表查詢 ────────────────────────────────────

    def get_locations(self) -> list:
        """取得所有 location_config"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM location_config ORDER BY id")
                return [dict(r) for r in cur.fetchall()]

    def get_ownerships(self) -> list:
        """取得所有 ownership_config"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM ownership_config ORDER BY id")
                return [dict(r) for r in cur.fetchall()]

    def get_devices(self) -> list:
        """取得所有 device_config"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM device_config ORDER BY id")
                return [dict(r) for r in cur.fetchall()]

    def get_systems(self) -> list:
        """取得所有 system_config"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM system_config ORDER BY id")
                return [dict(r) for r in cur.fetchall()]

    def get_tb_profiles(self) -> list:
        """取得所有 Tb_Device_Profile"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM tb_device_profile ORDER BY id")
                return [dict(r) for r in cur.fetchall()]

    # ── 參照表寫入 ────────────────────────────────────

    def upsert_location(self, bu: str, site: str, zone: str) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO location_config (bu, site, zone)
                    VALUES (%s, %s, %s)
                    ON CONFLICT ON CONSTRAINT uq_location DO NOTHING
                    RETURNING id""",
                    (bu, site, zone),
                )
                row = cur.fetchone()
                return row[0] if row else 0

    def upsert_ownership(self, department: str, data_owner: str) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO ownership_config (department, data_owner)
                    VALUES (%s, %s)
                    ON CONFLICT ON CONSTRAINT uq_department_owner DO NOTHING
                    RETURNING id""",
                    (department, data_owner),
                )
                row = cur.fetchone()
                return row[0] if row else 0

    def upsert_device(
        self, device_name: str, driver_type: str,
        site: str = None, system_code: str = None,
        ip_address: str = None, description: str = None,
    ) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO device_config
                    (device_name, driver_type, site, system_code,
                     ip_address, description)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (device_name) DO UPDATE SET
                        driver_type = EXCLUDED.driver_type,
                        site = EXCLUDED.site,
                        system_code = EXCLUDED.system_code,
                        ip_address = EXCLUDED.ip_address,
                        description = EXCLUDED.description
                    RETURNING id""",
                    (device_name, driver_type, site, system_code,
                     ip_address, description),
                )
                return cur.fetchone()[0]

    def upsert_system(self, system_code: str, system_name: str = None,
                      description: str = None) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO system_config (system_code, system_name, description)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (system_code) DO UPDATE SET
                        system_name = EXCLUDED.system_name,
                        description = EXCLUDED.description
                    RETURNING id""",
                    (system_code, system_name, description),
                )
                return cur.fetchone()[0]

    def upsert_tb_profile(self, name: str, description: str = None) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO tb_device_profile (name, description)
                    VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        description = EXCLUDED.description
                    RETURNING id""",
                    (name, description),
                )
                return cur.fetchone()[0]

    def delete_ref_row(self, table: str, row_id: int) -> bool:
        """刪除參照表的一筆資料"""
        allowed = {
            "location_config", "ownership_config",
            "device_config", "system_config", "tb_device_profile",
        }
        if table not in allowed:
            raise ValueError(f"不允許操作的表: {table}")
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))
                return cur.rowcount > 0

    # ── 使用者管理 (kepitsimple_user) ──────────────────

    def get_user_by_username(self, username: str) -> dict:
        """依帳號查詢使用者"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM kepitsimple_user WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def get_all_users(self) -> list:
        """取得所有使用者（不含密碼）"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, username, display_name, role,
                           is_active, created_at, updated_at
                    FROM kepitsimple_user ORDER BY id
                """)
                return [dict(r) for r in cur.fetchall()]

    def create_user(self, username: str, password_hash: str,
                    display_name: str = "", role: str = "operator") -> dict:
        """建立使用者"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO kepitsimple_user
                        (username, password_hash, display_name, role)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, username, display_name, role,
                              is_active, created_at
                """, (username, password_hash, display_name, role))
                return dict(cur.fetchone())

    def update_user(self, user_id: int, **fields) -> bool:
        """更新使用者欄位"""
        allowed = {"display_name", "role", "is_active"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        updates["updated_at"] = "NOW()"
        set_parts = []
        params = []
        for k, v in updates.items():
            if v == "NOW()":
                set_parts.append(f"{k} = NOW()")
            else:
                set_parts.append(f"{k} = %s")
                params.append(v)
        params.append(user_id)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE kepitsimple_user SET {', '.join(set_parts)} WHERE id = %s",
                    params,
                )
                return cur.rowcount > 0

    def update_user_password(self, user_id: int, password_hash: str) -> bool:
        """更新使用者密碼"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE kepitsimple_user SET password_hash = %s, updated_at = NOW() WHERE id = %s",
                    (password_hash, user_id),
                )
                return cur.rowcount > 0

    def delete_user(self, user_id: int) -> bool:
        """刪除使用者"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kepitsimple_user WHERE id = %s", (user_id,))
                return cur.rowcount > 0

    def count_users(self) -> int:
        """計算使用者數量"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM kepitsimple_user")
                return cur.fetchone()[0]

    # ── Kepware Gateway 管理 ────────────────────────────

    def get_gateways(self) -> list:
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, name, url, username, verify_ssl,
                           zone, is_default, created_at, updated_at
                    FROM kepware_gateway ORDER BY id
                """)
                return [dict(r) for r in cur.fetchall()]

    def get_gateway(self, gw_id: int) -> dict:
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM kepware_gateway WHERE id = %s", (gw_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    def create_gateway(self, name: str, url: str, username: str,
                       password_enc: str, verify_ssl: bool = True,
                       zone: str = None, is_default: bool = False) -> dict:
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if is_default:
                    cur.execute("UPDATE kepware_gateway SET is_default = FALSE WHERE is_default = TRUE")
                cur.execute("""
                    INSERT INTO kepware_gateway
                        (name, url, username, password_enc, verify_ssl, zone, is_default)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, name, url, username, verify_ssl,
                              zone, is_default, created_at, updated_at
                """, (name, url, username, password_enc, verify_ssl, zone, is_default))
                return dict(cur.fetchone())

    def update_gateway(self, gw_id: int, **fields) -> bool:
        allowed = {"name", "url", "username", "password_enc",
                   "verify_ssl", "zone", "is_default"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                if updates.get("is_default"):
                    cur.execute("UPDATE kepware_gateway SET is_default = FALSE WHERE is_default = TRUE")
                set_parts = []
                params = []
                for k, v in updates.items():
                    set_parts.append(f"{k} = %s")
                    params.append(v)
                set_parts.append("updated_at = NOW()")
                params.append(gw_id)
                cur.execute(
                    f"UPDATE kepware_gateway SET {', '.join(set_parts)} WHERE id = %s",
                    params,
                )
                return cur.rowcount > 0

    def delete_gateway(self, gw_id: int) -> bool:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kepware_gateway WHERE id = %s", (gw_id,))
                return cur.rowcount > 0

    # ── 操作日誌 (activity_log) ──────────────────────────

    def add_activity_log(self, username: str, action: str,
                         detail: str = "", ip_addr: str = ""):
        """新增操作日誌"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO activity_log (username, action, detail, ip_addr)
                    VALUES (%s, %s, %s, %s)
                """, (username, action, detail, ip_addr))

    def get_activity_logs(self, page: int = 1, page_size: int = 50,
                          username: str = None, action: str = None) -> dict:
        """查詢操作日誌（分頁）"""
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                where_parts = []
                params = []
                if username:
                    where_parts.append("username = %s")
                    params.append(username)
                if action:
                    where_parts.append("action = %s")
                    params.append(action)
                where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

                cur.execute(f"SELECT COUNT(*) FROM activity_log {where_sql}", params)
                total = cur.fetchone()["count"]

                offset = (page - 1) * page_size
                cur.execute(f"""
                    SELECT id, username, action, detail, ip_addr, created_at
                    FROM activity_log {where_sql}
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """, params + [page_size, offset])
                rows = [dict(r) for r in cur.fetchall()]
                return {"total": total, "page": page,
                        "page_size": page_size, "rows": rows}

    def cleanup_activity_logs(self, days: int = 7) -> int:
        """清理超過指定天數的操作日誌"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM activity_log
                    WHERE created_at < NOW() - INTERVAL '%s days'
                """, (days,))
                count = cur.rowcount
                log.info(f"已清理 {count} 筆超過 {days} 天的操作日誌")
                return count

    # ── PG 正式表寫入 ─────────────────────────────────

    def _ensure_formal_table(self, conn):
        """確保 tags 正式表存在，並同步 sequence"""
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tags (
                    tag_id SERIAL PRIMARY KEY,
                    tagname VARCHAR(255) NOT NULL UNIQUE,
                    description TEXT,
                    node_name VARCHAR(255),
                    driver_type VARCHAR(100),
                    address VARCHAR(500),
                    tablename VARCHAR(255),
                    zone VARCHAR(100),
                    bu VARCHAR(100),
                    site VARCHAR(100),
                    floor VARCHAR(50),
                    system VARCHAR(100),
                    owner VARCHAR(255),
                    department VARCHAR(255),
                    data_type VARCHAR(50) DEFAULT 'float',
                    created_date TIMESTAMP DEFAULT NOW(),
                    updated_date TIMESTAMP DEFAULT NOW()
                )
            """)
            # 同步 sequence，避免 tag_id 衝突
            cur.execute("""
                SELECT setval(
                    pg_get_serial_sequence('tags', 'tag_id'),
                    COALESCE((SELECT MAX(tag_id) FROM tags), 0) + 1,
                    false
                )
            """)
            log.info("[_ensure_formal_table] sequence 已同步")
            # 確保 system 欄位存在（舊表可能沒有）
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tags'
                        AND column_name = 'system'
                    ) THEN
                        ALTER TABLE tags
                        ADD COLUMN system VARCHAR(100);
                    END IF;
                END $$;
            """)

    def import_formal(self, derived_rows: list) -> dict:
        """將推導後的資料寫入 tags 正式表"""
        log.info(f"[import_formal] 開始寫入，共 {len(derived_rows)} 筆")
        if not derived_rows:
            log.warning("[import_formal] derived_rows 為空，跳過")
            return {"inserted": 0, "skipped": 0, "errors": [], "total": 0}

        inserted = 0
        skipped = 0
        errors = []

        try:
            with self._get_conn() as conn:
                self._ensure_formal_table(conn)
                log.info("[import_formal] 正式表 tags 已確認存在")
                with conn.cursor() as cur:
                    for i, row in enumerate(derived_rows):
                        tagname = row.get("tagname", "")
                        if not tagname:
                            log.warning(f"[import_formal] 第 {i} 筆 tagname 為空，跳過")
                            skipped += 1
                            continue
                        try:
                            cur.execute("""
                                INSERT INTO tags
                                (tagname, description, node_name, driver_type,
                                 address, tablename, zone, bu, site, floor,
                                 system, owner, department, data_type,
                                 created_date)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                                ON CONFLICT (tagname) DO UPDATE SET
                                    description = EXCLUDED.description,
                                    node_name = EXCLUDED.node_name,
                                    driver_type = EXCLUDED.driver_type,
                                    address = EXCLUDED.address,
                                    tablename = EXCLUDED.tablename,
                                    zone = EXCLUDED.zone,
                                    bu = EXCLUDED.bu,
                                    site = EXCLUDED.site,
                                    floor = EXCLUDED.floor,
                                    system = EXCLUDED.system,
                                    owner = EXCLUDED.owner,
                                    department = EXCLUDED.department,
                                    data_type = EXCLUDED.data_type,
                                    updated_date = NOW()
                            """, (
                                tagname,
                                row.get("description", ""),
                                row.get("node_name", ""),
                                row.get("driver_type", ""),
                                row.get("address", ""),
                                row.get("tabname", ""),
                                row.get("zone", ""),
                                row.get("bu", ""),
                                row.get("site", ""),
                                row.get("floor", ""),
                                row.get("system", ""),
                                row.get("owner", ""),
                                row.get("department", ""),
                                row.get("data_type", "float"),
                            ))
                            inserted += 1
                            log.info(f"[import_formal] 寫入成功: {tagname}")
                        except Exception as e:
                            log.error(f"[import_formal] 寫入失敗 tagname={tagname}: {e}")
                            errors.append({"tagname": tagname, "reason": str(e)})
        except Exception as e:
            log.error(f"[import_formal] 連線或建表失敗: {e}", exc_info=True)
            return {
                "inserted": inserted,
                "skipped": skipped,
                "errors": [{"tagname": "_connection", "reason": str(e)}],
                "total": len(derived_rows),
            }

        log.info(f"[import_formal] 完成: inserted={inserted}, skipped={skipped}, errors={len(errors)}")
        return {
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors,
            "total": len(derived_rows),
        }

    def delete_formal_by_tagnames(self, tagnames: list) -> int:
        """從正式表刪除指定 tagname 的資料（用於回滾）"""
        if not tagnames:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tags WHERE tagname = ANY(%s)",
                    (tagnames,)
                )
                return cur.rowcount

    # ── Collector DB 寫入 ──────────────────────────────

    def _get_collector_conn_params(self):
        """取得 Collector DB 連線參數（同主機，僅 database 不同）"""
        collector_db = os.getenv("COLLECTOR_DB_DATABASE", "").strip()
        if not collector_db:
            raise RuntimeError("未設定 COLLECTOR_DB_DATABASE 環境變數")
        params = dict(self.conn_params)
        params["database"] = collector_db
        return params

    @contextmanager
    def _get_collector_conn(self):
        params = self._get_collector_conn_params()
        conn = psycopg2.connect(**params)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_collector_connection(self) -> dict:
        """測試 Collector DB 連線"""
        with self._get_collector_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                return {"success": True, "version": version}

    def _ensure_collector_tags_table(self, conn):
        """確保 Collector DB 的 tags 表存在"""
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tags (
                    tag_id SERIAL PRIMARY KEY,
                    tagname VARCHAR(255) NOT NULL UNIQUE,
                    tag_address VARCHAR(500),
                    scan_group VARCHAR(255) DEFAULT '',
                    unit VARCHAR(100) DEFAULT '',
                    description TEXT,
                    enable BOOLEAN DEFAULT TRUE,
                    target_table VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

    def import_collector_tags(self, rows: list) -> dict:
        """將推導後的資料寫入 Collector DB 的 tags 表"""
        log.info(f"[import_collector] 開始寫入 Collector tags，共 {len(rows)} 筆")
        if not rows:
            return {"inserted": 0, "errors": [], "total": 0}

        inserted = 0
        errors = []

        with self._get_collector_conn() as conn:
            self._ensure_collector_tags_table(conn)
            with conn.cursor() as cur:
                for row in rows:
                    tagname = row.get("tagname", "")
                    if not tagname:
                        continue
                    try:
                        cur.execute("""
                            INSERT INTO tags
                            (tagname, tag_address, scan_group, description,
                             enable, target_table)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (tagname) DO UPDATE SET
                                tag_address = EXCLUDED.tag_address,
                                scan_group = EXCLUDED.scan_group,
                                description = EXCLUDED.description,
                                enable = EXCLUDED.enable,
                                target_table = EXCLUDED.target_table,
                                updated_at = NOW()
                        """, (
                            tagname,
                            row.get("tag_address", ""),
                            row.get("scan_group", ""),
                            row.get("description", ""),
                            row.get("enable", True),
                            row.get("target_table", ""),
                        ))
                        inserted += 1
                    except Exception as e:
                        log.error(f"[import_collector] 寫入失敗 tagname={tagname}: {e}")
                        errors.append({"tagname": tagname, "reason": str(e)})

        log.info(f"[import_collector] 完成: inserted={inserted}, errors={len(errors)}")
        return {"inserted": inserted, "errors": errors, "total": len(rows)}

    # ── Tag 推導邏輯 ──────────────────────────────────

    def derive_tb_fields(self, staging_rows: list) -> list:
        """對暫存表資料推導 TB 建點所需欄位"""
        # 載入參照表做快取
        devices = {d["device_name"]: d for d in self.get_devices()}
        profiles = {p["name"]: p for p in self.get_tb_profiles()}
        locations = self.get_locations()

        results = []
        for row in staging_rows:
            tag_name = row.get("tag_name", "")
            parts = tag_name.split("_") if tag_name else []

            site_prefix = parts[0] if len(parts) > 0 else ""
            floor = parts[1] if len(parts) > 1 else ""
            if floor.upper() == "BF":
                floor = "B1F"
            # system 優先用 CSV 的 system_code，fallback 才從 tag_name 拆
            system = (row.get("system_code") or "").strip() or (parts[2] if len(parts) > 2 else "")

            # 查 driver_type: 先查 device_config，查不到就用 io_device 值本身判斷
            io_device = row.get("io_device", "")
            driver_type = _resolve_driver_type(io_device, devices)

            # 推導 nodename: 優先用 scada_node_name 去掉 _，否則用 site + system_code
            scada_node_name = (row.get("scada_node_name") or "").strip()
            site_val = (row.get("site") or "").strip()
            system_code_val = (row.get("system_code") or "").strip()
            if scada_node_name:
                nodename = scada_node_name.replace("_", "")
            elif site_val or system_code_val:
                nodename = site_val + system_code_val
            else:
                nodename = site_prefix + system

            # DeviceProfile: CSV 有填就優先用
            csv_profile = (row.get("device_profile") or "").strip()
            if csv_profile:
                derived_profile = csv_profile
            elif driver_type.upper() == "IFIX":
                # PLC-IFIX: nodename-zone-site-system
                matched_loc = next((l for l in locations if l["site"] == site_val), None)
                zone = matched_loc["zone"] if matched_loc else ""
                derived_profile = f"{nodename}-{zone}-{site_prefix}-{system}"
            else:
                derived_profile = f"{nodename}-{system}-{floor}-{system}"

            # label: OPC 類加 ns=2;s= 前綴
            io_address = row.get("io_address", "")
            if driver_type.upper() in ("OPC", "OPC_UA"):
                label = f"ns=2;s={io_address}"
            else:
                label = io_address

            # 查 Tb_Device_Profile 確認是否存在
            profile_exists = derived_profile in profiles
            # tabname from Tb_Device_Profile.description
            tabname = profiles.get(derived_profile, {}).get("description", "")

            results.append({
                "id": row.get("id"),
                "tag_name": tag_name,
                "tb_name": tag_name,
                "tb_type": derived_profile,
                "tb_label": label,
                "tb_description": row.get("description", ""),
                "profile_exists": profile_exists,
                "tabname": tabname,
                "driver_type": driver_type,
                "nodename": nodename,
                "site_prefix": site_prefix,
                "floor": floor,
                "system": system,
                "source": "csv" if csv_profile else "derived",
            })

        return results

    def derive_kw_fields(self, staging_rows: list) -> list:
        """對暫存表資料推導 Kepware 建點所需欄位

        從 tb_type (DeviceProfile) 拆解 Kepware 路徑：
        tb_type "K8CHS-CHS-2F-CHS" → channel=K8CHS, device=CHS, tag_groups=2F/CHS
        """
        devices = {d["device_name"]: d for d in self.get_devices()}
        profiles = {p["name"]: p for p in self.get_tb_profiles()}
        locations = self.get_locations()

        results = []
        for row in staging_rows:
            tag_name = row.get("tag_name", "")
            if not tag_name:
                continue
            parts = tag_name.split("_") if tag_name else []

            site_prefix = parts[0] if len(parts) > 0 else ""
            floor = parts[1] if len(parts) > 1 else ""
            if floor.upper() == "BF":
                floor = "B1F"
            system = (row.get("system_code") or "").strip() or (parts[2] if len(parts) > 2 else "")

            io_device = row.get("io_device", "")
            driver_type = _resolve_driver_type(io_device, devices)

            scada_node_name = (row.get("scada_node_name") or "").strip()
            site_val = (row.get("site") or "").strip()
            system_code_val = (row.get("system_code") or "").strip()
            if scada_node_name:
                nodename = scada_node_name.replace("_", "")
            elif site_val or system_code_val:
                nodename = site_val + system_code_val
            else:
                nodename = site_prefix + system

            csv_profile = (row.get("device_profile") or "").strip()
            if csv_profile:
                tb_type = csv_profile
            elif driver_type.upper() == "IFIX":
                matched_loc = next((l for l in locations if l["site"] == site_val), None)
                zone = matched_loc["zone"] if matched_loc else ""
                tb_type = f"{nodename}-{zone}-{site_prefix}-{system}"
            else:
                tb_type = f"{nodename}-{system}-{floor}-{system}"

            # 從 tb_type 拆 Kepware 路徑
            tp = tb_type.split("-")
            channel_name = tp[0] if len(tp) > 0 else ""
            device_name = tp[1] if len(tp) > 1 else ""
            tag_groups = ".".join(tp[2:]) if len(tp) > 2 else ""

            profile_exists = tb_type in profiles

            # address 前綴：OPC 類加 ns=2;s=
            io_address = row.get("io_address", "")
            if driver_type.upper() in ("OPC", "OPC_UA"):
                address = f"ns=2;s={io_address}" if io_address else ""
            else:
                address = io_address

            results.append({
                "id": row.get("id"),
                "tag_name": tag_name,
                "tb_type": tb_type,
                "channel_name": channel_name,
                "device_name": device_name,
                "tag_groups": tag_groups,
                "address": address,
                "description": row.get("description", ""),
                "driver_type": driver_type,
                "profile_exists": profile_exists,
            })

        return results

    def derive_pg_fields(self, staging_rows: list) -> list:
        """對暫存表資料推導 PG 正式表所需欄位"""
        locations = self.get_locations()
        ownerships = self.get_ownerships()
        devices = {d["device_name"]: d for d in self.get_devices()}
        profiles = {p["name"]: p for p in self.get_tb_profiles()}

        # 建立 owner → department 快取
        owner_dept = {}
        for o in ownerships:
            owner_dept[o["data_owner"]] = o["department"]

        results = []
        for row in staging_rows:
            tag_name = row.get("tag_name", "")
            parts = tag_name.split("_") if tag_name else []

            site_prefix = parts[0] if len(parts) > 0 else ""
            floor = parts[1] if len(parts) > 1 else ""
            if floor.upper() == "BF":
                floor = "B1F"
            # system 優先用 CSV 的 system_code，fallback 才從 tag_name 拆
            system = (row.get("system_code") or "").strip() or (parts[2] if len(parts) > 2 else "")

            # 查 location (site → bu, zone)
            site = row.get("site", "")
            matched_loc = None
            for loc in locations:
                if loc["site"] == site:
                    matched_loc = loc
                    break
            bu = matched_loc["bu"] if matched_loc else ""
            zone = matched_loc["zone"] if matched_loc else ""

            # 查 driver_type: 先查 device_config，查不到就用 io_device 值本身判斷
            io_device = row.get("io_device", "")
            driver_type = _resolve_driver_type(io_device, devices)

            # 查 department
            data_owner = row.get("data_owner", "")
            department = owner_dept.get(data_owner, "")

            # 推導 nodename: 優先用 scada_node_name 去掉 _，否則用 site + system_code
            scada_node_name = (row.get("scada_node_name") or "").strip()
            site_val = (row.get("site") or "").strip()
            system_code_val = (row.get("system_code") or "").strip()
            if scada_node_name:
                nodename = scada_node_name.replace("_", "")
            elif site_val or system_code_val:
                nodename = site_val + system_code_val
            else:
                nodename = site_prefix + system

            # tabname: 先查 Tb_Device_Profile，查不到就用規則推導
            csv_profile = (row.get("device_profile") or "").strip()
            if csv_profile and csv_profile in profiles:
                tabname = profiles[csv_profile].get("description", "")
            elif driver_type.upper() == "IFIX":
                # PLC-IFIX: dp_name = nodename-zone-site-system
                dp_name = f"{nodename}-{zone}-{site_prefix}-{system}"
                if dp_name in profiles:
                    tabname = profiles[dp_name].get("description", "")
                else:
                    tabname = f"{zone}_{site_prefix}_{system}" if zone else ""
            else:
                dp_name = f"{nodename}-{system}-{floor}-{system}"
                if dp_name in profiles:
                    tabname = profiles[dp_name].get("description", "")
                else:
                    tabname = f"{bu}_{site_prefix}_{system}" if bu else ""

            # Collector 用欄位：address 加前綴
            io_address = row.get("io_address", "")
            if driver_type.upper() in ("OPC", "OPC_UA"):
                tag_address = f"ns=2;s={io_address}" if io_address else ""
            else:
                tag_address = io_address

            results.append({
                "id": row.get("id"),
                "tagname": tag_name,
                "description": row.get("description", ""),
                "node_name": row.get("scada_node_name", ""),
                "driver_type": driver_type,
                "address": row.get("io_address", ""),
                "tabname": tabname,
                "zone": zone,
                "bu": bu,
                "site": site,
                "floor": floor,
                "system": system,
                "owner": data_owner,
                "department": department,
                "data_type": "float",
                "scan_group": row.get("scan_group", ""),
                "tag_address": tag_address,
            })

        return results

    def derive_scale_fields(self, staging_rows: list) -> list:
        """對暫存表資料推導 Kepware Tag Scaling 與 Data Type 設定

        所有 tag 都會處理：
        - scale_enabled=YES → scaling_type=1 (Linear)，帶 raw/scaled 值
        - scale_enabled=NO  → scaling_type=0 (None)，data_type=8 (Float)

        從 tb_type (DeviceProfile) 拆解 Kepware 路徑：
        tb_type "K8CHS-CHS-2F-CHS" → channel=K8CHS, device=CHS, tag_group=2F.CHS
        tag_name 送出格式: "{tag_group}.{tag_name}"（API 自動拆分 group）
        """
        devices = {d["device_name"]: d for d in self.get_devices()}
        locations = self.get_locations()
        results = []
        for row in staging_rows:
            tag_name = row.get("tag_name", "")
            if not tag_name:
                continue

            # ── 推導 tb_type（與 derive_tb_fields 同邏輯）──
            parts = tag_name.split("_") if tag_name else []
            site_prefix = parts[0] if len(parts) > 0 else ""
            floor = parts[1] if len(parts) > 1 else ""
            if floor.upper() == "BF":
                floor = "B1F"
            system = (row.get("system_code") or "").strip() or (parts[2] if len(parts) > 2 else "")

            io_device = row.get("io_device", "")
            driver_type = _resolve_driver_type(io_device, devices)

            scada_node_name = (row.get("scada_node_name") or "").strip()
            site_val = (row.get("site") or "").strip()
            system_code_val = (row.get("system_code") or "").strip()
            if scada_node_name:
                nodename = scada_node_name.replace("_", "")
            elif site_val or system_code_val:
                nodename = site_val + system_code_val
            else:
                nodename = site_prefix + system

            csv_profile = (row.get("device_profile") or "").strip()
            if csv_profile:
                tb_type = csv_profile
            elif driver_type.upper() == "IFIX":
                matched_loc = next((l for l in locations if l["site"] == site_val), None)
                zone = matched_loc["zone"] if matched_loc else ""
                tb_type = f"{nodename}-{zone}-{site_prefix}-{system}"
            else:
                tb_type = f"{nodename}-{system}-{floor}-{system}"

            # ── 從 tb_type 拆 Kepware 路徑 ──
            tp = tb_type.split("-")
            channel_name = tp[0] if len(tp) > 0 else ""
            device_name = tp[1] if len(tp) > 1 else ""
            # 剩餘段作為 tag_group，用 "." 接
            tag_groups = ".".join(tp[2:]) if len(tp) > 2 else ""
            # 完整 tag_name = tag_group.tag_name（API 自動拆分 group/tag）
            full_tag_name = f"{tag_groups}.{tag_name}" if tag_groups else tag_name

            # ── Scale 判斷 ──
            scale_enabled = row.get("scale_enabled")
            if scale_enabled:
                raw_low = row.get("raw_low")
                raw_high = row.get("raw_high")
                scaled_low = row.get("scaled_low")
                scaled_high = row.get("scaled_high")
                # 有完整範圍才設 Linear，否則 fallback 為 None
                if raw_low is not None and raw_high is not None and scaled_low is not None and scaled_high is not None:
                    scaling_type = 1  # Linear
                else:
                    scaling_type = 0
            else:
                scaling_type = 0
                raw_low = raw_high = scaled_low = scaled_high = None

            entry = {
                "id": row.get("id"),
                "tag_name": tag_name,
                "tb_type": tb_type,
                "channel_name": channel_name,
                "device_name": device_name,
                "full_tag_name": full_tag_name,
                "scale_enabled": bool(scale_enabled),
                "scaling_type": scaling_type,
                "data_type": 8,  # Float
            }
            if scaling_type == 1:
                entry.update({
                    "scaling_raw_low": float(raw_low),
                    "scaling_raw_high": float(raw_high),
                    "scaling_scaled_low": float(scaled_low),
                    "scaling_scaled_high": float(scaled_high),
                    "scaling_clamp_low": False,
                    "scaling_clamp_high": False,
                    "scaling_scaled_data_type": 8,  # Float
                })

            results.append(entry)

        return results


# ── 工具函式 ──────────────────────────────────────────

def _resolve_driver_type(io_device: str, devices_cache: dict) -> str:
    """解析 driver_type：先查 device_config 表，查不到就用 io_device 值本身判斷"""
    device_info = devices_cache.get(io_device, {})
    driver_type = device_info.get("driver_type", "")
    if driver_type:
        return driver_type
    # Fallback: 直接用 io_device 值判斷
    val = io_device.upper()
    if "OPC_UA" in val or "OPCUA" in val:
        return "OPC_UA"
    if "OPC" in val or "IGS" in val:
        return "OPC"
    if "IFIX" in val:
        return "IFIX"
    return io_device


def _get(row: dict, *keys) -> str:
    """嘗試多個 key 取值"""
    for k in keys:
        val = row.get(k)
        if val is not None:
            return str(val).strip()
    return ""


def _parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("true", "1", "yes", "on", "enabled")


def _parse_num(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
