const { createApp, reactive, ref, computed, watch, onMounted, nextTick } = Vue

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
            gwList.value = []; selectedGwId.value = null
            structureSync.counts = null
            Object.assign(imp, { fileName: null, totalRows: 0, headers: [], preview: [], importing: false, result: null, uploadId: null })
            Object.assign(staging, { data: [], total: 0, page: 0, totalPages: 0, filterTb: '', filterPg: '', filterScale: '' })
            Object.assign(kwDerive, { data: [], loading: false, executing: false, result: null })
            Object.assign(pgDerive, { data: [], loading: false, executing: false, result: null })
            Object.assign(scaleDerive, { data: [], loading: false, executing: false, result: null })
            Object.assign(collectorState, { testing: false, reloading: false, status: null, message: '' })
            Object.assign(delState, { fileName: null, totalRows: 0, uploadId: null, dryRun: true, executing: false, result: null })
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
        const selectedGwId = ref(null)
        const selectedGw = computed(() => gwList.value.find(g => g.id === selectedGwId.value) || null)
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
                    const def = gwList.value.find(g => g.is_default)
                    if (def && !selectedGwId.value) selectedGwId.value = def.id
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

        function onGwChange() {
            structureSync.counts = null
        }

        function _resetGwForm() {
            gwForm.editing = null; gwForm.name = ''; gwForm.url = ''
            gwForm.username = ''; gwForm.password = ''; gwForm.zone = ''
            gwForm.verify_ssl = true; gwForm.is_default = false; gwForm.error = ''
        }

        // ── Structure Sync ──
        const structureSync = reactive({ loading: false, counts: null })

        async function syncStructure() {
            if (!selectedGwId.value) return
            structureSync.loading = true
            try {
                const resp = await fetch(`/api/kw/gateways/${selectedGwId.value}/sync`, {
                    method: 'POST', headers: _headers(),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                structureSync.counts = { channels: data.channels, devices: data.devices, groups: data.groups }
            } catch (e) { alert('結構同步失敗: ' + e.message) }
            finally { structureSync.loading = false }
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
                const resp = await fetch('/api/csv/upload', { method: 'POST', body: form })
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
        })

        async function loadStaging() {
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

        // Step 3: Kepware Derive + Execute
        const kwDerive = reactive({ data: [], loading: false, executing: false, result: null })

        async function deriveKw() {
            kwDerive.loading = true; kwDerive.result = null
            try {
                const body = { tb_status: 'pending' }
                if (selectedGwId.value) body.gateway_id = selectedGwId.value
                const resp = await fetch('/api/kw/derive', {
                    method: 'POST', headers: _headers(), body: JSON.stringify(body),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                kwDerive.data = data.data || []
            } catch (e) { alert('推導失敗: ' + e.message) }
            finally { kwDerive.loading = false }
        }

        async function saveKwOverride(row) {
            try {
                const resp = await fetch('/api/pg/staging/kw-override', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({
                        id: row.id,
                        channel: row.channel_name || '',
                        device: row.device_name || '',
                        tag_groups: row.tag_groups || '',
                    }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                row.overridden = true
            } catch (e) { alert('儲存覆寫失敗: ' + e.message) }
        }

        async function executeKw() {
            if (!kwDerive.data.length) return
            if (!confirm(`確定要建立 ${kwDerive.data.length} 筆 Tag 到 Kepware？`)) return
            kwDerive.executing = true; kwDerive.result = null
            try {
                const body = { delay: 0.2, batch_size: 50, batch_pause: 5 }
                if (selectedGwId.value) body.gateway_id = selectedGwId.value
                const resp = await fetch('/api/kw/execute', {
                    method: 'POST', headers: _headers(), body: JSON.stringify(body),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                kwDerive.result = await resp.json()
                await deriveKw()
            } catch (e) { kwDerive.result = { success: 0, failed: 1, message: e.message } }
            finally { kwDerive.executing = false }
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
        const scaleDerive = reactive({ data: [], loading: false, executing: false, result: null })

        async function deriveScale() {
            scaleDerive.loading = true; scaleDerive.result = null
            try {
                const resp = await fetch('/api/pg/derive/scale', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({ scale_status: 'pending' }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                scaleDerive.data = data.data || []
            } catch (e) { alert('Scale 推導失敗: ' + e.message) }
            finally { scaleDerive.loading = false }
        }

        async function executeScale() {
            if (!scaleDerive.data.length) return
            if (!confirm(`確定要設定 ${scaleDerive.data.length} 筆 Tag 的 Scale？`)) return
            scaleDerive.executing = true; scaleDerive.result = null
            try {
                const body = { delay: 0.2, batch_size: 50, batch_pause: 5 }
                if (selectedGwId.value) body.gateway_id = selectedGwId.value
                const resp = await fetch('/api/pg/execute/scale', {
                    method: 'POST', headers: _headers(), body: JSON.stringify(body),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                scaleDerive.result = await resp.json()
                await deriveScale()
            } catch (e) { scaleDerive.result = { success: 0, failed: 1, message: e.message } }
            finally { scaleDerive.executing = false }
        }

        // Step 6: Collector
        const collectorState = reactive({ testing: false, reloading: false, status: null, message: '' })

        async function testCollector() {
            collectorState.testing = true; collectorState.status = null
            try {
                const resp = await fetch('/api/collector/test')
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
            dryRun: true, executing: false, result: null,
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
                const resp = await fetch('/api/csv/upload', { method: 'POST', body: form })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                delState.uploadId = data.upload_id; delState.fileName = data.filename
                delState.totalRows = data.total_rows; delState.result = null
            } catch (e) { alert('CSV 上傳失敗: ' + e.message) }
        }

        async function executeDelete() {
            if (!delState.uploadId || !selectedGwId.value) return
            if (!delState.dryRun && !confirm('確定要刪除？此操作無法復原！')) return
            delState.executing = true; delState.result = null
            try {
                const resp = await fetch('/api/kw/delete-batch', {
                    method: 'POST', headers: _headers(),
                    body: JSON.stringify({
                        upload_id: delState.uploadId,
                        gateway_id: selectedGwId.value,
                        dry_run: delState.dryRun,
                    }),
                })
                if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail) }
                const data = await resp.json()
                const taskId = data.task_id
                let attempts = 0          // 最多輪詢 600 次（約 10 分鐘）後放棄
                let errors = 0            // 連續錯誤達 5 次才中止，容忍暫時性失敗
                const poll = async () => {
                    if (++attempts > 600) {
                        alert('刪除任務逾時，請至「歷史」分頁確認結果')
                        delState.executing = false
                        return
                    }
                    try {
                        const sr = await fetch(`/api/tasks/${taskId}`, { headers: _headers() })
                        if (!sr.ok) {
                            if (++errors >= 5) { alert('無法取得刪除任務狀態，請至「歷史」分頁確認'); delState.executing = false; return }
                            setTimeout(poll, 1000); return
                        }
                        errors = 0
                        const st = await sr.json()
                        if (st.done) {
                            delState.result = { success: st.summary?.success || 0, fail: st.summary?.fail || 0 }
                            delState.executing = false
                        } else {
                            setTimeout(poll, 1000)
                        }
                    } catch (e) {
                        if (++errors >= 5) { alert('輪詢刪除狀態失敗: ' + e.message); delState.executing = false; return }
                        setTimeout(poll, 1000)
                    }
                }
                poll()
            } catch (e) { alert('刪除失敗: ' + e.message); delState.executing = false }
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

        function downloadIoMapping() {
            if (!ioMapping.mappingId) return
            window.open(`/api/io-mapping/download/${ioMapping.mappingId}`, '_blank')
        }

        // ── History ──
        const historyList = ref([])

        async function loadHistory() {
            try {
                const resp = await fetch('/api/history')
                if (resp.ok) historyList.value = await resp.json()
            } catch (e) { console.error('載入歷史失敗:', e) }
        }

        // ── Settings: PG Connection ──
        const pgConn = reactive({ testing: false, status: '', statusText: '未連線' })

        async function testPgConn() {
            pgConn.testing = true
            try {
                const resp = await fetch('/api/pg/test')
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
                const resp = await fetch(`/api/pg/ref/${key}`)
                if (resp.ok) refData[key] = await resp.json()
            } catch (e) { console.error(`載入 ${key} 失敗:`, e) }
        }

        async function deleteRefRow(table, id) {
            if (!confirm('確定刪除此筆資料？')) return
            try {
                await fetch('/api/pg/ref/delete', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ table, id }),
                })
                if (activeRefTable.value) loadRefTable(activeRefTable.value)
            } catch (e) { alert('刪除失敗: ' + e.message) }
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
            if (auth.role === 'admin') loadUsers()
        }

        if (auth.token) _initAfterLogin()

        return {
            auth, loginForm, doLogin, doLogout, clock,
            tabs, activeTab,
            gwList, selectedGwId, selectedGw, gwForm,
            loadGateways, saveGw, editGw, deleteGw, testGw, onGwChange,
            structureSync, syncStructure,
            importSteps, importStep,
            imp, handleFile, onDrop, importToStaging, resetUpload,
            staging, loadStaging,
            kwDerive, deriveKw, executeKw, saveKwOverride, rowStatus,
            pgDerive, derivePg, executePg,
            scaleDerive, deriveScale, executeScale,
            collectorState, testCollector, reloadCollector,
            delState, handleDeleteFile, onDeleteDrop, executeDelete,
            QUERY_FIELDS, QUERY_OPS, dbQuery, addCondition, removeCondition, searchTags, exportTags,
            ioMapping, executeIoMapping, downloadIoMapping,
            historyList, loadHistory,
            pgConn, testPgConn,
            refTables, refData, activeRefTable, refTableCols, loadRefTable, deleteRefRow,
            userList, showUserCreate, newUser, loadUsers, createUser, resetUserPw, deleteUser,
            actLogs, loadActivityLogs,
            showPwDialog, pwForm, changePassword,
            chatBodyRef, chat, chatSend,
            formatTime, statusKind,
        }
    }
}).mount('#app')
