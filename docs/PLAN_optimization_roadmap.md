# KIS v3 優化調整計畫書（模型交接文件）

> **文件目的**：本文件為 AI 助理交接文件。由 Fable 5 於 2026-07-03 完成全專案盤點後撰寫，
> 供後續模型（Sonnet / Opus 等）依此持續優化。每完成一項請在對應項目打勾並註記 commit hash，
> 讓下一個接手的 session 能快速掌握進度。
>
> **開始工作前必讀**：`CLAUDE.md`（專案總覽 + 舊命名詞彙表）、`docs/FLOW_kepware_import.md`（匯入流程規格）。

---

## 一、現況總覽

| 項目 | 狀態 |
|------|------|
| 後端 | FastAPI 單體 `app/main.py`（1873 行，67 個路由），port 9000 |
| 前端 | Vue 3 無建置流程：`static/index.html`（1030 行）+ `static/js/app.js`（943 行，單一 setup()） |
| 資料庫 | PostgreSQL（暫存表 `scada_tag_config` 需預先存在）+ Collector DB |
| 測試 | **無任何測試**，全靠手動 UI 驗證 |
| 部署 | `python run.py`（uvicorn hot-reload）；Windows `start.bat` |

**功能面已完成**：6 步匯入流程、多 Gateway 管理（Fernet 加密）、結構同步驗證、批次刪除（含限速 UI + CSV 範例）、Tags 多條件查詢/匯出、IO Mapping、Step 3 Channel/Device/Groups 覆寫（批次儲存按鈕）、暫存表勾選刪除/清空、PG 自動連線測試、JWT 三角色權限、操作日誌。

**環境限制備註**：雲端開發沙箱**無法連 PostgreSQL 與 Kepware**，只能做語法檢查
（`node --check static/js/app.js`、`python -c "import ast; ast.parse(...)"`）。
實際行為需請使用者在 `10.10.51.81:9087` 環境驗證。修改 Python 後**必須重啟伺服器**
（`nohup python run.py >> /tmp/server.log 2>&1 &`），hot-reload 不一定會生效。

---

## 二、問題盤點（附證據位置）

### 🔴 P0-1：Step 3 / Step 5 執行時凍結整個伺服器（最嚴重）

`/api/kw/execute`（main.py:1356）與 `/api/pg/execute/scale`（main.py:1579）是
**`async def` handler 內直接呼叫 `time.sleep()`**（main.py:1433、1436、1638、1641）。
`time.sleep` 會阻塞 uvicorn 的 event loop——批次建點跑幾分鐘，**期間所有使用者的所有請求全部卡死**
（包含登入、查詢、甚至靜態檔案）。

對照組：批次刪除（main.py:880 `/api/kw/delete-batch`）已正確使用 `task_manager.py`
背景執行緒 + 前端輪詢 `/api/tasks/{id}`（app.js `executeDelete` 的 poll 迴圈），是現成的正確範本。

**修法（擇一，建議 A）**：
- **A（正解）**：將兩個 execute 遷移到 `task_manager` 背景任務，回傳 `task_id`，
  前端改成與 `executeDelete` 相同的輪詢模式（可直接複用該 poll 邏輯，抽成共用函式）。
  順帶解決「長請求被 proxy 斷線、結果遺失」的問題，還能顯示進度。
- **B（止血）**：把 `async def` 改成 `def`（FastAPI 會丟進 threadpool），一行修掉阻塞。
  若時程緊可先做 B，之後再做 A。

**驗收**：執行 500 筆建點期間，另開視窗操作查詢分頁應完全不卡。

### 🔴 P0-2：大量端點無認證保護

以下端點**完全沒有** `Depends`（對照 main.py 路由表逐一確認過）：

| 端點 | 行號 | 風險 |
|------|------|------|
| `POST /api/csv/upload` | 598 | 未登入可上傳、寫檔到 `data/csv_uploads/`（磁碟塞爆向量） |
| `GET/DELETE /api/history` | 687/692 | 未登入可**清空**歷史 |
| `GET /api/tasks/{id}` | 698 | 任務內容外洩 |
| `POST /api/pg/staging/query` | 1089 | 未登入可讀暫存表 |
| `POST /api/pg/staging/status` | 1167 | 未登入可**改寫狀態**（mutating！） |
| `GET/POST /api/pg/ref/*` + `POST /api/pg/ref/delete` | 1200–1298 | 未登入可**增刪參照表**（直接影響推導結果！） |
| `/api/config/*`（11 個） | 988–1057 | 遺留端點，UI 已不用，仍可未登入讀寫 |
| `POST /api/kw-gw/test` | 1545 | 可拿伺服器當跳板探測內網 Kepware |
| `GET /api/collector/test` | 1663 | 資訊外洩（`/api/collector/reload` 1675 有保護，test 沒有） |
| `POST /api/io-mapping/execute`、`GET /api/io-mapping/download/{id}` | 1784/1855 | 未登入可用運算資源/下載結果 |
| `GET /api/pg/test`、`GET /api/templates/*` | 1062/714 | 低風險，但一併補上 |

**修法**：讀取類至少加 `Depends(get_current_user)`；mutating 類（staging/status、ref 增刪、history 清除）
加 `require_role("admin", "operator")` 或 `require_admin`（比照既有同類端點的權限層級）。
`/api/config/*` 見 P2-4（建議直接移除）。
**注意**：前端呼叫這些端點的地方（app.js）有些沒帶 `_headers()`（如 `loadHistory`、`loadRefTable`、
`deleteRefRow`、`testPgConn`、`testCollector`），加認證後**必須同步補上 headers**，否則功能會壞。

**驗收**：未帶 token 呼叫上述任一端點回 401；UI 各分頁功能登入後全部正常。

### 🔴 P0-3：安全預設值問題

1. **JWT 預設密鑰**：`auth.py:23` `SECRET_KEY = os.getenv("JWT_SECRET_KEY", "kepitsimple-default-secret-change-me")`。
   預設值是公開的（在 git 裡），拿到原始碼即可偽造任意 admin token。
   **修法**：未設定環境變數時，啟動即產生隨機密鑰並在 log 大聲警告（token 會隨重啟失效，屬可接受），
   或直接拒絕啟動並提示設定 `JWT_SECRET_KEY`。
2. **Token 走 query string**：`auth.py:92` 接受 `?token=`，會被 access log / proxy log 記錄。
   確認前端只有下載類需要（目前 `downloadIoMapping` 用 `window.open` 且該端點無認證）；
   收斂為「僅下載端點接受 query token」或改用一次性下載 ticket。
3. **登入無防暴力破解**：`/api/user/login`（main.py:464）無限次嘗試。
   加簡單的 in-memory 計數器：同帳號/IP 5 次失敗鎖 5 分鐘，失敗寫入操作日誌。

### 🟠 P1-1：`page_size=9999` 靜默截斷

main.py:1362、1586（以及 derive 端點同模式）用 `get_staging_list(page=0, page_size=9999)` 撈 pending。
**超過 9999 筆時默默漏掉**，且一次全載記憶體。修法：pg_client 加 `get_staging_all(status_filter)`
不分頁版本（或 `page_size=None` 語意），execute 迴圈內分批撈。

### 🟠 P1-2：`data/csv_uploads/` 無清理機制

上傳檔永久累積（main.py:117–128 只有 put/get，無 TTL）。已有週期性日誌清理排程
（main.py 啟動時註冊「每週一凌晨 3 點」），**掛進同一排程**：刪除超過 N 天（建議 30）的 upload JSON；
另在 upload 端點加單檔大小上限（如 10MB / 50,000 列）。

### 🟠 P1-3：PG 連線無 pool

`pg_client.py:31` 每個操作開新連線。目前規模可接受（已有 connect_timeout），
但 execute 迴圈中每次 `update_staging_status` 都重連。
**低優先**：改用 `psycopg2.pool.SimpleConnectionPool`（min=1, max=5），注意 thread 安全
（task_manager 背景執行緒也會用）。P0-1 做完後再評估。

### 🟠 P1-4：前端 `window.fetch` monkey-patch

app.js:50 覆寫全域 fetch 處理 401。可運作但脆弱（影響第三方腳本、疊加覆寫）。
改成顯式 `_apiFetch()` wrapper（自動帶 `_headers()` + 統一 401/錯誤處理），全檔替換呼叫點。
順帶統一「`if (!resp.ok) { const e = await resp.json(); throw ... }`」這段在 app.js 重複 20+ 次的樣板。

### 🟠 P1-5：Step 3/5 限速參數前端寫死

app.js `executeKw` / `executeScale` 寫死 `delay: 0.2, batch_size: 50, batch_pause: 5`。
刪除分頁已有限速設定 UI（delState.delay/batchSize/batchPause），比照抽成共用元件/區塊加到 Step 3 與 Step 5。
（若 P0-1 選方案 A，一起做最省事。）

### 🟡 P2-1：零測試

建議引入 pytest，**從純邏輯開始**（不需要 DB）：
1. `pg_client.py` 的 `_derive_base_fields`（~1112 行）——推導規則是本專案核心且規則繁多
   （nodename fallback 鏈、BF→B1F、IFIX 分支、tb_type 拆解），最容易回歸也最值得測。
   現在它依賴查表結果傳入，把 ref 資料做成 fixture 即可離線測。
2. `auth.py` token 產生/驗證/過期/竄改。
3. CSV 解析（編碼偵測、BOM、欄位別名）。
4. 之後：FastAPI TestClient + 假 pg_client 測權限矩陣（每個端點 × 三角色 × 未登入）。
   加 `requirements-dev.txt`（pytest）；在 README/CLAUDE.md 記錄 `pytest` 指令。

### 🟡 P2-2：main.py 單檔 1873 行

拆成 APIRouter 模組：`routers/auth_users.py`、`routers/gateways.py`、`routers/staging.py`、
`routers/derive_execute.py`、`routers/ref_tables.py`、`routers/iomapping.py`、`routers/misc.py`。
**純搬移不改邏輯**，一次一個 router 一個 commit，方便驗證。共用的 request models 移到 `app/schemas.py`，
`_resolve_gw_credentials` / `_decrypt_gw_password` / CSV store 移到 `app/deps.py` 或各自模組。

### 🟡 P2-3：app.js 單檔 943 行

無建置流程是刻意設計（**不要引入 npm/vite，除非使用者同意**）。可接受的整理方式：
拆成多個 plain script（`static/js/modules/auth.js`、`import-flow.js`、`query.js`…），
每個檔案輸出一個 `window.KIS.xxx = function(ctx){...}` 工廠，`app.js` 的 setup() 內組裝。
效益中等、風險中等——**優先級低於後端拆分**，若做請務必逐分頁手動驗證。

### 🟡 P2-4：遺留死代碼清理

| 項目 | 證據 | 動作 |
|------|------|------|
| `static/css/style.css`（287 行） | index.html 只引 v3.css，全案無引用 | 刪除（連同 `.claude/skills/check-css` 中的提及一併更新） |
| `app/config_manager.py` + `/api/config/*` 11 個端點 | CLAUDE.md 已標記 legacy，v3 UI 不呼叫 | 與使用者確認後移除；若保留則補 admin 認證（見 P0-2） |
| `data/config.json` | 同上 | 隨 config_manager 一併處理 |
| 假聊天機器人（index.html chat widget + app.js `_chatReplies`） | 寫死 4 組關鍵字回覆 | 問使用者：接真 LLM API、或標示「離線 FAQ」、或移除 |

**禁止**：不要重命名 `tb_status`/`tb_type`/`tb_device_profile` 等 DB 欄位/表（CLAUDE.md 詞彙表有完整說明，
改名要動 DB migration，風險高收益低）。

### 🟡 P2-5：操作一致性小項

- `except (UnicodeDecodeError, Exception)`（main.py:615）→ `except Exception` 即可。
- 錯誤處理慣例：凡 route 內會呼叫 `_decrypt_gw_password`/`_resolve_gw_credentials` 者，
  generic handler 前要有 `except HTTPException: raise`（CLAUDE.md 慣例，新端點常漏）。
- 暫存表勾選刪除的「全選」只選當頁 50 筆（行為合理，但 UI 可加註「僅本頁」）。
- `loadStaging` 失敗只 console.error（app.js），改成顯示錯誤橫幅（PG 斷線時使用者只看到空表）。

### 🟢 P3：功能候選（先問使用者再做）

1. Step 3/5 執行進度條（P0-1 方案 A 完成後，task 已有 progress 資料，前端顯示即可）。
2. 查詢分頁：常用條件儲存、結果欄位挑選、依欄位排序。
3. 暫存表：單筆欄位 inline 編輯（比照 Step 3 覆寫模式）。
4. 匯入報告下載（每步驟成功/失敗明細 CSV——task_manager 已有 `results` 與 `add_result`，缺匯出端點）。
5. 使用者停用（is_active）切換 UI（後端 PUT /api/admin/users/{id} 已存在）。
6. Gateway 連線狀態實際偵測（topbar 的 ONLINE/OFFLINE 目前依賴 `selectedGw.status`，確認資料來源是否即時）。

---

## 三、建議執行順序（Phase 規劃）

```
Phase 1「止血」（P0，建議一個 session 內完成）
  1.1 P0-1 方案 B：async def → def（2 行，先解凍結）      [S]
  1.2 P0-2：補齊全部端點認證 + 前端補 headers               [M]
  1.3 P0-3：JWT 密鑰啟動檢查 + 登入鎖定 + query token 收斂   [S]
  → 交付使用者驗證一輪

Phase 2「補強」
  2.1 P0-1 方案 A：execute 遷移 task_manager + 前端輪詢+進度 [L]
  2.2 P1-5：Step 3/5 限速 UI（與 2.1 同批做）               [S]
  2.3 P1-1：page_size=9999 截斷                            [S]
  2.4 P1-2：csv_uploads 清理排程 + 上傳上限                 [S]
  → 交付使用者驗證一輪

Phase 3「體質」
  3.1 P2-1：pytest + 推導規則測試（最優先的測試標的）        [M]
  3.2 P2-2：main.py 拆 router（一 router 一 commit）        [L]
  3.3 P1-4：_apiFetch wrapper 統一前端請求                   [M]
  3.4 P2-4：死代碼清理（先與使用者確認 config/chat 去留）    [S]

Phase 4「打磨」：P2-5 小項 + P3 依使用者需求挑選
```

尺寸：S ≈ 半小時內、M ≈ 一至數小時、L ≈ 需要跨多次驗證。

---

## 四、接手模型執行守則

1. **語言**：後端註解與 UI 文字一律繁體中文；與使用者對話用繁體中文。
2. **驗證限制**：沙箱無 PG/Kepware。每次改完：跑 `node --check` / `ast.parse` 語法檢查 →
   commit + push → 請使用者在他的環境測試。**改 Python 要提醒使用者重啟伺服器**。
3. **瀏覽器快取**：使用者曾遇過舊 app.js 被快取（伺服器停掉時）。若使用者回報「改了沒反應」，
   先確認伺服器有跑、再請他 Ctrl+Shift+R。
4. **Git**：一律開發並推送到 `claude/kepware-web-interface-l3WXm`；一個主題一個 commit；
   未經要求不開 PR。
5. **不做的事**：不引入前端建置流程；不改 tb_* 命名；不自行 `CREATE TABLE scada_tag_config`
   （它必須預先存在，程式只 ALTER 補欄位）；不在未告知的情況下刪除使用者資料。
6. **每完成一項**：回到本文件對應項目打勾（`- [x]`）並附 commit hash，維持交接鏈完整。

---

## 五、進度追蹤

- [x] P0-1B 解除 event loop 阻塞（kw/execute、pg/execute/scale 改同步 def）— commit `e84ce8f`
- [x] P0-2 端點認證補齊（65 路由稽核，僅 / 與 login 公開）— commit `e84ce8f`
- [x] P0-3 JWT 密鑰 / 登入鎖定 / query token 移除 — commit `e84ce8f`
- [x] P0-1A execute 遷移背景任務 + 進度輪詢 — 後端 commit `2814206`，前端 commit `9d444ae`
- [x] P1-1 page_size 截斷 — commit `2814206`
- [x] P1-2 csv_uploads 清理 — commit `cd6d5ad`
- [ ] P1-4 _apiFetch 統一
- [x] P1-5 Step 3/5 限速 UI — commit `9d444ae`
- [ ] P2-1 pytest + 推導測試
- [ ] P2-2 main.py 拆 router
- [ ] P2-3 app.js 模組化（低優先，需評估）
- [ ] P2-4 死代碼清理（需使用者確認範圍)
- [ ] P2-5 一致性小項
- [ ] P3 功能候選（逐項與使用者確認）

---

## 六、Phase 1 交付紀錄（commit `e84ce8f`，2026-07-03）

**已完成**：P0-1B、P0-2、P0-3 三項（見上方勾選）。

**沙箱已驗證**：Python/JS 語法檢查通過；伺服器乾淨啟動（Application startup complete，
可服務 `/`、CSS、JS）；路由稽核 65 條僅 `/` 與 `/api/user/login` 公開。

**⚠️ 需使用者在正式環境（`10.10.51.81:9087`）驗證的行為**（沙箱無法常駐伺服器做 HTTP 測試）：
1. **重啟伺服器**後（改了 Python），未帶 token 呼叫任一受保護端點應回 **401**。
2. 各分頁登入後功能全部正常（特別是：**歷史**分頁、**設定 > 參照表**讀取/刪除、
   **Collector 測試**、**PG 測試**、**IO Map 下載結果**、**刪除分頁的「下載 CSV 範例」**——
   這些先前沒帶 token，本次已補；若有一個壞掉代表 headers 沒補到）。
3. **登入鎖定**：同帳號連續打錯密碼 5 次後，第 6 次應回 **429**（約 5 分鐘後自動解除）。
4. **JWT 金鑰**：若環境未設 `JWT_SECRET_KEY`，啟動 log 會出現警告且重啟後需重新登入；
   正式環境**務必設定** `JWT_SECRET_KEY`（否則每次重啟所有人被登出）。
5. CSV 上傳、IO Mapping 上傳（multipart）仍正常（改用 `_authHeader()` 只帶 Authorization）。

**下一步**：Phase 2 從 **P0-1A**（execute 遷移 task_manager + 前端輪詢/進度）接續，
與 **P1-5**（Step 3/5 限速 UI）同批做最省事。

---

*盤點基準：commit `9615529`，2026-07-03。Phase 1 完成：commit `e84ce8f`。*
