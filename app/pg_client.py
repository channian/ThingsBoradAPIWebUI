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

    def test_connection(self) -> dict:
        """測試 PG 連線"""
        with self._get_conn() as conn:
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
                         description, project_name, data_owner, device_profile)
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
        if field not in ("tb_status", "pg_status"):
            raise ValueError("field must be tb_status or pg_status")
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

    # ── Tag 推導邏輯 ──────────────────────────────────

    def derive_tb_fields(self, staging_rows: list) -> list:
        """對暫存表資料推導 TB 建點所需欄位"""
        # 載入參照表做快取
        devices = {d["device_name"]: d for d in self.get_devices()}
        profiles = {p["name"]: p for p in self.get_tb_profiles()}

        results = []
        for row in staging_rows:
            tag_name = row.get("tag_name", "")
            parts = tag_name.split("_") if tag_name else []

            site_prefix = parts[0] if len(parts) > 0 else ""
            floor = parts[1] if len(parts) > 1 else ""
            system = parts[2] if len(parts) > 2 else ""

            # 查 driver_type: 先查 device_config，查不到就用 io_device 值本身判斷
            io_device = row.get("io_device", "")
            driver_type = _resolve_driver_type(io_device, devices)

            # 推導 nodename: 用 CSV 的 scada_node_name 去掉 _ (如 K3_CHS → K3CHS)
            scada_node_name = (row.get("scada_node_name") or "").strip()
            if scada_node_name:
                nodename = scada_node_name.replace("_", "")
            else:
                nodename = site_prefix + system
            # iFIX 加後綴
            if driver_type.upper() == "IFIX" and not nodename.upper().endswith("IFIX"):
                nodename = nodename + "IFIX"

            # DeviceProfile: CSV 有填就優先用
            csv_profile = (row.get("device_profile") or "").strip()
            if csv_profile:
                derived_profile = csv_profile
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
            system = parts[2] if len(parts) > 2 else ""

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

            # 推導 nodename: 用 CSV 的 scada_node_name 去掉 _
            scada_node_name = (row.get("scada_node_name") or "").strip()
            if scada_node_name:
                nodename = scada_node_name.replace("_", "")
            else:
                nodename = site_prefix + system
            if driver_type.upper() == "IFIX" and not nodename.upper().endswith("IFIX"):
                nodename = nodename + "IFIX"

            # tabname = BU_SITE_SYSTEM，可被 Tb_Device_Profile 覆寫
            csv_profile = (row.get("device_profile") or "").strip()
            if csv_profile and csv_profile in profiles:
                tabname = profiles[csv_profile].get("description", "")
            else:
                dp_name = f"{nodename}-{system}-{floor}-{system}"
                if dp_name in profiles:
                    tabname = profiles[dp_name].get("description", "")
                else:
                    tabname = f"{bu}_{site_prefix}_{system}" if bu else ""

            results.append({
                "id": row.get("id"),
                "tagname": tag_name,
                "node_name": row.get("scada_node_name", ""),
                "driver_type": driver_type,
                "address": row.get("io_address", ""),
                "zone": zone,
                "bu": bu,
                "site": site,
                "floor": floor,
                "system": system,
                "tabname": tabname,
                "owner": data_owner,
                "department": department,
                "data_type": "float",
                "description": row.get("description", ""),
                "scale_enabled": row.get("scale_enabled", False),
                "raw_low": row.get("raw_low"),
                "raw_high": row.get("raw_high"),
                "scaled_low": row.get("scaled_low"),
                "scaled_high": row.get("scaled_high"),
            })

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
    if "OPC" in val:
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
