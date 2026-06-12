# Kepware 建點流程重構計畫書

## 一、目標

將系統從「ThingsBoard + Kepware 混合」轉型為「純 Kepware 路線」，並升級建點流程從「盲推導」進化為「推導 + 比對 + 可編輯確認」。

---

## 二、現況 vs. 新架構

### 現況
```
CSV → 暫存 → 盲推導 → 直接打 Kepware API → PG 匯入 → Scale → Reload
                ↑
          純靠命名規則，無法事前驗證
```

### 新架構
```
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

#### A. Kepware 結構同步

從 Kepware API Gateway 拉取現有的 Channel / Device / Tag Group 樹狀結構，存入 DB。

**新增 DB 表：`kepware_structure`**
```
id          SERIAL PRIMARY KEY
channel     VARCHAR(255) NOT NULL
device      VARCHAR(255)
tag_group   VARCHAR(500)          -- 完整路徑，如 "2F.CHS"
synced_at   TIMESTAMP DEFAULT NOW()
UNIQUE(channel, device, tag_group)
```

**新增 API：**
| Method | Path | 說明 |
|--------|------|------|
| POST | `/api/kw/sync-structure` | 呼叫 Kepware API 拉取結構並寫入 DB |
| GET | `/api/kw/structure` | 取得已快取的結構（前端用） |

**同步時機：**
- 使用者進入 Step 3 時，頁面頂部有「同步 Kepware 結構」按鈕
- 同步完成顯示：X 個 Channel、Y 個 Device、Z 個 Group

#### B. 推導 + 比對

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
- `channel_exists`：DB 中有此 channel
- `device_exists`：DB 中有此 channel + device 組合
- `group_exists`：DB 中有完整的 group 路徑
- `group_auto_create`：channel + device 存在但 group 不存在 → 建議自動建立

#### C. 可編輯預覽

Step 3 預覽表格中，以下欄位可直接編輯：

| 欄位 | 編輯方式 | 說明 |
|------|---------|------|
| `channel_name` | 下拉選單（從快取載入）+ 可輸入 | 推導結果預填，使用者可修正 |
| `device_name` | 下拉選單（依選擇的 channel 篩選）+ 可輸入 | 連動 channel |
| `tag_groups` | 文字輸入 | 用 `.` 分隔，如 `2F.CHS` |

其餘欄位（tag_name、address、description）為唯讀顯示。

#### D. 推導邏輯優化

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
┌─────────────────────────────────────────────────────────────┐
│ Kepware 建點推導預覽                                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─ Kepware Gateway 連線 ────────────────────────────────┐  │
│  │ [GW URL        ] [帳號    ] [密碼    ] [測試連線]  ●已連線 │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─ Kepware 結構快取 ────────────────────────────────────┐  │
│  │ [同步 Kepware 結構]  最後同步: 2026-06-12 17:00       │  │
│  │ 已快取: 5 Channels, 12 Devices, 28 Groups            │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  [重新推導]                                                  │
│                                                             │
│  ┌─ 推導結果（可編輯）──────────────────────────────────────┐  │
│  │                                                         │
│  │  驗證摘要：                                              │
│  │  ● 12 筆路徑完全匹配  ● 5 筆需新建 Group  ▲ 2 筆 Channel 不存在 │
│  │                                                         │
│  │  ┌─────────────────────────────────────────────────┐    │
│  │  │Tag Name │Channel▼│Device▼│Groups   │Address│Desc│狀態│ │
│  │  ├─────────┼────────┼───────┼─────────┼───────┼────┼───┤ │
│  │  │K8_2F_.. │[K8CHS▼]│[CHS▼] │[2F.CHS ]│ns=2;..│...│ ✓ │ │
│  │  │K8_3F_.. │[K8CHS▼]│[CHS▼] │[3F.CHS ]│ns=2;..│...│ +G│ │
│  │  │XX_1F_.. │[XXABC▼]│[PMS▼] │[1F.PMS ]│ns=2;..│...│ ▲ │ │
│  │  └─────────────────────────────────────────────────┘    │
│  │                                                         │
│  │  狀態圖例：                                              │
│  │  ✓ = 路徑已存在    +G = 將自動建立 Group                   │
│  │  ▲ = Channel/Device 不存在（需確認）                       │
│  │                                                         │
│  │  [▼] = 下拉選單（從 Kepware 快取載入，可手動輸入新值）        │
│  │  Groups 欄位為文字輸入，用 . 分隔層級                       │
│  └─────────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─ 限速設定 ─────────────────────────────────────────────┐  │
│  │ 單筆延遲: [0.2]s  批次大小: [50]  批次暫停: [5]s        │  │
│  │ ☐ Kepware 建點 + PG 匯入一次完成                        │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  [執行 Kepware 建點]                                        │
│  ✓ 成功建立 17 筆 Tag                                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 4.4「批次刪除」頁面

```
┌─────────────────────────────────────────────────────────────┐
│ Kepware Tag 批次刪除                                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─ Kepware Gateway 連線 ────────────────────────────────┐  │
│  │ [GW URL        ] [帳號    ] [密碼    ] [測試連線]  ●已連線 │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─ 上傳刪除清單 ────────────────────────────────────────┐  │
│  │ 拖曳 CSV 至此，或點擊選擇（需包含 name + type 欄位）    │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─ 預覽 ───────────────────────────────────────────────┐  │
│  │ Tag Name        │ Channel │ Device │ Groups           │  │
│  │ K8_2F_CHS_AHU01 │ K8CHS   │ CHS    │ 2F.CHS          │  │
│  │ ...              │         │        │                  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ⚠ 此操作將直接從 Kepware 刪除 Tag，無法復原！                 │
│  [執行刪除 (18 筆)]                                         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 五、新增 API 端點

| Method | Path | 說明 |
|--------|------|------|
| POST | `/api/kw/sync-structure` | 從 Kepware API 拉取 channel/device/group 寫入 DB |
| GET | `/api/kw/structure` | 取得快取的結構樹（前端下拉選單用） |
| POST | `/api/kw/derive` | 推導 + 比對（原 `/api/pg/derive/tb`） |
| POST | `/api/kw/execute` | 執行建點（原 `/api/pg/execute/tb`） |
| POST | `/api/kw/delete-batch` | 批次刪除（已有，搬移路由） |

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
                    ┌─────────────┐
                    │ Kepware API │
                    │   Gateway   │
                    └──────┬──────┘
                           │ POST /api/kw/sync-structure
                           ▼
                ┌──────────────────┐
                │kepware_structure │
                │  (DB 快取表)      │
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
                                    │ ensure_tag_groups  │
                                    │ + create_tag       │
                                    └─────────┬─────────┘
                                              │
                              ┌────────────────┼───────────────┐
                              ▼                ▼               ▼
                    ┌──────────────┐  ┌──────────────┐  ┌────────────┐
                    │ PG 正式表     │  │ Collector DB  │  │ Scale 設定  │
                    │ + Collector   │  │   tags 表     │  │            │
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
| Phase 2 | 推導邏輯優化 | 抽出 `_derive_base_fields` 共用函式 |
| Phase 3 | Kepware 結構同步 | 新增 DB 表 + 同步 API + `kw_gw_client.py` 列舉方法 |
| Phase 4 | 推導比對 | derive 結果加入 validation 狀態 |
| Phase 5 | 可編輯預覽 | 前端 Step 3 表格改為可編輯 |
| Phase 6 | 批次刪除獨立 | 搬出為一級分頁 |
