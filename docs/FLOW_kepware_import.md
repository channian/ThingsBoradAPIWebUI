# Kepware 匯入全流程詳細規格

> 本文件記錄**純 Kepware 版**（已脫離 ThingsBoard）建點全流程的資料流、推導規則、比對規則，含 `file:line` 對照。
> 涉及 `tb_status` / `tb_type` / `tb_device_profile` 等命名請參考 CLAUDE.md 的「Legacy naming glossary」——它們現在都是 Kepware 概念。

對照表（`tags` 正式表實際欄位等項目）需連回公司 `tagsystem` 資料庫核對；本文件描述的是**程式碼的行為**。

---

## 總覽：6 步流程

| 步驟 | 名稱 | 主要 API | 狀態欄位 |
|------|------|---------|---------|
| 1 | CSV 上傳 | `POST /api/csv/upload` | — |
| 2 | 匯入暫存表 | `POST /api/pg/staging/import` | 寫入 `scada_tag_config` |
| 3 | Kepware 建點 | `POST /api/kw/derive` → `/api/kw/execute` | `tb_status` |
| 4 | PG 正式表 (+Collector) | `POST /api/pg/derive/pg` → `/api/pg/execute/pg` | `pg_status` |
| 5 | Scale 設定 | `POST /api/pg/derive/scale` → `/api/pg/execute/scale` | `scale_status` |
| 6 | Collector Reload | `POST /api/collector/reload` | — |

---

## Step 1-2：CSV → 暫存表

### CSV 上傳 `POST /api/csv/upload` (`main.py:578-662`，無需認證)
1. `await file.read()` 讀 bytes。
2. **編碼自動偵測**（依序，第一個能解析且有表頭就採用）：`utf-8-sig` → `utf-8` → `big5` → `gb2312` (`main.py:586-596`)。
3. 解析失敗 → 400「無法解析 CSV 檔案」。
4. 表頭/值清洗：去空白、丟空欄、全空列丟棄 (`main.py:602-614`)。
5. `upload_id = uuid4().hex[:8]`；原始列（CSV 欄→值，**尚未對應 DB 欄位**）以 JSON 存到 `data/csv_uploads/{upload_id}.json` (`_csv_store_put`, `main.py:121-125`) —— 可撐過重啟，且 Kepware 批次刪除也共用此檔。
6. 回傳：`upload_id, headers, preview[0:10], total_rows, filename, unique_types, duplicate_names`。

### 匯入暫存表 `POST /api/pg/staging/import` (`main.py:1054`，admin|operator) → `import_staging` (`pg_client.py:171-249`)

**CSV → `scada_tag_config` 欄位對應**（每欄接受 snake_case 或 Excel 風格表頭）：

| DB 欄位 | 接受的 CSV key（依序） | 轉換 |
|---------|---------------------|------|
| `site` | `site` / `Site` | strip |
| `system_code` | `system_code` / `System` | strip |
| `scada_node_name` | `scada_node_name` / `SCADA Node Name` | strip |
| `tag_name` | `tag_name` / `Tag Name` | strip，**必填**（空則跳過該列） |
| `io_device` | `io_device` / `I/O DEVICE` | strip |
| `io_address` | `io_address` / `I/O ADDRESS` | strip |
| `scale_enabled` | `scale_enabled` / `SCALE Enabled` | `_parse_bool`（true/1/yes/on/enabled） |
| `raw_low` / `raw_high` | `raw_low`/`Raw Low`、`raw_high`/`Raw High` | `_parse_num`（float 或 None） |
| `scaled_low` / `scaled_high` | 同上規則 | `_parse_num` |
| `description` | `description` / `Description` | strip |
| `project_name` | `project_name` / `專案名稱` | strip |
| `data_owner` | `data_owner` / `DataOwner` | strip |
| `device_profile` | `device_profile`（僅此） | strip |
| `scan_group` | `scan_group` / `Scan Group` | strip |

共 16 欄 (`pg_client.py:191-206`)。`_get` 只要主 key 存在（即使是空字串）就**不會** fallback 到備用 key (`pg_client.py:1203-1209`)。

**去重**：`INSERT ... ON CONFLICT (tag_name) DO NOTHING RETURNING id` (`pg_client.py:218-232`)。需要 `tag_name` 上有 UNIQUE 約束。`skipped` = 空 `tag_name` 列 + 被衝突丟棄的列（含同批內重複）。整批是單一 INSERT，DB 級錯誤會整批 rollback。

> ⚠️ **`scada_tag_config` 程式碼裡沒有 `CREATE TABLE`** —— 必須在 DB 預先存在。程式只 `ALTER` 補 `scale_status` / `scan_group` (`_ensure_extra_columns`, `pg_client.py:126-157`)。

**三個狀態欄**：`tb_status` / `pg_status` / `scale_status`，預設 `pending`，值域 `{pending, done, skip}` (`update_staging_status`, `pg_client.py:298-313`，更新時戳 `processed_at = NOW()`)。

---

## 推導規則核心 `_derive_base_fields` (`pg_client.py:1112-1183`)

模組級函式，被三個 `derive_*` 方法各呼叫一次/列；`tag_name` 空則回 `None`（該列略過）。
`parts = tag_name.split("_")`。快取由 `_load_derive_caches` (`pg_client.py:982-987`) 提供：`devices`（device_config，keyed by device_name）、`profiles`（tb_device_profile，keyed by name）、`locations`（location_config 原始 list）。

| 欄位 | 規則（含 fallback） | 行 |
|------|--------------------|----|
| **nodename** | `scada_node_name` 去掉所有 `_` → 否則 `site + system_code`（直接相接）→ 否則 `parts[0] + system` | 1132-1140 |
| **system** | CSV `system_code` → 否則 `parts[2]` | 1127 |
| **floor** | `parts[1]`；若等於 `BF`（不分大小寫）→ 改成 `B1F` | 1124-1126 |
| **driver_type** | 由 CSV `io_device` 查 `device_config` 的 `driver_type` → 否則啟發式：`OPC_UA/OPCUA→OPC_UA`、`OPC/IGS→OPC`、`IFIX→IFIX`、否則原值 | 1130, 1186-1200 |
| **tb_type**（DeviceProfile） | ① CSV `device_profile` → ② IFIX：`{nodename}-{zone}-{site_prefix}-{system}` → ③ 其他：`{nodename}-{system}-{floor}-{system}` | 1144-1151 |
| **channel / device / tag_groups** | `tb_type.split("-")` → `channel=tp[0]`、`device=tp[1]`、`tag_groups=".".join(tp[2:])` | 1153-1156 |
| **address** | OPC/OPC_UA → `ns=2;s={io_address}`；否則原 `io_address` | 1158-1162 |

**比對規則**：
- **matched_loc** = `location_config` 中第一個 `site` 等於本列 `site` 者 (`pg_client.py:1142`) → 提供 `zone` / `bu`。
- **owner / department**（`derive_pg_fields`）：`owner = data_owner`；`department = ownership_config` 以 `data_owner` 為 key 查 (`pg_client.py:1026-1027`)。
- **tablename/tabname**：`tb_device_profile.description`（matched profile）→ 空則 IFIX `{zone}_{site_prefix}_{system}` / 非 IFIX `{bu}_{site_prefix}_{system}` (`pg_client.py:1033-1038, 1164-1165`)。

> 範例 `K8CHS-CHS-2F-CHS` → channel `K8CHS`、device `CHS`、tag_groups `2F.CHS`。

**`derive_pg_fields` 欄位映射差異**：PG `address` 欄存原始 `io_address` (`pg_client.py:1046`)，而帶前綴的版本另存為 `tag_address` (`:1057`)；Kepware 建點 (`derive_kw_fields`) 用帶前綴的 `address` (`:1004`)。

**`derive_scale_fields`** (`pg_client.py:1061-1107`)：
- `full_tag_name = "{tag_groups}.{tag_name}"`（無 group 則只有 tag_name）。
- `scaling_type = 1`（Linear）僅當 `scale_enabled` 為真 **且** raw/scaled 四值皆非 None；否則 `0`（None）。
- 一律含 `data_type: 8`（Kepware Double）。type==1 時加 `scaling_raw/scaled_low/high`（float）、`scaling_clamp_low/high = False`、`scaling_scaled_data_type: 8`。

---

## Step 3：Kepware 建點

### Gateway 模型（`kepware_gateway`, `pg_client.py:103-114`）
欄位：`id, name(UNIQUE), url, username, password_enc(Fernet), verify_ssl, zone, is_default, created_at, updated_at`。
- 密碼：`KW_ENCRYPT_KEY` 經 SHA-256 → Fernet (`main.py:38-47`)；解密失敗（金鑰變更）由 `_decrypt_gw_password` 轉成 **400** 清楚訊息 (`main.py:50-59`)。
- `get_gateways` 不回傳 `password_enc` (`pg_client.py:549`)。
- `is_default` 單一：create/update 前先把所有 default 清為 FALSE。
- 憑證解析 `_resolve_gw_credentials(gateway_id | 手動 url/user/pass)` (`main.py:62-78`)。
- CRUD：`GET /api/kw/gateways`（任意使用者）、`POST/PUT/DELETE`（admin）、`POST /{id}/test`。

### 結構同步 `POST /api/kw/gateways/{id}/sync` (`main.py:821-841`)
`login` → `fetch_structure()` (`kw_gw_client.py:113-140`) 走 `GET /api/config/channels` → `.../devices` → `.../tag_groups`，組成 `{channel:{device:[group_paths]}}`（device/group 錯誤被吞，不中止）。
`sync_structure` (`pg_client.py:613-638`) 把樹攤平寫入 `kepware_structure`（`UNIQUE(gateway_id,channel,device,tag_group)`，先刪該 gateway 全部再 bulk insert）。回 `{channels, devices, groups}` 計數。

### 驗證狀態（`POST /api/kw/derive` 帶 `gateway_id` 時，`main.py:1232-1244`）
```
ch_exists  = ch in structure
dev_exists = ch_exists and dev in structure[ch]
grp_exists = dev_exists and grp in structure[ch][dev]
group_exists      = grp_exists if grp else True
group_auto_create = dev_exists and not grp_exists and bool(grp)
```
前端 `rowStatus` (`app.js:320-325`) 映射：
- **✓ ok**：channel+device+group 都在。
- **+G add**：device 在、group 不在 → 執行時自動建 group。
- **▲ warn**：其他（通常 channel 或 device 不存在 —— 這些**不會**自動建立，建點會失敗）。

### 執行建點 `POST /api/kw/execute` (`main.py:1271-1367`)
1. 取 `tb_status="pending"` → `derive_kw_fields`。
2. 解析憑證 → `login`。
3. **預建 tag group**：蒐集不重複 `(ch,dev,grp)` → `ensure_tag_groups` (`kw_gw_client.py:165-184`)：路徑 `"2F.CHS"` 拆 `.` 逐層建（`parent_group` 為前綴），**409 視為已存在**跳過。
4. **逐 tag** `create_tag` → `POST /api/config/tags`：
   - **409** → 視為成功（冪等重跑）。
   - **429** → sleep `batch_pause` 後重試一次。
   - 其他 → 記入 `failed`。
5. **限速**：每筆 `sleep(delay + rand(0.01,0.05))`；每 `batch_size` 筆額外 `sleep(batch_pause)`。
6. 成功（含 409）的 id → `update_staging_status(..., "tb_status", "done")`；失敗維持 `pending`。

**Kepware REST 端點**（`kw_gw_client.py`）：`POST /api/auth/login`、`GET /api/config/channels[/{ch}/devices[/{dev}/tag_groups]]`、`POST /api/config/tag_groups`、`POST/DELETE /api/config/tags`、`PUT /api/config/tags`（scaling）。

---

## Step 4：PG 正式表 + Collector `POST /api/pg/execute/pg` (`main.py:1370-1455`)

### 4a. 正式表 `import_formal` (`pg_client.py:779-871`)
寫 14 欄 + `created_date=NOW()`：`tagname, description, node_name, driver_type, address, tablename, zone, bu, site, floor, system, owner, department, data_type`（`tagname←tag_name`、`tablename←tabname`、`address←io_address`、`data_type` 預設 `"float"`）。
**ON CONFLICT (tagname) DO UPDATE**：更新除 `tagname`/`created_date` 外所有欄 + `updated_date=NOW()`。
**新插入追蹤**：先 SELECT 本批已存在的 tagname，成功且非既有者收進 `new_tagnames`（供回滾用，`pg_client.py:796-849`）。

### 4b. Collector 寫入（僅當 `COLLECTOR_DB_DATABASE` 有設，`main.py:1393-1430`）
同主機、不同 DB 名。`import_collector_tags` (`pg_client.py:935-978`) 寫 6 欄：`tagname, tag_address(←derived address), scan_group, description, enable=True, target_table(←tabname)`，ON CONFLICT DO UPDATE。

**跨庫回滾**（Collector 失敗時）：正式表已 commit，補償只刪「本次新插入且在 collector 批次中」的 tagname (`main.py:1414-1428`) —— **不會誤刪原本就存在、只是被 update 的列**。
> ⚠️ 兩階段風險：兩個 DB 無分散式交易。補償 DELETE 本身可能失敗（只記 log）；被 update 的既有列其新值不會還原；極端情況下兩庫可能不一致。

### `pg_status` 更新 (`main.py:1432-1438`)
`error_tagnames` = 正式表錯誤 ∪ collector 錯誤；非錯誤的 id → `pg_status="done"`。

---

## Step 5：Scale 設定 `POST /api/pg/execute/scale` (`main.py:1494-1573`)

`derive_scale_fields` → 逐筆 `set_tag_scaling`（`kw_gw_client.py:233-260`，`PUT /api/config/tags`）。
config：`channel_name, device_name, tag_name=full_tag_name, data_type, scaling_type`；type==1 時加 `scaling_raw/scaled_low/high`、`scaling_clamp_low/high`、`scaling_scaled_data_type`。
> 注意：derive 寫 clamp=False，故實際 clamp 為 **False**；**從不送 `scaling_units`**。

限速同 Step 3。成功 id → `scale_status="done"`。

---

## Step 6：Collector Reload

- `GET /api/collector/test` (`main.py:1578-1585`) → 連 Collector DB 回 `version()`。
- `POST /api/collector/reload` (`main.py:1588-1620`)：需 `COLLECTOR_RELOAD_URL` + `COLLECTOR_RELOAD_TOKEN`（缺 → 400）。發 `requests.post(url, headers={"X-Reload-Token": token}, timeout=30)`，**無 body**。200 回 `{success, **data}`（含 `total_tags`）；連線失敗 → 502；其他 → 500。

---

## 相關環境變數

| 變數 | 用於 |
|------|------|
| `PG_HOST/PORT/DATABASE/USER/PASSWORD` | 主 PG 連線（含 `connect_timeout` 預設 10s） |
| `KW_ENCRYPT_KEY` | Gateway 密碼 Fernet 加密金鑰（**改了會使既有密碼無法解密**） |
| `KW_GW_VERIFY_SSL` / `KW_GW_CA_BUNDLE` / `KW_GW_USE_PROXY` | Gateway 連線 SSL / Proxy 全域預設（個別 gateway 的 `verify_ssl` 可覆蓋） |
| `COLLECTOR_DB_DATABASE` | 啟用 Step 4 Collector 寫入（同主機不同 DB） |
| `COLLECTOR_RELOAD_URL` / `COLLECTOR_RELOAD_TOKEN` | Step 6 Reload webhook |
| `ADMIN_USERNAME/PASSWORD` / `JWT_SECRET_KEY` / `JWT_EXPIRE_HOURS` | 使用者認證 |
