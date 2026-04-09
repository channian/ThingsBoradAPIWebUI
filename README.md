# Kepware 點位管理系統

基於 ThingsBoard REST API 的 Kepware 裝置點位批次管理 Web 介面。

提供 CSV 批次新增/刪除裝置、即時進度追蹤、操作歷史紀錄、裝置查詢匯出等功能，取代手動透過 ThingsBoard 網頁逐筆操作的流程。

## 功能特色

### 批次新增裝置
- 上傳 CSV 檔案，預覽資料後批次建立裝置到 ThingsBoard
- 支援 DRY RUN 預演模式，確認無誤再正式執行
- CSV 欄位：`name`、`type`、`label`、`description`

### 批次刪除裝置
- 上傳包含裝置名稱的 CSV，批次查詢並刪除
- LIVE 模式執行前強制二次確認，防止誤刪
- CSV 欄位：`name`

### 裝置查詢
- 關鍵字搜尋（名稱 / Type / Label）
- 分頁瀏覽（支援 20/50/100 筆切換）
- 勾選裝置直接刪除
- 匯出全部裝置為 CSV

### 操作歷史紀錄
- 自動記錄每次批次操作結果
- 顯示操作類型、模式、成功/失敗/略過統計
- 下載單次任務結果明細 CSV

### Kepware 匯入（五步驟流程）

CSV → PG 暫存表 → ThingsBoard 建點 → PG 正式表 → Kepware Scale 設定

| 步驟 | 名稱 | 說明 |
|------|------|------|
| Step 1 | CSV 上傳 | 上傳 Kepware 點位 CSV，解析後存入 PG 暫存表 `scada_tag_config` |
| Step 2 | 暫存資料 | 檢視暫存表，可依 `tb_status`/`pg_status`/`scale_status` 篩選，支援勾選刪除 |
| Step 3 | TB 建點 | 從暫存表推導 TB 欄位，批次呼叫 ThingsBoard API 建立裝置，支援限速控制 |
| Step 4 | PG 匯入 | 從暫存表推導 PG 正式欄位，寫入 `tags` 正式表 |
| Step 5 | Scale 設定 | 從暫存表推導 Kepware Tag Scaling，透過 Kepware API Gateway 設定 |

每個步驟的狀態追蹤欄位：`tb_status`、`pg_status`、`scale_status`（pending → done / skip）

### 其他
- 即時進度條 + 串流日誌（SSE）
- 進階流量控制設定（每筆延遲、批次暫停、隨機抖動）
- 429 Too Many Requests 自動處理與重試
- 深色 / 淺色模式切換（自動記憶偏好）
- CSV 範本下載
- 多編碼 CSV 支援（UTF-8-BOM、UTF-8、Big5、GB2312）
- 連線資訊自動記憶（localStorage）

## 技術架構

```
┌─────────────────────────────────────┐
│  瀏覽器 (Vue 3 SPA)                │
│  static/index.html                  │
└──────────┬──────────────────────────┘
           │ HTTP / SSE
┌──────────▼──────────────────────────┐
│  FastAPI Backend (port 9000)        │
│  app/main.py          路由 + API    │
│  app/task_manager.py   背景任務管理  │
│  app/tb_client.py      TB API 封裝  │
│  app/pg_client.py      PG 資料操作  │
│  app/kw_gw_client.py   Kepware GW   │
│  app/config_manager.py 設定管理      │
└──────┬──────────┬──────────┬────────┘
       │ REST API │ SQL      │ REST API
┌──────▼──────┐┌──▼────────┐┌▼───────────────┐
│ ThingsBoard ││ PostgreSQL││ Kepware API GW  │
│ (CE)        ││           ││                 │
└─────────────┘└───────────┘└─────────────────┘
```

| 層級 | 技術 |
|------|------|
| 前端 | Vue 3（本地載入，支援離線環境） |
| 後端 | Python FastAPI + Uvicorn |
| 即時通訊 | Server-Sent Events (SSE) |
| 資料儲存 | PostgreSQL（正式資料）+ `data/config.json`（TB 直接加點設定）+ `data/history.json`（操作歷史） |

## 快速開始

### 環境需求

- Python 3.8+
- 可連線的 ThingsBoard 伺服器

### Windows 一鍵啟動

```bat
start.bat
```

首次執行會自動建立虛擬環境並安裝相依套件。

### 手動啟動

```bash
# 建立虛擬環境
python -m venv .venv

# 啟用虛擬環境
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 安裝相依套件
pip install -r requirements.txt

# 啟動伺服器
python run.py
```

啟動後開啟瀏覽器前往 `http://localhost:9000`。

### 離線安裝（企業內網）

在有網路的電腦上先下載套件：

```bash
pip download -r requirements.txt -d ./packages
```

將整個專案資料夾複製到目標伺服器，再執行：

```bash
pip install --no-index --find-links=./packages -r requirements.txt
```

## 設定

複製 `.env.example` 為 `.env` 修改預設值（選用）：

```env
# ThingsBoard 預設連線資訊（可在網頁上覆蓋）
TB_URL=http://10.11.64.64:8082
TB_USERNAME=tenant@thingsboard.org
TB_PASSWORD=

# PostgreSQL 連線設定
PG_HOST=10.11.64.11
PG_PORT=5432
PG_DATABASE=your_db_name
PG_USER=your_username
PG_PASSWORD=your_password

# Kepware API Gateway 連線設定（可在網頁上覆蓋）
KW_GW_URL=http://localhost:8000
KW_GW_USERNAME=admin
KW_GW_PASSWORD=

# Web 伺服器設定
WEB_HOST=0.0.0.0
WEB_PORT=9000
```

> 連線資訊也可以直接在網頁介面上填寫，會自動記憶。

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/api/auth/login` | 測試 ThingsBoard 連線 |
| `POST` | `/api/csv/upload` | 上傳 CSV 並預覽 |
| `POST` | `/api/tasks/execute` | 建立批次任務（新增/刪除） |
| `GET` | `/api/tasks/{id}/stream` | SSE 即時進度串流 |
| `GET` | `/api/tasks/{id}/export` | 下載任務結果 CSV |
| `POST` | `/api/devices/query` | 查詢裝置列表 |
| `POST` | `/api/devices/delete-direct` | 直接刪除指定裝置 |
| `POST` | `/api/devices/export` | 匯出全部裝置 CSV |
| `GET` | `/api/history` | 取得操作歷史 |
| `DELETE` | `/api/history` | 清除歷史紀錄 |
| `GET` | `/api/templates/{type}` | 下載 CSV 範本（create/delete） |

## CSV 格式

### 新增用 CSV

```csv
name,type,label,description
Channel1.Device1.Tag1,Kepware,溫度感測器,一樓溫度
Channel1.Device1.Tag2,Kepware,濕度感測器,一樓濕度
```

### 刪除用 CSV

```csv
name
Channel1.Device1.Tag1
Channel1.Device1.Tag2
```

> 支援 UTF-8（含 BOM）、Big5、GB2312 編碼。可從網頁下載 CSV 範本。

## 映射規則

系統中有兩套獨立的映射/設定機制，分別用於不同功能：

### 設定儲存位置總覽

| 設定項 | 儲存位置 | 用途 |
|--------|----------|------|
| 下拉選項管理 | `data/config.json`（本地檔案） | TB 直接加點的表單下拉選項 |
| 預設值 | `data/config.json`（本地檔案） | TB 直接加點的欄位預設值 |
| 映射規則 | `data/config.json`（本地檔案） | TB 直接加點的前綴→值對應 |
| PG 參照表 | PostgreSQL `*_config` 表 | Kepware 匯入 Step 3/4/5 推導用 |
| DeviceProfile | TB API → 同步到 PG `tb_device_profile` 表 | Kepware 匯入 推導 `tb_type` 和 `tabname` |

### TB 直接加點（config.json）

設定頁的「下拉選項管理」、「預設值」、「映射規則」三個子頁面，資料存於 `data/config.json`。
僅用於「TB 直接加點」和「批次刪除」的手動操作表單，與 Kepware 匯入流程無關。

### Kepware 匯入推導規則（PG 參照表）

Kepware 匯入的 Step 3 ~ Step 5 使用 PG 參照表進行欄位推導，設定頁的「PG 參照表」子頁面可管理。

#### PG 參照表

| 表名 | 用途 | 欄位 |
|------|------|------|
| `location_config` | 廠區→BU/Zone 對應 | `bu`, `site`, `zone` |
| `ownership_config` | Owner→Department 對應 | `data_owner`, `department` |
| `device_config` | I/O Device→Driver Type 對應 | `device_name`, `driver_type`, `site`, `system_code` |
| `system_config` | System Code 名稱管理 | `system_code`, `system_name` |
| `tb_device_profile` | DeviceProfile→tabname 對應 | `name`, `description` |

#### Step 3: TB 建點推導（derive_tb_fields）

從暫存表推導 ThingsBoard 裝置欄位：

| TB 欄位 | 推導邏輯 |
|---------|----------|
| `name` | 直接使用 CSV 的 `tag_name` |
| `type` (DeviceProfile) | 優先用 CSV `device_profile`，否則推導為 `{nodename}-{system}-{floor}-{system}` |
| `label` | 直接使用 CSV 的 `tag_name` |
| `description` | 直接使用 CSV 的 `description` |

**nodename 推導規則**：
1. 優先使用 CSV 的 `scada_node_name`（去掉底線 `_`）
2. 無 `scada_node_name` 時，使用 `site` + `system_code` 拼接
3. 如果 `driver_type` 為 IFIX 且 nodename 不以 IFIX 結尾，自動加上 `IFIX` 後綴

**system 推導規則**：優先使用 CSV 的 `system_code`，否則從 `tag_name` 以 `_` 分割取第三段

**floor 推導規則**：從 `tag_name` 以 `_` 分割取第二段

**driver_type 推導規則**：
1. 查 `device_config` 表的 `device_name` 對應 `driver_type`
2. 查不到時，若 `io_device` 為 `OPC` 或 `IGS` → `OPC`，否則原值

#### Step 4: PG 正式表推導（derive_pg_fields）

從暫存表推導 `tags` 正式表欄位：

| tags 欄位 | 推導邏輯 |
|-----------|----------|
| `tagname` | CSV `tag_name` |
| `description` | CSV `description` |
| `node_name` | CSV `scada_node_name` |
| `driver_type` | 查 `device_config` 表，OPC/IGS → OPC |
| `address` | CSV `io_address` |
| `tablename` | 查 `tb_device_profile` 的 `description`，fallback 為 `{BU}_{site}_{system}` |
| `zone` | 查 `location_config` 表（by site） |
| `bu` | 查 `location_config` 表（by site） |
| `site` | CSV `site` |
| `floor` | `tag_name` 分割取第二段 |
| `system` | 優先 CSV `system_code`，fallback `tag_name` 第三段 |
| `owner` | CSV `data_owner` |
| `department` | 查 `ownership_config` 表（by data_owner） |
| `data_type` | 固定 `float` |
| `created_date` | 寫入時自動填入 `NOW()` |

#### Step 5: Kepware Scale 推導（derive_scale_fields）

從暫存表推導 Kepware API Gateway 的 Tag Scaling 設定：

**Kepware 路徑拆解**（從 `tb_type` / DeviceProfile）：

```
tb_type = "K8CHS-CHS-2F-CHS"
         ├─ channel_name = "K8CHS"
         ├─ device_name  = "CHS"
         └─ tag_groups   = "2F.CHS"

full_tag_name = "{tag_groups}.{tag_name}"  → API 自動拆分 group/tag
```

**Scale 判斷邏輯**：

| CSV `scale_enabled` | `scaling_type` | `data_type` | 額外欄位 |
|---------------------|----------------|-------------|----------|
| YES（且有完整 raw/scaled 範圍） | 1 (Linear) | 8 (Float) | `scaling_raw_low/high`, `scaling_scaled_low/high`, `scaling_clamp_low=no`, `scaling_clamp_high=no` |
| YES（缺少範圍值） | 0 (None) | 8 (Float) | — |
| NO / 空值 | 0 (None) | 8 (Float) | — |

**Kepware API 呼叫方式**：`PUT /api/config/tags` with Bearer token

## 專案結構

```
ThingsBoradAPIWebUI/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 路由、CSV 解析、匯入流程
│   ├── task_manager.py    # 背景任務執行、批次新增/刪除邏輯
│   ├── tb_client.py       # ThingsBoard REST API 封裝
│   ├── pg_client.py       # PostgreSQL 操作（暫存表/正式表/參照表/推導）
│   ├── kw_gw_client.py    # Kepware API Gateway 封裝（登入/Scale 設定）
│   └── config_manager.py  # config.json 設定管理
├── static/
│   ├── index.html         # Vue 3 單頁應用（完整前端）
│   └── vue.global.prod.js # Vue 3.5.13（本地載入）
├── data/                  # 執行時自動建立
│   ├── history.json       # 操作歷史紀錄
│   ├── config.json        # TB 直接加點設定（下拉/預設/映射）
│   └── csv_uploads/       # CSV 暫存檔案（file-based，重啟不遺失）
├── .env.example           # 環境變數範本
├── .gitignore
├── requirements.txt
├── run.py                 # 啟動入口
├── start.bat              # Windows 一鍵啟動腳本
└── README.md
```

## 相依套件

| 套件 | 版本 | 用途 |
|------|------|------|
| FastAPI | 0.115.6 | Web 框架 |
| Uvicorn | 0.32.1 | ASGI 伺服器 |
| python-multipart | 0.0.18 | 檔案上傳處理 |
| Requests | 2.32.3 | ThingsBoard API 呼叫 |
| python-dotenv | 1.0.1 | 環境變數載入 |

## 遠端存取

如需從其他電腦存取，在 Windows Server 上開放防火牆：

```powershell
New-NetFirewallRule -DisplayName "Kepware Web UI" -Direction Inbound -Port 9000 -Protocol TCP -Action Allow
```

然後透過 `http://<伺服器IP>:9000` 存取。

---

## 舊版桌面應用程式（參考）

> 以下記錄的是本專案 Web 版開發前的桌面版本架構，使用 CustomTkinter + DuckDB 實作。
> 目前已由 Web 版取代，保留此文件作為後續 PostgreSQL 整合的設計參考。

### 桌面版架構

```
Desktop Version (已停用)
├── tag_manager_v3.py      # 資料層：DuckDB CRUD + CSV 匯入匯出
├── tag_manager_ui.py      # 主視窗：左側選單 + 頁面切換框架
├── import_page.py         # 匯入頁面：四步驟匯入流程
├── settings_page.py       # 設定頁面：下拉選單/預設值/DB 設定
├── config_manager.py      # 設定管理：config.json 讀寫
├── data_validator.py      # 資料驗證：CSV 格式/欄位/地址檢查
├── address_page.py        # Address 管理頁面（未提供）
└── config.json            # 設定檔：下拉選單選項/預設值/DB路徑
```

| 層級 | 技術 |
|------|------|
| 前端 | CustomTkinter（Python 桌面 GUI） |
| 資料庫 | DuckDB（本地嵌入式） |
| 設定 | config.json |

### 資料庫結構（DuckDB）

桌面版使用三張資料表：

**tags 主表** — 儲存所有 Kepware 點位資訊

| 欄位 | 型別 | 說明 |
|------|------|------|
| tag_id | INTEGER PK | 自動遞增 |
| tagname | VARCHAR UNIQUE | 點位名稱（必填） |
| description | VARCHAR | 描述 |
| node_name | VARCHAR | 節點名稱 |
| driver_type | VARCHAR | PLC/OPC 類型 |
| address | VARCHAR | PLC 位址 |
| tabname | VARCHAR | 頁籤名稱 |
| zone | VARCHAR | 區域 |
| bu | VARCHAR | 事業單位 |
| site | VARCHAR | 廠區 |
| floor | VARCHAR | 樓層 |
| owner | VARCHAR | 負責人 |
| department | VARCHAR | 部門 |
| data_type | VARCHAR | 資料類型 |
| created_date | TIMESTAMP | 建立時間 |
| updated_date | TIMESTAMP | 更新時間 |

**projects 專案表**

| 欄位 | 型別 | 說明 |
|------|------|------|
| project_id | INTEGER PK | 自動遞增 |
| project_name | VARCHAR UNIQUE | 專案名稱 |
| created_date | TIMESTAMP | 建立時間 |

**tag_projects 關聯表**（多對多）

| 欄位 | 型別 | 說明 |
|------|------|------|
| tag_id | INTEGER FK | 關聯 tags |
| project_id | INTEGER FK | 關聯 projects |
| date | DATE | 關聯日期 |

### 匯入流程（四步驟）

桌面版的匯入頁面實作了完整的四步驟流程，後續整合至 Web 版時可參考：

1. **選擇 CSV** — 瀏覽檔案 + 自動觸發 DataValidator 驗證
2. **檢查重複** — 比對 DB 中現有 tagname，列出重複項目與所屬專案
3. **分配 Address** — PLC 點位自動分配可用的 D/E 系列位址（支援預覽/正式執行）
4. **匯入資料庫** — 指定專案名稱、重複處理策略（skip 略過 / update 更新）

### 設定管理（config.json）

桌面版透過 `config.json` 集中管理所有可設定項目：

**下拉選單選項（dropdown_options）**

管理 12 個欄位的下拉選單：

| 欄位 | 範例選項 |
|------|----------|
| driver_type | Zone1_PLC, Zone2_PLC, OPC, Modbus |
| node_name | Node1, Node2, Node3 |
| zone | ZoneA, ZoneB, ZoneC, ZoneD |
| bu | BU1, BU2, BU3 |
| site | Site1, Site2, Site3 |
| floor | 1F, 2F, 3F, B1, B2 |
| owner | Ray, John, Mary, Tom |
| department | Engineering, Production, Maintenance, QA |
| data_type | Float, Bool, Word, Short, Long, DWord, BCD, LBCD |
| system | SCADA, MES, DCS, PLC |
| tabname | Main, Alarm, Trend, Control |
| memory_type | D, E |

**預設值（defaults）** — 各欄位新增時自動填入的預設值

**資料庫設定（database）** — DB 路徑、自動備份開關、備份保留天數

### 資料驗證（DataValidator）

匯入前自動執行五項驗證：

| 檢查項目 | 類型 | 說明 |
|----------|------|------|
| 必填欄位 | Error | tagname 必須存在 |
| tagname 檢查 | Error | 空值、CSV 內部重複、特殊字元 |
| 選項值檢查 | Warning | 值是否在 config.json 允許的下拉選單範圍內 |
| Address 格式 | Error | PLC 位址格式驗證（D/E 系列、偶數規則、範圍檢查） |
| DataType-Address 對應 | Error | Bool 必須用 Bit 地址（D00000.00）、非 Bool 不可用 Bit 地址 |

### Web 版整合規劃

桌面版功能整合至 Web 版的進度：

| 桌面版功能 | Web 版對應 | 狀態 |
|------------|------------|------|
| CSV → ThingsBoard 批次操作 | `app/main.py` + `app/task_manager.py` | 已完成 |
| DuckDB 主檔管理 | PostgreSQL `tags` 正式表 + `scada_tag_config` 暫存表 | 已完成 |
| config.json 設定管理 | Web API + 設定頁面（下拉/預設/映射） | 已完成 |
| 下拉選單管理 UI | 設定 > 下拉選項管理 | 已完成 |
| Kepware 匯入流程 | 五步驟流程（CSV→暫存→TB→PG→Scale） | 已完成 |
| PG 參照表管理 | 設定 > PG 參照表（Location/Ownership/Device/System/TB Profile） | 已完成 |
| DeviceProfile 同步 | 設定 > DeviceProfile（一鍵從 TB 同步到 PG） | 已完成 |
| Kepware Scale 設定 | Kepware API Gateway 整合 | 已完成 |
| DataValidator 驗證 | 預計整合至 CSV 上傳流程 | 規劃中 |
| Address 自動分配 | 預計整合至匯入流程 | 規劃中 |
