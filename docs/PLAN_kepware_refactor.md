# Kepware 建點流程重構計畫書

## 一、目標

將系統從「ThingsBoard + Kepware 混合」轉型為「純 Kepware 路線」，並升級建點流程從「盲推導」進化為「推導 + 比對 + 可編輯確認」。支援多組 Kepware API Gateway 切換。

---

## 二、現況 vs. 新架構

### 現況
```
CSV → 暫存 → 盲推導 → 直接打 Kepware API → PG 匯入 → Scale → Reload
                ↑               ↑
          純靠命名規則      單一 Gateway（手動輸入）
```

### 新架構
```
              ┌──────────────────┐
              │ 多組 Kepware GW   │
              │ (DB 管理，下拉切換) │
              └────────┬─────────┘
                       │ 選擇目標 GW
                       ▼
                  Kepware API
                  ↓ 同步（手動觸發）
            kepware_structure 表
                  ↓ 比對
CSV → 暫存 → 推導 + 驗證 → 可編輯預覽 → 確認執行 → PG 匯入 → Scale → Reload
                            ↑
                  Channel/Device/Groups
                  可在網頁上修正
```

---

## 三、功能範圍

### 3.1 移除項目

| 項目 | 說明 |
|------|------|
| `app/tb_client.py` | ThingsBoard API client 整個移除 |
| `static/js/components/tb-direct.js` | TB 直接新增模組移除 |
| 「TB 直接新增」分頁 | 前端 tab 移除 |
| TB 相關 API 路由 | `/api/tasks/*`、TB device CRUD 路由 |
| TB 相關 Pydantic models | `TaskCreateRequest`、`DeviceQueryRequest` 等 |
| `task_manager.py` 中 TB batch | `execute_batch_create`、`execute_batch_delete` |
| `.env` TB 設定 | `TB_URL`、`TB_USERNAME`、`TB_PASSWORD`、`TB_VERIFY_SSL` 等 |

### 3.2 保留並搬移

| 項目 | 說明 |
|------|------|
| 批次刪除（Kepware API） | 從 `tb-direct.js` 搬出，成為獨立分頁 |

### 3.3 改名（TB → Kepware）

| 現名 | 新名 | 位置 |
|------|------|------|
| `/api/pg/derive/tb` | `/api/kw/derive` | main.py route |
| `/api/pg/execute/tb` | `/api/kw/execute` | main.py route |
| `ExecuteTbRequest` | `ExecuteKwRequest` | main.py model |
| `derive_tb_fields()` | 刪除（死碼） | pg_client.py |
| `deriveTB()` / `executeTB()` | `deriveKW()` / `executeKW()` | import.js |
| `tbDerive` reactive | `kwDerive` | import.js |
| `importSubTab === 'tb'` | `'kw'` | index.html |
| `tb_status` DB 欄位 | **保留不改**（避免 DB migration 風險，UI 標示改為「Kepware 狀態」） |

### 3.4 新增功能

#### A. 多組 Kepware Gateway 管理

支援多組 Kepware API Gateway 設定，存入 DB，使用者透過下拉選單切換。

**新增 DB 表：`kepware_gateway`**
```
id          SERIAL PRIMARY KEY
name        VARCHAR(255) NOT NULL UNIQUE    -- 顯示名稱，如 "K8 廠區 GW"
url         VARCHAR(500) NOT NULL           -- API Gateway URL
username    VARCHAR(255) NOT NULL
password    VARCHAR(255) NOT NULL           -- 加密儲存
verify_ssl  BOOLEAN DEFAULT TRUE
is_default  BOOLEAN DEFAULT FALSE           -- 預設選中的 GW
created_at  TIMESTAMP DEFAULT NOW()
updated_at  TIMESTAMP DEFAULT NOW()
```

**新增 API：**
| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/kw/gateways` | 列出所有 Gateway（密碼不回傳） |
| POST | `/api/kw/gateways` | 新增 Gateway |
| PUT | `/api/kw/gateways/{id}` | 更新 Gateway（密碼空值=不更新） |
| DELETE | `/api/kw/gateways/{id}` | 刪除 Gateway |
| POST | `/api/kw/gateways/{id}/test` | 測試指定 Gateway 連線 |

**使用方式：**
- 所有需要 Kepware GW 的操作（Step 3 建點、Step 5 Scale、批次刪除）改為**下拉選單選擇 Gateway**
- 選擇後自動帶入該 GW 的連線資訊，不再手動輸入 URL/帳號/密碼
- 測試連線改為選中 Gateway 後的一鍵操作

#### B. Kepware 結構同步

從**選定的 Gateway** 拉取 Channel / Device / Tag Group 樹狀結構，存入 DB。

**新增 DB 表：`kepware_structure`**
```
id          SERIAL PRIMARY KEY
gateway_id  INTEGER REFERENCES kepware_gateway(id) ON DELETE CASCADE
channel     VARCHAR(255) NOT NULL
device      VARCHAR(255)
tag_group   VARCHAR(500)          -- 完整路徑，如 "2F.CHS"
synced_at   TIMESTAMP DEFAULT NOW()
UNIQUE(gateway_id, channel, device, tag_group)
```

**新增 API：**
| Method | Path | 說明 |
|--------|------|------|
| POST | `/api/kw/gateways/{id}/sync` | 從指定 GW 拉取結構並寫入 DB |
| GET | `/api/kw/gateways/{id}/structure` | 取得指定 GW 的快取結構 |

**同步時機：**
- 使用者進入 Step 3 時，頁面頂部有「同步 Kepware 結構」按鈕
- 同步完成顯示：X 個 Channel、Y 個 Device、Z 個 Group
- 結構快取**綁定 Gateway**——切換 GW 時載入對應的快取

#### C. 推導 + 比對

`derive_kw_fields()` 在推導完成後，額外比對 `kepware_structure` 表：

每筆推導結果新增驗證狀態欄位：
```json
{
  "tag_name": "K8_2F_CHS_AHU01_SAT",
  "channel_name": "K8CHS",
  "device_name": "CHS",
  "tag_groups": "2F.CHS",
  "address": "ns=2;s=...",
  "description": "...",
  "validation": {
    "channel_exists": true,
    "device_exists": true,
    "group_exists": false,
    "group_auto_create": true
  }
}
```

驗證邏輯：
- `channel_exists`：DB 中有此 channel（對應選定的 gateway_id）
- `device_exists`：DB 中有此 channel + device 組合
- `group_exists`：DB 中有完整的 group 路徑
- `group_auto_create`：channel + device 存在但 group 不存在 → 建議自動建立

#### D. 可編輯預覽

Step 3 預覽表格中，以下欄位可直接編輯：

| 欄位 | 編輯方式 | 說明 |
|------|---------|------|
| `channel_name` | 下拉選單（從快取載入）+ 可輸入新值 | 推導結果預填，使用者可修正 |
| `device_name` | 下拉選單（依選擇的 channel 篩選）+ 可輸入新值 | 連動 channel |
| `tag_groups` | 文字輸入 | 用 `.` 分隔，如 `2F.CHS` |

其餘欄位（tag_name、address、description）為唯讀顯示。

修改後即時重新比對驗證狀態（前端 local 比對，不需再打 API）。

#### E. 推導邏輯優化

將三個 derive 函式（`derive_kw_fields`、`derive_pg_fields`、`derive_scale_fields`）中重複的共用邏輯抽出：

```python
def _derive_base_fields(row, devices_cache, profiles_cache, locations):
    """共用推導：site_prefix, floor, system, driver_type, nodename, tb_type,
    channel, device, tag_groups, address (含前綴)"""
    ...
    return {
        "site_prefix", "floor", "system", "driver_type", "nodename",
        "tb_type", "channel_name", "device_name", "tag_groups",
        "address", "io_address", "profile_exists", "tabname",
    }
```

三個 derive 函式各自只加自己的專屬邏輯：
- `derive_kw_fields`：附加 validation 比對結果
- `derive_pg_fields`：附加 bu/zone/owner/department/scan_group
- `derive_scale_fields`：附加 scaling 參數

---

## 四、前端頁面規劃

### 4.1 主選單（Top-level Tabs）

```
[ Kepware Import ] [ 批次刪除 ] [ 查詢 ] [ 歷史 ] [ 設定 ]
```

- 移除「TB 直接新增」
- 「批次刪除」獨立為一級分頁（從 tb-direct 搬出）

### 4.2 Kepware Import 子步驟（Sub-tabs）

```
[ 1. CSV 上傳 ] [ 2. 暫存資料 ] [ 3. Kepware 建點 ] [ 4. PG 匯入 ] [ 5. Scale 設定 ] [ 6. Collector Reload ]
```

### 4.3 Step 3「Kepware 建點」頁面（重點重新設計）

```
┌─────────────────────────────────────────────────────────────────┐
│ Kepware 建點推導預覽                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─ Kepware Gateway ─────────────────────────────────────────┐  │
│  │ 選擇 Gateway: [ K8 廠區 GW          ▼ ]  [測試連線] ●已連線  │  │
│  │                                                           │  │
│  │ URL: https://10.11.64.50:8080  帳號: admin                │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─ Kepware 結構快取 ────────────────────────────────────────┐  │
│  │ [同步結構]  最後同步: 2026-06-12 17:00                     │  │
│  │ 已快取: 5 Channels, 12 Devices, 28 Groups                │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  [重新推導]                                                      │
│                                                                 │
│  ┌─ 推導結果（可編輯）────────────────────────────────────────┐  │
│  │                                                           │  │
│  │  驗證摘要：                                                │  │
│  │  ● 12 筆完全匹配  ● 5 筆需新建 Group  ▲ 2 筆路徑不存在     │  │
│  │                                                           │  │
│  │  ┌──────────────────────────────────────────────────────┐ │  │
│  │  │Tag Name    │Channel    ▼│Device  ▼│Groups    │Addr │狀態│ │  │
│  │  ├────────────┼───────────┼─────────┼──────────┼─────┼───┤ │  │
│  │  │K8_2F_CHS.. │[ K8CHS  ▼]│[ CHS  ▼]│[ 2F.CHS ]│ns=2 │ ✓ │ │  │
│  │  │K8_3F_CHS.. │[ K8CHS  ▼]│[ CHS  ▼]│[ 3F.CHS ]│ns=2 │ +G│ │  │
│  │  │XX_1F_PMS.. │[ XXABC  ▼]│[ PMS  ▼]│[ 1F.PMS ]│ns=2 │ ▲ │ │  │
│  │  └──────────────────────────────────────────────────────┘ │  │
│  │                                                           │  │
│  │  狀態圖例：                                                │  │
│  │  ✓ = 路徑已存在（Channel + Device + Group 皆在）            │  │
│  │  +G = Channel/Device 存在，Group 將自動建立                 │  │
│  │  ▲ = Channel 或 Device 不存在（需確認或手動修正）            │  │
│  │                                                           │  │
│  │  [▼] = 下拉選單（從 Kepware 快取載入，也可手動輸入新值）      │  │
│  │  Groups 欄位為文字輸入，用 . 分隔層級                        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─ 限速設定 ──────────────────────────────────────────────┐   │
│  │ 單筆延遲: [0.2]s  批次大小: [50]  批次暫停: [5]s         │   │
│  │ ☐ Kepware 建點 + PG 匯入一次完成                         │   │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  [執行 Kepware 建點]                                             │
│  ✓ 成功建立 17 筆 Tag                                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.4「批次刪除」頁面

```
┌─────────────────────────────────────────────────────────────────┐
│ Kepware Tag 批次刪除                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─ Kepware Gateway ─────────────────────────────────────────┐  │
│  │ 選擇 Gateway: [ K8 廠區 GW          ▼ ]  [測試連線] ●已連線  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─ 上傳刪除清單 ────────────────────────────────────────────┐  │
│  │ 拖曳 CSV 至此，或點擊選擇（需包含 name + type 欄位）       │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─ 預覽 ────────────────────────────────────────────────────┐  │
│  │ Tag Name         │ Channel │ Device │ Groups              │  │
│  │ K8_2F_CHS_AHU01  │ K8CHS   │ CHS    │ 2F.CHS             │  │
│  │ ...               │         │        │                    │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ⚠ 此操作將直接從 Kepware 刪除 Tag，無法復原！                      │
│  [執行刪除 (18 筆)]                                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.5 設定頁面 — Kepware Gateway 管理（新增子頁籤）

```
設定 > [ ... 其他子頁籤 ... ] [ Kepware Gateway ]

┌─────────────────────────────────────────────────────────────────┐
│ Kepware Gateway 管理                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─ 已設定的 Gateway ────────────────────────────────────────┐  │
│  │                                                           │  │
│  │  ┌────────────────────────────────────────────────────┐   │  │
│  │  │ 名稱          │ URL                    │ 帳號  │ 狀態 │   │  │
│  │  ├────────────────┼───────────────────────┼───────┼─────┤   │  │
│  │  │ K8 廠區 GW ⭐  │ https://10.11.64.50   │ admin │ [測試]│   │  │
│  │  │ K9 廠區 GW     │ https://10.11.65.50   │ admin │ [測試]│   │  │
│  │  │ 測試環境       │ http://localhost:8000  │ test  │ [測試]│   │  │
│  │  └────────────────────────────────────────────────────┘   │  │
│  │                                                           │  │
│  │  ⭐ = 預設 Gateway                                        │  │
│  │                                                           │  │
│  │  [編輯] [刪除] [設為預設]（每列操作按鈕）                     │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─ 新增 / 編輯 Gateway ─────────────────────────────────────┐  │
│  │                                                           │  │
│  │  名稱:     [ K8 廠區 GW                                  ] │  │
│  │  URL:      [ https://10.11.64.50:8080                    ] │  │
│  │  帳號:     [ admin                                       ] │  │
│  │  密碼:     [ ••••••••                                    ] │  │
│  │  SSL 驗證: [✓]                                            │  │
│  │                                                           │  │
│  │  [測試連線]  [儲存]                                        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.6 共用元件：Gateway 選擇器

所有需要 Kepware GW 的頁面（Step 3、Step 5、批次刪除）使用統一的 Gateway 選擇器元件：

```
┌─ Kepware Gateway ──────────────────────────────────────────┐
│ 選擇 Gateway: [ K8 廠區 GW          ▼ ]  [測試連線] ●已連線  │
│                                                             │
│ URL: https://10.11.64.50:8080  帳號: admin                  │
└─────────────────────────────────────────────────────────────┘
```

行為：
- 下拉選單列出所有已設定的 Gateway，預設選中 `is_default = true` 的
- 選擇後自動載入 URL/帳號（密碼不顯示）
- 「測試連線」使用選中 GW 的完整連線資訊
- 連線狀態同步到所有使用同一 GW 的頁面

---

## 五、新增 API 端點

### Gateway 管理
| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/kw/gateways` | 列出所有 Gateway（密碼不回傳） |
| POST | `/api/kw/gateways` | 新增 Gateway |
| PUT | `/api/kw/gateways/{id}` | 更新 Gateway |
| DELETE | `/api/kw/gateways/{id}` | 刪除 Gateway |
| POST | `/api/kw/gateways/{id}/test` | 測試連線 |

### 結構同步
| Method | Path | 說明 |
|--------|------|------|
| POST | `/api/kw/gateways/{id}/sync` | 從指定 GW 拉取結構寫入 DB |
| GET | `/api/kw/gateways/{id}/structure` | 取得指定 GW 快取結構 |

### 建點 / 刪除（改用 gateway_id 取代手動帶帳密）
| Method | Path | 說明 |
|--------|------|------|
| POST | `/api/kw/derive` | 推導 + 比對（帶 gateway_id） |
| POST | `/api/kw/execute` | 執行建點（帶 gateway_id） |
| POST | `/api/kw/delete-batch` | 批次刪除（帶 gateway_id） |
| POST | `/api/pg/execute/scale` | Scale 設定（帶 gateway_id） |

---

## 六、Kepware API Gateway 需求

結構同步功能需要 Kepware API Gateway 提供以下端點（待確認）：

| 需求 | 預期端點 | 回傳格式 |
|------|---------|---------|
| 列出所有 Channel | `GET /api/config/channels` | `[{name, ...}]` |
| 列出 Channel 下的 Device | `GET /api/config/channels/{ch}/devices` | `[{name, ...}]` |
| 列出 Device 下的 Tag Group | `GET /api/config/tag_groups?channel=...&device=...` | `[{name, path, ...}]` |

> **Action Item**：請提供 Kepware API Gateway 的 Swagger 或 API 文件，確認端點格式。

---

## 七、資料流程圖

```
                ┌──────────────────┐
                │ kepware_gateway  │
                │  (多組 GW 設定)   │──── 設定頁管理
                └────────┬─────────┘
                         │ 選擇目標 GW
                         ▼
                  ┌─────────────┐
                  │ Kepware API │
                  │   Gateway   │
                  └──────┬──────┘
                         │ POST /api/kw/gateways/{id}/sync
                         ▼
              ┌──────────────────┐
              │kepware_structure │
              │(per-GW 快取)     │
              └────────┬─────────┘
                       │ 比對
  ┌──────┐    ┌────────▼─────────┐    ┌──────────────┐
  │ CSV  │───▶│  推導 + 驗證      │───▶│ 可編輯預覽     │
  └──────┘    │ derive_kw_fields │    │ (前端表格)     │
              └──────────────────┘    └───────┬──────┘
                                              │ 使用者確認
                                              ▼
                                  ┌───────────────────┐
                                  │ 執行 Kepware 建點   │
                                  │ (目標 GW)          │
                                  └─────────┬─────────┘
                                            │
                            ┌────────────────┼───────────────┐
                            ▼                ▼               ▼
                  ┌──────────────┐  ┌──────────────┐  ┌────────────┐
                  │ PG 正式表     │  │ Collector DB  │  │ Scale 設定  │
                  │ + Collector   │  │   tags 表     │  │ (目標 GW)  │
                  └──────────────┘  └──────────────┘  └────────────┘
                                                             │
                                                             ▼
                                                    ┌──────────────┐
                                                    │  Collector   │
                                                    │   Reload     │
                                                    └──────────────┘
```

---

## 八、實作順序（建議）

| 順序 | 項目 | 說明 |
|------|------|------|
| Phase 1 | 移除 TB + 改名 | 清理乾淨，確保現有功能不受影響 |
| Phase 2 | 多組 Gateway 管理 | DB 表 + CRUD API + 設定頁面 + 共用選擇器元件 |
| Phase 3 | 推導邏輯優化 | 抽出 `_derive_base_fields` 共用函式 |
| Phase 4 | Kepware 結構同步 | 新增 DB 表 + 同步 API + `kw_gw_client.py` 列舉方法 |
| Phase 5 | 推導比對 | derive 結果加入 validation 狀態 |
| Phase 6 | 可編輯預覽 | 前端 Step 3 表格改為可編輯 |
| Phase 7 | 批次刪除獨立 | 搬出為一級分頁 |

---

## 九、密碼安全

Gateway 密碼存入 DB 時需加密處理：
- 使用 `cryptography.fernet` 對稱加密（非 hash，因為需要還原明文呼叫 API）
- 加密金鑰從環境變數 `KW_ENCRYPT_KEY` 讀取
- API 回傳 Gateway 列表時**不含密碼欄位**
- 更新 Gateway 時，密碼欄位為空代表不更新
