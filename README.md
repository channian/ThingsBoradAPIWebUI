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
│  app/main.py        路由 + API      │
│  app/task_manager.py 背景任務管理    │
│  app/tb_client.py    TB API 封裝    │
└──────────┬──────────────────────────┘
           │ REST API
┌──────────▼──────────────────────────┐
│  ThingsBoard (Community Edition)    │
└─────────────────────────────────────┘
```

| 層級 | 技術 |
|------|------|
| 前端 | Vue 3（本地載入，支援離線環境） |
| 後端 | Python FastAPI + Uvicorn |
| 即時通訊 | Server-Sent Events (SSE) |
| 資料儲存 | 歷史紀錄存於 `data/history.json`，無需資料庫 |

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

# Web 伺服器設定
WEB_HOST=0.0.0.0
WEB_PORT=9000
```

> 連線資訊也可以直接在網頁介面上填寫，會自動記憶。

## 專案結構

```
ThingsBoradAPIWebUI/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 路由、CSV 解析、歷史管理
│   ├── task_manager.py    # 背景任務執行、批次新增/刪除邏輯
│   └── tb_client.py       # ThingsBoard REST API 封裝
├── static/
│   ├── index.html         # Vue 3 單頁應用（完整前端）
│   └── vue.global.prod.js # Vue 3.5.13（本地載入）
├── data/                  # 執行時自動建立
│   └── history.json       # 操作歷史紀錄
├── .env.example           # 環境變數範本
├── .gitignore
├── requirements.txt
├── run.py                 # 啟動入口
├── start.bat              # Windows 一鍵啟動腳本
└── README.md
```

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
