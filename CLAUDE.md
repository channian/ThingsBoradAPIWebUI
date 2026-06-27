# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Pure-Kepware** point management system integrating PostgreSQL + Kepware API Gateway + Collector. A FastAPI backend serves a Vue 3 SPA frontend (no build step) for a 6-step Kepware import flow plus batch delete, DB query, and IO mapping.

> **This project no longer integrates with ThingsBoard.** It was migrated from a "ThingsBoard + Kepware hybrid" to pure Kepware. Several identifiers still carry a `tb_` / `tb-` prefix from that era but now refer to **Kepware** concepts — see **Legacy naming glossary** below. There is no TB runtime code, client, env var, URL, or token left (`app/tb_client.py` was removed).

The 6-step import flow: **CSV Upload → Staging → Kepware 建點 (Tag Creation) → PG Formal Import (+ Collector DB) → Kepware Scale Configuration → Collector Reload.**

## Running the Project

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py               # Starts on http://localhost:9000 with hot-reload
```

Windows shortcut: `start.bat` (auto-creates venv + installs deps).

No test suite exists. Manual testing via the web UI.

## Architecture

```
Browser (Vue 3 SPA: static/index.html templates + static/js/app.js logic)
    ↕ HTTP
FastAPI Backend (app/main.py, port 9000)
    ↕                       ↕                          ↕
PostgreSQL              Kepware API Gateway        Collector
(staging + formal       (REST: tag/group CRUD,     (Collector DB `tags`
 + reference tables)     structure, scaling)        write + Reload webhook)
```

**Backend modules:**

| Module | Role |
|--------|------|
| `app/main.py` | FastAPI routes, request models, CSV parsing, Fernet password crypto, task orchestration |
| `app/pg_client.py` | All PostgreSQL ops: staging/formal/reference/gateway/structure CRUD, and field derivation (module-level `_derive_base_fields` shared by methods `derive_kw_fields`, `derive_pg_fields`, `derive_scale_fields`) |
| `app/kw_gw_client.py` | Kepware API Gateway wrapper (Bearer auth; channel/device/group listing, tag-group + tag CRUD, `PUT /api/config/tags` for scaling) |
| `app/task_manager.py` | Background task execution (used by Kepware batch delete) with progress tracking, batch throttling, 429 retry. Tasks capped at 100 (oldest-completed evicted). |
| `app/config_manager.py` | `data/config.json` CRUD for dropdown options/defaults/mapping rules — **legacy, not used by the current UI or the Kepware import** (see Key Data Flow). |
| `app/auth.py` | JWT auth, PBKDF2-SHA256 passwords, role checks (admin/operator/user). |

**Frontend:** Vue 3 Composition API, **split into two files** (no build system):
- `static/index.html` (~900 lines) — all templates (login, top bar, 6-step stepper, delete/query/iomap/history/settings tabs, modals, chat widget).
- `static/js/app.js` (~780 lines) — all reactive state + API logic in one `setup()`. Plain `<script>` (no ES modules).
- Vue 3.5.13 loaded locally from `static/vue.global.prod.js`. v3 design system in `static/css/v3.css`.

## Legacy naming glossary (TB-era names that now mean Kepware)

These are **not bugs** — they are the main source of confusion when comparing against the old TB-bound version. All are safe; renaming is optional and risky (DB columns/tables).

| Identifier | Where | What it means NOW |
|-----------|-------|-------------------|
| `tb_status` (staging column) | `scada_tag_config`, main.py, app.js | **Step 3 Kepware 建點 status** (pending/done/skip) |
| `tb_type` (derived field/var) | pg_client derivation | **Kepware DeviceProfile string** e.g. `K8CHS-CHS-2F-CHS`, decomposed into channel/device/tag_groups |
| `tb_device_profile` (table) | PG reference tables | **Kepware DeviceProfile** lookup table (its `description` → `tablename`) |
| `/api/pg/ref/tb-profiles`, `get_tb_profiles`, `RefTbProfileRequest`, UI "TB Profile" | main.py, app.js | CRUD for the Kepware DeviceProfile reference table |
| `device_profile` (CSV column) | CSV input | Genuine input field — the CSV's DeviceProfile value (Kepware concept) |

## Key Data Flow

### Two independent configuration systems

1. **`data/config.json`** (local file, via `config_manager.py`) — dropdown options/defaults/mapping rules. **Legacy**: it fed an old manual "direct add" form that no longer exists in the v3 UI. The `/api/config/*` endpoints still exist but the frontend does not call them.
2. **PG reference tables** (`location_config`, `ownership_config`, `device_config`, `system_config`, `tb_device_profile`) — **this is what the Kepware import uses** for field derivation (Steps 3-5). Managed via Settings > PG Reference Tables.

### Kepware import derivation (pg_client.py — `_derive_base_fields`, ~line 1112)

`tag_name` is split on `_` into `parts`; many fields index into it. All rules below include the actual fallbacks:

- **nodename**: `scada_node_name` with all `_` removed → else `site + system_code` (concatenated, no separator) → else `parts[0] + system`. *(No IFIX suffix — IFIX only affects `tb_type` below.)*
- **system**: CSV `system_code` → else `parts[2]` (3rd `_` segment).
- **floor**: `parts[1]` (2nd `_` segment); then normalized **`BF` → `B1F`**.
- **driver_type**: from CSV **`io_device`** via `device_config` lookup → heuristic on the value: `OPC_UA/OPCUA→OPC_UA`, `OPC/IGS→OPC`, `IFIX→IFIX`, else passthrough.
- **DeviceProfile (`tb_type`)** — three branches:
  1. CSV `device_profile` if present, else
  2. if driver_type is **IFIX** → `{nodename}-{zone}-{site_prefix}-{system}`, else
  3. `{nodename}-{system}-{floor}-{system}`.
- **channel / device / tag_groups**: split `tb_type` on `-` → `channel=tp[0]`, `device=tp[1]`, `tag_groups=".".join(tp[2:])`. E.g. `K8CHS-CHS-2F-CHS` → channel `K8CHS`, device `CHS`, groups `2F.CHS`.
- **address**: OPC/OPC_UA → `ns=2;s={io_address}`; otherwise raw `io_address`.
- **location match** (`location_config` by `site`) → `zone`, `bu`; **ownership** (`ownership_config` by `data_owner`) → `department`.
- **tablename/tabname**: `tb_device_profile.description` for the matched profile → else IFIX `{zone}_{site_prefix}_{system}` / non-IFIX `{bu}_{site_prefix}_{system}`.

See **`docs/FLOW_kepware_import.md`** for the full line-referenced spec (data flow, every derivation rule, matching rules, validation, throttling).

### Status tracking

Staging table `scada_tag_config` tracks three status columns, each `pending`/`done`/`skip`:
- `tb_status` → Step 3 Kepware 建點; set `done` after tag creation.
- `pg_status` → Step 4 PG formal import; set `done` after write.
- `scale_status` → Step 5 Kepware scale config; set `done` after scaling.

### Multi-Gateway + structure validation

- Gateways are DB-managed (`kepware_gateway` table, free naming, optional `zone`, one `is_default`). Passwords are **Fernet-encrypted** (`KW_ENCRYPT_KEY`); a key change yields a clear 400 ("re-enter password"), not a 500.
- Step 3 can **sync structure** from a gateway (`/api/kw/gateways/{id}/sync`) into the `kepware_structure` cache, then derive validates each row: **✓** path exists / **+G** group auto-created on execute / **▲** channel or device missing (not auto-created).

## Database Tables

- **`scada_tag_config`** — staging table for CSV imports. **Must pre-exist in the DB** (the code does NOT `CREATE` it; it only `ALTER`s to add `scale_status` and `scan_group` if missing, and relies on a UNIQUE constraint on `tag_name`).
- **`tags`** — formal table, auto-created via `_ensure_formal_table`. Columns: `tag_id`, `tagname` (UNIQUE), `description`, `node_name`, `driver_type`, `address`, `tablename`, `zone`, `bu`, `site`, `floor`, `system`, `owner`, `department`, `data_type`, `created_date`, `updated_date`. *(No `project_name` / building column — `project_name` lives only in staging.)*
- **Collector `tags`** — separate Collector DB (same host, DB name from `COLLECTOR_DB_DATABASE`), auto-created via `_ensure_collector_tags_table`: `tagname`, `tag_address`, `scan_group`, `unit`, `description`, `enable`, `target_table`.
- **`*_config` / `tb_device_profile` / `kepware_gateway` / `kepware_structure`** — auto-created via `_ensure_ref_tables`.

## Conventions

- **Language**: Backend comments and UI text in Traditional Chinese.
- **API endpoints**: kebab-case (e.g. `/api/kw/gateways/{id}/sync`, `/api/pg/ref/tb-profiles`).
- **Python**: snake_case functions, PascalCase classes. **DB tables**: lowercase_underscore.
- **CSV encoding**: auto-detects UTF-8-BOM → UTF-8 → Big5 → GB2312 (tries in order). Headers accept snake_case OR Excel-style names (e.g. `tag_name` or `Tag Name`).
- **File-based persistence**: CSV uploads stored as `data/csv_uploads/{upload_id}.json` (survive restarts); reused by both staging import and Kepware batch delete.
- **Error pattern**: `except Exception as e: log.error(...); raise HTTPException(status_code, detail=str(e))`. When a route may raise its own `HTTPException` (e.g. via `_decrypt_gw_password` or `_resolve_gw_credentials`), add `except HTTPException: raise` before the generic handler so 4xx isn't masked as 500.
- **Throttling pattern**: per-item `delay` + `batch_size` + `batch_pause` + 429 auto-retry (Steps 3 & 5, and `task_manager.py`).
- **Gateway credentials**: resolve via `_resolve_gw_credentials(gateway_id | manual url/user/pass)`; decrypt via `_decrypt_gw_password`.
- **Static caching**: all `/static/` files served with `Cache-Control: no-cache, no-store, must-revalidate` (middleware) to prevent stale CSS/JS.
