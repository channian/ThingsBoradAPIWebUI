# Kep It Simple · Kepware 點位管理系統 — API Schema 參考文件

> 純 Kepware 架構，**不再整合 ThingsBoard**（見 `CLAUDE.md`）。本文件已於
> 2026-07 全面校正，移除已不存在的 TB 端點，並補上先前完全沒有文件記載的
> `/api/kw/delete-batch`、`/api/kw/gateways`。

**Base URL**: `http://localhost:9000`  
**Content-Type**: `application/json`（除特別標註外）  
**認證方式**: JWT Bearer Token（登入後取得，放入 `Authorization: Bearer <token>` 標頭）

---

## 目錄

1. [認證與帳號](#1-認證與帳號)
2. [Kepware 匯入流程（6 步驟）](#2-kepware-匯入流程6-步驟)
3. [帳號管理（admin only）](#3-帳號管理admin-only)
4. [操作日誌（admin only）](#4-操作日誌admin-only)
5. [PG 參照表](#5-pg-參照表)
6. [IO Mapping](#6-io-mapping)
7. [Kepware Gateway 管理 + 批次刪除](#7-kepware-gateway-管理--批次刪除)
8. [設定管理（Config，legacy）](#8-設定管理configlegacy)

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

## 2. Kepware 匯入流程（6 步驟）

**權限要求**：所有執行類端點（import、execute）需要 `admin` 或 `operator` 角色。  
**完整流程**：CSV Upload → 匯入暫存表 → Kepware 建點 → PG 正式匯入 → Kepware Scale 設定 → Collector Reload。

> ⚠️ **本專案已於 Phase 4+ 完全脫離 ThingsBoard，改為純 Kepware 架構**（見 `CLAUDE.md`）。
> 舊版文件曾記載 `/api/pg/derive/tb`、`/api/pg/execute/tb`、`/api/auth/login`（TB 登入）等端點，
> 這些**在目前的 `main.py` 裡已不存在**，本節已全面校正為實際存在的端點；
> 若您手上的整合程式是照舊版文件寫的，請對照下方重新確認。

### 背景任務與輪詢（重要，共用契約）

`POST /api/kw/execute`、`POST /api/pg/execute/scale`、`POST /api/kw/delete-batch`
三個「真正呼叫 Kepware API」的端點，因為批次執行動輒數分鐘，**都是背景任務**：
呼叫後立即回傳 `{"task_id": "..."}`，**HTTP 200 只代表任務已建立、開始排隊，
不代表已經執行完成、更不代表 Kepware 已經套用變更**。

必須接著輪詢 `GET /api/tasks/{task_id}` 直到 `done: true`，才能從 `summary` 得知真正結果：

```bash
curl http://localhost:9000/api/tasks/abc12345 -H "Authorization: Bearer $TOKEN"
```

```json
{
  "task_id": "abc12345",
  "done": true,
  "progress": { "current": 60, "total": 60, "success": 58, "fail": 2, "skip": 0 },
  "summary": {
    "total": 60, "success": 58, "fail": 2, "skip": 0,
    "errors": [{ "name": "TAG099", "reason": "HTTP 429: ..." }],
    "message": "成功建立 58 筆 Tag"
  }
}
```

`done: false` 時 `summary` 為 `null`；請務必檢查 `summary.fail`／`summary.errors`，
**只看 HTTP 狀態碼或只看 `task_id` 回來就當作成功，是最容易誤判「已生效」的寫法**。

**Python 輪詢範例**（三個背景任務端點通用）：

```python
import time, requests

def run_and_wait(url, body, headers, poll_interval=1, timeout=600):
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()
    task_id = r.json()["task_id"]
    waited = 0
    while waited < timeout:
        r = requests.get(f"{BASE}/api/tasks/{task_id}", headers=headers)
        st = r.json()
        if st["done"]:
            return st["summary"]
        time.sleep(poll_interval)
        waited += poll_interval
    raise TimeoutError(f"task {task_id} 逾時未完成")
```

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

### 步驟 2：推導 Kepware 建點欄位

#### POST /api/kw/derive

從暫存表推導 Kepware 建點所需欄位（channel/device/tag_groups/address，預覽、不寫入）。
若帶 `gateway_id`，會比對該 Gateway 的結構快取（`kepware_structure`，需先呼叫
`POST /api/kw/gateways/{id}/sync` 同步過）並回傳每筆的 `validation` 狀態。  
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "ids": null,
  "tb_status": "pending",
  "gateway_id": 1
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| ids | list? | null | 指定暫存表 id 清單；null = 全部符合條件的資料 |
| tb_status | string? | "pending" | 篩選 Step 3 建點狀態（null 不篩選） |
| gateway_id | int? | null | 指定則比對結構快取，回傳 `validation` |

**Response 200**

```json
{
  "data": [
    {
      "id": 1,
      "tag_name": "K8_2F_CHS_TAG001",
      "tb_type": "K8CHS-CHS-2F-CHS",
      "channel_name": "K8CHS",
      "device_name": "CHS",
      "tag_groups": "2F.CHS",
      "address": "",
      "description": "K8_2F_CHS TAG001",
      "driver_type": "OPC",
      "profile_exists": true,
      "overridden": false,
      "validation": {
        "channel_exists": true,
        "device_exists": true,
        "group_exists": false,
        "group_auto_create": true
      }
    }
  ],
  "total": 245
}
```

| validation 欄位 | 說明 |
|-----------------|------|
| group_exists=false 且 group_auto_create=true | 對應網頁 UI 的 `+G` 標記，執行時會自動建立 |
| channel_exists=false 或 device_exists=false | 對應網頁 UI 的 `▲` 標記，**不會自動建立**，需先在 Kepware 手動建好 |

**curl 範例**

```bash
curl -X POST http://localhost:9000/api/kw/derive \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tb_status":"pending","gateway_id":1}'
```

---

### 步驟 3：執行 Kepware 建點（背景任務）

#### POST /api/kw/execute

推導欄位後呼叫 Kepware API Gateway 建立 tag group + tag，成功後更新 `tb_status = done`。
**回傳 `{"task_id": ...}`，請依上方「背景任務與輪詢」章節輪詢結果，不要只看 HTTP 200。**  
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "gateway_id": 1,
  "ids": null,
  "delay": 1.1,
  "batch_size": 50,
  "batch_pause": 5.0
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| gateway_id | int? | null | 使用 DB 中已設定的 Gateway（`kepware_gateway` 表，含加密密碼）；**建議用這個，與網頁 UI 行為一致** |
| kw_gw_url / kw_gw_username / kw_gw_password | string? | null | 不指定 `gateway_id` 時，改用這三個手動帶入憑證（不會存進 DB）；**兩者擇一，優先用 gateway_id** |
| ids | list? | null | 指定暫存表 id；null = 全部 `tb_status=pending` |
| delay | float | **1.1** | 每筆間隔秒數。Kepware API Gateway 限速為滑動視窗 60 次/分鐘（per 帳號+路徑），1.1 秒 ≈ 每分鐘 48 筆留有餘裕；若 Gateway 端調高限速可調低加速 |
| batch_size | int | 50 | 批次大小（達到後暫停 `batch_pause`） |
| batch_pause | float | 5.0 | 批次暫停秒數；收到 429 時優先讀取 Kepware 回應的 `Retry-After` 決定實際等待秒數，讀不到才用此值，最多重試 3 次 |

**Response 200（立即回傳，非最終結果）**

```json
{ "task_id": "abc12345" }
```

**輪詢 `GET /api/tasks/abc12345` 取得的 `summary`（`done:true` 之後）**

```json
{
  "total": 245, "success": 240, "fail": 5, "skip": 0,
  "errors": [
    { "name": "K8_2F_CHS_TAG099", "reason": "HTTP 409: ..." }
  ],
  "message": "成功建立 240 筆 Tag"
}
```

**curl 範例**

```bash
TASK_ID=$(curl -s -X POST http://localhost:9000/api/kw/execute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":1}' | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

# 務必輪詢，不要只看上面這個 200
curl http://localhost:9000/api/tasks/$TASK_ID -H "Authorization: Bearer $TOKEN"
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

### 步驟 5（執行）：設定 Kepware Scale（背景任務）

#### POST /api/pg/execute/scale

推導欄位後呼叫 Kepware API Gateway 設定 Scale，成功後更新 `scale_status = done`。
**回傳 `{"task_id": ...}`，請依上方「背景任務與輪詢」章節輪詢結果，不要只看 HTTP 200。**
> 這是最容易踩坑的端點：舊版文件曾記載此端點直接同步回傳 `{success, failed, ...}`，
> 但實際上早已改為背景任務，只回傳 `task_id`。若您的程式檢查 `response["success"]`
> 一類的欄位，會因為該 key 不存在而誤判、或直接吞掉 `KeyError`，
> 造成「HTTP 200 但 Kepware 沒有實際套用變更」的假象。  
> **需要 JWT（admin / operator）**

**Request Body**

```json
{
  "gateway_id": 1,
  "ids": null,
  "delay": 1.1,
  "batch_size": 50,
  "batch_pause": 5.0
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| gateway_id | int? | null | 使用 DB 中已設定的 Gateway；**建議用這個，與網頁 UI 行為一致**——若改用手動 `kw_gw_url` 且填錯，會打到錯誤的 Kepware 實例而完全看不出來 |
| kw_gw_url / kw_gw_username / kw_gw_password | string? | null | 不指定 `gateway_id` 時的手動憑證，兩者擇一 |
| ids | list? | null | 指定暫存表 id；null = 全部 `scale_status=pending` |
| delay | float | **1.1** | 同 `/api/kw/execute`，理由同上（60 次/分鐘限速） |
| batch_size | int | 50 | 批次大小 |
| batch_pause | float | 5.0 | 批次暫停秒數；429 時優先讀取 `Retry-After` |

**Response 200（立即回傳，非最終結果——這裡不會有 success/failed！）**

```json
{ "task_id": "def67890" }
```

**輪詢 `GET /api/tasks/def67890` 取得的 `summary`（`done:true` 之後）**

```json
{
  "total": 200, "success": 198, "fail": 2, "skip": 0,
  "errors": [
    { "tag_name": "K8_2F_CHS_TAG099", "reason": "Tag not found in Kepware" }
  ],
  "message": "成功設定 198 筆 Scale"
}
```

**curl 範例**

```bash
TASK_ID=$(curl -s -X POST http://localhost:9000/api/pg/execute/scale \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gateway_id":1}' | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

curl http://localhost:9000/api/tasks/$TASK_ID -H "Authorization: Bearer $TOKEN"
```

---

### 完整流程 curl 腳本範例

```bash
#!/bin/bash
BASE="http://localhost:9000"
GATEWAY_ID=1   # 對應設定頁 > Kepware Gateway 管理裡的 Gateway id

# 輪詢背景任務的共用函式
wait_task() {
  local task_id="$1"
  while true; do
    local resp=$(curl -s "$BASE/api/tasks/$task_id" -H "Authorization: Bearer $TOKEN")
    local done=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['done'])")
    if [ "$done" = "True" ]; then
      echo "$resp" | python3 -m json.tool
      return
    fi
    sleep 1
  done
}

# 1. 登入取得 Token
TOKEN=$(curl -s -X POST $BASE/api/user/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

echo "Token: $TOKEN"

# 2. 上傳 CSV
UPLOAD_ID=$(curl -s -X POST $BASE/api/csv/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@kepware_tags.csv" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['upload_id'])")

echo "Upload ID: $UPLOAD_ID"

# 3. 匯入暫存表
curl -s -X POST $BASE/api/pg/staging/import \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"upload_id\":\"$UPLOAD_ID\"}" | python3 -m json.tool

# 4. 執行 Kepware 建點（背景任務，需輪詢）
TASK_ID=$(curl -s -X POST $BASE/api/kw/execute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"gateway_id\":$GATEWAY_ID}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
wait_task "$TASK_ID"

# 5. 執行 PG 寫入（同步）
curl -s -X POST $BASE/api/pg/execute/pg \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool

# 6. 執行 Scale 設定（背景任務，需輪詢）
TASK_ID=$(curl -s -X POST $BASE/api/pg/execute/scale \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"gateway_id\":$GATEWAY_ID}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
wait_task "$TASK_ID"
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

## 7. Kepware Gateway 管理 + 批次刪除

> Gateway 是 DB 管理的（`kepware_gateway` 表，密碼 Fernet 加密，見 `KW_ENCRYPT_KEY`），
> 可以自由命名、指定多組、其中一組設為 `is_default`。**這裡才是取得 `gateway_id` 的地方**
> ——第 2 節所有 execute 端點的 `gateway_id` 參數，都是指這裡的 id。

### GET /api/kw/gateways

列出所有 Gateway（**密碼不會回傳**）。**需要 JWT**（任何已登入角色）。

**Response 200**

```json
[
  { "id": 1, "name": "K8 廠區 GW", "url": "https://10.11.64.70:57412",
    "username": "admin", "zone": "A", "verify_ssl": true, "is_default": true }
]
```

---

### POST /api/kw/gateways

新增 Gateway。**admin only**

```json
{
  "name": "K8 廠區 GW",
  "url": "https://10.11.64.70:57412",
  "username": "admin",
  "password": "password",
  "verify_ssl": true,
  "zone": "A",
  "is_default": false
}
```

---

### PUT /api/kw/gateways/{gw_id} / DELETE /api/kw/gateways/{gw_id}

更新／刪除 Gateway。**admin only**（`PUT` 的 `password` 欄位留空代表不更新密碼）

---

### POST /api/kw/gateways/{gw_id}/test

測試 Gateway 連線（實際呼叫該 Gateway 的登入 API）。**需要 JWT**（任何已登入角色）。

**Response 200**

```json
{ "success": true, "message": "連線成功" }
```

---

### POST /api/kw/gateways/{gw_id}/sync

同步該 Gateway 的 channel/device/tag_group 結構到 `kepware_structure` 快取表，
供 `/api/kw/derive` 做結構驗證用。**需要 JWT（admin / operator）**

**Response 200**

```json
{ "success": true, "channels": 5, "devices": 12, "groups": 34 }
```

---

### POST /api/kw/delete-batch （背景任務）

透過 Kepware API 批次刪除 Tag（從 CSV 的 `name`/`type` 欄位推導 channel/device/tag_group 路徑）。
**回傳 `{"task_id": ...}`，比照第 2 節「背景任務與輪詢」——不要只看 HTTP 200。**  
**需要 JWT（admin / operator）**

**CSV 欄位**: `name`（tag 名稱）, `type`（DeviceProfile，格式如 `K8CHS-CHS-2F-CHS`）

**Request Body**

```json
{
  "upload_id": "a1b2c3d4",
  "gateway_id": 1,
  "dry_run": true,
  "delay": 1.1,
  "batch_size": 50,
  "batch_pause": 5.0
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| upload_id | string | ✓ | 先呼叫 `POST /api/csv/upload` 取得 |
| gateway_id | int? | null | 同執行建點/Scale，建議用這個而非手動憑證 |
| dry_run | bool | true | 預演模式，不實際呼叫刪除 API |
| delay / batch_size / batch_pause | | 1.1 / 50 / 5.0 | 同執行建點/Scale |

**Response 200（立即回傳，非最終結果）**

```json
{ "task_id": "a3f7b2c1d9e4f580" }
```

**輪詢 `GET /api/tasks/{task_id}` 取得的 `summary`**

```json
{
  "total": 200, "success": 195, "fail": 5, "skip": 0,
  "errors": [{ "name": "TAG001", "reason": "HTTP 404" }]
}
```

---

## 8. 設定管理（Config，legacy）

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
