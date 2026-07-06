# Kepware ThingsBoard 點位管理系統 — API Schema 參考文件

**Base URL**: `http://localhost:9000`  
**Content-Type**: `application/json`（除特別標註外）  
**認證方式**: JWT Bearer Token（登入後取得，放入 `Authorization: Bearer <token>` 標頭）

---

## 目錄

1. [認證與帳號](#1-認證與帳號)
2. [Kepware 匯入流程（5 步驟）](#2-kepware-匯入流程5-步驟)
3. [帳號管理（admin only）](#3-帳號管理admin-only)
4. [操作日誌（admin only）](#4-操作日誌admin-only)
5. [PG 參照表](#5-pg-參照表)
6. [IO Mapping](#6-io-mapping)
7. [ThingsBoard 直接建點](#7-thingsboard-直接建點)
8. [設定管理（Config）](#8-設定管理config)

---

## 1. 認證與帳號

### POST /api/user/login

使用者登入，取得 JWT Token。

**Request Body**

```json
{
  "username": "admin",
  "password": "admin"
}
```

| 欄位 | 型別 | 必填 | 說明 |
|------|------|------|------|
| username | string | ✓ | 帳號 |
| password | string | ✓ | 密碼 |

**Response 200**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "username": "admin",
  "role": "admin",
  "display_name": "管理員"
}
```

| 欄位 | 說明 |
|------|------|
| token | JWT Token，有效期 24 小時（可由 `JWT_EXPIRE_HOURS` 環境變數調整） |
| role | `admin` / `operator` / `user` |

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/user/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}'
```

---

### GET /api/user/me

取得目前登入者資訊（需 JWT）。

**Response 200**

```json
{
  "sub": "admin",
  "role": "admin",
  "name": "管理員",
  "exp": 1746300000
}
```

**curl 範例**

```bash
TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

curl http://localhost:9000/api/user/me \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/user/change-password

使用者自行修改密碼（需 JWT，任何角色均可使用）。

**Request Body**

```json
{
  "old_password": "oldpass",
  "new_password": "newpass123"
}
```

| 欄位 | 型別 | 必填 | 說明 |
|------|------|------|------|
| old_password | string | 否 | 舊密碼（填寫則驗證舊密碼） |
| new_password | string | ✓ | 新密碼 |

**Response 200**

```json
{ "success": true, "message": "密碼已更新" }
```

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/user/change-password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password":"admin","new_password":"newpass123"}'
```

---

## 2. Kepware 匯入流程（5 步驟）

**權限要求**：所有執行類端點（import、execute）需要 `admin` 或 `operator` 角色。  
**完整流程**：CSV Upload → 匯入暫存表 → 推導 TB → 執行 TB 建點 → 推導 PG → 執行 PG 寫入 → 推導 Scale → 執行 Scale 設定。

---

### 步驟 0：上傳 CSV

#### POST /api/csv/upload

上傳 CSV 檔案並解析，回傳 `upload_id` 供後續步驟使用。  
**Content-Type**: `multipart/form-data`

**Request Form Data**

| 欄位 | 型別 | 說明 |
|------|------|------|
| file | File | CSV 檔案（支援 UTF-8-BOM / UTF-8 / Big5 / GB2312） |

**Response 200**

```json
{
  "upload_id": "a1b2c3d4",
  "headers": ["name", "site", "system_code", "tag_name", "device_profile", "scale_enabled", "raw_low", "raw_high", "scaled_low", "scaled_high"],
  "preview": [
    {
      "name": "K8CHS-CHS-2F-TAG001",
      "site": "K8",
      "system_code": "CHS",
      "tag_name": "K8_2F_CHS_TAG001",
      "device_profile": "K8CHS-CHS-2F-CHS",
      "scale_enabled": "true",
      "raw_low": "0",
      "raw_high": "4000",
      "scaled_low": "0",
      "scaled_high": "100"
    }
  ],
  "total_rows": 250,
  "filename": "kepware_tags.csv",
  "unique_types": ["K8CHS-CHS-2F-CHS", "K8CHS-CHS-3F-CHS"],
  "duplicate_names": []
}
```

| 欄位 | 說明 |
|------|------|
| upload_id | 8 字元 hex，用於後續步驟 |
| preview | 前 10 筆資料預覽 |
| unique_types | CSV 中不重複的 `type`（DeviceProfile 名稱）清單 |
| duplicate_names | CSV 內 `name` 欄位重複的項目清單 |

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/csv/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@kepware_tags.csv"
```

> ⚠️ 需要 JWT（admin / operator）。

---

### 步驟 1：匯入暫存表

#### POST /api/pg/staging/import

將已上傳的 CSV 資料匯入 PG `scada_tag_config` 暫存表。  
**需要 JWT（admin / operator）**

**Request Body**

```json
{ "upload_id": "a1b2c3d4" }
```

**Response 200**

```json
{
  "inserted": 245,
  "skipped": 5,
  "errors": []
}
```

| 欄位 | 說明 |
|------|------|
| inserted | 成功新增筆數 |
| skipped | 已存在（依 tag_name 去重）筆數 |
| errors | 各筆錯誤清單 `[{"tagname": "...", "reason": "..."}]` |

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/pg/staging/import \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"upload_id":"a1b2c3d4"}'
```

---

#### POST /api/pg/staging/query

分頁查詢暫存表。**需要 JWT**（任何已登入角色）。

**Request Body**

```json
{
  "page": 0,
  "page_size": 50,
  "tb_status": "pending",
  "pg_status": null,
  "scale_status": null
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| page | int | 0 | 頁碼（0-based） |
| page_size | int | 50 | 每頁筆數 |
| tb_status | string? | null | 篩選：`pending` / `done` / `skip` |
| pg_status | string? | null | 篩選：`pending` / `done` / `skip` |
| scale_status | string? | null | 篩選：`pending` / `done` / `skip` |

**Response 200**

```json
{
  "data": [
    {
      "id": 1,
      "tag_name": "K8_2F_CHS_TAG001",
      "site": "K8",
      "system_code": "CHS",
      "device_profile": "K8CHS-CHS-2F-CHS",
      "scada_node_name": "K8CHS",
      "scale_enabled": true,
      "raw_low": 0.0,
      "raw_high": 4000.0,
      "scaled_low": 0.0,
      "scaled_high": 100.0,
      "tb_status": "pending",
      "pg_status": "pending",
      "scale_status": "pending",
      "created_at": "2026-05-02T10:30:00"
    }
  ],
  "total": 250,
  "page": 0,
  "page_size": 50
}
```

---

#### POST /api/pg/staging/status

批次更新暫存表狀態欄位。**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "ids": [1, 2, 3],
  "field": "tb_status",
  "status": "skip"
}
```

| field 可選值 | 說明 |
|-------------|------|
| tb_status | ThingsBoard 建點狀態 |
| pg_status | PG 正式表寫入狀態 |
| scale_status | Scale 設定狀態 |

**status 可選值**: `pending` / `done` / `skip`

**Response 200**

```json
{ "success": true, "updated": 3 }
```

---

#### POST /api/pg/staging/delete

刪除指定暫存資料（**admin only**）。

**Request Body**

```json
{ "ids": [1, 2, 3] }
```

**Response 200**

```json
{ "success": true, "deleted": 3 }
```

---

#### DELETE /api/pg/staging/clear

清空整個暫存表（**admin only**）。

**Response 200**

```json
{ "success": true, "deleted": 250 }
```

---

### 步驟 2：推導 TB 欄位

#### POST /api/pg/derive/tb

從暫存表推導 ThingsBoard 建點所需欄位（預覽，不寫入任何資料）。  
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "ids": null,
  "tb_status": "pending",
  "pg_status": "pending"
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| ids | list? | null | 指定 id 清單；null = 全部符合條件的資料 |
| tb_status | string? | "pending" | 篩選 tb_status（null 不篩選） |
| pg_status | string? | "pending" | 篩選 pg_status（null 不篩選） |

**Response 200**

```json
{
  "data": [
    {
      "id": 1,
      "tag_name": "K8_2F_CHS_TAG001",
      "tb_name": "K8CHS-CHS-2F-TAG001",
      "tb_type": "K8CHS-CHS-2F-CHS",
      "tb_label": "K8_2F_CHS TAG001",
      "tb_description": "K8_2F_CHS_TAG001"
    }
  ],
  "total": 245
}
```

**推導邏輯說明**

| 欄位 | 推導規則 |
|------|---------|
| tb_name | `{site}{system_code}-{system}-{floor}-{tag_suffix}`（去除 `_`） |
| tb_type | CSV `device_profile` → 否則 `{nodename}-{system}-{floor}-{system}` |
| floor | `tag_name.split("_")[1]`；若值為 `BF` 自動轉換為 `B1F` |

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/pg/derive/tb \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tb_status":"pending"}'
```

---

### 步驟 3：執行 TB 建點

#### POST /api/pg/execute/tb

推導欄位後呼叫 ThingsBoard API 建立裝置，成功後更新 `tb_status = done`。  
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "tb_url": "http://thingsboard.example.com",
  "tb_username": "tenant@example.com",
  "tb_password": "password",
  "ids": null,
  "delay": 0.2,
  "batch_size": 50,
  "batch_pause": 5.0
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| tb_url | string | ✓ | ThingsBoard URL |
| tb_username | string | ✓ | TB 帳號 |
| tb_password | string | ✓ | TB 密碼 |
| ids | list? | null | 指定 id；null = 全部 pending |
| delay | float | 0.2 | 每筆間隔秒數 |
| batch_size | int | 50 | 批次大小（達到後暫停） |
| batch_pause | float | 5.0 | 批次暫停秒數（429 限速也使用此值） |

**Response 200**

```json
{
  "success": 240,
  "failed": 5,
  "errors": [
    { "name": "K8CHS-CHS-2F-TAG001", "reason": "HTTP 409: Device already exists" }
  ],
  "message": "成功建立 240 筆裝置"
}
```

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/pg/execute/tb \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tb_url": "http://thingsboard.example.com",
    "tb_username": "tenant@example.com",
    "tb_password": "password",
    "delay": 0.2,
    "batch_size": 50,
    "batch_pause": 5.0
  }'
```

---

### 步驟 4：推導 PG 欄位

#### POST /api/pg/derive/pg

從暫存表推導 PG 正式表（`tags`）所需欄位（預覽，不寫入）。  
**需要 JWT（admin / operator）**

**Request Body**（同 `derive/tb`）

```json
{
  "ids": null,
  "pg_status": "pending"
}
```

**Response 200**

```json
{
  "data": [
    {
      "id": 1,
      "tagname": "K8_2F_CHS_TAG001",
      "description": "K8_2F_CHS TAG001",
      "node_name": "K8CHS",
      "driver_type": "OPC",
      "address": "",
      "tablename": "",
      "zone": "A",
      "bu": "CIM",
      "site": "K8",
      "floor": "2F",
      "system": "CHS",
      "owner": "",
      "department": "Facilities",
      "data_type": "Float"
    }
  ],
  "total": 245
}
```

---

### 步驟 4（執行）：寫入 PG 正式表

#### POST /api/pg/execute/pg

推導欄位後寫入 `tags` 正式表，成功後更新 `pg_status = done`。  
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "ids": null
}
```

| 欄位 | 型別 | 說明 |
|------|------|------|
| ids | list? | 指定 id；null = 全部 pg_status=pending |

**Response 200**

```json
{
  "inserted": 238,
  "skipped": 7,
  "errors": [],
  "message": "成功寫入 238 筆至正式表"
}
```

---

### 步驟 5：推導 Scale 設定

#### POST /api/pg/derive/scale

推導 Kepware Scale 設定所需欄位（預覽，不寫入）。  
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "scale_status": "pending",
  "ids": null
}
```

**Response 200**

```json
{
  "data": [
    {
      "id": 1,
      "tag_name": "K8_2F_CHS_TAG001",
      "full_tag_name": "K8.2F.CHS.K8_2F_CHS_TAG001",
      "channel_name": "K8CHS",
      "device_name": "CHS",
      "data_type": "Float",
      "scaling_type": 1,
      "scaling_raw_low": 0.0,
      "scaling_raw_high": 4000.0,
      "scaling_scaled_low": 0.0,
      "scaling_scaled_high": 100.0,
      "scaling_clamp_low": true,
      "scaling_clamp_high": true,
      "scaling_scaled_data_type": 8
    }
  ],
  "total": 200
}
```

**Scale 路徑推導說明**

`tb_type` 如 `K8CHS-CHS-2F-CHS` 拆解為：
- channel = `K8CHS`（第 1 段）
- device = `CHS`（第 2 段）
- groups = `2F.CHS`（第 3、4 段，以 `.` 連接）
- full_tag_name = `{groups}.{tag_name}`

---

### 步驟 5（執行）：設定 Kepware Scale

#### POST /api/pg/execute/scale

推導欄位後呼叫 Kepware API Gateway 設定 Scale，成功後更新 `scale_status = done`。  
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "kw_gw_url": "http://kepware-gw.example.com",
  "kw_gw_username": "admin",
  "kw_gw_password": "password",
  "ids": null,
  "delay": 0.2,
  "batch_size": 50,
  "batch_pause": 5.0
}
```

| 欄位 | 型別 | 說明 |
|------|------|------|
| kw_gw_url | string | Kepware API Gateway URL |
| kw_gw_username | string | Gateway 帳號 |
| kw_gw_password | string | Gateway 密碼 |
| ids | list? | null = 全部 scale_status=pending |
| delay | float | 每筆間隔秒數（預設 0.2） |
| batch_size | int | 批次大小（預設 50） |
| batch_pause | float | 批次暫停秒數（預設 5.0） |

**Response 200**

```json
{
  "success": 198,
  "failed": 2,
  "errors": [
    { "tag_name": "K8_2F_CHS_TAG099", "reason": "Tag not found in Kepware" }
  ],
  "message": "成功設定 198 筆 Scale"
}
```

---

### 完整流程 curl 腳本範例

```bash
#!/bin/bash
BASE="http://localhost:9000"

# 1. 登入取得 Token
TOKEN=$(curl -s -X POST $BASE/api/user/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

echo "Token: $TOKEN"

# 2. 上傳 CSV
UPLOAD_ID=$(curl -s -X POST $BASE/api/csv/upload \
  -F "file=@kepware_tags.csv" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_id'])")

echo "Upload ID: $UPLOAD_ID"

# 3. 匯入暫存表
curl -s -X POST $BASE/api/pg/staging/import \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"upload_id\":\"$UPLOAD_ID\"}" | python3 -m json.tool

# 4. 執行 TB 建點
curl -s -X POST $BASE/api/pg/execute/tb \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tb_url":"http://thingsboard.example.com",
    "tb_username":"tenant@example.com",
    "tb_password":"password"
  }' | python3 -m json.tool

# 5. 執行 PG 寫入
curl -s -X POST $BASE/api/pg/execute/pg \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool

# 6. 執行 Scale 設定
curl -s -X POST $BASE/api/pg/execute/scale \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "kw_gw_url":"http://kepware-gw.example.com",
    "kw_gw_username":"admin",
    "kw_gw_password":"password"
  }' | python3 -m json.tool
```

---

## 3. 帳號管理（admin only）

### GET /api/admin/users

取得所有使用者清單。

**Response 200**

```json
[
  {
    "id": 1,
    "username": "admin",
    "display_name": "管理員",
    "role": "admin",
    "is_active": true,
    "created_at": "2026-01-01T00:00:00"
  }
]
```

---

### POST /api/admin/users

新增使用者。

**Request Body**

```json
{
  "username": "operator01",
  "password": "pass1234",
  "display_name": "操作員甲",
  "role": "operator"
}
```

| role 可選值 | 說明 |
|------------|------|
| admin | 完整功能（含設定） |
| operator | 全功能，不含設定頁面 |
| user | 僅 TB 直接建點，不能使用 Kepware 匯入或設定 |

**Response 200**

```json
{
  "id": 2,
  "username": "operator01",
  "display_name": "操作員甲",
  "role": "operator",
  "is_active": true
}
```

---

### PUT /api/admin/users/{user_id}

更新使用者資訊（只更新填寫的欄位）。

**Request Body**

```json
{
  "display_name": "操作員乙",
  "role": "user",
  "is_active": false
}
```

| 欄位 | 型別 | 必填 | 說明 |
|------|------|------|------|
| display_name | string? | 否 | 顯示名稱 |
| role | string? | 否 | 角色 |
| is_active | bool? | 否 | 帳號是否啟用 |

**Response 200**

```json
{ "success": true }
```

---

### POST /api/admin/users/{user_id}/reset-password

管理員重設指定使用者密碼。

**Request Body**

```json
{ "new_password": "newpass123" }
```

**Response 200**

```json
{ "success": true, "message": "密碼已重設" }
```

---

### DELETE /api/admin/users/{user_id}

刪除使用者（不能刪除自己）。

**Response 200**

```json
{ "success": true }
```

---

## 4. 操作日誌（admin only）

### POST /api/admin/logs/query

查詢操作日誌（分頁 + 條件篩選）。

**Request Body**

```json
{
  "page": 1,
  "page_size": 50,
  "username": null,
  "action": null
}
```

| 欄位 | 型別 | 說明 |
|------|------|------|
| page | int | 頁碼（1-based） |
| page_size | int | 每頁筆數 |
| username | string? | 篩選特定使用者 |
| action | string? | 篩選特定操作（見下表） |

**常見 action 值**

| action | 說明 |
|--------|------|
| login | 使用者登入 |
| create_user | 新增帳號 |
| update_user | 更新帳號 |
| staging_import | 匯入暫存表 |
| execute_tb | TB 建點 |
| execute_pg | PG 寫入 |
| execute_scale | Scale 設定 |
| cleanup_logs | 手動清理日誌 |

**Response 200**

```json
{
  "data": [
    {
      "id": 1001,
      "username": "admin",
      "action": "execute_tb",
      "detail": "TB 建點成功 240 筆，失敗 5 筆",
      "ip_addr": "192.168.1.100",
      "created_at": "2026-05-02T10:35:00"
    }
  ],
  "total": 500,
  "page": 1,
  "page_size": 50
}
```

---

### POST /api/admin/logs/cleanup

手動清理過期日誌（預設清除 7 天前的記錄；可由 `LOG_CLEANUP_DAYS` 環境變數調整）。

**Response 200**

```json
{ "success": true, "deleted": 1234 }
```

---

## 5. PG 參照表

參照表提供 Kepware 匯入推導用的對照資料。**GET 需要 JWT（任何已登入角色）；POST 需要 JWT（admin / operator）。**

### GET /api/pg/ref/locations

```json
[
  { "id": 1, "bu": "CIM", "site": "K8", "zone": "A" }
]
```

### POST /api/pg/ref/locations

**需要 JWT（admin / operator）**

```json
{ "bu": "CIM", "site": "K8", "zone": "A" }
```

---

### GET /api/pg/ref/ownerships

```json
[
  { "id": 1, "department": "Facilities", "data_owner": "FM Team" }
]
```

### POST /api/pg/ref/ownerships

**需要 JWT（admin / operator）**

```json
{ "department": "Facilities", "data_owner": "FM Team" }
```

---

### GET /api/pg/ref/devices

```json
[
  {
    "id": 1,
    "device_name": "CHS",
    "driver_type": "OPC",
    "site": "K8",
    "system_code": "CHS",
    "ip_address": "192.168.1.10",
    "description": "冷凍主機"
  }
]
```

### POST /api/pg/ref/devices

**需要 JWT（admin / operator）**

```json
{
  "device_name": "CHS",
  "driver_type": "OPC",
  "site": "K8",
  "system_code": "CHS",
  "ip_address": "192.168.1.10",
  "description": "冷凍主機"
}
```

---

### GET /api/pg/ref/systems

```json
[
  { "id": 1, "system_code": "CHS", "system_name": "Chiller", "description": "冷凍主機系統" }
]
```

### POST /api/pg/ref/systems

**需要 JWT（admin / operator）**

```json
{ "system_code": "CHS", "system_name": "Chiller", "description": "冷凍主機系統" }
```

---

### GET /api/pg/ref/tb-profiles

```json
[
  { "id": 1, "name": "K8CHS-CHS-2F-CHS", "description": "" }
]
```

### POST /api/pg/ref/tb-profiles

**需要 JWT（admin / operator）**

```json
{ "name": "K8CHS-CHS-2F-CHS", "description": "" }
```

---

### POST /api/pg/ref/tb-profiles/sync

從 ThingsBoard 一鍵同步所有 DeviceProfile 到 PG。

**Request Body**

```json
{
  "tb_url": "http://thingsboard.example.com",
  "tb_username": "tenant@example.com",
  "tb_password": "password"
}
```

**Response 200**

```json
{
  "success": true,
  "synced": 45,
  "total": 45,
  "errors": [],
  "message": "成功同步 45/45 個 DeviceProfile 到 PG"
}
```

---

### POST /api/pg/ref/delete

刪除任一參照表的指定資料列。**需要 JWT（admin / operator）**

```json
{
  "table": "location_config",
  "id": 3
}
```

**table 可選值**: `location_config` / `ownership_config` / `device_config` / `system_config` / `tb_device_profile`

---

### GET /api/pg/test

測試 PostgreSQL 連線狀態。**需要 JWT**（任何已登入角色）。

**Response 200**

```json
{
  "success": true,
  "message": "PG 連線正常",
  "db_version": "PostgreSQL 14.5"
}
```

---

## 6. IO Mapping

### POST /api/io-mapping/execute

上傳 IO List（A 檔）與 iFIX 導出表（B 檔），執行 Tag Name join mapping。  
**Content-Type**: `multipart/form-data`  
**需要 JWT（admin / operator）**

**Request Form Data**

| 欄位 | 說明 |
|------|------|
| a_file | IO List CSV（含 `Tag Name` 欄位） |
| b_file | iFIX 導出表（含 `A_TAG` 欄位，支援 `!` 前綴格式） |

**Response 200**

```json
{
  "mapping_id": "ab12cd34",
  "total": 500,
  "matched": 450,
  "unmatched": 50,
  "columns": ["Site", "System", "SCADA Node Name", "Tag Name", "I/O DEVICE", "I/O ADDRESS", "SCALE Enabled", "Raw Low", "Raw High", "Scaled Low", "Scaled High", "Description"],
  "data": [
    {
      "Site": "K8",
      "System": "CHS",
      "SCADA Node Name": "K8CHS",
      "Tag Name": "K8_2F_CHS_TAG001",
      "I/O DEVICE": "OPCDA.1",
      "I/O ADDRESS": "Server.Channel.Device.TAG001",
      "SCALE Enabled": "True",
      "Raw Low": "0",
      "Raw High": "4000",
      "Scaled Low": "0",
      "Scaled High": "100",
      "Description": "冷凍主機 TAG001",
      "_matched": true
    }
  ]
}
```

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/io-mapping/execute \
  -H "Authorization: Bearer $TOKEN" \
  -F "a_file=@io_list.csv" \
  -F "b_file=@ifix_export.csv"
```

---

### GET /api/io-mapping/download/{mapping_id}

下載 mapping 結果 CSV（UTF-8 BOM，Excel 相容）。**需要 JWT**（任何已登入角色）。

```bash
curl http://localhost:9000/api/io-mapping/download/ab12cd34 \
  -H "Authorization: Bearer $TOKEN" \
  -o IO_Mapping_Result.csv
```

---

## 7. ThingsBoard 直接建點

這套流程透過 SSE 串流即時推送進度（非 Kepware 匯入流程）。

### POST /api/auth/login

測試 ThingsBoard 連線並取得 TB Token。

```json
{
  "tb_url": "http://thingsboard.example.com",
  "username": "tenant@example.com",
  "password": "password"
}
```

**Response 200**

```json
{ "success": true, "token": "eyJ..." }
```

---

### POST /api/csv/upload → POST /api/tasks/execute

上傳 CSV 後啟動批次建點或刪除任務。

**CSV 欄位（create）**: `name`, `type`（DeviceProfile）, `label`, `description`  
**CSV 欄位（delete）**: `name`, `type`

**Request Body**

```json
{
  "upload_id": "a1b2c3d4",
  "operation": "create",
  "dry_run": true,
  "tb_url": "http://thingsboard.example.com",
  "tb_username": "tenant@example.com",
  "tb_password": "password",
  "delay": 0.2,
  "batch_size": 50,
  "batch_pause": 5,
  "csv_filename": "devices.csv"
}
```

| operation | 說明 |
|-----------|------|
| create | 批次建立裝置 |
| delete | 批次刪除裝置（依 name 查詢後刪除） |

**Response 200**

```json
{ "task_id": "a3f7b2c1d9e4f580" }
```

---

### GET /api/tasks/{task_id}/stream

SSE 串流接收任務進度（Server-Sent Events）。

**Event 格式**

```
data: {"type":"log","level":"info","message":"[成功] K8CHS-001"}

data: {"type":"progress","done":50,"total":200,"success":48,"fail":2,"skip":0}

data: {"type":"complete","total":200,"success":195,"fail":5,"skip":0}
```

**curl 範例**

```bash
curl -N http://localhost:9000/api/tasks/a3f7b2c1d9e4f580/stream
```

---

### GET /api/tasks/{task_id}/status

查詢任務狀態（輪詢用）。

```json
{
  "task_id": "a3f7b2c1d9e4f580",
  "type": "create",
  "done": true,
  "progress": { "done": 200, "total": 200, "success": 195, "fail": 5, "skip": 0 },
  "summary": { "total": 200, "success": 195, "fail": 5, "skip": 0 }
}
```

---

### GET /api/tasks/{task_id}/export

下載任務結果 CSV（UTF-8 BOM，Excel 相容）。

欄位：`name`, `status`, `detail`

---

### POST /api/devices/query

查詢 ThingsBoard 裝置（分頁）。

```json
{
  "tb_url": "http://thingsboard.example.com",
  "tb_token": "eyJ...",
  "page": 0,
  "page_size": 20,
  "text_search": "K8CHS"
}
```

**Response 200**: ThingsBoard `data[]` + `hasNext`, `totalElements`

---

### POST /api/devices/export

匯出 ThingsBoard 裝置清單 CSV（最多 2000 筆）。  
欄位：`name`, `type`, `label`, `createdTime`

---

### POST /api/device-profiles/check

驗證 CSV 中的 type 名稱是否為合法的 DeviceProfile。

```json
{
  "tb_url": "http://thingsboard.example.com",
  "tb_token": "eyJ...",
  "type_names": ["K8CHS-CHS-2F-CHS", "INVALID_PROFILE"]
}
```

**Response 200**

```json
{
  "existing_profiles": ["K8CHS-CHS-2F-CHS", "K8CHS-AHU-3F-AHU"],
  "check_results": {
    "K8CHS-CHS-2F-CHS": true,
    "INVALID_PROFILE": false
  },
  "total_checked": 2,
  "valid_count": 1,
  "invalid_count": 1
}
```

---

### POST /api/kw-gw/test

測試 Kepware API Gateway 連線。

```json
{
  "kw_gw_url": "http://kepware-gw.example.com",
  "kw_gw_username": "admin",
  "kw_gw_password": "password"
}
```

**Response 200**

```json
{ "success": true, "message": "Kepware API Gateway 連線成功" }
```

---

## 8. 設定管理（Config）

本節管理 `data/config.json`（TB 直接建點用的下拉選項、預設值、映射規則；
v3 UI 已不使用此套設定，屬遺留端點）。**本節全部端點皆需要 JWT（admin only）。**

### GET /api/config

取得完整設定（包含 dropdowns + defaults + mapping）。

### GET /api/config/dropdown

取得所有下拉選項。

### PUT /api/config/dropdown

更新指定欄位的選項清單。

```json
{ "field": "bu", "values": ["CIM", "FM", "IT"] }
```

### POST /api/config/dropdown/add

新增一個選項值。

```json
{ "field": "bu", "value": "HR" }
```

### POST /api/config/dropdown/remove

移除一個選項值。

```json
{ "field": "bu", "value": "HR" }
```

### GET /api/config/defaults

取得欄位預設值。

### PUT /api/config/defaults

更新預設值。

```json
{ "defaults": { "bu": "CIM", "zone": "A" } }
```

### GET /api/config/mapping

取得映射規則。

### PUT /api/config/mapping

更新映射規則。

```json
{
  "rule_name": "bu_mapping",
  "mapping": { "K8": "CIM", "K9": "FM" }
}
```

### DELETE /api/config/mapping/{rule_name}

刪除映射規則。

---

## 錯誤回應格式

所有 API 錯誤統一格式：

```json
{
  "detail": "錯誤說明文字"
}
```

| HTTP 狀態碼 | 說明 |
|------------|------|
| 400 | 請求格式錯誤或業務邏輯錯誤 |
| 401 | 未登入或 Token 無效/過期 |
| 403 | 角色權限不足 |
| 404 | 資源不存在 |
| 409 | 資源衝突（帳號重複、選項已存在） |
| 500 | 伺服器內部錯誤（PG/TB/Kepware 連線問題等） |

---

## 環境變數

| 變數名稱 | 預設值 | 說明 |
|---------|--------|------|
| `JWT_SECRET_KEY` | *(無公開預設值)* | JWT 簽名金鑰。**留空或未設定時，啟動會自動產生一組臨時隨機金鑰並記錄警告**——服務每次重啟都會換金鑰，導致所有既有 Token 失效（使用者需重新登入）。正式環境務必設定固定值。**注意**：`.env` 中若寫成 `JWT_SECRET_KEY=`（有此行但值為空），效果等同未設定，一樣會觸發臨時金鑰。 |
| `JWT_EXPIRE_HOURS` | `24` | Token 有效時數。`.env` 留空同樣視為未設定，套用預設值 24（不會因空字串而噴錯）。 |
| `KW_ENCRYPT_KEY` | `kepitsimple-default-encrypt-key`（不安全預設值，僅記錄警告，不會像 JWT 一樣自動產生隨機值） | 加密「已持久化在 DB 中的 Gateway 密碼」用的金鑰。**務必設定為隨機字串**；未設定時任何取得原始碼者皆可解密資料庫中已儲存的 Gateway 密碼。設定/變更此金鑰後，既有 Gateway 密碼需在設定頁重新輸入一次（金鑰不符時系統會回傳清楚的 400 提示，不是不明的 500）。 |
| `LOG_CLEANUP_DAYS` | `7` | 日誌保留天數（每週一 3AM 自動清理）。`.env` 留空視為未設定，套用預設值。 |
| `ADMIN_USERNAME` | `admin` | 初始管理員帳號（僅在資料庫尚無任何使用者時建立一次）。`.env` 留空視為未設定，套用預設值。 |
| `ADMIN_PASSWORD` | `admin` | 初始管理員密碼。**`.env` 中若寫成 `ADMIN_PASSWORD=`（有此行但值為空），現已視為未設定並套用預設值 `admin`**（先前版本會直接以空字串當密碼，屬安全性問題，已修正）。 |
| `PG_HOST` | `localhost` | PostgreSQL 主機 |
| `PG_PORT` | `5432` | PostgreSQL 端口。`.env` 留空視為未設定，套用預設值（不會因空字串而噴錯）。 |
| `PG_CONNECT_TIMEOUT` | `10` | PostgreSQL 連線逾時秒數。`.env` 留空同樣視為未設定。 |
| `PG_DATABASE` | — | 資料庫名稱 |
| `PG_USER` | — | 資料庫使用者 |
| `PG_PASSWORD` | — | 資料庫密碼 |
| `CSV_UPLOAD_MAX_BYTES` | `10485760`（10MB） | 單檔 CSV 上傳大小上限，超過回 400。`.env` 留空視為未設定，套用預設值。 |
| `CSV_UPLOAD_MAX_ROWS` | `50000` | 單檔 CSV 資料筆數上限，超過回 400。`.env` 留空視為未設定，套用預設值。 |
| `CSV_UPLOAD_CLEANUP_DAYS` | `30` | 暫存 CSV 上傳檔（`data/csv_uploads/`）保留天數，超過即於每週一凌晨 3 點清理排程中刪除。`.env` 留空視為未設定，套用預設值。 |

> **關於「.env 有此行但值留空」**：`os.getenv("X", default)` 只在環境變數**完全不存在**時才回傳 `default`；
> 若 `.env` 寫了 `X=`（有這一行、但等號後面沒填值），`os.getenv` 會回傳**空字串**而非 `default`。
> 上表中標註「留空視為未設定」的變數，程式碼已額外處理這個情境（空字串一律視為未設定、套用預設值）；
> 未特別標註的變數（如 `PG_DATABASE`）目前仍會直接使用空字串。
