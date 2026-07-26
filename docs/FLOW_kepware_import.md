# Kepware 加點全流程規格（逐環節核對版）

> **基準 commit `0c11094`**（2026-07-20 重新追過全部程式碼路徑後改寫，行號皆為當下實際位置）。
> **狀態更新 commit `726fc48`**（2026-07-26）：待確認事項 #1/#2/#3/#11 已修正、#4/#9 已確認不成立，
> 詳見文末分區；本文正文中對這些項目的描述已標註修正後行為，但**行號未重新校準**（修正後有位移）。
> 用途：**逐環節核對資料如何流動、每個欄位從哪來、狀態何時改變**。
> 命名沿革（`tb_status`/`tb_type`/`tb_device_profile` 現在都是 Kepware 概念）見 `CLAUDE.md`；
> API 呼叫格式見 `API_SCHEMA.md`。

---

## 0. 總覽

```
CSV 檔案
  │ ① POST /api/csv/upload         → 記憶體解析 → data/csv_uploads/{upload_id}.json
  ▼
upload_id
  │ ② POST /api/pg/staging/import  → 寫入暫存表 scada_tag_config
  ▼
暫存表（三個獨立狀態欄：tb_status / pg_status / scale_status，皆預設 pending）
  ├─ ③ /api/kw/derive（預覽）  → /api/kw/execute      建 Kepware group+tag → tb_status=done
  ├─ ④ /api/pg/derive/pg（預覽）→ /api/pg/execute/pg   寫 PG 正式表 + Collector → pg_status=done
  └─ ⑤ /api/pg/derive/scale（預覽）→ /api/pg/execute/scale  設 Kepware Scale → scale_status=done
  ▼
⑥ POST /api/collector/reload → 通知 Collector 重新載入
```

**三個狀態欄彼此獨立、沒有前後 gating**——程式不會阻止在未建點（`tb_status=pending`）時直接跑 Step 4 或 5，順序由使用者/呼叫端自行保證。

### 同步 vs 背景任務（呼叫端最容易誤判之處）

| 端點 | 型態 | 回傳 |
|------|------|------|
| `/api/csv/upload`、`/api/pg/staging/import` | 同步 | 最終結果 |
| 三支 `derive`（`/api/kw/derive`、`/api/pg/derive/pg`、`/api/pg/derive/scale`） | 同步 | 預覽資料，**不寫入任何地方** |
| **`/api/kw/execute`** | **背景任務** | `{task_id}` ← 必須輪詢 |
| `/api/pg/execute/pg` | **同步** | 最終結果（**只有這支 execute 是同步的**） |
| **`/api/pg/execute/scale`** | **背景任務** | `{task_id}` ← 必須輪詢 |
| **`/api/kw/delete-batch`** | **背景任務** | `{task_id}` ← 必須輪詢 |

背景任務一律輪詢 `GET /api/tasks/{task_id}`（`main.py:828`）直到 `done:true`，再讀 `summary`（含 `success`/`fail`/`errors`）。**HTTP 200 只代表任務已建立，不代表已執行完成。**

---

## ① CSV 上傳 `POST /api/csv/upload`

`main.py:715` · 權限 **admin / operator** · `multipart/form-data`

| 環節 | 行為 |
|------|------|
| 大小上限 | `CSV_UPLOAD_MAX_BYTES`（預設 10MB），超過 → 400 |
| 編碼偵測 | 依序 `utf-8-sig` → `utf-8` → `big5` → `gb2312`，第一個能解析且有表頭者採用；全失敗 → 400 |
| 筆數上限 | `CSV_UPLOAD_MAX_ROWS`（預設 50000），超過 → 400 |
| 清理 | 表頭與每欄值皆 `strip()`；濾掉空白表頭；**整列全空則丟棄** |
| 落地 | `data/csv_uploads/{upload_id}.json`（`upload_id` = 8 字元 hex），撐過重啟；超過 `CSV_UPLOAD_CLEANUP_DAYS`（預設 30 天）由每週一 3AM 排程刪除 |

**回傳**：`upload_id`、`headers`、`preview`（前 10 筆）、`total_rows`、`filename`、`unique_types`（不重複的 `type` 值）、`duplicate_names`（CSV 內部 `name` 重複清單）。

> 此步驟**只解析、不驗證欄位齊全度**——缺 `io_address` 之類不會有任何警告。存的是**原始 CSV 欄位**，尚未對應 DB 欄位。Kepware 批次刪除也共用這份檔案。

---

## ② 匯入暫存表 `POST /api/pg/staging/import`

`main.py:1215` → `import_staging`（`pg_client.py:184`）· 權限 **admin / operator**

### 欄位對映（表頭比對**不分大小寫**，`_row_get_ci` @ `pg_client.py:1364`）

| 暫存表欄位 | 接受的 CSV 表頭（任一，忽略大小寫與前後空白） | 轉換 |
|-----------|--------------------------------------------|------|
| `tag_name` | `tag_name` / `Tag Name` | **空值 → 整列跳過並計入 skipped** |
| `site` | `site` / `Site` | strip |
| `system_code` | `system_code` / `System` | strip |
| `scada_node_name` | `scada_node_name` / `SCADA Node Name` | strip |
| `io_device` | `io_device` / `I/O DEVICE` | strip |
| `io_address` | `io_address` / `I/O ADDRESS` | strip |
| `scale_enabled` | `scale_enabled` / `SCALE Enabled` | `_parse_bool`：`true/1/yes/on/enabled` 為真 |
| `raw_low` / `raw_high` | `raw_low`·`Raw Low` / `raw_high`·`Raw High` | `_parse_num`：空或非數字 → **NULL** |
| `scaled_low` / `scaled_high` | 同上規則 | 同上 |
| `description` | `description` / `Description` | strip |
| `project_name` | `project_name` / `專案名稱` | strip（**僅存暫存表，不進正式表**） |
| `data_owner` | `data_owner` / `DataOwner` | strip |
| `device_profile` | `device_profile` | strip |
| `scan_group` | `scan_group` / `Scan Group` | strip |

> 2026-07 修正前此處為**大小寫敏感的精確比對**，表頭變體（如 `I/O Address`）會被靜默對成空字串——這是 address 空白髒資料事故的第一因。

### 寫入行為（核對重點）

- `INSERT ... ON CONFLICT (tag_name) DO NOTHING`（`pg_client.py:239`）——**純新增、永不更新**
- `tag_name` 已存在 → 靜默跳過，**新 CSV 的內容不會覆蓋既有暫存資料**
- 回傳含 `message`：`skipped > 0` 時明示「既有資料內容不會被更新，如需修正請先刪除該批暫存資料後重新匯入」（前端 Step 1 以警示底色顯示）
- 整批 `execute_values` 一次送出；整批失敗（表結構/型別問題）→ 拋錯 rollback，`errors` 記一筆 `BULK_INSERT`
- `skipped` = 空 `tag_name` 列 + 被衝突丟棄的列（含**同批內重複**）

> ⚠️ **`scada_tag_config` 必須預先存在於 DB**——程式沒有 `CREATE TABLE`，只 `ALTER` 補欄位
> （`_ensure_extra_columns` @ `pg_client.py:129`：`scale_status`、`scan_group`，以及 `kw_channel`+`kw_device`+`kw_tag_groups` 三欄一起加，
> 由「是否存在 `kw_channel`」單一條件把關）。需要 `tag_name` 的 UNIQUE 約束，否則 ON CONFLICT 無法運作。
>
> ⚠️ **這些 ALTER 只在 `test_connection()` 內執行**（`pg_client.py:172`），而 `test_connection` 只有 `GET /api/pg/test` 會呼叫。
> 網頁登入時前端會自動打這支（Phase 1 加的），所以網頁使用者不會遇到問題；但**純 API 呼叫端若從未打過 `/api/pg/test`，
> 這些欄位永遠不會被建立**。**已於 commit 726fc48 修正**：改為啟動時亦執行一次（見待確認 #11）。

**三個狀態欄**：值域 `{pending, done, skip}`，預設 `pending`（`update_staging_status` @ `pg_client.py:330`）。

---

## 推導核心 `_derive_base_fields`（`pg_client.py:1264`）

模組級共用函式，Step 3/4/5 三個 `derive_*` 各自對每列呼叫一次。`tag_name` 為空 → 回 `None`（該列略過）。
`parts = tag_name.split("_")`。快取由 `_load_derive_caches` 提供：`devices`（`device_config`，key = `device_name`）、`profiles`（`tb_device_profile`，key = `name`）、`locations`（`location_config` 原始 list）。

| 推導欄位 | 規則（依序 fallback） |
|---------|---------------------|
| `site_prefix` | `parts[0]` |
| `floor` | `parts[1]`；**`BF`（不分大小寫）→ 正規化為 `B1F`** |
| `system` | CSV `system_code` → 否則 `parts[2]` |
| `driver_type` | `_resolve_driver_type`（`pg_client.py:1338`）：先用 `io_device` 查 `device_config` → 查不到則關鍵字判斷（`OPC_UA`/`OPCUA`→`OPC_UA`；`OPC`/`IGS`→`OPC`；`IFIX`→`IFIX`）→ 都不中則原值 |
| `nodename` | `scada_node_name` **去除所有 `_`** → 否則 `site + system_code`（直接串接無分隔）→ 否則 `site_prefix + system` |
| `matched_loc` | `location_config` 中第一個 `site` 等於本列 `site` 者 → 提供 `zone`、`bu` |
| **`tb_type`**（= Kepware DeviceProfile） | ① CSV `device_profile` 有值就用 → ② `driver_type` 為 IFIX：`{nodename}-{zone}-{site_prefix}-{system}` → ③ 其他：`{nodename}-{system}-{floor}-{system}` |
| `channel_name` / `device_name` / `tag_groups` | `tb_type.split("-")` → `tp[0]` / `tp[1]` / `".".join(tp[2:])` |
| `address` | `driver_type` 為 `OPC`/`OPC_UA` 且 `io_address` 非空 → `ns=2;s={io_address}`；否則 `io_address` 原值 |
| `profile_exists` | `tb_type` 是否存在於 `tb_device_profile` |
| `tabname` | 該 profile 的 `description` |

> 範例：`tb_type = K8CHS-CHS-2F-CHS` → channel `K8CHS`、device `CHS`、tag_groups `2F.CHS`

---

## ③ Kepware 建點

### 3-0. Gateway 與結構同步（前置）

- Gateway 存於 DB `kepware_gateway`：`id, name(UNIQUE), url, username, password_enc(Fernet), verify_ssl, zone, is_default`
- 密碼以 `KW_ENCRYPT_KEY` 經 SHA-256 → Fernet 加密；解密失敗（金鑰變更）由 `_decrypt_gw_password` 轉成 **400 清楚訊息**而非 opaque 500
- `get_gateways` 不回傳 `password_enc`；`is_default` 唯一（create/update 前先清空其他）
- 憑證解析 `_resolve_gw_credentials`：帶 `gateway_id` 用 DB 的，否則用手動 `kw_gw_url/username/password`
- `POST /api/kw/gateways/{id}/sync`（`main.py:971`）：`login` → `fetch_structure()`（`kw_gw_client.py:113`，走 `GET /api/config/channels` → `.../devices` → `.../tag_groups`，device/group 層錯誤被吞不中止）→ 攤平寫入 `kepware_structure`（`UNIQUE(gateway_id,channel,device,tag_group)`，先刪該 gateway 全部再 bulk insert）→ 回 `{channels, devices, groups}` 計數

### 3-1. 推導預覽 `POST /api/kw/derive`（`main.py:1450`）

`derive_kw_fields`（`pg_client.py:1114`）在 base 之上處理**手動覆寫**：

```
channel_name = kw_channel     or base.channel_name
device_name  = kw_device      or base.device_name
tag_groups   = kw_tag_groups（非 None 就用，含空字串）or base.tag_groups
overridden   = 上述任一與 base 推導值不同
```

覆寫值由 Step 3 畫面編輯後按「儲存覆寫」寫入暫存表（`POST /api/pg/staging/kw-override` @ `main.py:1244`；前端只送出**有變更**的列）。

帶 `gateway_id` 時每列附 `validation`（比對 `kepware_structure`）：

```
ch_exists  = ch in structure
dev_exists = ch_exists and dev in structure[ch]
grp_exists = dev_exists and grp in structure[ch][dev]
group_exists      = grp_exists if grp else True
group_auto_create = dev_exists and not grp_exists and bool(grp)
```

| 畫面標記 | 條件 | 執行時行為 |
|---------|------|-----------|
| **✓** | channel + device + group 都存在 | 直接建 tag |
| **+G** | channel+device 存在、group 不存在 | **執行時自動建立 group** |
| **▲** | channel 或 device 不存在 | **不會自動建立**，須先在 Kepware 手動建好，否則建 tag 失敗 |

### 3-2. 執行建點 `POST /api/kw/execute`（`main.py:1505`）· **背景任務**

**同步階段**（可立即回饋錯誤）：撈 `tb_status=pending` 全部（`page_size=None`，**無 9999 截斷**）→ 依 `ids` 篩選 → `derive_kw_fields` → 解析 Gateway 憑證。無資料 → **400**。

**背景階段**：
1. 登入 Gateway（失敗 → 整批標記失敗並結束）
2. **預建所有不重複 tag group**：收集 unique `(channel, device, group)` → `ensure_tag_groups`（`kw_gw_client.py:165`）路徑 `2F.CHS` 拆 `.` 逐層建（`parent_group` 為前綴），**409 視為已存在**。此迴圈**亦套用 delay 節流 + 429 退避**
3. 逐筆 `create_tag`（`kw_gw_client.py:188`）→ `POST /api/config/tags`

| 結果 | 處理 |
|------|------|
| 成功 | 計入 success_ids |
| **HTTP 409** | **視為成功**（冪等重跑），明細標「已存在」 |
| HTTP 429 | 依回應 `Retry-After` 退避重試，最多 3 次 |
| 其他 | 計入 fail，`errors` 記 `{name, reason}` |

4. `success_ids` → `tb_status=done`（失敗者維持 `pending`，可重跑）
5. 寫 `activity_log`（action `execute_tb`）+ 操作歷史

**送往 Kepware 的 payload**：
```json
{ "channel_name": "...", "device_name": "...",
  "tag": { "name": "<tag_name>", "data_type": 0,
           "address": "<僅在非空時才帶此 key>",
           "description": "..." },
  "tag_group": "<僅在非空時才帶>" }
```

> ⚠️ **`address` 為空時 `main.py` 的 `or None` 會讓這個 key 完全不出現在 payload**——Kepware 照樣建出 tag 並回 200，但該 tag **沒綁定任何 I/O 位址（空殼 tag）**，後續 Scale 也會「成功但看不到效果」。這是 2026-07 事故的直接成因。
> `data_type` 固定送 `0`（`create_tag` 預設值），未依實際點位型別調整。

---

## ④ PG 正式表 + Collector `POST /api/pg/execute/pg`（`main.py:1642`）· **同步**

### 4-1. 推導 `derive_pg_fields`（`pg_client.py:1163`）

| 正式表欄位 | 來源 |
|-----------|------|
| `tagname` | `base.tag_name` |
| `description` | 暫存表 `description` |
| **`node_name`** | 暫存表 **`scada_node_name` 原值**（實際資料無底線，與推導值相同——見「已確認不成立」#4） |
| `driver_type` | `base.driver_type` |
| **`address`** | 暫存表 **`io_address` 原值**（**刻意**不帶 `ns=2;s=` 前綴，見下方「已確認為刻意設計」） |
| `tabname` → 寫入欄位 `tablename` | `base.tabname` → 若空：IFIX `{zone}_{site_prefix}_{system}`、非 IFIX `{bu}_{site_prefix}_{system}`（對應值為空則整體為空字串） |
| `zone` / `bu` | `location_config` 比對結果 |
| `site` | 暫存表 `site` 原值 |
| `floor` / `system` | `base.floor` / `base.system` |
| `owner` | 暫存表 `data_owner` |
| `department` | 用 `data_owner` 查 `ownership_config` |
| `data_type` | **固定字串 `"float"`** |
| `scan_group` | 暫存表 `scan_group`（**僅給 Collector 用**） |
| **`tag_address`** | **`base.address`**（帶 `ns=2;s=` 前綴，**僅給 Collector 用**） |

### 4-2. 寫正式表 `import_formal`（`pg_client.py:811`）

- `_ensure_formal_table`（`pg_client.py:763`）：`CREATE TABLE IF NOT EXISTS tags` + 同步 `tag_id` sequence（避免衝突）+ 補 `system` 欄位
- 逐筆 `INSERT ... ON CONFLICT (tagname) DO UPDATE`（**會更新**，與暫存表的 DO NOTHING 相反），更新除 `tagname`/`created_date` 外所有欄 + `updated_date=NOW()`
- `tagname` 空 → 跳過計入 skipped
- 先 SELECT 本批已存在的 tagname，記錄 `new_tagnames`（本次真正新插入者），供回滾只刪這些
- **正式表沒有 `project_name` 欄位**——該欄只存在暫存表

### 4-3. 寫 Collector（僅當 `COLLECTOR_DB_DATABASE` 有設）

Collector 是**同主機、不同 database**。排除正式表已出錯的 tagname 後，逐筆寫入 `import_collector_tags`（`pg_client.py:1038`）：

| Collector 欄位 | 來源 |
|---------------|------|
| `tagname` | 同正式表 |
| `tag_address` | **`base.address`（帶 `ns=2;s=` 前綴）** |
| `scan_group` / `description` | 暫存表對應欄位 |
| `enable` | 固定 `True`，但**依 `information_schema` 查到的實際欄位型別轉換**（`smallint` → `1`/`0`；`boolean` → 原樣） |
| `target_table` | `tabname` |

- `INSERT ... ON CONFLICT (tagname) DO UPDATE`
- **逐筆 SAVEPOINT**：單筆失敗只回滾該筆，不會造成 `current transaction is aborted` 連環失敗
- 表若由外部系統預先建立，`CREATE TABLE IF NOT EXISTS` **不生效**——這正是 `enable` 需依實際型別轉換的原因（正式環境為 `smallint`）

### 4-4. 回滾與狀態

| 情況 | 行為 |
|------|------|
| Collector **整支拋例外** | 刪除本次 `new_tagnames` 對應的正式表資料（**不誤刪原本就存在、只是被 update 的列**）→ 回 500 |
| Collector **逐筆失敗（不拋例外）** | **不觸發回滾**；該 tagname 併入 `error_tagnames`，**其 `pg_status` 維持 pending 可重跑**，回應 message 明示 |

- `success_ids = [d["id"] for d in derived if d["tagname"] not in error_tagnames]` → `pg_status=done`
- 寫 `activity_log`（action `execute_pg`）；前端 Step 4 顯示正式表/Collector 失敗明細（各前 10 筆）

> ⚠️ **兩階段風險**：兩個 DB 無分散式交易。補償 DELETE 本身可能失敗（只記 log）；被 update 的既有列其舊值不會還原；極端情況兩庫可能不一致。

---

## ⑤ Kepware Scale `POST /api/pg/execute/scale`（`main.py:1774`）· **背景任務**

### 5-1. 推導 `derive_scale_fields`（`pg_client.py:1213`）

| 欄位 | 規則 |
|------|------|
| `full_tag_name` | `{base.tag_groups}.{tag_name}`；`tag_groups` 空則只有 `tag_name` |
| `channel_name` / `device_name` | **`base` 推導值**（**已修正**：與 `derive_kw_fields` 一致讀取 `kw_channel`/`kw_device` 覆寫值） |
| `data_type` | **固定 `8`**（Kepware Double） |
| `scaling_type` | `scale_enabled` 為真 **且** `raw_low`/`raw_high`/`scaled_low`/`scaled_high` **四個都非 NULL** → `1`（Linear）；否則 `0`（無） |
| Linear 才附帶 | `scaling_raw_low/high`、`scaling_scaled_low/high`（轉 float）、`scaling_clamp_low/high` **固定 `False`**、`scaling_scaled_data_type` **固定 `8`** |

> **從不送 `scaling_units`**。

### 5-2. 執行

**同步階段**：撈 `scale_status=pending` → 篩 `ids` → 推導。無資料 / 推導後空清單 → **400**。
**背景階段**：登入 Gateway → 逐筆 `set_tag_scaling`（`kw_gw_client.py:233`，`PUT /api/config/tags`）→ 節流 + 429 退避 → `success_ids` 標 `scale_status=done` → 寫 `activity_log`（`execute_scale`）。

> Scale 是 **PUT（更新語意）**——tag 必須已存在於 Kepware（Step 3 完成）才有意義。

---

## ⑥ Collector Reload

- `GET /api/collector/test`：連 Collector DB 回 `version()`
- `POST /api/collector/reload`（`main.py:1911`）：需 `COLLECTOR_RELOAD_URL` + `COLLECTOR_RELOAD_TOKEN`（缺任一 → 400）。發 `POST` 帶標頭 `X-Reload-Token`、**無 body**、timeout 30s。200 → 回 `{success, **data}`（含 `total_tags`）；連線失敗 → 502；其他 → 500

---

## 限速與節流（Step 3 / 5 / 批次刪除共用）

Kepware API Gateway 端限速為**滑動視窗 60 次/分鐘**（per `IP + 帳號 + 路徑`）。建點、Scale、刪除**都打 `/api/config/tags`，共用同一個額度桶**——同時執行多個任務會互相吃額度。

| 參數 | 預設 | 說明 |
|------|------|------|
| `delay` | **1.1** 秒/筆 | ≈ 每分鐘 48 筆，低於 60 上限留餘裕（先前預設 0.2 秒 ≈ 260 筆/分，第 60 筆必撞） |
| `batch_size` | 50 | 每 N 筆額外暫停 |
| `batch_pause` | 5.0 秒 | 批次暫停；亦為 429 無 `Retry-After` 時的 fallback |
| 429 退避 | 最多 3 次 | 優先讀回應 `Retry-After` → 其次 body `retry_after` → 最後 `batch_pause`，上限 120 秒 |

每筆之後 `sleep(delay + rand(0.01, 0.05))`；每 `batch_size` 筆額外 `sleep(batch_pause)`。
**沒有跨任務的全域鎖**——同一 Gateway 可被多個背景任務同時操作。

---

## 待確認事項

### ✅ 已修正（commit `726fc48`，2026-07-26）

| # | 問題 | 修法 |
|---|------|------|
| **1** | `derive_scale_fields` 不吃 `kw_channel`/`kw_device`/`kw_tag_groups` 覆寫，導致 Step 3 建在 A 路徑、Step 5 卻對 B 路徑設定 | 比照 `derive_kw_fields` 讀取覆寫值，`full_tag_name` 亦改用覆寫後的 `tag_groups` |
| **2** | `scale_enabled=true` 但範圍值缺任一 → 靜默降級 `scaling_type=0` 且照樣計為成功 | 該筆帶 `scale_error` 說明缺哪些欄位；執行時直接記為失敗、不呼叫 Kepware、不進 `success_ids`（`scale_status` 維持 pending 可重跑）。前端 Step 5 預覽以三態 pill／淡紅底列／總計橫幅呈現。<br>**`scale_enabled` 為 falsy 走 `scaling_type=0` 是既有正確設計，維持不變** |
| **3** | `import_formal` 逐筆 try/except 沒有 SAVEPOINT | 逐筆加 SAVEPOINT（作法與 `import_collector_tags` 一致） |
| **11** | schema 遷移只在 `test_connection()` 內執行 | 新增冪等的 `PGClient.ensure_schema()`，並在 `on_startup` 中以 try/except 呼叫；`test_connection` 亦改為呼叫它 |

### ✅ 已確認不成立

| # | 項目 | 結論 |
|---|------|------|
| **4** | 正式表 `node_name` 用原值 vs 推導的去底線版 | **實際資料的 `scada_node_name` 不含底線**（2026-07-26 使用者以 SQL 確認查無結果），兩者永遠相同，無分歧。底線移除本身是為配合 Kepware channel 命名規則的刻意設計 |
| **9** | 暫存表 `ON CONFLICT DO NOTHING`（永不更新） | **維持現狀**（2026-07-26 使用者決定）。Step 3 網頁已可手動編輯 Channel/Device/Groups 覆寫後寫入，不必重新上傳 CSV；且已加警示 message 讓跳過行為可見 |

### ⏸ 尚未處理（另議）

| # | 問題 | 位置 | 影響 / 為何緩議 |
|---|------|------|----------------|
| **6** | `data_type` 三處各自寫死且不一致：建 tag `0`、Scale `8`、正式表字串 `"float"` | 多處 | 無法依實際點位型別調整。要正確處理需先確定型別來源（CSV 有此欄？還是查 Kepware？），屬功能設計而非修 bug |
| **7** | `scaling_clamp_low/high` 固定 `False`；從不送 `scaling_units` | `pg_client.py:1254` | 無法設定夾制行為與單位，同樣需要領域輸入 |
| **8** | 三個狀態欄無前後 gating | 全流程 | 可在未建點時直接跑 Step 4/5，程式不阻止也不警告。屬「加保護」而非「修錯誤」 |
| **10** | 無跨任務全域鎖 | `task_manager` | 同一 Gateway 併發任務會疊加消耗限速額度。加鎖會改變現有可用性（併發呼叫將被擋） |

> 編號 #5 已移至下方「已確認為刻意設計」，編號保留不重排以維持既有引用。

---

## 已確認為刻意設計（非問題，勿再標記）

| 項目 | 說明 |
|------|------|
| **`address` 前綴在三處刻意不同**<br>`pg_client.py:1198` / `1209` | 正式表 `tags.address` 存**原始** `io_address`（**不帶**前綴）；Kepware 建點與 Collector 的 `tag_address` 存**帶 `ns=2;s=` 前綴**的版本。<br>**原因（2026-07-20 使用者確認）**：Kepware 建點與 OPC UA job 輪詢都需要前綴才能正確定址，而正式表作為資料清單不需要（也不應該）帶協定前綴。此為刻意區分，非 bug。 |

---

## 相關環境變數

| 變數 | 預設 | 用途 |
|------|------|------|
| `PG_HOST` / `PG_PORT` / `PG_DATABASE` / `PG_USER` / `PG_PASSWORD` | localhost / 5432 / — | 主資料庫 |
| `PG_CONNECT_TIMEOUT` | 10 | 連線逾時（避免 PG 不可達時卡住 thread） |
| `COLLECTOR_DB_DATABASE` | 空則**跳過** Collector 寫入 | Collector DB 名稱（同主機） |
| `COLLECTOR_RELOAD_URL` / `COLLECTOR_RELOAD_TOKEN` | — | Step 6 Reload webhook |
| `KW_ENCRYPT_KEY` | 內建不安全預設值（僅警告） | Gateway 密碼 Fernet 金鑰，**正式環境務必設定**；變更後既有密碼需重新輸入 |
| `KW_GW_VERIFY_SSL` / `KW_GW_CA_BUNDLE` / `KW_GW_USE_PROXY` | true / — / false | Gateway SSL 與 Proxy 全域預設（個別 gateway 的 `verify_ssl` 可覆蓋） |
| `CSV_UPLOAD_MAX_BYTES` / `CSV_UPLOAD_MAX_ROWS` / `CSV_UPLOAD_CLEANUP_DAYS` | 10485760 / 50000 / 30 | CSV 上傳限制與暫存檔清理 |
| `JWT_SECRET_KEY` / `JWT_EXPIRE_HOURS` | 隨機產生（警告）/ 24 | 登入 Token，**正式環境務必設定固定值**否則重啟即全部登出 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | admin / admin | 初始管理員（僅在無任何使用者時建立一次） |
| `LOG_CLEANUP_DAYS` | 7 | 操作日誌保留天數 |

> 所有數值型環境變數在 `.env` 寫成 `KEY=`（空值）時，均已處理為「視為未設定、套用預設」。

---

*文件基準 commit `0c11094`（2026-07-20）。*
