# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kepware point management system integrating ThingsBoard API + PostgreSQL + Kepware API Gateway. A FastAPI backend serves a Vue 3 SPA frontend (no build step) for batch device management and a 5-step Kepware import flow: CSV Upload → Staging → TB Device Creation → PG Formal Import → Kepware Scale Configuration.

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
Browser (Vue 3 SPA, static/index.html)
    ↕ HTTP / SSE
FastAPI Backend (app/main.py, port 9000)
    ↕                  ↕                    ↕
ThingsBoard CE     PostgreSQL           Kepware API GW
(REST API)         (staging + formal)   (Scale config)
```

**Backend modules:**

| Module | Role |
|--------|------|
| `app/main.py` | FastAPI routes, request models, CSV parsing, SSE streaming, task orchestration |
| `app/pg_client.py` | PostgreSQL operations: staging/formal table CRUD, reference table CRUD, field derivation logic (`derive_tb_fields`, `derive_pg_fields`, `derive_scale_fields`) |
| `app/tb_client.py` | ThingsBoard REST API wrapper (login, device CRUD, DeviceProfile) |
| `app/kw_gw_client.py` | Kepware API Gateway wrapper (Bearer auth, `PUT /api/config/tags`) |
| `app/task_manager.py` | Background task execution with SSE progress, batch throttling, 429 retry |
| `app/config_manager.py` | `data/config.json` CRUD for dropdown options, defaults, mapping rules |

**Frontend:** Single-file Vue 3 Composition API app in `static/index.html` (~2100 lines). Vue 3.5.13 loaded locally from `static/vue.global.prod.js`. No build system.

## Key Data Flow

### Two independent configuration systems

1. **`data/config.json`** (local file) — dropdown options, defaults, mapping rules for the "TB Direct Add" manual form. Not used by Kepware import.
2. **PG reference tables** (`location_config`, `ownership_config`, `device_config`, `system_config`, `tb_device_profile`) — used by Kepware import Steps 3-5 for field derivation. Managed via Settings > PG Reference Tables.

### Kepware import derivation (pg_client.py)

- **nodename**: `scada_node_name` (remove `_`) → fallback `site` + `system_code`; append `IFIX` suffix if driver_type is IFIX
- **system**: CSV `system_code` → fallback `tag_name.split('_')[2]`
- **floor**: `tag_name.split('_')[1]`
- **driver_type**: lookup `device_config` table → heuristic (OPC/IGS→OPC)
- **DeviceProfile (tb_type)**: CSV `device_profile` → fallback `{nodename}-{system}-{floor}-{system}`
- **Scale path**: tb_type `K8CHS-CHS-2F-CHS` decomposes to channel=`K8CHS`, device=`CHS`, groups=`2F.CHS`

### Status tracking

Staging table (`scada_tag_config`) tracks three status columns: `tb_status`, `pg_status`, `scale_status` (pending/done/skip).

## Database Tables

- **`scada_tag_config`** — staging table for CSV imports (auto-created on PG connect)
- **`tags`** — formal table with columns: `tag_id`, `tagname`, `description`, `node_name`, `driver_type`, `address`, `tablename`, `zone`, `bu`, `site`, `floor`, `system`, `owner`, `department`, `data_type`, `created_date`, `updated_date`
- **`*_config` tables** — reference/lookup tables (auto-created via `_ensure_ref_tables`)

## Conventions

- **Language**: Backend comments and UI text in Traditional Chinese
- **API endpoints**: kebab-case (`/api/pg/ref/tb-profiles/sync`)
- **Python**: snake_case functions, PascalCase classes
- **DB tables**: lowercase_underscore
- **CSV encoding**: auto-detects UTF-8-BOM, UTF-8, Big5, GB2312 (tries in order)
- **File-based persistence**: CSV uploads stored as `data/csv_uploads/{upload_id}.json` to survive server restarts
- **Error pattern**: `except Exception as e: log.error(...); raise HTTPException(status_code, detail=str(e))`
- **Throttling pattern**: per-item `delay` + `batch_size` + `batch_pause` + 429 auto-retry (matches `task_manager.py` pattern)
- **Frontend caching**: `index.html` served with `Cache-Control: no-cache, no-store, must-revalidate` headers to prevent stale JS
