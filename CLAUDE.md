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

**Tests** (pytest):
```bash
pip install -r requirements-dev.txt
pytest                        # from repo root
```
DB-backed tests need a PostgreSQL role with `CREATEDB`, configured via `KIS_TEST_PG_HOST` / `KIS_TEST_PG_PORT` / `KIS_TEST_PG_USER` / `KIS_TEST_PG_PASSWORD` (default `localhost:5432`, role/db `kis_test`/`kis_test_pw`). They only ever create/drop the `kis_pytest` and `kis_pytest_collector` databases, with a guard that refuses to touch any other database; if PostgreSQL is unreachable, DB tests auto-skip and pure-logic tests still run. There is no real Kepware to test against: Step 3/5/batch-delete execution is tested end-to-end with a fake client (`tests/fake_kepware.py`, monkeypatched over `KepwareGatewayClient`), so manual verification via the web UI is still recommended alongside pytest. When asserting the Gateway lock is released after a task, use `wait_lock_released()` — the lock is freed in `finally` *after* `push_complete` sets `done=True`, so an immediate assert is flaky.

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
| `app/task_manager.py` | Background task execution — used by all three long-running endpoints: `/api/kw/execute` (Step 3), `/api/pg/execute/scale` (Step 5), and `/api/kw/delete-batch`. Each returns a `task_id` immediately; callers poll `GET /api/tasks/{task_id}` for progress/result. Progress tracking, batch throttling, 429 retry. Tasks capped at 100 (oldest-completed evicted). |
| `app/config_manager.py` | `data/config.json` CRUD for dropdown options/defaults/mapping rules — **legacy, not used by the current UI or the Kepware import** (see Key Data Flow). |
| `app/auth.py` | JWT auth, PBKDF2-SHA256 passwords, role checks (admin/operator/user). |

**Frontend:** Vue 3 Composition API, **split into two files** (no build system):
- `static/index.html` (~1140 lines) — all templates (login, top bar, 6-step stepper, delete/query/iomap/history/settings tabs, modals, chat widget).
- `static/js/app.js` (~1050 lines) — all reactive state + API logic in one `setup()`. Plain `<script>` (no ES modules).
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

### Kepware import derivation (pg_client.py — `_derive_base_fields`)

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

**Step 3 manual override columns** (`scada_tag_config.kw_channel` / `kw_device` / `kw_tag_groups` / `kw_data_type`): when set on a staging row, both `derive_kw_fields` (Step 3) and `derive_scale_fields` (Step 5) use them in place of the derived channel/device/tag_groups/data_type, so the two steps stay consistent for a given tag.

**`data_type`**: Kepware tag data type defaults to `8` (Float, `KW_DATA_TYPE_DEFAULT` in `pg_client.py`), overridable per-row via `kw_data_type`; `scaling_scaled_data_type` sent for Step 5 scaling is fixed at `8`. See `API_SCHEMA.md` for the full data-type enum.

See **`docs/FLOW_kepware_import.md`** for the full line-referenced spec (data flow, every derivation rule, matching rules, validation, throttling).

### Status tracking

Staging table `scada_tag_config` tracks three status columns, each `pending`/`done`/`skip`:
- `tb_status` → Step 3 Kepware 建點; set `done` after tag creation.
- `pg_status` → Step 4 PG formal import; set `done` after write.
- `scale_status` → Step 5 Kepware scale config; set `done` after scaling.

### Multi-Gateway + structure validation

- Gateways are DB-managed (`kepware_gateway` table, free naming, optional `zone`, one `is_default`). Passwords are **Fernet-encrypted** (`KW_ENCRYPT_KEY`); a key change yields a clear 400 ("re-enter password"), not a 500.
- Step 3 can **sync structure** from a gateway (`/api/kw/gateways/{id}/sync`) into the `kepware_structure` cache, then derive validates each row: **✓** path exists / **+G** group auto-created on execute / **▲** channel or device missing (not auto-created).
- **Gateway concurrency lock**: `/api/kw/execute`, `/api/pg/execute/scale`, and `/api/kw/delete-batch` each acquire an in-process lock keyed by gateway (`_acquire_gateway_lock`/`_release_gateway_lock` in `main.py`) before starting their background task; a second task against the same gateway gets `409` (with the busy `task_id`) while other gateways run in parallel. Each background function's entire body is a single `try`/`finally` that releases the lock — **when editing these functions, keep `try:` as the function's only top-level statement**, or a code path that skips the `finally` will leave the gateway permanently locked.
- **Rate limiting**: Kepware API Gateway enforces a sliding-window limit (~60 calls/minute). Step 3/5 default `delay=1.1s`, `batch_size=50`, `batch_pause=5s`; on `429` the client backs off using the `Retry-After` header and retries up to 3 times (`_kw_call_with_429_retry`).
- **Scale behavior (Step 5)**: rows with `scale_enabled=false` are skipped without calling Kepware (marked `skip`); rows with `scale_enabled=true` but incomplete range values (`raw_low`/`raw_high`/`scaled_low`/`scaled_high`) are recorded as `scale_error` failures.

## Database Tables

- **`scada_tag_config`** — staging table for CSV imports. **Must pre-exist in the DB** (the code does NOT `CREATE` it; it only `ALTER`s to add missing columns — `scale_status`, `scan_group`, and the Step 3 override columns `kw_channel`/`kw_device`/`kw_tag_groups`/`kw_data_type` — via `_ensure_extra_columns`, and relies on a UNIQUE constraint on `tag_name`).
- **`tags`** — formal table, auto-created via `_ensure_formal_table`. Columns: `tag_id`, `tagname` (UNIQUE), `description`, `node_name`, `driver_type`, `address`, `tablename`, `zone`, `bu`, `site`, `floor`, `system`, `owner`, `department`, `data_type`, `created_date`, `updated_date`. *(No `project_name` / building column — `project_name` lives only in staging.)*
- **Collector `tags`** — separate Collector DB (same host, DB name from `COLLECTOR_DB_DATABASE`), auto-created via `_ensure_collector_tags_table`: `tagname`, `tag_address`, `scan_group`, `unit`, `description`, `enable`, `target_table`. The `enable` column's type (boolean vs. smallint) is detected at write time since an externally pre-created Collector DB may use either, and the value is coerced accordingly.
- **`*_config` / `tb_device_profile` / `kepware_gateway` / `kepware_structure`** — auto-created via `_ensure_ref_tables`.

All of the above schema migrations run via `PGClient.ensure_schema()` (calls `_ensure_ref_tables` + `_ensure_extra_columns`), which is invoked both on app startup (`on_startup` in `main.py`) and on every `GET /api/pg/test` call.

## Conventions

- **Language**: Backend comments and UI text in Traditional Chinese.
- **API endpoints**: kebab-case (e.g. `/api/kw/gateways/{id}/sync`, `/api/pg/ref/tb-profiles`).
- **Python**: snake_case functions, PascalCase classes. **DB tables**: lowercase_underscore.
- **CSV encoding**: auto-detects UTF-8-BOM → UTF-8 → Big5 → GB2312 (tries in order). Headers accept snake_case OR Excel-style names (e.g. `tag_name` or `Tag Name`), matched case-insensitively (`_row_get_ci` in `pg_client.py`, stripped + lower-cased) so header case variants (e.g. `I/O Address` vs `I/O ADDRESS`) aren't silently dropped.
- **File-based persistence**: CSV uploads stored as `data/csv_uploads/{upload_id}.json` (survive restarts); reused by both staging import and Kepware batch delete.
- **Error pattern**: `except Exception as e: log.error(...); raise HTTPException(status_code, detail=str(e))`. When a route may raise its own `HTTPException` (e.g. via `_decrypt_gw_password` or `_resolve_gw_credentials`), add `except HTTPException: raise` before the generic handler so 4xx isn't masked as 500.
- **Throttling pattern**: per-item `delay` (default `1.1s`) + `batch_size` (default `50`) + `batch_pause` (default `5s`) + 429 auto-retry (Steps 3 & 5, and `task_manager.py`).
- **Gateway credentials**: resolve via `_resolve_gw_credentials(gateway_id | manual url/user/pass)`; decrypt via `_decrypt_gw_password`.
- **Static caching**: all `/static/` files served with `Cache-Control: no-cache, no-store, must-revalidate` (middleware) to prevent stale CSS/JS.
- **Background task endpoints**: an HTTP `200` from `/api/kw/execute`, `/api/pg/execute/scale`, or `/api/kw/delete-batch` only means the background task was *created*, not that it finished — the response is `{"task_id": ...}` and the caller must poll `GET /api/tasks/{task_id}` for actual progress/result. Any docs or integration examples for these endpoints must show the polling loop, not just the initial call.
