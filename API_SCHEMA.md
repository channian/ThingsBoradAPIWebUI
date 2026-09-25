# Kep It Simple · Kepware 點位管理系統 — API Schema 參考文件

> 純 Kepware 架構，**不再整合 ThingsBoard**（見 `CLAUDE.md`）。本文件已於
> 2026-09 再次全面校正並同步至 commit `4f3422c`，補上四個未同步文件即
> 上線、曾造成「HTTP 200 但實際沒生效」事故的行為變更：
> - 全新端點 `POST /api/pg/staging/kw-override`（先前完全沒有文件）
> - `POST /api/kw/execute`、`POST /api/pg/execute/scale`、
>   `POST /api/kw/delete-batch` 新增的 **Gateway 併發鎖（409）**
> - `POST /api/pg/derive/scale` / `POST /api/pg/execute/scale` 的
>   `scale_error`／`not_built_count`／`skip` 行為
> - CSV 匯入表頭不分大小寫、上傳大小/筆數上限、Collector 寫入結果欄位
>
> （上一次全面校正在 2026-07，移除已不存在的 TB 端點，並補上當時未記載的
> `/api/kw/delete-batch`、`/api/kw/gateways`。）

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

#### Gateway 併發鎖（HTTP 409）

`POST /api/kw/execute`、`POST /api/pg/execute/scale`、`POST /api/kw/delete-batch`
這三個端點，**同一個 Gateway（以 `gateway_id` 識別；未帶 `gateway_id` 時以手動輸入的
`kw_gw_url` 識別）同一時間只允許一個背景任務在跑**。若該 Gateway 已有任務執行中，
呼叫會立即收到：

```
HTTP 409
{
  "detail": "此 Gateway 目前有任務執行中（task_id=abc12345），請輪詢該任務直到完成後再重試"
}
```

**理由**：Kepware API Gateway 的限速是滑動視窗 60 次/分鐘（per 帳號+路徑），建點／Scale／
刪除三個操作打的是同一組路徑、共用同一額度桶；若允許併發，只會讓實際呼叫速率疊加、更快撞
到限速（429），並不會讓整體跑得更快，所以用「直接拒絕」取代「排隊等待」，讓呼叫端自行決定
何時重試。

**不同 Gateway 之間互不影響、可以並行執行**（例如多網段、多台 Gateway 部署時，對 GW-A 執行
建點的同時，可以對 GW-B 執行 Scale，兩者不會互相鎖住）。

呼叫端正確處理方式：收到 409 → 從 `detail` 字串中解析出佔用中的 `task_id` → 輪詢
`GET /api/tasks/{task_id}` 直到該任務 `done: true` → 再重送原本的請求。

**Python 輪詢範例**（三個背景任務端點通用，含 409 併發鎖處理）：

```python
import time, requests

def run_and_wait(url, body, headers, poll_interval=1, timeout=600,
                 retry_wait=2, max_busy_retries=5):
    """送出背景任務請求並等待完成，回傳 summary（請務必檢查其中的 fail / errors）。

    遇到 409（同一 Gateway 已有任務執行中）會等佔用中的任務結束後重送，
    最多重送 max_busy_retries 次；任何一次等待超過 timeout 秒即拋出 TimeoutError。
    ⚠️ 不要寫成無上限的重試迴圈：Gateway 上若有任務卡住，程式會永遠等下去且毫無錯誤訊息。
    """
    base = url.rsplit('/api/', 1)[0]

    def wait_task(task_id):
        """輪詢直到 done 並回傳 summary；逾時拋 TimeoutError。
        查無任務（404，例如伺服器重啟後記憶體中的任務已消失）回傳 None。"""
        waited = 0
        while waited < timeout:
            r = requests.get(f"{base}/api/tasks/{task_id}", headers=headers)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            st = r.json()
            if st["done"]:
                return st["summary"]
            time.sleep(poll_interval)
            waited += poll_interval
        raise TimeoutError(f"task {task_id} 超過 {timeout} 秒仍未完成")

    for _ in range(max_busy_retries + 1):
        r = requests.post(url, headers=headers, json=body)
        if r.status_code == 409:
            # detail 範例："此 Gateway 目前有任務執行中（task_id=abc12345），請輪詢…"
            detail = r.json()["detail"]
            busy_task_id = detail.split("task_id=")[1].split("）")[0].split(")")[0]
            wait_task(busy_task_id)   # 佔用任務已結束或已不存在 → 重送；逾時則拋出
            time.sleep(retry_wait)    # 讓 Gateway 限速視窗稍微釋放
            continue

        r.raise_for_status()
        task_id = r.json()["task_id"]
        summary = wait_task(task_id)
        if summary is None:
            raise RuntimeError(f"task {task_id} 已遺失（伺服器可能重啟），請至網頁「歷史」分頁確認結果")
        return summary

    raise RuntimeError(f"Gateway 持續忙碌，重送 {max_busy_retries} 次後仍無法執行")
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

**上傳限制**（超過回 **HTTP 400**）：

| 限制 | 環境變數 | 預設值 |
|------|---------|--------|
| 單檔大小上限 | `CSV_UPLOAD_MAX_BYTES` | `10485760`（10MB）——超過回 `檔案大小超過上限（10MB），請分批上傳` |
| 資料筆數上限 | `CSV_UPLOAD_MAX_ROWS` | `50000`——超過回 `資料筆數超過上限（50000 筆），請分批上傳` |

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

將已上傳的 CSV 資料匯入 PG `scada_tag_config` 暫存表，以 `tag_name` 的 UNIQUE 限制搭配
`INSERT ... ON CONFLICT (tag_name) DO NOTHING` 去重。  
**需要 JWT（admin / operator）**

**CSV 表頭比對不分大小寫**：欄位比對先 `strip()` 再轉小寫比對，因此 `I/O Address`、
`io address`、`I/O ADDRESS` 都會對到 `io_address`（表頭大小寫變體先前會被靜默當成空欄位，
是 2026-07 那次 address 空白事故的根因，已修正）。各欄位接受的別名（snake_case 或
Excel 風格，擇一符合即可，皆不分大小寫）：

| snake_case | Excel 風格別名 |
|-----------|----------------|
| `tag_name` | `Tag Name` |
| `site` | `Site` |
| `system_code` | `System` |
| `scada_node_name` | `SCADA Node Name` |
| `io_device` | `I/O DEVICE` |
| `io_address` | `I/O ADDRESS` |
| `scale_enabled` | `SCALE Enabled` |
| `raw_low` / `raw_high` | `Raw Low` / `Raw High` |
| `scaled_low` / `scaled_high` | `Scaled Low` / `Scaled High` |
| `description` | `Description` |
| `project_name` | `專案名稱` |
| `data_owner` | `DataOwner` |
| `device_profile` | （同名） |
| `scan_group` | `Scan Group` |

**Request Body**

```json
{ "upload_id": "a1b2c3d4" }
```

**Response 200（有跳過時）**

```json
{
  "inserted": 240,
  "skipped": 5,
  "errors": [],
  "total": 245,
  "message": "新增 240 筆；5 筆因 tag_name 已存在（或 tag_name 空白）而跳過——既有資料內容不會被更新，如需修正請先刪除該批暫存資料後重新匯入"
}
```

**Response 200（全數成功、無跳過時）**

```json
{
  "inserted": 245,
  "skipped": 0,
  "errors": [],
  "total": 245,
  "message": "新增 245 筆"
}
```

| 欄位 | 說明 |
|------|------|
| inserted | 成功新增筆數（`ON CONFLICT DO NOTHING` 實際新插入的列數） |
| skipped | 跳過筆數：`tag_name` 已存在於暫存表，**或** `tag_name` 為空白。**`ON CONFLICT DO NOTHING` 語意是永遠跳過、不會更新既有列**——重複匯入同一批 CSV（即使其他欄位內容已修正過）不會覆蓋原本已在庫裡的髒資料，必須先刪除該批暫存資料（`POST /api/pg/staging/delete` 或 `DELETE /api/pg/staging/clear`）再重新匯入 |
| errors | 資料預處理階段失敗的錯誤清單 `[{"tag_name": "...", "reason": "..."}]`（注意鍵名是 `tag_name`，不是 `tagname`）；若整批 INSERT 失敗（如欄位型態不合），會出現單一項 `{"tag_name": "BULK_INSERT", "reason": "..."}` |
| message | `skipped > 0` 時會明確警示既有資料不會被更新，需先刪除再重新匯入；`skipped == 0` 時只回報新增筆數。**前端 Step 1 會以警示底色顯示此訊息**，呼叫端也應該檢查此欄位，不要只看 `inserted`/`skipped` 數字 |

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
      "data_type": 8,
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

| 欄位 | 說明 |
|------|------|
| data_type | Kepware tag 資料型別 enum（整數）。暫存列有手動覆寫（`kw_data_type` 非 NULL）就用覆寫值，否則預設 **8（Float）**——與是否需要 Scale 無關，任何 tag 都需要正確型別。enum 對照表見下方「步驟 3（可選）：手動覆寫」章節 |
| overridden | 是否曾手動覆寫過（供 UI 標示）。條件：`channel`/`device` 覆寫值存在且與推導值不同、或 `tag_groups` 覆寫值（可為空字串）與推導值不同、或 `data_type` 有覆寫（`kw_data_type` 非 NULL，即使覆寫值恰好等於預設 8 也算） |

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

### 步驟 3（可選）：手動覆寫 Kepware 建點欄位

#### POST /api/pg/staging/kw-override

儲存 Step 3 手動覆寫的 Channel / Device / Groups / Data Type 到暫存表
（`scada_tag_config` 的 `kw_channel` / `kw_device` / `kw_tag_groups` / `kw_data_type` 欄位），
下次 `/api/kw/derive`、`/api/pg/derive/scale` 會優先採用覆寫值而非推導值。
**需要 JWT（admin / operator）**

**Request Body**

```json
{
  "id": 1,
  "channel": "K8CHS",
  "device": "CHS",
  "tag_groups": "2F.CHS",
  "data_type": 8
}
```

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| id | int | ✓ | 暫存表 `scada_tag_config.id` |
| channel | string | `""` | 覆寫的 Channel 名稱；空字串會存成 **NULL**（清除覆寫、回到推導值） |
| device | string | `""` | 覆寫的 Device 名稱；空字串同樣存成 **NULL**（清除覆寫） |
| tag_groups | string | `""` | 覆寫的 Tag Groups 路徑；**空字串會被存成空字串（不是 NULL）**——見下方警語 |
| data_type | int \| null | `null` | Kepware data_type enum；`null` 或省略會清除覆寫（回到預設 Float=8），有值則存整數 |

**Response 200**

```json
{ "success": true }
```

失敗（找不到該筆暫存資料）回 **404**：`{"detail": "找不到暫存列 id=999"}`

> ⚠️ **這是整筆覆寫（full replace），不是只改單一欄位——四個欄位每次呼叫都會一起被設定**，
> 語意務必先弄清楚再呼叫：
> - `channel` / `device`：空字串 → 存 NULL（清除覆寫，回到推導值）
> - `tag_groups`：**空字串會被存成空字串本身，不是 NULL**；而推導邏輯（`derive_kw_fields` /
>   `derive_scale_fields`）只要 `kw_tag_groups` 非 NULL 就會採用它，**因此空字串等於把該 tag
>   覆寫到 device 根層級（不在任何 group 之下）**，不是「不覆寫」
> - `data_type`：`null`（或省略）→ 清除覆寫、回到預設 Float(8)；帶整數 → 存該型別
>
> **後果**：呼叫端若只想改其中一個欄位（例如只想改 data_type，送
> `{"id":5,"data_type":1}`），channel/device/tag_groups 會被一併帶成空字串／清空——
> **等於連帶把該筆 tag_groups 覆寫成 device 根層級、並清除既有的 channel/device 覆寫**，
> 不會停留在「原本的推導值」。
>
> **正確用法**：呼叫前先用 `POST /api/kw/derive` 取得該筆目前實際生效的
> `channel_name` / `device_name` / `tag_groups` / `data_type`（覆寫值優先、否則為推導值），
> **四個欄位全部帶上目前值**，只改動想改的那個欄位再送出。網頁 UI（`saveAllKwOverrides`）
> 正是這樣做的——每次都把 `kwDerive.data` 該列目前顯示的四個欄位整包送出。

**Kepware data_type enum 對照表**（`static/js/app.js` `KW_DATA_TYPES`）：

| 值 | 型別 | 值 | 型別 | 值 | 型別 |
|----|------|----|------|----|------|
| 1 | Boolean | 6 | Long | 11 | BCD |
| 2 | Char | 7 | DWord | 12 | LBCD |
| 3 | Byte | 8 | **Float（預設）** | 13 | Date |
| 4 | Short | 9 | Double | 14 | LLong |
| 5 | Word | 10 | String | 15 | QWord |

實務上 9 成點位皆為 Float（8），因此後端 `KW_DATA_TYPE_DEFAULT = 8` 為未覆寫時的預設值。

**curl 範例**（正確用法：四個欄位皆帶上目前值）

```bash
# 1. 先查目前實際生效的值
curl -s -X POST http://localhost:9000/api/kw/derive \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ids":[5],"tb_status":null}' | python3 -m json.tool
# 假設回傳 channel_name=K8CHS, device_name=CHS, tag_groups=2F.CHS, data_type=8

# 2. 只想把 data_type 改成 1（Boolean），channel/device/tag_groups 仍要帶目前值
curl -X POST http://localhost:9000/api/pg/staging/kw-override \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id":5,"channel":"K8CHS","device":"CHS","tag_groups":"2F.CHS","data_type":1}'
```

---

### 步驟 3：執行 Kepware 建點（背景任務）

#### POST /api/kw/execute

推導欄位後呼叫 Kepware API Gateway 建立 tag group + tag，成功後更新 `tb_status = done`。
**回傳 `{"task_id": ...}`，請依上方「背景任務與輪詢」章節輪詢結果，不要只看 HTTP 200。**
**同一 Gateway 若已有任務在跑會回 409，見上方「Gateway 併發鎖（HTTP 409）」。**  
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

**Request Body**（結構同 `/api/kw/derive`，但用 `pg_status` 篩選而非 `tb_status`）

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
      "tabname": "",
      "zone": "A",
      "bu": "CIM",
      "site": "K8",
      "floor": "2F",
      "system": "CHS",
      "owner": "",
      "department": "Facilities",
      "data_type": "float",
      "scan_group": "",
      "tag_address": ""
    }
  ],
  "total": 245
}
```

> 注意：實際回傳的欄位名稱是 **`tabname`**（不是 `tablename`；`tablename` 是寫入 PG 正式表
> `tags` 資料表後、該資料表自己的欄位名稱）；`data_type` 目前固定回傳字串 `"float"`（小寫，
> 不受 Step 3 的 `kw_data_type` 覆寫影響——這支端點推導的是 PG 正式表欄位，不是 Kepware tag
> 型別）；另外還有 `scan_group`、`tag_address`（給 Collector DB 用）。

---

### 步驟 4（執行）：寫入 PG 正式表

#### POST /api/pg/execute/pg

推導欄位後寫入 `tags` 正式表 + Collector DB `tags` 表（若有設定 `COLLECTOR_DB_DATABASE`），
成功後更新 `pg_status = done`。**這是同步端點，不是背景任務**——呼叫會等到正式表（與
Collector，若有）都寫完才回應，不會回傳 `task_id`，不需要輪詢。  
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
  "total": 245,
  "new_tagnames": ["K8_2F_CHS_TAG001", "..."],
  "message": "成功寫入 238 筆至正式表，Collector 235 筆",
  "collector_inserted": 235,
  "collector_errors": [
    { "tagname": "K8_2F_CHS_TAG050", "reason": "value too long for type character varying(50)" }
  ]
}
```

| 欄位 | 說明 |
|------|------|
| inserted / skipped / errors | PG 正式表 `tags` 的寫入結果（`errors`: `[{"tagname","reason"}]`，逐筆 SAVEPOINT，單筆錯誤不影響其餘筆） |
| new_tagnames | 本次「新插入」（非 `ON CONFLICT DO UPDATE` 更新既有列）的 tagname 清單 |
| collector_inserted | 只在有設定 `COLLECTOR_DB_DATABASE` 時才出現：Collector DB `tags` 表成功寫入筆數 |
| collector_errors | 同上，Collector 端逐筆錯誤 `[{"tagname","reason"}]` |
| message | Collector 部分失敗時會附註「該批 pg_status 維持 pending，修正後可重新執行」——失敗的那幾筆 `pg_status` 不會被標記為 `done`，可直接修正資料後重跑本端點 |

> 若 Collector DB 寫入整批失敗（例如連線失敗，而非個別筆錯誤），會回 **HTTP 500** 並
> **自動回滾本次新插入到 PG 正式表的資料**（只刪 `new_tagnames` 中的、不影響原本已存在
> 只是被更新的列），訊息為 `Collector DB 寫入失敗（已回滾本次新增的正式表資料）: ...`。

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

**Response 200（正常有效點位）**

```json
{
  "data": [
    {
      "id": 1,
      "tag_name": "K8_2F_CHS_TAG001",
      "tb_type": "K8CHS-CHS-2F-CHS",
      "full_tag_name": "2F.CHS.K8_2F_CHS_TAG001",
      "channel_name": "K8CHS",
      "device_name": "CHS",
      "scale_enabled": true,
      "data_type": 8,
      "scale_error": null,
      "scaling_type": 1,
      "scaling_raw_low": 0.0,
      "scaling_raw_high": 4000.0,
      "scaling_scaled_low": 0.0,
      "scaling_scaled_high": 100.0,
      "scaling_clamp_low": false,
      "scaling_clamp_high": false,
      "scaling_scaled_data_type": 8
    }
  ],
  "total": 200,
  "not_built_count": 3
}
```

**Response 200（`scale_enabled=true` 但缺範圍值——資料不全，僅預覽提示，仍會在清單中）**

```json
{
  "id": 2,
  "tag_name": "K8_2F_CHS_TAG050",
  "channel_name": "K8CHS",
  "device_name": "CHS",
  "full_tag_name": "2F.CHS.K8_2F_CHS_TAG050",
  "scale_enabled": true,
  "data_type": 8,
  "scaling_type": 0,
  "scale_error": "scale_enabled 為 true 但缺少 scaled_low"
}
```

| 欄位 | 說明 |
|------|------|
| tag_groups / channel_name / device_name / full_tag_name | 與 `/api/kw/derive` 一致：若該筆在 Step 3 有手動覆寫（`kw_channel`/`kw_device`/`kw_tag_groups`），這裡套用的是**覆寫後**的路徑，確保 Step 3 建點路徑與 Step 5 設 Scale 的路徑一致（不會出現「點建在 A 路徑、Scale 卻設到推導出的 B 路徑」） |
| data_type | Kepware tag 資料型別 enum（整數）。套用 Step 3 的覆寫值（`kw_data_type`），未覆寫則預設 8（Float），與 `/api/kw/derive` 的 `data_type` 邏輯一致 |
| scaling_scaled_data_type | **固定回傳 8**，不受上面 `data_type` 覆寫影響——這是 Scale 換算後輸出值的型別，Kepware 固定用 Float 儲存縮放結果，與 tag 本身的原始型別是兩件事 |
| scale_error | `null` 或字串。`scale_enabled=true` 但 `raw_low`/`raw_high`/`scaled_low`/`scaled_high` 任一缺值時，不會再靜默降級為「不設 Scale」，而是回傳此欄位說明缺哪些欄位；此時 `scaling_type` 仍是 0，但**不代表可以安全跳過**——執行時會被判定失敗（見下方執行端點） |
| scale_enabled=false（falsy） | 這是既有正常設計，`scaling_type=0` 且 `scale_error=null`——執行時會被跳過（`skip`），不算錯誤 |
| not_built_count（頂層） | 本批（推導結果的 `data`）中 `tb_status != done` 的筆數。Scale 對 Kepware 而言是**更新**操作（PUT），若該 tag 尚未在 Kepware 建點，執行時必然失敗；此欄位僅供**提示**，不會阻擋推導或執行——合法情境包括「Kepware 上已手動建好點位，只想補 Scale 設定」 |

**Scale 路徑推導說明**

`tb_type` 如 `K8CHS-CHS-2F-CHS` 拆解為：
- channel = `K8CHS`（第 1 段）
- device = `CHS`（第 2 段）
- groups = `2F.CHS`（第 3、4 段，以 `.` 連接）
- full_tag_name = `{groups}.{tag_name}`（若 groups 為空，則 `full_tag_name = tag_name`）

---

### 步驟 5（執行）：設定 Kepware Scale（背景任務）

#### POST /api/pg/execute/scale

推導欄位後呼叫 Kepware API Gateway 設定 Scale，成功後更新 `scale_status = done`。
**回傳 `{"task_id": ...}`，請依上方「背景任務與輪詢」章節輪詢結果，不要只看 HTTP 200。**
**同一 Gateway 若已有任務在跑會回 409，見上方「Gateway 併發鎖（HTTP 409）」。**
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

**執行行為（`_exec_pg_execute_scale`，逐筆判斷，三種結果）**

1. **`scale_error` 非空**（`scale_enabled=true` 但缺範圍值）→ **直接記為失敗，完全不呼叫
   Kepware API**（不浪費限速額度打一個註定失敗的請求）；計入 `summary.errors`
   （`[{"tag_name","reason"}]`，`reason` 就是 `scale_error` 的內容）；該筆 `scale_status`
   **維持 `pending`**，修正暫存資料（補齊範圍值）後可直接重跑本端點。
2. **`scale_enabled=false`**（`scaling_type=0` 且 `scale_error=null`）→ 這是正常情境（該點本
   來就不需要 Scale），**同樣不呼叫 Kepware**，直接把該筆 `scale_status` 標記為 **`skip`**，
   計入 `summary.skip`。
3. 其餘（`scale_enabled=true` 且範圍值齊全）→ 才真正呼叫 Kepware `set_tag_scaling` API；
   成功則 `scale_status = done`、計入 `summary.success`；HTTP 失敗則計入 `summary.errors`、
   `scale_status` 維持 `pending`。

**輪詢 `GET /api/tasks/def67890` 取得的 `summary`（`done:true` 之後）**

```json
{
  "total": 200, "success": 178, "fail": 12, "skip": 10,
  "errors": [
    { "tag_name": "K8_2F_CHS_TAG050", "reason": "scale_enabled 為 true 但缺少 scaled_low" },
    { "tag_name": "K8_2F_CHS_TAG099", "reason": "Tag not found in Kepware" }
  ],
  "message": "成功設定 178 筆 Scale"
}
```

| 欄位 | 說明 |
|------|------|
| success | 成功呼叫 Kepware 並設定 Scale 的筆數，`scale_status` 已更新為 `done` |
| skip | `scale_enabled=false` 而跳過（不需要 Scale）的筆數，`scale_status` 已更新為 `skip`——**不是先前版本文件裡恆為 0 的舊行為** |
| fail | `scale_error`（資料不全）+ 實際呼叫 Kepware 失敗，兩者合計；這些筆 `scale_status` 皆維持 `pending` |
| errors | `[{"tag_name","reason"}]`，資料不全與 Kepware 呼叫失敗混在同一個陣列裡，可用 `reason` 文字（`scale_enabled 為 true 但缺少 ...` 開頭）分辨是否為資料不全 |

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
**同一 Gateway 若已有任務在跑會回 409，見第 2 節「Gateway 併發鎖（HTTP 409）」。**  
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
