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
DB-backed tests need a PostgreSQL role with `CREATEDB`, configured via `KIS_TEST_PG_HOST` / `KIS_TEST_PG_PORT` / `KIS_TEST_PG_USER` / `KIS_TEST_PG_PASSWORD` (default `localhost:5432`, role/db `kis_test`/`kis_test_pw`). They only ever create/drop the `kis_pytest` and `kis_pytest_collector` databases, with a guard that refuses to touch any other database; if PostgreSQL is unreachable, DB tests auto-skip and pure-logic tests still run. There is no real Kepware to test against: Step 3/5/batch-delete execution is tested end-to-end with a fake client (`tests/fake_kepware.py`, monkeypatched over `KepwareGatewayClient`), so manual verification via the web UI is still recommended alongside pytest. To make a task "running" without sleeping, block a fake call with a `threading.Event` (`state.add_gate("create_tag", tag)` → `gate.wait_entered()` … `gate.open()`); `make_fake_kepware_client({url: state, ...})` keeps one `FakeKepwareState` per Gateway URL. For data_type tests the fake also has `get_tag` / `set_tag_data_type` over a settable remote-tag table (`state.set_remote_tag(...)`, `put_applies_data_type` / `put_side_effects` / `put_hook` to simulate a PUT that misbehaves, `queue_get_tag_error(tag, None, err)` where `None` = let that call pass). `wait_lock_released()` now just checks `gateway_queue.snapshot()` is empty: each part releases its queue slot *before* the last one calls `push_complete`, so once `done=True` the queue is already empty (the short timeout only guards against hanging on a regression). The `api_client` fixture also redirects `HISTORY_FILE` to a temp dir and fails the test at teardown if the queue still holds jobs.

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
| `app/gateway_router.py` | **Pure-logic** (no DB / FastAPI) Gateway auto-routing: `resolve_gateway`, `group_by_gateway`, `build_channel_index`, `find_unmapped`, `localize_reason`. Callers load the data (channel index, route maps) and pass it in. |
| `app/gateway_queue.py` | **Pure-logic** per-Gateway FIFO job queue (`GatewayQueue`: `enqueue_many` / `wait_turn` / `release` / `wake` / `position` / `snapshot`) and `gateway_key` (`gw:<id>`, or normalized URL `url:<url>` for manual credentials). Queue head = running, the rest = waiting; cancellable. |
| `app/gateway_jobs.py` | **Pure-logic** multi-Gateway job runner `run_job(task, parts, worker, queue, blocked=...)`: one request = one `Task` (single `task_id`) = one `PartSpec`/thread per Gateway (different Gateways in parallel, same Gateway FIFO). Builds the summary (`gateways`/`blocked`/`cancelled`/`cancelled_remaining`, plus optional worker-supplied `details` list, concatenated per Gateway with `gateway` filled in) and calls `push_complete` exactly once. Workers must **not** call `push_complete`. |
| `app/safety.py` | **Pure-logic** safety limits from env vars: `check_batch_size(op, n)` (`KIS_LIMIT_KW_EXECUTE` / `KIS_LIMIT_SCALE` / `KIS_LIMIT_DELETE`, default 10000/10000/2000, `n > limit` rejected; op `"datatype"` reuses `KIS_LIMIT_KW_EXECUTE`) and per-Gateway queue depth (`KIS_LIMIT_QUEUE_PER_GATEWAY`, default 5). |
| `app/tag_datatype.py` | **Pure-logic** (stdlib only) Kepware `data_type` helpers: `DATA_TYPE_NAMES`/`data_type_label`, `extract_tag_data_type(payload)` (lenient but never guesses: ambiguous/unrecognized → `None`; **field names are inferred from docs, still unconfirmed against a real Gateway** — adjust `DATA_TYPE_KEYS`/`WRAPPER_KEYS` after `scripts/kw_probe_tag.py`), `describe_payload_keys`, `diff_tag_payloads` (what else changed after a PUT). |
| `app/kw_gw_client.py` | Kepware API Gateway wrapper (Bearer auth; channel/device/group listing, tag-group + tag CRUD, `get_tag` (read one tag), `PUT /api/config/tags` for scaling and `set_tag_data_type` (body = channel/device/tag/data_type[/tag_group] only)) |
| `app/task_manager.py` | Background task execution — used by all long-running endpoints: `/api/kw/execute` (Step 3), `/api/pg/execute/scale` (Step 5), `/api/kw/delete-batch`, and `/api/kw/datatype-check`. Each returns a `task_id` immediately; callers poll `GET /api/tasks/{task_id}` for progress/result. Progress tracking, batch throttling, 429 retry. Tasks capped at 100 (oldest-completed evicted). |
| `app/config_manager.py` | `data/config.json` CRUD for dropdown options/defaults/mapping rules — **legacy, not used by the current UI or the Kepware import** (see Key Data Flow). |
| `app/auth.py` | JWT auth, PBKDF2-SHA256 passwords, role checks (admin/operator/user). |

**Frontend:** Vue 3 Composition API, **split into two files** (no build system):
- `static/index.html` (~1400 lines) — all templates (login, top bar, 6-step stepper, delete/query/iomap/history/settings tabs, modals incl. the 作業佇列 queue panel, chat widget) plus the `#task-panel-tpl` x-template (placed *outside* `#app`).
- `static/js/app.js` (~1350 lines) — all reactive state + API logic in one `setup()`, plus one small presentational component `TaskPanel` (registered with `.component('task-panel', TaskPanel)`; it only renders a task-state object and emits `cancel`/`queue`/`resume`/`history`). Plain `<script>` (no ES modules).
- Vue 3.5.13 loaded locally from `static/vue.global.prod.js`. v3 design system in `static/css/v3.css`.
- **Gateway selection (Step 3 / Step 5 / Delete share `selectedGwId`)**: `null` = 自動 (default; request **omits** `gateway_id`, each row goes to its routed Gateway), a value = only that Gateway's rows (others are skipped, reported in `excluded`). Step 3's sync button is 「同步全部 Gateway」 in auto mode (sequential, a failing Gateway doesn't stop the rest). Derive preview tables show `route` (Gateway column + badge + summary chips, ✗ rows are never sent).
- **Background tasks (`_runGatewayTask` → `_startTask` + `_trackTask`)**: one task-state object per flow (`kwDerive.task` / `scaleDerive.task` / `delState.task`, built by `_newTask()`); polls `GET /api/tasks/{id}` every 1 s **until `done`** (no attempt cap; 404 → "任務已遺失"; 10 consecutive failures → `stalled` with a 「繼續追蹤」 button); `queued:true` shows the queue banner; cancel via `POST /api/tasks/{id}/cancel` (409 treated as normal); 422/429/400 show the backend `detail`. `executing` stays true from submit to done (queued counts). Settings has the 「Gateway 對應表」 card (`/api/kw/routes`, write = admin); `routeDataVersion` bumps after sync/route/gateway edits so already-derived previews show a "可能已過期" banner.

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

**Step 3 manual override columns** (`scada_tag_config.kw_channel` / `kw_device` / `kw_tag_groups` / `kw_data_type`): when set on a staging row, both `derive_kw_fields` (Step 3) and `derive_scale_fields` (Step 5) use them in place of the derived channel/device/tag_groups/data_type, so the two steps stay consistent for a given tag. `POST /api/pg/staging/kw-override` is a **partial update** (`model_fields_set`): fields not sent are left untouched; sent values keep their semantics (`channel`/`device` `""` → NULL, `tag_groups` `""` → empty string = device-root override, *not* NULL, `data_type` null/`""` → NULL); sending none of the four → 400.

**`data_type`**: Kepware tag data type defaults to `8` (Float, `KW_DATA_TYPE_DEFAULT` in `pg_client.py`), overridable per-row via `kw_data_type`; `scaling_scaled_data_type` sent for Step 5 scaling is fixed at `8`. See `API_SCHEMA.md` for the full data-type enum.

See **`docs/FLOW_kepware_import.md`** for the full line-referenced spec (data flow, every derivation rule, matching rules, validation, throttling).

### Status tracking

Staging table `scada_tag_config` tracks three status columns, each `pending`/`done`/`skip`:
- `tb_status` → Step 3 Kepware 建點; set `done` after tag creation.
- `pg_status` → Step 4 PG formal import; set `done` after write.
- `scale_status` → Step 5 Kepware scale config; set `done` after scaling.

### Multi-Gateway + structure validation

- Gateways are DB-managed (`kepware_gateway` table, free naming, optional `zone`, one `is_default`). Passwords are **Fernet-encrypted** (`KW_ENCRYPT_KEY`); a key change yields a clear 400 ("re-enter password"), not a 500.
- Step 3 can **sync structure** from a gateway (`/api/kw/gateways/{id}/sync`) into the `kepware_structure` cache, then derive validates each row: **✓** path exists / **+G** group auto-created on execute / **▲** channel or device missing (not auto-created). `/api/kw/derive` validates against the request's `gateway_id` when given (every row), otherwise (auto mode) each row against **its own `route.gateway_id`'s** structure (unroutable rows get no `validation`); each Gateway's structure is read once per request.
- **Gateway job queue (replaces the old 409 lock)**: `/api/kw/execute`, `/api/pg/execute/scale` and `/api/kw/delete-batch` share one request flow in `main.py` (`_plan_gateway_parts` → `_dispatch_gateway_job`), then `gateway_jobs.run_job` runs one thread per Gateway. **Same Gateway = FIFO queue, different Gateways = parallel**; a busy Gateway no longer returns 409 — the response is `200` with `queued: true` and a message ("已有任務進行中，已進入佇列"). The single `task_id` is kept; poll `GET /api/tasks/{id}` (adds `status`/`gateways[].state`/`position`). The queue lives in memory (`gateway_queue` in `main.py`): a restart loses queued/running jobs.
  - **Request order (do not reorder)**: batch limit (`422`) → derive rows (`400` if none) → pick mode + route + resolve credentials (`400/404`, **before any enqueue**) → `create_task` → **synchronous** `enqueue_many` with `queue_max_per_gateway` (`QueueFullError` → drop the task, `429`) → `run_job`. If anything raises between enqueue and `run_job`, release every key and drop the task, or the queue head is stuck forever. Credentials live in `PartSpec.payload`, never in `task.params`.
  - **Three modes**: *manual* (no `gateway_id`, has `kw_gw_url`: one part, no routing, queue key = normalized URL so `http://x:1/` and `http://X:1` share a queue) / *filter* (`gateway_id` given: only rows routed to that Gateway are processed; other-Gateway and unroutable rows are excluded — not sent, not counted in `total` — and reported in `excluded`) / *auto* (neither: route every row, group by Gateway). In auto mode **unroutable rows (`conflict`/`conflict_channel`/`unrouted`) are never sent**: they stay `pending`, and are reported as `blocked` (counted in `total`/`fail`/`errors`). Nothing routable at all → `400`. With only 1 Gateway all modes behave as before (`single`, no structure sync needed).
  - **Workers** (`_kw_execute_worker` / `_scale_worker` / `_kw_delete_worker`, called once per Gateway as `worker(view, part)`): re-check `tb_status`/`scale_status` via `PGClient.get_staging_statuses` when their turn comes and skip rows that are no longer `pending` (processed by an earlier queued task; counted in `skip`); login failure = every pending row of that Gateway is `fail`; poll `view.cancelled` between rows and use `_sleep_interruptible` for every wait (delay, batch pause, 429 back-off); succeeded rows are written back in a `finally` so cancel/exception never loses them. **A queued task runs on the derivation snapshot taken at submit time (it is not re-derived)** — if you change Step 3 overrides while a task is queued, cancel it and resubmit.
  - **Cancel**: `POST /api/tasks/{id}/cancel` (admin/operator) sets `task.cancelled`, then `gateway_queue.wake()`; queued parts leave the queue at once, running parts stop after the current row (`summary.cancelled_remaining`). Idempotent; `409` once finished. `GET /api/kw/queue` lists every Gateway's queue.
  - **Safety limits** (`app/safety.py`, env `KIS_LIMIT_*`): batch size `422` (N = rows after the `ids` filter, before routing; delete `dry_run=true` is exempt and never queues — it uses a private `dry:<task_id>:<i>` queue key); queue depth `429`.
  - **data_type check/fix** (`POST /api/kw/datatype-check`; task types `kw_datatype_check` / `kw_datatype_fix`; worker `_datatype_worker`): same flow/queue/limits (`datatype` op → `KIS_LIMIT_KW_EXECUTE`; shares the normal Gateway queue key, no dry key). Targets `tb_status='done'` rows; expected = `derive_kw_fields` `data_type` (incl. override). `apply=false` (default) is **read-only** (GET only; mismatches → `summary.details` for the UI to tick). `apply=true` **requires non-empty `ids`** (never "fix all"), re-checks `tb_status` on its turn, PUTs only actual≠expected rows. Fails closed: unreadable/ambiguous/404/HTTP error → row `fail`, never written. **Circuit breaker**: the first 3 successful PUTs are read back (data_type == expected and `diff_tag_payloads` empty); any miss → that row `fail` and the rest of *that Gateway* is dropped (`cancelled_remaining`, `cancelled` stays false). Never changes staging status columns. PUT partial-update semantics and GET field names are **unverified on a real Gateway** — first real run: 1 row.
  - `_append_history` is guarded by `_history_lock` (several parts/tasks can finish at once); `started_at` falls back to `ended_at` when every part was cancelled while queued; the record has a `cancelled` flag.
- **Gateway auto-routing** (`app/gateway_router.py`, pure logic): with several Gateways, which one a tag belongs to is decided automatically by cross-checking two sources — (1) the **channel** in each Gateway's `kepware_structure` cache, (2) the **`gateway_route_map`** table (棟別 `site` → Gateway, else 園區 `zone` → Gateway; site wins). They must agree, otherwise the row is blocked — **never fall back to the default Gateway**. `route.status`: `single` (only 1 Gateway exists → bypass, no sync/map needed, same as pre-multi-GW behavior) / `ok` / `channel_only` / `map_only` are routable; `conflict` (channel vs map disagree) / `conflict_channel` (channel on ≥2 Gateways) / `unrouted` are blocked. Managed via `GET/POST/DELETE /api/kw/routes` (read = any login, write = admin); `/api/kw/derive` and `/api/pg/derive/scale` attach a `route` (plus `site`/`zone`) to every item, computed from data loaded once per request (`_load_route_context` / `_route_for` in `main.py`). The three execute endpoints use the same routing to split work per Gateway (see *Gateway job queue* above).
- **Rate limiting**: Kepware API Gateway enforces a sliding-window limit (~60 calls/minute). Step 3/5 default `delay=1.1s`, `batch_size=50`, `batch_pause=5s`; on `429` the client backs off using the `Retry-After` header and retries up to 3 times (`_kw_call_with_429_retry`).
- **Scale behavior (Step 5)**: rows with `scale_enabled=false` are skipped without calling Kepware (marked `skip`); rows with `scale_enabled=true` but incomplete range values (`raw_low`/`raw_high`/`scaled_low`/`scaled_high`) are recorded as `scale_error` failures.

## Database Tables

- **`scada_tag_config`** — staging table for CSV imports. **Must pre-exist in the DB** (the code does NOT `CREATE` it; it only `ALTER`s to add missing columns — `scale_status`, `scan_group`, and the Step 3 override columns `kw_channel`/`kw_device`/`kw_tag_groups`/`kw_data_type` — via `_ensure_extra_columns`, and relies on a UNIQUE constraint on `tag_name`).
- **`tags`** — formal table, auto-created via `_ensure_formal_table`. Columns: `tag_id`, `tagname` (UNIQUE), `description`, `node_name`, `driver_type`, `address`, `tablename`, `zone`, `bu`, `site`, `floor`, `system`, `owner`, `department`, `data_type`, `created_date`, `updated_date`. *(No `project_name` / building column — `project_name` lives only in staging.)*
- **Collector `tags`** — separate Collector DB (same host, DB name from `COLLECTOR_DB_DATABASE`), auto-created via `_ensure_collector_tags_table`: `tagname`, `tag_address`, `scan_group`, `unit`, `description`, `enable`, `target_table`. The `enable` column's type (boolean vs. smallint) is detected at write time since an externally pre-created Collector DB may use either, and the value is coerced accordingly.
- **`*_config` / `tb_device_profile` / `kepware_gateway` / `kepware_structure` / `gateway_route_map`** — auto-created via `_ensure_ref_tables`. `gateway_route_map` (`match_type` `site`|`zone`, `match_value`, `gateway_id` → `kepware_gateway` ON DELETE CASCADE, UNIQUE(`match_type`,`match_value`)) must be created after `kepware_gateway`.

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
- **Background task endpoints**: an HTTP `200` from `/api/kw/execute`, `/api/pg/execute/scale`, or `/api/kw/delete-batch` only means the background task was *created* (and possibly queued) — the response is `{"task_id", "queued", "queue", "blocked", "message", ["excluded"]}` and the caller must poll `GET /api/tasks/{task_id}` for actual progress/result. Any docs or integration examples for these endpoints must show the polling loop, not just the initial call. There is no `409` any more: busy → `queued: true`; `429` = queue full; `422` = batch over the safety limit.
