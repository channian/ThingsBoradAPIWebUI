const { createApp, reactive, ref, computed, watch, onMounted, nextTick } = Vue

// 背景任務進度區元件（Step 3 / Step 5 / 批次刪除共用）。純呈現：所有狀態都在 setup() 的 task 物件裡，
// 動作（取消、查看佇列…）以事件交回 setup()。模板在 index.html 的 #task-panel-tpl。
const TaskPanel = {
    props: {
        task: { type: Object, required: true },   // _newTask() 產生的任務狀態
        label: { type: String, default: '' },     // 結果標題用，例如「建點」「Scale 設定」「預演」
        canCancel: { type: Boolean, default: false },
    },
    emits: ['cancel', 'queue', 'resume', 'history'],
    setup(props) {
        const stateText = (g) => {
            if (g.state === 'queued') return g.position ? `排隊中（前面 ${g.position} 個）` : '排隊中'
            if (g.state === 'running') return '執行中'
            if (g.state === 'cancelled') return '已取消'
            if (g.state === 'failed') return '失敗'
            // done：任務被取消且這台還有沒處理完的筆數 → 視為已取消
            if (props.task.cancelled && (g.success + g.fail + g.skip) < g.total) return '已取消'
            return '完成'
        }
        const stateClass = (g) => {
            const t = stateText(g)
            if (g.state === 'queued') return 'pill-warn'
            if (g.state === 'running') return 'pill-info'
            if (g.state === 'failed') return 'pill-err'
            if (t === '已取消') return 'pill-muted'
            return 'pill-ok'
        }
        const pct = (n, total) => (total ? Math.min(100, n / total * 100) : 0)
        // 排隊提示：任何一台還在排隊（送出後第一次輪詢前以送出回應的 queued 為準）
        const queuedNow = computed(() => {
            const t = props.task
            if (t.done) return false
            if (t.gateways.length) return t.gateways.some(g => g.state === 'queued')
            return t.submitQueued
        })
        const excludedText = computed(() => {
            const ex = props.task.excluded
            if (!ex) return ''
            const parts = []
            if (ex.other_gateway) parts.push(`屬於其他 Gateway 的 ${ex.other_gateway} 筆`)
            if (ex.blocked) parts.push(`無法判斷所屬 Gateway 的 ${ex.blocked} 筆`)
            return parts.length ? `已排除（不送出、不計入）：${parts.join('、')}` : ''
        })
        const running = computed(() => !!props.task.id && !props.task.done && !props.task.lost && !props.task.stalled)
        const visible = computed(() => {
            const t = props.task
            return !!(t.submitting || t.id || t.error)
        })
        const errorsShown = computed(() => (props.task.summary?.errors || []).slice(0, 10))
        return { stateText, stateClass, pct, queuedNow, excludedText, running, visible, errorsShown }
    },
    template: '#task-panel-tpl',
}

createApp({
    setup() {
        // ── Auth ──
        const auth = reactive({
            token: localStorage.getItem('kep_token') || '',
            user: localStorage.getItem('kep_user') || '',
            role: localStorage.getItem('kep_role') || '',
            displayName: localStorage.getItem('kep_display') || '',
        })
        const loginForm = reactive({ username: '', password: '', error: '', busy: false })

        function _headers() {
            return auth.token
                ? { 'Authorization': `Bearer ${auth.token}`, 'Content-Type': 'application/json' }
                : { 'Content-Type': 'application/json' }
        }

        // FormData（multipart）專用：只帶 Authorization，讓瀏覽器自行設定 multipart boundary
        function _authHeader() {
            return auth.token ? { 'Authorization': `Bearer ${auth.token}` } : {}
        }

        async function doLogin() {
            loginForm.busy = true; loginForm.error = ''
            try {
                const resp = await fetch('/api/user/login', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username: loginForm.username, password: loginForm.password }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || '登入失敗') }
                const data = await resp.json()
                auth.token = data.token; auth.user = data.username
                auth.role = data.role; auth.displayName = data.display_name || ''
                localStorage.setItem('kep_token', data.token)
                localStorage.setItem('kep_user', data.username)
                localStorage.setItem('kep_role', data.role)
                localStorage.setItem('kep_display', data.display_name || '')
                loginForm.username = ''; loginForm.password = ''
                _initAfterLogin()
            } catch (e) { loginForm.error = e.message }
            finally { loginForm.busy = false }
        }

        function doLogout() {
            auth.token = ''; auth.user = ''; auth.role = ''; auth.displayName = ''
            localStorage.removeItem('kep_token'); localStorage.removeItem('kep_user')
            localStorage.removeItem('kep_role'); localStorage.removeItem('kep_display')
            _resetState()
        }

        // 統一 401 處理：任何 API 回 401（且仍持有 token）視為登入過期，自動登出
        const _nativeFetch = window.fetch.bind(window)
        window.fetch = async (...args) => {
            const resp = await _nativeFetch(...args)
            if (resp.status === 401 && auth.token) {
                doLogout()
            }
            return resp
        }

        // 登出時清空所有記憶體狀態，避免換使用者後殘留前一人資料
        function _resetState() {
            _taskEpoch++          // 讓進行中的任務輪詢迴圈停止，不把舊使用者的任務狀態寫進新工作階段
            gwList.value = []; selectedGwId.value = null
            Object.assign(structureSync, { loading: false, results: [], done: 0, total: 0 })
            closeQueuePanel()
            Object.assign(queuePanel, { data: [], error: '', note: '' })
            Object.assign(routeMap, { list: [], unmappedSites: [], unmappedZones: [], error: '', formError: '', saving: false })
            routeDataVersion.value = 0
            Object.assign(imp, { fileName: null, totalRows: 0, headers: [], preview: [], importing: false, result: null, uploadId: null })
            Object.assign(staging, { data: [], total: 0, page: 0, totalPages: 0, filterTb: '', filterPg: '', filterScale: '', clearing: false, deleting: false, selectedIds: [] })
            Object.assign(kwDerive, { data: [], loading: false, executing: false, savingOverrides: false, task: _newTask(), derivedGw: undefined, derivedVer: 0 })
            Object.assign(pgDerive, { data: [], loading: false, executing: false, result: null })
            Object.assign(scaleDerive, { data: [], loading: false, executing: false, task: _newTask(), notBuiltCount: 0, derivedGw: undefined, derivedVer: 0 })
            Object.assign(collectorState, { testing: false, reloading: false, status: null, message: '' })
            Object.assign(pgConn, { testing: false, status: '', statusText: '未連線' })
            Object.assign(delState, { fileName: null, totalRows: 0, uploadId: null, dryRun: true, executing: false, task: _newTask(), resultDry: true })
            Object.assign(dbQuery, { conditions: [{ field: 'tagname', op: 'contains', value: '' }], logic: 'AND', page: 0, totalPages: 0, total: 0, loading: false, exporting: false, results: [], sqlPreview: '' })
            Object.assign(ioMapping, { fileA: null, fileB: null, executing: false, mappingId: null, result: null, data: [], columns: [] })
            historyList.value = []
            userList.value = []; actLogs.value = []
            Object.keys(refData).forEach(k => delete refData[k]); activeRefTable.value = null
            chat.messages = []; chat.open = false
            importStep.value = 0; activeTab.value = 'import'
        }

        // ── Clock ──
        const clock = ref(new Date().toLocaleTimeString('zh-TW', { hour12: false }))
        setInterval(() => { clock.value = new Date().toLocaleTimeString('zh-TW', { hour12: false }) }, 1000)

        // ── Tabs ──
        const tabs = [
            { id: 'import', label: '匯入', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" style="width:15px;height:15px"><path d="M8 11V3m0 0L5 6m3-3l3 3M3 13h10"/></svg>' },
            { id: 'delete', label: '刪除', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:15px;height:15px"><path d="M3 5h10M6 5V3.5C6 3 6.5 2.5 7 2.5h2c.5 0 1 .5 1 1V5M5 5l.5 8c0 .5.5 1 1 1h3c.5 0 1-.5 1-1L11 5"/></svg>' },
            { id: 'query', label: '查詢', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" style="width:15px;height:15px"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L13.5 13.5"/></svg>' },
            { id: 'iomap', label: 'IO Map', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:15px;height:15px"><circle cx="3.5" cy="4" r="1.5"/><circle cx="3.5" cy="12" r="1.5"/><circle cx="12.5" cy="8" r="1.5"/><path d="M5 4h2.5L11 8M5 12h2.5L11 8"/></svg>' },
            { id: 'history', label: '歷史', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:15px;height:15px"><circle cx="8" cy="8" r="6"/><path d="M8 5v3.5L10.5 10"/></svg>' },
            { id: 'settings', label: '設定', icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" style="width:15px;height:15px"><circle cx="8" cy="8" r="2.2"/><path d="M8 1v2M8 13v2M3.5 3.5l1.5 1.5M11 11l1.5 1.5M1 8h2M13 8h2M3.5 12.5L5 11M11 5l1.5-1.5"/></svg>' },
        ]
        const activeTab = ref('import')

        // ── Gateway Management ──
        const gwList = ref([])
        // 目前選擇的 Gateway（Step 3 / Step 5 / 批次刪除三個分頁共用）：null＝自動（每筆依 channel 與
        // 棟別/園區對應表送往所屬的 Gateway）；有值＝只處理屬於這台的點，其他台的點略過。預設自動。
        const selectedGwId = ref(null)
        const selectedGw = computed(() => gwList.value.find(g => g.id === selectedGwId.value) || null)
        const canOperate = computed(() => auth.role === 'admin' || auth.role === 'operator')
        const gwForm = reactive({
            show: false, editing: null,
            name: '', url: '', username: '', password: '',
            zone: '', verify_ssl: true, is_default: false, error: '',
        })

        async function loadGateways() {
            try {
                const resp = await fetch('/api/kw/gateways', { headers: _headers() })
                if (resp.status === 401) { doLogout(); return }
                if (resp.ok) {
                    gwList.value = await resp.json()
                    // 預設是「自動」，不再自動選 is_default；已選的 Gateway 若被刪除了就退回自動
                    if (selectedGwId.value && !gwList.value.some(g => g.id === selectedGwId.value)) selectedGwId.value = null
                }
            } catch (e) { console.error('載入 Gateway 失敗:', e) }
        }

        async function saveGw() {
            gwForm.error = ''
            if (!gwForm.name || !gwForm.url || !gwForm.username) {
                gwForm.error = '名稱、URL、帳號為必填'; return
            }
            if (!gwForm.editing && !gwForm.password) {
                gwForm.error = '新增時密碼為必填'; return
            }
            try {
                const body = {
                    name: gwForm.name, url: gwForm.url, username: gwForm.username,
                    verify_ssl: gwForm.verify_ssl, zone: gwForm.zone || null,
                    is_default: gwForm.is_default,
                }
                if (gwForm.password) body.password = gwForm.password
                let resp
                if (gwForm.editing) {
                    resp = await fetch(`/api/kw/gateways/${gwForm.editing}`, {
                        method: 'PUT', headers: _headers(), body: JSON.stringify(body),
                    })
                } else {
                    resp = await fetch('/api/kw/gateways', {
                        method: 'POST', headers: _headers(), body: JSON.stringify(body),
                    })
                }
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || '操作失敗') }
                gwForm.show = false
                _resetGwForm()
                await loadGateways()
                routeDataVersion.value++      // Gateway 名稱／網址可能變了，已推導的預覽表提示過期
            } catch (e) { gwForm.error = e.message }
        }

        function editGw(g) {
            gwForm.editing = g.id; gwForm.name = g.name; gwForm.url = g.url
            gwForm.username = g.username; gwForm.password = ''
            gwForm.zone = g.zone || ''; gwForm.verify_ssl = g.verify_ssl
            gwForm.is_default = g.is_default; gwForm.error = ''
            gwForm.show = true
        }

        async function deleteGw(id) {
            if (!confirm('確定刪除此 Gateway？')) return
            try {
                const resp = await fetch(`/api/kw/gateways/${id}`, { method: 'DELETE', headers: _headers() })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                if (selectedGwId.value === id) selectedGwId.value = null
                await loadGateways()
                routeDataVersion.value++
                loadRoutes()      // 指向被刪 Gateway 的對應會連帶刪除
            } catch (e) { alert('刪除失敗: ' + e.message) }
        }

        async function testGw(id) {
            try {
                const resp = await fetch(`/api/kw/gateways/${id}/test`, { method: 'POST', headers: _headers() })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const d = await resp.json()
                alert(d.message || '連線成功')
            } catch (e) { alert('連線失敗: ' + e.message) }
        }

        function _resetGwForm() {
            gwForm.editing = null; gwForm.name = ''; gwForm.url = ''
            gwForm.username = ''; gwForm.password = ''; gwForm.zone = ''
            gwForm.verify_ssl = true; gwForm.is_default = false; gwForm.error = ''
        }

        // ── Structure Sync ──
        // results：最近一次同步每台 Gateway 的結果 [{id, name, ok, channels, devices, groups, error}]；
        // 選定單台＝只同步那台，自動＝依序同步全部 Gateway（失敗不中斷其他台）
        const structureSync = reactive({ loading: false, results: [], done: 0, total: 0 })

        // 路由資料版本：結構同步或對應表變動後 +1，用來判斷已推導的預覽表是否過期（見 kwStale / scaleStale）
        const routeDataVersion = ref(0)

        // 把錯誤回應轉成可讀文字：FastAPI 的 detail 可能是字串（我們自己丟的 HTTPException）
        // 或物件陣列（請求格式驗證 422），兩種都處理；讀不到就退回 HTTP 狀態碼
        async function _respDetail(resp) {
            try {
                const e = await resp.json()
                const d = e && e.detail
                if (typeof d === 'string' && d) return d
                if (Array.isArray(d) && d.length) return d.map(x => x.msg || JSON.stringify(x)).join('；')
                if (d) return JSON.stringify(d)
            } catch (_) { /* 非 JSON 回應 */ }
            return `HTTP ${resp.status}`
        }

        async function _syncOneGateway(g) {
            try {
                const resp = await fetch(`/api/kw/gateways/${g.id}/sync`, { method: 'POST', headers: _headers() })
                if (!resp.ok) throw new Error(await _respDetail(resp))
                const d = await resp.json()
                return { id: g.id, name: g.name, ok: true, channels: d.channels, devices: d.devices, groups: d.groups }
            } catch (e) { return { id: g.id, name: g.name, ok: false, error: e.message } }
        }

        async function syncStructure() {
            const targets = selectedGwId.value ? gwList.value.filter(g => g.id === selectedGwId.value) : gwList.value.slice()
            if (!targets.length) { alert('尚未設定任何 Gateway，請先到「設定」新增'); return }
            structureSync.loading = true; structureSync.results = []
            structureSync.done = 0; structureSync.total = targets.length
            for (const g of targets) {
                if (!auth.token) break          // 中途登出（401）就不再繼續
                structureSync.results.push(await _syncOneGateway(g))
                structureSync.done++
            }
            structureSync.loading = false
            routeDataVersion.value++            // 結構變了，已推導的路由／驗證可能過期
        }

        // ── Import Flow ──
        const importSteps = [
            { label: 'CSV 上傳' },
            { label: '暫存表' },
            { label: 'Kepware 建點' },
            { label: 'PG Import' },
            { label: 'Scale 設定' },
            { label: 'Collector' },
        ]
        const importStep = ref(0)

        // Step 1: CSV Upload
        const imp = reactive({
            fileName: null, totalRows: 0, headers: [], preview: [],
            importing: false, result: null, uploadId: null,
        })

        async function _uploadCSV(file) {
            const form = new FormData()
            form.append('file', file)
            try {
                const resp = await fetch('/api/csv/upload', { method: 'POST', headers: _authHeader(), body: form })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                imp.uploadId = data.upload_id; imp.headers = data.headers
                imp.preview = data.preview; imp.totalRows = data.total_rows
                imp.fileName = data.filename; imp.result = null
            } catch (e) { alert('CSV 上傳失敗: ' + e.message) }
        }

        function handleFile(e) {
            const file = e.target.files[0]
            if (file) _uploadCSV(file)
        }

        function onDrop(e) {
            const file = e.dataTransfer.files[0]
            if (file && file.name.toLowerCase().endsWith('.csv')) _uploadCSV(file)
            else alert('請上傳 .csv 檔案')
        }

        async function importToStaging() {
            if (!imp.uploadId) return
            imp.importing = true
            try {
                const resp = await fetch('/api/pg/staging/import', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ upload_id: imp.uploadId }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                imp.result = await resp.json()
            } catch (e) { alert('匯入失敗: ' + e.message) }
            finally { imp.importing = false }
        }

        function resetUpload() {
            imp.fileName = null; imp.totalRows = 0; imp.headers = []
            imp.preview = []; imp.importing = false; imp.result = null; imp.uploadId = null
        }

        // Step 2: Staging
        const staging = reactive({
            data: [], total: 0, page: 0, totalPages: 0,
            filterTb: '', filterPg: '', filterScale: '',
            clearing: false, deleting: false, selectedIds: [],
        })

        async function clearStaging() {
            if (!confirm('確定要清空整個暫存表？此操作無法復原！')) return
            staging.clearing = true
            try {
                const resp = await fetch('/api/pg/staging/clear', { method: 'DELETE', headers: _headers() })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                await loadStaging()
            } catch (e) { alert('清空失敗: ' + e.message) }
            finally { staging.clearing = false }
        }

        function toggleStagingSelectAll(e) {
            staging.selectedIds = e.target.checked ? staging.data.map(r => r.id) : []
        }

        function toggleStagingRow(id) {
            const idx = staging.selectedIds.indexOf(id)
            if (idx >= 0) staging.selectedIds.splice(idx, 1)
            else staging.selectedIds.push(id)
        }

        async function deleteSelectedStaging() {
            if (!staging.selectedIds.length) return
            if (!confirm(`確定要刪除選取的 ${staging.selectedIds.length} 筆暫存資料？此操作無法復原！`)) return
            staging.deleting = true
            try {
                const resp = await fetch('/api/pg/staging/delete', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ ids: staging.selectedIds }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                staging.selectedIds = []
                await loadStaging()
            } catch (e) { alert('刪除失敗: ' + e.message) }
            finally { staging.deleting = false }
        }

        async function loadStaging() {
            staging.selectedIds = []
            try {
                const resp = await fetch('/api/pg/staging/query', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({
                        page: staging.page, page_size: 50,
                        tb_status: staging.filterTb || null,
                        pg_status: staging.filterPg || null,
                        scale_status: staging.filterScale || null,
                    }),
                })
                if (!resp.ok) throw new Error('Query failed')
                const data = await resp.json()
                staging.data = data.data; staging.total = data.total
                staging.totalPages = data.total_pages
            } catch (e) { console.error('載入暫存資料失敗:', e) }
        }

        // ── 背景任務（Step 3 建點 / Step 5 Scale / 批次刪除共用）──
        // 三個執行端點送出後都只回 task_id（HTTP 200＝任務已建立，可能正在排隊），
        // 要輪詢 GET /api/tasks/{id} 直到 done 才有結果。每個流程各有一個任務狀態物件（kwDerive.task 等）。
        const TASK_POLL_MS = 1000          // 輪詢間隔
        const TASK_POLL_MAX_FAIL = 10      // 連續失敗幾次才放棄（單次網路失敗不中斷）
        let _taskEpoch = 0                 // 登出時 +1，讓舊的輪詢迴圈自行結束

        const CANCEL_CONFIRM = '確定要取消嗎？排隊中的會立即退出；執行中的會在處理完目前這筆後停止，已完成的點位不會復原'

        function _newTask() {
            return {
                id: null, submitting: false, done: false,
                status: '',            // 伺服器回報：queued / running / done / cancelled
                cancelled: false,      // 是否已收到取消要求
                submitQueued: false, message: '', blocked: 0, excluded: null,   // 送出回應的內容
                progress: null, gateways: [], summary: null,
                cancelling: false, cancelError: '', error: '',
                lost: false, stalled: false,   // 404 任務遺失／連續輪詢失敗
            }
        }

        const _sleep = (ms) => new Promise(r => setTimeout(r, ms))

        function _applyTaskStatus(ts, st) {
            ts.status = st.status || ''
            ts.cancelled = !!st.cancelled
            ts.progress = st.progress || null
            ts.gateways = st.gateways || []
            if (st.done) { ts.done = true; ts.summary = st.summary || {} }
        }

        // 送出背景任務。成功回 true（ts 記下 task_id 與排隊／blocked／excluded 資訊）；
        // 失敗（400 無可處理資料、422 超過批次上限、429 佇列已滿、404…）把後端 detail 放進 ts.error 並回 false
        async function _startTask(ts, url, body, what) {
            ts.submitting = true
            try {
                const resp = await fetch(url, { method: 'POST', headers: _headers(), body: JSON.stringify(body) })
                if (!resp.ok) {
                    ts.error = `${what}未送出（HTTP ${resp.status}）：${await _respDetail(resp)}`
                    return false
                }
                const data = await resp.json()
                ts.id = data.task_id
                ts.submitQueued = !!data.queued
                ts.message = data.message || ''
                ts.blocked = data.blocked || 0
                ts.excluded = data.excluded || null
                return true
            } catch (e) {
                ts.error = `${what}未送出：${e.message}`
                return false
            } finally { ts.submitting = false }
        }

        // 輪詢到任務結束（沒有次數上限，排隊可能很久）。404＝伺服器重啟遺失任務；
        // 單次錯誤容忍重試，連續失敗 TASK_POLL_MAX_FAIL 次才標記 stalled（畫面提供「繼續追蹤」）。
        async function _trackTask(ts) {
            const epoch = _taskEpoch
            let fails = 0
            ts.stalled = false
            while (epoch === _taskEpoch && auth.token) {
                try {
                    const r = await fetch(`/api/tasks/${ts.id}`, { headers: _headers() })
                    if (epoch !== _taskEpoch) return
                    if (r.status === 404) { ts.lost = true; return }
                    if (!r.ok) throw new Error(`HTTP ${r.status}`)
                    const st = await r.json()
                    fails = 0
                    _applyTaskStatus(ts, st)
                    if (st.done) return
                } catch (e) {
                    if (epoch !== _taskEpoch) return
                    if (++fails >= TASK_POLL_MAX_FAIL) { ts.stalled = true; return }
                }
                await _sleep(TASK_POLL_MS)
            }
        }

        // 一個執行流程：送出 → 追蹤到結束 → afterDone（重新推導）。executing 從送出一路到結束（含排隊中），
        // 避免重複送出。owner 是 kwDerive / scaleDerive / delState。
        async function _runGatewayTask(owner, url, body, what, afterDone) {
            owner.executing = true
            owner.task = _newTask()
            const ts = owner.task                 // 取 reactive 代理，之後的寫入才會觸發畫面更新
            const epoch = _taskEpoch
            try {
                if (await _startTask(ts, url, body, what)) await _trackTask(ts)
            } finally {
                if (epoch === _taskEpoch) owner.executing = false
            }
            if (epoch === _taskEpoch && ts.done && afterDone) await afterDone()
        }

        // 輪詢連續失敗後使用者按「繼續追蹤」
        async function resumeTask(owner, afterDone) {
            const ts = owner.task
            if (!ts.id || ts.done) return
            owner.executing = true
            const epoch = _taskEpoch
            try { await _trackTask(ts) }
            finally { if (epoch === _taskEpoch) owner.executing = false }
            if (epoch === _taskEpoch && ts.done && afterDone) await afterDone()
        }

        // 取消任務（進度區與作業佇列共用）。回 {ok, status, detail}；409＝已結束，視為正常。
        async function _cancelById(taskId) {
            try {
                const resp = await fetch(`/api/tasks/${taskId}/cancel`, { method: 'POST', headers: _headers() })
                if (resp.ok) return { ok: true, status: 200, detail: '' }
                return { ok: false, status: resp.status, detail: await _respDetail(resp) }
            } catch (e) { return { ok: false, status: 0, detail: e.message } }
        }

        async function cancelTask(owner) {
            const ts = owner.task
            if (!ts.id || ts.done || ts.cancelling) return
            if (!confirm(CANCEL_CONFIRM)) return
            ts.cancelling = true; ts.cancelError = ''
            const r = await _cancelById(ts.id)
            if (r.ok) return                       // 保持「取消中…」，輪詢會看到取消生效並走到 done
            ts.cancelling = false
            if (r.status === 409) return           // 剛好已結束：輪詢下一輪就會拿到 done 與結果
            ts.cancelError = r.status === 404 ? '任務已遺失，無法取消' : `取消失敗（${r.detail}）`
        }

        // ── Gateway 路由預覽（Step 3 / Step 5 推導表格共用）──
        // 推導結果每列帶 route {gateway_id, gateway_name, status, reason}：
        // single/ok/channel_only/map_only 可放行；conflict/conflict_channel/unrouted 無法判斷，不會送出
        const ROUTE_BLOCKED = ['conflict', 'conflict_channel', 'unrouted']

        function routeBlocked(r) { return !!r.route && ROUTE_BLOCKED.includes(r.route.status) }

        // 單台模式下，路由到其他台的列會被略過（無法判斷的列另外算「不會送出」）
        function rowSkipped(r) {
            return !!selectedGwId.value && !!r.route && !routeBlocked(r) && r.route.gateway_id !== selectedGwId.value
        }

        function routeBadge(r) {
            const rt = r.route
            if (!rt) return null
            if (rt.status === 'ok') return { text: '✓ 已對應', cls: 'pill-ok', title: rt.reason }
            if (rt.status === 'channel_only' || rt.status === 'map_only') {
                return { text: '⚠ 單一依據', cls: 'pill-warn', title: rt.reason }
            }
            if (ROUTE_BLOCKED.includes(rt.status)) return { text: '✗ 無法判斷', cls: 'pill-err', title: rt.reason }
            return null                            // single：不特別標
        }

        // 摘要：各 Gateway 筆數、無法判斷筆數、單台模式下將略過的筆數、實際會送出的筆數
        function _routeSummary(rows) {
            const sel = selectedGwId.value
            const per = new Map()
            let blocked = 0, skipOther = 0, willSend = 0
            for (const r of rows) {
                const rt = r.route
                if (!rt) { willSend++; continue }   // 沒有 route（舊後端）：交給後端判斷
                if (ROUTE_BLOCKED.includes(rt.status)) { blocked++; continue }
                const skip = !!sel && rt.gateway_id !== sel
                let g = per.get(rt.gateway_id)
                if (!g) { g = { id: rt.gateway_id, name: rt.gateway_name || `Gateway ${rt.gateway_id}`, count: 0, skip }; per.set(rt.gateway_id, g) }
                g.count++
                if (skip) skipOther++; else willSend++
            }
            return { gateways: [...per.values()], blocked, skipOther, willSend }
        }

        // 送出前確認文字的附註：說明有幾筆不會送出
        function _sendNotes(s) {
            const notes = []
            if (s.blocked) notes.push(`${s.blocked} 筆無法判斷所屬 Gateway，不會送出`)
            if (s.skipOther) notes.push(`${s.skipOther} 筆屬於其他 Gateway，將略過`)
            return notes.length ? `\n（另有 ${notes.join('；')}）` : ''
        }

        function _gwModeText() {
            return selectedGw.value ? `Gateway「${selectedGw.value.name}」（只處理屬於這台的點）` : '自動（每筆依 channel 與對應表送往所屬 Gateway）'
        }

        // Step 3: Kepware Derive + Execute
        // derivedGw / derivedVer：推導當時的 Gateway 選擇與路由資料版本，用來提示預覽表是否過期
        const kwDerive = reactive({
            data: [], loading: false, executing: false, savingOverrides: false,
            delay: 1.1, batchSize: 50, batchPause: 5, task: _newTask(),
            derivedGw: undefined, derivedVer: 0,
        })
        const kwRoute = computed(() => _routeSummary(kwDerive.data))
        const kwStale = computed(() => kwDerive.data.length > 0
            && (kwDerive.derivedGw !== selectedGwId.value || kwDerive.derivedVer !== routeDataVersion.value))

        // Kepware tag 資料型別 enum（依 Kepware API Gateway 文件）；實務上 9 成為 Float
        const KW_DATA_TYPES = [
            { v: 1, label: 'Boolean' }, { v: 2, label: 'Char' }, { v: 3, label: 'Byte' },
            { v: 4, label: 'Short' }, { v: 5, label: 'Word' }, { v: 6, label: 'Long' },
            { v: 7, label: 'DWord' }, { v: 8, label: 'Float' }, { v: 9, label: 'Double' },
            { v: 10, label: 'String' }, { v: 11, label: 'BCD' }, { v: 12, label: 'LBCD' },
            { v: 13, label: 'Date' }, { v: 14, label: 'LLong' }, { v: 15, label: 'QWord' },
        ]

        function _stampKwOrig(row) {
            row._origChannel = row.channel_name || ''
            row._origDevice = row.device_name || ''
            row._origTagGroups = row.tag_groups || ''
            row._origDataType = row.data_type
        }

        function _kwRowChanged(row) {
            return (row.channel_name || '') !== (row._origChannel ?? '')
                || (row.device_name || '') !== (row._origDevice ?? '')
                || (row.tag_groups || '') !== (row._origTagGroups ?? '')
                || row.data_type !== row._origDataType
        }

        // keepTask=true：任務結束後自動重新推導時使用，保留進度區的結果摘要；手動按「推導欄位」則清掉舊結果
        async function deriveKw(keepTask) {
            kwDerive.loading = true
            if (keepTask !== true && !kwDerive.executing) kwDerive.task = _newTask()
            try {
                // 自動模式不帶 gateway_id（後端對每筆路由到的那台做結構驗證）；單台模式帶 gateway_id
                const body = { tb_status: 'pending' }
                if (selectedGwId.value) body.gateway_id = selectedGwId.value
                const resp = await fetch('/api/kw/derive', {
                    method: 'POST', headers: _headers(), body: JSON.stringify(body),
                })
                if (!resp.ok) throw new Error(await _respDetail(resp))
                const data = await resp.json()
                kwDerive.data = data.data || []
                kwDerive.data.forEach(_stampKwOrig)
                kwDerive.derivedGw = body.gateway_id || null
                kwDerive.derivedVer = routeDataVersion.value
            } catch (e) { alert('推導失敗: ' + e.message) }
            finally { kwDerive.loading = false }
        }

        async function saveAllKwOverrides() {
            if (!kwDerive.data.length) return
            const changed = kwDerive.data.filter(_kwRowChanged)
            if (!changed.length) { alert('沒有欄位變更，無需儲存'); return }
            kwDerive.savingOverrides = true
            let saved = 0
            let errorMsg = ''
            for (const row of changed) {
                try {
                    const resp = await fetch('/api/pg/staging/kw-override', {
                        method: 'POST', headers: _headers(),
                        body: JSON.stringify({
                            id: row.id,
                            channel: row.channel_name || '',
                            device: row.device_name || '',
                            tag_groups: row.tag_groups || '',
                            data_type: row.data_type,
                        }),
                    })
                    if (!resp.ok) {
                        const e = await resp.json().catch(() => ({}))
                        errorMsg = e.detail || `HTTP ${resp.status}`
                        break
                    }
                    row.overridden = true; saved++
                    _stampKwOrig(row)
                } catch (e) { errorMsg = e.message; break }
            }
            kwDerive.savingOverrides = false
            if (errorMsg) {
                alert(`儲存失敗：${errorMsg}\n（已成功 ${saved} / ${changed.length} 筆，其餘未處理）`)
            } else {
                // 覆寫 channel 會改變該筆路由到哪台 Gateway：全部存妥後重新推導，預覽表的 Gateway／驗證欄才會跟著更新
                await deriveKw(true)
                alert(`儲存完成：共 ${saved} 筆已更新`)
            }
        }

        async function executeKw() {
            if (!kwDerive.data.length || kwDerive.executing) return
            const s = kwRoute.value
            if (!s.willSend) {
                alert('目前推導結果中沒有可送出的資料（全部無法判斷所屬 Gateway' + (selectedGwId.value ? '，或屬於其他 Gateway' : '') + '）。\n請確認各 Gateway 已同步結構，或到「設定 › Gateway 對應表」設定棟別/園區後重新推導。')
                return
            }
            const unsaved = kwDerive.data.filter(_kwRowChanged).length
            if (!confirm(`確定要建立 ${s.willSend} 筆 Tag 到 Kepware？\nGateway：${_gwModeText()}${_sendNotes(s)}`
                + (unsaved ? `\n⚠ 有 ${unsaved} 筆欄位修改尚未儲存，執行時不會套用（請先按「儲存覆寫」）` : ''))) return
            // 自動模式不帶 gateway_id；單台模式帶 gateway_id
            const body = { delay: kwDerive.delay, batch_size: kwDerive.batchSize, batch_pause: kwDerive.batchPause }
            if (selectedGwId.value) body.gateway_id = selectedGwId.value
            await _runGatewayTask(kwDerive, '/api/kw/execute', body, '建點', () => deriveKw(true))
        }

        function rowStatus(v) {
            if (!v) return 'warn'
            if (v.channel_exists && v.device_exists && v.group_exists) return 'ok'
            if (v.group_auto_create) return 'add'
            return 'warn'
        }

        // Step 4: PG Derive + Execute
        const pgDerive = reactive({ data: [], loading: false, executing: false, result: null })

        async function derivePg() {
            pgDerive.loading = true; pgDerive.result = null
            try {
                const resp = await fetch('/api/pg/derive/pg', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ pg_status: 'pending' }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                pgDerive.data = data.data || []
            } catch (e) { alert('PG 推導失敗: ' + e.message) }
            finally { pgDerive.loading = false }
        }

        async function executePg() {
            if (!pgDerive.data.length) return
            if (!confirm(`確定要將 ${pgDerive.data.length} 筆資料寫入 PG 正式表？`)) return
            pgDerive.executing = true; pgDerive.result = null
            try {
                const resp = await fetch('/api/pg/execute/pg', {
                    method: 'POST', headers: _headers(), body: JSON.stringify({}),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                pgDerive.result = await resp.json()
                await derivePg()
            } catch (e) { pgDerive.result = { inserted: 0, message: e.message } }
            finally { pgDerive.executing = false }
        }

        // Step 5: Scale
        const scaleDerive = reactive({
            data: [], loading: false, executing: false,
            delay: 1.1, batchSize: 50, batchPause: 5, task: _newTask(),
            notBuiltCount: 0,   // 本批中尚未在 Kepware 建點（tb_status != done）的筆數
            derivedGw: undefined, derivedVer: 0,
        })
        const scaleRoute = computed(() => _routeSummary(scaleDerive.data))
        const scaleStale = computed(() => scaleDerive.data.length > 0
            && (scaleDerive.derivedGw !== selectedGwId.value || scaleDerive.derivedVer !== routeDataVersion.value))

        // keepTask=true：任務結束後自動重新推導，保留進度區的結果摘要
        async function deriveScale(keepTask) {
            scaleDerive.loading = true
            if (keepTask !== true && !scaleDerive.executing) scaleDerive.task = _newTask()
            try {
                const resp = await fetch('/api/pg/derive/scale', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ scale_status: 'pending' }),
                })
                if (!resp.ok) throw new Error(await _respDetail(resp))
                const data = await resp.json()
                scaleDerive.data = data.data || []
                scaleDerive.notBuiltCount = data.not_built_count || 0
                scaleDerive.derivedGw = selectedGwId.value || null
                scaleDerive.derivedVer = routeDataVersion.value
            } catch (e) { alert('Scale 推導失敗: ' + e.message) }
            finally { scaleDerive.loading = false }
        }

        async function executeScale() {
            if (!scaleDerive.data.length || scaleDerive.executing) return
            const s = scaleRoute.value
            if (!s.willSend) {
                alert('目前推導結果中沒有可送出的資料（全部無法判斷所屬 Gateway' + (selectedGwId.value ? '，或屬於其他 Gateway' : '') + '）。\n請確認各 Gateway 已同步結構，或到「設定 › Gateway 對應表」設定棟別/園區後重新推導。')
                return
            }
            if (!confirm(`確定要設定 ${s.willSend} 筆 Tag 的 Scale？\nGateway：${_gwModeText()}${_sendNotes(s)}`)) return
            // 自動模式不帶 gateway_id；單台模式帶 gateway_id
            const body = { delay: scaleDerive.delay, batch_size: scaleDerive.batchSize, batch_pause: scaleDerive.batchPause }
            if (selectedGwId.value) body.gateway_id = selectedGwId.value
            await _runGatewayTask(scaleDerive, '/api/pg/execute/scale', body, 'Scale 設定', () => deriveScale(true))
        }

        // Step 6: Collector
        const collectorState = reactive({ testing: false, reloading: false, status: null, message: '' })

        async function testCollector() {
            collectorState.testing = true; collectorState.status = null
            try {
                const resp = await fetch('/api/collector/test', { headers: _headers() })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                collectorState.status = 'ok'; collectorState.message = 'Collector DB 連線正常'
            } catch (e) {
                collectorState.status = 'error'; collectorState.message = '連線失敗: ' + e.message
            } finally { collectorState.testing = false }
        }

        async function reloadCollector() {
            if (!confirm('確定要呼叫 Collector Reload？')) return
            collectorState.reloading = true; collectorState.status = null
            try {
                const resp = await fetch('/api/collector/reload', { method: 'POST', headers: _headers() })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                collectorState.status = 'ok'
                collectorState.message = `Collector 重載成功，共 ${data.total_tags || '?'} 筆 tag`
            } catch (e) {
                collectorState.status = 'error'; collectorState.message = '重載失敗: ' + e.message
            } finally { collectorState.reloading = false }
        }

        // ── Delete Tab ──
        const delState = reactive({
            fileName: null, totalRows: 0, uploadId: null,
            dryRun: true, executing: false, task: _newTask(), resultDry: true,
            delay: 1.1, batchSize: 50, batchPause: 5,
        })

        function handleDeleteFile(e) {
            const file = e.target.files[0]
            if (file) _uploadDeleteCSV(file)
        }

        function onDeleteDrop(e) {
            const file = e.dataTransfer.files[0]
            if (file && file.name.toLowerCase().endsWith('.csv')) _uploadDeleteCSV(file)
            else alert('請上傳 .csv 檔案')
        }

        async function _uploadDeleteCSV(file) {
            const form = new FormData()
            form.append('file', file)
            try {
                const resp = await fetch('/api/csv/upload', { method: 'POST', headers: _authHeader(), body: form })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                delState.uploadId = data.upload_id; delState.fileName = data.filename
                delState.totalRows = data.total_rows
                if (!delState.executing) delState.task = _newTask()
            } catch (e) { alert('CSV 上傳失敗: ' + e.message) }
        }

        async function executeDelete() {
            if (!delState.uploadId || delState.executing) return
            if (!delState.dryRun && !confirm(`確定要刪除？此操作無法復原！\nGateway：${_gwModeText()}`)) return
            delState.resultDry = delState.dryRun      // 結果標題用送出當下的模式（之後勾選框改了也不影響）
            // 自動模式不帶 gateway_id（依 CSV type 的第一段 channel 路由）；單台模式帶 gateway_id
            const body = {
                upload_id: delState.uploadId,
                dry_run: delState.dryRun,
                delay: delState.delay,
                batch_size: delState.batchSize,
                batch_pause: delState.batchPause,
            }
            if (selectedGwId.value) body.gateway_id = selectedGwId.value
            await _runGatewayTask(delState, '/api/kw/delete-batch', body, delState.dryRun ? '預演' : '刪除', null)
        }

        async function downloadDeleteTemplate() {
            try {
                const resp = await fetch('/api/templates/delete', { headers: _headers() })
                if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${resp.status}`) }
                const blob = await resp.blob()
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = 'template_delete.csv'
                a.click()
                URL.revokeObjectURL(url)
            } catch (e) { alert('下載失敗: ' + e.message) }
        }

        // ── Query Tab (Tags 正式表搜尋) ──
        const QUERY_FIELDS = [
            { key: 'tagname',     label: 'Tag Name' },
            { key: 'system',      label: 'System' },
            { key: 'site',        label: 'Site' },
            { key: 'bu',          label: 'BU' },
            { key: 'floor',       label: 'Floor' },
            { key: 'zone',        label: 'Zone' },
            { key: 'owner',       label: 'Owner' },
            { key: 'department',  label: 'Department' },
            { key: 'driver_type', label: 'Driver Type' },
            { key: 'node_name',   label: 'Node Name' },
            { key: 'tablename',   label: 'Table Name' },
            { key: 'description', label: 'Description' },
            { key: 'address',     label: 'Address' },
            { key: 'data_type',   label: 'Data Type' },
        ]
        const QUERY_OPS = [
            { key: 'contains',   label: '包含' },
            { key: 'equals',     label: '等於' },
            { key: 'starts_with',label: '開頭為' },
            { key: 'ends_with',  label: '結尾為' },
            { key: 'is_empty',   label: '為空' },
            { key: 'not_empty',  label: '不為空' },
        ]
        const dbQuery = reactive({
            conditions: [{ field: 'tagname', op: 'contains', value: '' }],
            logic: 'AND',
            page: 0,
            totalPages: 0,
            total: 0,
            loading: false,
            exporting: false,
            results: [],
            sqlPreview: '',
        })

        function addCondition() {
            if (dbQuery.conditions.length < 6)
                dbQuery.conditions.push({ field: 'tagname', op: 'contains', value: '' })
        }

        function removeCondition(i) {
            dbQuery.conditions.splice(i, 1)
            if (!dbQuery.conditions.length) addCondition()
        }

        async function searchTags(resetPage) {
            if (resetPage) dbQuery.page = 0
            dbQuery.loading = true
            try {
                const resp = await fetch('/api/pg/tags/search', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({
                        conditions: dbQuery.conditions,
                        logic: dbQuery.logic,
                        page: dbQuery.page,
                        page_size: 50,
                    }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                dbQuery.results = data.data
                dbQuery.total = data.total
                dbQuery.totalPages = data.total_pages
                dbQuery.sqlPreview = data.sql_preview || ''
            } catch (e) { alert('查詢失敗: ' + e.message) }
            finally { dbQuery.loading = false }
        }

        async function exportTags() {
            dbQuery.exporting = true
            try {
                const resp = await fetch('/api/pg/tags/export', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ conditions: dbQuery.conditions, logic: dbQuery.logic }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const blob = await resp.blob()
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = `tags_export_${Date.now()}.csv`
                a.click()
                URL.revokeObjectURL(url)
            } catch (e) { alert('匯出失敗: ' + e.message) }
            finally { dbQuery.exporting = false }
        }

        // ── IO Mapping ──
        const ioMapping = reactive({
            fileA: null, fileB: null, executing: false,
            mappingId: null, result: null, data: [], columns: [],
        })

        async function executeIoMapping() {
            if (!ioMapping.fileA || !ioMapping.fileB) return
            ioMapping.executing = true; ioMapping.result = null; ioMapping.data = []
            try {
                const form = new FormData()
                form.append('a_file', ioMapping.fileA)
                form.append('b_file', ioMapping.fileB)
                const headers = auth.token ? { 'Authorization': `Bearer ${auth.token}` } : {}
                const resp = await fetch('/api/io-mapping/execute', { method: 'POST', headers, body: form })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const r = await resp.json()
                ioMapping.mappingId = r.mapping_id; ioMapping.columns = r.columns
                ioMapping.data = r.data; ioMapping.result = { matched: r.matched, unmatched: r.unmatched, total: r.total }
            } catch (e) { alert('Mapping 失敗: ' + e.message) }
            finally { ioMapping.executing = false }
        }

        async function downloadIoMapping() {
            if (!ioMapping.mappingId) return
            try {
                const resp = await fetch(`/api/io-mapping/download/${ioMapping.mappingId}`, { headers: _headers() })
                if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${resp.status}`) }
                const blob = await resp.blob()
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = `IO_Mapping_Result_${ioMapping.mappingId}.csv`
                a.click()
                URL.revokeObjectURL(url)
            } catch (e) { alert('下載失敗: ' + e.message) }
        }

        // ── History ──
        const historyList = ref([])

        async function loadHistory() {
            try {
                const resp = await fetch('/api/history', { headers: _headers() })
                if (resp.ok) historyList.value = await resp.json()
            } catch (e) { console.error('載入歷史失敗:', e) }
        }

        // ── Settings: PG Connection ──
        const pgConn = reactive({ testing: false, status: '', statusText: '未連線' })

        async function testPgConn() {
            pgConn.testing = true
            try {
                const resp = await fetch('/api/pg/test', { headers: _headers() })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                pgConn.status = 'connected'; pgConn.statusText = '已連線'
            } catch (e) {
                pgConn.status = 'error'; pgConn.statusText = '連線失敗: ' + e.message
            } finally { pgConn.testing = false }
        }

        // ── Settings: Reference Tables ──
        const refTables = [
            { key: 'locations', label: 'Location' },
            { key: 'ownerships', label: 'Ownership' },
            { key: 'devices', label: 'Device' },
            { key: 'systems', label: 'System' },
            { key: 'tb-profiles', label: 'TB Profile' },
        ]
        const refData = reactive({})
        const activeRefTable = ref(null)
        const refTableCols = {
            locations: ['id', 'bu', 'site', 'zone'],
            ownerships: ['id', 'department', 'data_owner'],
            devices: ['id', 'device_name', 'driver_type', 'site', 'system_code'],
            systems: ['id', 'system_code', 'system_name'],
            'tb-profiles': ['id', 'name', 'description'],
        }

        async function loadRefTable(key) {
            activeRefTable.value = key
            try {
                const resp = await fetch(`/api/pg/ref/${key}`, { headers: _headers() })
                if (resp.ok) refData[key] = await resp.json()
            } catch (e) { console.error(`載入 ${key} 失敗:`, e) }
        }

        async function deleteRefRow(table, id) {
            if (!confirm('確定刪除此筆資料？')) return
            try {
                await fetch('/api/pg/ref/delete', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ table, id }),
                })
                if (activeRefTable.value) loadRefTable(activeRefTable.value)
            } catch (e) { alert('刪除失敗: ' + e.message) }
        }

        // ── 作業佇列（各 Gateway 目前的佇列：第 0 個＝執行中，其後排隊中）──
        const TASK_TYPE_LABELS = { kw_execute: 'Step 3 建點', pg_execute_scale: 'Step 5 Scale', kw_delete: '批次刪除' }
        const queuePanel = reactive({ open: false, loading: false, data: [], error: '', note: '', updatedAt: '', cancelling: '' })
        let _queueTimer = null

        async function loadQueue(silent) {
            if (silent !== true) queuePanel.loading = true
            try {
                const resp = await fetch('/api/kw/queue', { headers: _headers() })
                if (!resp.ok) throw new Error(await _respDetail(resp))
                queuePanel.data = await resp.json()
                queuePanel.error = ''
                queuePanel.updatedAt = new Date().toLocaleTimeString('zh-TW', { hour12: false })
            } catch (e) { queuePanel.error = '讀取作業佇列失敗：' + e.message }
            finally { queuePanel.loading = false }
        }

        // 開著的時候每 2 秒自動更新一次，關閉或登出就停
        function openQueuePanel() {
            queuePanel.open = true; queuePanel.note = ''
            loadQueue()
            clearInterval(_queueTimer)
            _queueTimer = setInterval(() => loadQueue(true), 2000)
        }

        function closeQueuePanel() {
            queuePanel.open = false
            clearInterval(_queueTimer); _queueTimer = null
        }

        function queueStateText(t) { return t.position === 0 ? '執行中' : `排隊中（前面 ${t.position} 個）` }

        async function cancelQueuedTask(taskId) {
            if (queuePanel.cancelling) return
            if (!confirm(CANCEL_CONFIRM)) return
            queuePanel.cancelling = taskId; queuePanel.note = ''
            const r = await _cancelById(taskId)
            queuePanel.cancelling = ''
            if (r.ok) queuePanel.note = `已送出取消要求（任務 ${taskId}）；排隊中的會立即退出，執行中的會在處理完目前這筆後停止`
            else if (r.status === 409) queuePanel.note = `任務 ${taskId} 已結束`
            else queuePanel.note = `取消失敗：${r.detail}`
            await loadQueue(true)
        }

        // ── Settings: Gateway 對應表（棟別/園區 → Gateway）──
        const routeMap = reactive({
            list: [], unmappedSites: [], unmappedZones: [], loading: false, error: '',
            saving: false, formError: '', form: { type: 'site', value: '', gatewayId: null },
        })
        const routeValueInput = ref(null)

        async function loadRoutes() {
            routeMap.loading = true
            try {
                const resp = await fetch('/api/kw/routes', { headers: _headers() })
                if (!resp.ok) throw new Error(await _respDetail(resp))
                const d = await resp.json()
                routeMap.list = d.routes || []
                routeMap.unmappedSites = d.unmapped_sites || []
                routeMap.unmappedZones = d.unmapped_zones || []
                routeMap.error = ''
            } catch (e) { routeMap.error = '載入 Gateway 對應表失敗：' + e.message }
            finally { routeMap.loading = false }
        }

        // 新增或更新（同類型＋值已存在就改指向新的 Gateway）；僅 admin
        async function saveRoute() {
            const f = routeMap.form
            routeMap.formError = ''
            const value = (f.value || '').trim()
            if (!value) { routeMap.formError = '請輸入棟別或園區的值'; return }
            if (!f.gatewayId) { routeMap.formError = '請選擇對應的 Gateway'; return }
            routeMap.saving = true
            try {
                const resp = await fetch('/api/kw/routes', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ match_type: f.type, match_value: value, gateway_id: f.gatewayId }),
                })
                if (!resp.ok) throw new Error(await _respDetail(resp))
                f.value = ''
                routeDataVersion.value++          // 對應表變了，已推導的路由可能過期
                await loadRoutes()
            } catch (e) { routeMap.formError = e.message }
            finally { routeMap.saving = false }
        }

        async function deleteRoute(r) {
            if (!confirm(`確定刪除這筆對應？\n${r.match_type === 'site' ? '棟別' : '園區'} ${r.match_value} → ${r.gateway_name}`)) return
            routeMap.formError = ''
            try {
                const resp = await fetch(`/api/kw/routes/${r.id}`, { method: 'DELETE', headers: _headers() })
                if (!resp.ok) throw new Error(await _respDetail(resp))
                routeDataVersion.value++
                await loadRoutes()
            } catch (e) { routeMap.formError = '刪除失敗：' + e.message }
        }

        // 把「尚未對應」的棟別/園區（或既有的一筆）帶進新增表單
        function pickRoute(type, value, gatewayId) {
            const f = routeMap.form
            f.type = type; f.value = value
            if (gatewayId) f.gatewayId = gatewayId
            else if (!f.gatewayId && gwList.value.length === 1) f.gatewayId = gwList.value[0].id
            routeMap.formError = ''
            nextTick(() => routeValueInput.value && routeValueInput.value.focus())
        }

        // ── Settings: User Management ──
        const userList = ref([])
        const showUserCreate = ref(false)
        const newUser = reactive({ username: '', password: '', displayName: '', role: 'operator' })

        async function loadUsers() {
            try {
                const resp = await fetch('/api/admin/users', { headers: _headers() })
                if (resp.ok) userList.value = await resp.json()
            } catch (e) { console.error('載入使用者失敗:', e) }
        }

        async function createUser() {
            if (!newUser.username || !newUser.password) { alert('帳號和密碼為必填'); return }
            try {
                const resp = await fetch('/api/admin/users', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({
                        username: newUser.username, password: newUser.password,
                        display_name: newUser.displayName, role: newUser.role,
                    }),
                })
                if (!resp.ok) { const e = await resp.json(); alert(e.detail || '建立失敗'); return }
                newUser.username = ''; newUser.password = ''; newUser.displayName = ''; newUser.role = 'operator'
                showUserCreate.value = false
                loadUsers()
            } catch (e) { alert(e.message) }
        }

        async function resetUserPw(id) {
            const pw = prompt('請輸入新密碼:')
            if (!pw) return
            try {
                const resp = await fetch(`/api/admin/users/${id}/reset-password`, {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ new_password: pw }),
                })
                if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${resp.status}`) }
                alert('密碼已重設')
            } catch (e) { alert('重設失敗: ' + e.message) }
        }

        async function deleteUser(id) {
            if (!confirm('確定刪除此使用者？')) return
            try {
                const resp = await fetch(`/api/admin/users/${id}`, { method: 'DELETE', headers: _headers() })
                if (!resp.ok) { const e = await resp.json(); alert(e.detail || '刪除失敗'); return }
                loadUsers()
            } catch (e) { alert(e.message) }
        }

        // ── Settings: Activity Logs ──
        const actLogs = ref([])

        async function loadActivityLogs() {
            try {
                const resp = await fetch('/api/admin/logs/query', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ page: 1, page_size: 50 }),
                })
                if (resp.ok) {
                    const data = await resp.json()
                    actLogs.value = data.rows || []
                }
            } catch (e) { console.error('載入日誌失敗:', e) }
        }

        // ── Password Change ──
        const showPwDialog = ref(false)
        const pwForm = reactive({ old: '', new1: '', new2: '', msg: '', err: false })

        async function changePassword() {
            pwForm.msg = ''; pwForm.err = false
            if (!pwForm.new1) { pwForm.msg = '請輸入新密碼'; pwForm.err = true; return }
            if (pwForm.new1 !== pwForm.new2) { pwForm.msg = '兩次密碼不一致'; pwForm.err = true; return }
            try {
                const resp = await fetch('/api/user/change-password', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ old_password: pwForm.old, new_password: pwForm.new1 }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || '變更失敗') }
                pwForm.msg = '密碼已變更'; pwForm.err = false
                pwForm.old = ''; pwForm.new1 = ''; pwForm.new2 = ''
            } catch (e) { pwForm.msg = e.message; pwForm.err = true }
        }

        // ── Chat Widget ──
        const chatBodyRef = ref(null)
        const chat = reactive({
            open: false,
            input: '',
            typing: false,
            messages: [],
            quickQuestions: [
                '如何新增一個 Kepware Gateway？',
                '建點流程有幾個步驟？',
                'Scale 設定的原始值範圍怎麼填？',
                '匯入 CSV 需要哪些欄位？',
            ],
        })

        const _chatReplies = {
            'gateway': 'Gateway 管理在「設定」頁籤 → Kepware Gateway 管理。點擊「新增 Gateway」填入名稱、URL、帳號密碼即可。建議先「測試」確認連線後再使用。',
            '建點': '建點流程共 6 步驟：① CSV 上傳 → ② 暫存表確認 → ③ Kepware 建點（選 Gateway + 同步結構 + 推導 + 執行）→ ④ PG Import → ⑤ Scale 設定 → ⑥ Collector Reload',
            'scale': 'Scale 欄位：\n• Raw Low / High：Kepware 原始 ADC 值（例如 0 ~ 65535）\n• Scaled Low / High：工程單位換算後的值（例如 0.0 ~ 100.0）\n• 類型：Linear（線性換算）',
            'csv': 'CSV 必要欄位：\n• tag_name：點位名稱（格式：site_floor_system_…）\n• site：廠區代碼\n• system_code：系統代碼\n• device_profile：設備描述\n\n選填：description、scale_enabled、scaling_raw_low/high 等',
        }

        async function chatSend(text) {
            const msg = (text || chat.input).trim()
            if (!msg) return
            chat.input = ''
            chat.messages.push({ role: 'user', content: msg, time: new Date().toLocaleTimeString('zh-TW', { hour12: false }) })
            chat.typing = true
            await nextTick()
            if (chatBodyRef.value) chatBodyRef.value.scrollTop = chatBodyRef.value.scrollHeight
            await new Promise(r => setTimeout(r, 600 + Math.random() * 400))
            const msgLower = msg.toLowerCase()
            let reply = '這個問題我需要更多資訊才能回答。請參考系統說明文件，或聯繫管理員。'
            for (const [key, val] of Object.entries(_chatReplies)) {
                if (msgLower.includes(key)) { reply = val; break }
            }
            chat.typing = false
            chat.messages.push({ role: 'assistant', content: reply, time: new Date().toLocaleTimeString('zh-TW', { hour12: false }) })
            await nextTick()
            if (chatBodyRef.value) chatBodyRef.value.scrollTop = chatBodyRef.value.scrollHeight
        }

        // ── Utilities ──
        function formatTime(ts) {
            if (!ts) return '—'
            return new Date(ts).toLocaleString('zh-TW')
        }

        function statusKind(status) {
            if (status === 'done') return 'ok'
            if (status === 'pending') return 'warn'
            if (status === 'skip') return 'muted'
            return 'muted'
        }

        // ── Init on login ──
        function _initAfterLogin() {
            loadGateways()
            loadRoutes()
            testPgConn()
            if (auth.role === 'admin') loadUsers()
        }

        // 切到設定頁時重新載入對應表，「尚未對應」清單才會反映剛改過的參照表
        watch(activeTab, (tab) => { if (tab === 'settings' && auth.token) loadRoutes() })

        if (auth.token) _initAfterLogin()

        return {
            auth, loginForm, doLogin, doLogout, clock,
            tabs, activeTab,
            gwList, selectedGwId, selectedGw, gwForm, canOperate,
            loadGateways, saveGw, editGw, deleteGw, testGw,
            structureSync, syncStructure,
            importSteps, importStep,
            imp, handleFile, onDrop, importToStaging, resetUpload,
            staging, loadStaging, clearStaging, toggleStagingSelectAll, toggleStagingRow, deleteSelectedStaging,
            kwDerive, KW_DATA_TYPES, deriveKw, executeKw, saveAllKwOverrides, rowStatus,
            kwRoute, kwStale, scaleRoute, scaleStale, routeBlocked, rowSkipped, routeBadge,
            cancelTask, resumeTask,
            pgDerive, derivePg, executePg,
            scaleDerive, deriveScale, executeScale,
            collectorState, testCollector, reloadCollector,
            delState, handleDeleteFile, onDeleteDrop, executeDelete, downloadDeleteTemplate,
            QUERY_FIELDS, QUERY_OPS, dbQuery, addCondition, removeCondition, searchTags, exportTags,
            ioMapping, executeIoMapping, downloadIoMapping,
            historyList, loadHistory,
            pgConn, testPgConn,
            refTables, refData, activeRefTable, refTableCols, loadRefTable, deleteRefRow,
            queuePanel, TASK_TYPE_LABELS, openQueuePanel, closeQueuePanel, loadQueue, queueStateText, cancelQueuedTask,
            routeMap, routeValueInput, loadRoutes, saveRoute, deleteRoute, pickRoute,
            userList, showUserCreate, newUser, loadUsers, createUser, resetUserPw, deleteUser,
            actLogs, loadActivityLogs,
            showPwDialog, pwForm, changePassword,
            chatBodyRef, chat, chatSend,
            formatTime, statusKind,
        }
    }
}).component('task-panel', TaskPanel).mount('#app')
