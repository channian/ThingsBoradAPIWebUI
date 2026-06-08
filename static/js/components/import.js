import { authHeaders, userLogout } from '../shared.js'

export function useImport(auth, conn, dragTarget) {
    const { reactive, ref } = Vue

    const importSubTab = ref('upload')
    const pgConn = reactive({ status: 'disconnected', statusText: '未連線', testing: false })
    const imp = reactive({
        uploadId: null, fileName: null, headers: [], preview: [], totalRows: 0,
        duplicateNames: [], importing: false, result: null,
    })
    const staging = reactive({
        data: [], total: 0, page: 0, pageSize: 50, totalPages: 0,
        filterTb: '', filterPg: '', filterScale: '', loading: false,
        selected: [], clearing: false,
    })
    const tbDerive = reactive({ data: [], loading: false, executing: false, result: null, page: 0, pageSize: 50 })
    const pgDerive = reactive({ data: [], loading: false, executing: false, result: null, page: 0, pageSize: 50 })
    const importSettings = reactive({
        delay: 0.2, batchSize: 50, batchPause: 5, autoChain: false,
    })

    const kwGw = reactive({
        url: localStorage.getItem('kw_gw_url') || '',
        username: localStorage.getItem('kw_gw_username') || '',
        password: '',
        status: 'disconnected', statusText: '未連線', testing: false,
    })
    const scaleDerive = reactive({ data: [], loading: false, executing: false, result: null, page: 0, pageSize: 50 })
    const scaleSettings = reactive({ delay: 0.2, batchSize: 50, batchPause: 5 })

    const collectorConn = reactive({ status: 'disconnected', statusText: '未連線', testing: false })
    const collectorReload = reactive({ loading: false, result: null })

    function _headers() { return authHeaders(auth) }

    async function testPGConnection() {
        pgConn.testing = true
        try {
            const resp = await fetch('/api/pg/test')
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail) }
            pgConn.status = 'connected'; pgConn.statusText = '已連線'
        } catch (e) { pgConn.status = 'error'; pgConn.statusText = '連線失敗: ' + e.message }
        finally { pgConn.testing = false }
    }

    async function testCollectorConnection() {
        collectorConn.testing = true
        try {
            const resp = await fetch('/api/collector/test')
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail) }
            collectorConn.status = 'connected'; collectorConn.statusText = '已連線'
        } catch (e) { collectorConn.status = 'error'; collectorConn.statusText = '連線失敗: ' + e.message }
        finally { collectorConn.testing = false }
    }

    async function handleImportFile(event) {
        const file = event.target.files[0]
        if (file) uploadImportCSV(file)
    }
    function handleImportDrop(event) {
        dragTarget.value = null
        const file = event.dataTransfer.files[0]
        if (file && file.name.endsWith('.csv')) uploadImportCSV(file)
        else alert('請上傳 .csv 檔案')
    }

    async function uploadImportCSV(file) {
        const formData = new FormData()
        formData.append('file', file)
        try {
            const resp = await fetch('/api/csv/upload', { method: 'POST', body: formData })
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail) }
            const data = await resp.json()
            imp.uploadId = data.upload_id; imp.headers = data.headers
            imp.preview = data.preview; imp.totalRows = data.total_rows
            imp.fileName = data.filename; imp.result = null
            imp.duplicateNames = data.duplicate_names || []
        } catch (e) { alert('CSV 上傳失敗: ' + e.message) }
    }

    async function importToStaging() {
        if (!imp.uploadId) return
        imp.importing = true
        try {
            const resp = await fetch('/api/pg/staging/import', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({ upload_id: imp.uploadId })
            })
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail) }
            imp.result = await resp.json()
        } catch (e) { alert('匯入失敗: ' + e.message) }
        finally { imp.importing = false }
    }

    async function loadStaging() {
        staging.loading = true
        try {
            const resp = await fetch('/api/pg/staging/query', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({
                    page: staging.page, page_size: staging.pageSize,
                    tb_status: staging.filterTb || null,
                    pg_status: staging.filterPg || null,
                    scale_status: staging.filterScale || null,
                })
            })
            if (!resp.ok) throw new Error('Query failed')
            const data = await resp.json()
            staging.data = data.data; staging.total = data.total
            staging.totalPages = data.total_pages
        } catch (e) { console.error('載入暫存資料失敗:', e) }
        finally { staging.loading = false }
    }

    async function deleteSelectedStaging() {
        if (!staging.selected.length) return
        if (!confirm(`確定刪除勾選的 ${staging.selected.length} 筆暫存資料？`)) return
        try {
            const resp = await fetch('/api/pg/staging/delete', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({ ids: staging.selected })
            })
            if (resp.status === 401) { userLogout(auth); return }
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail) }
            const data = await resp.json()
            alert(`已刪除 ${data.deleted} 筆`)
            staging.selected = []
            loadStaging()
        } catch (e) { alert('刪除失敗: ' + e.message) }
    }

    async function clearAllStaging() {
        if (!confirm('確定清空全部暫存表資料？此操作無法復原！')) return
        staging.clearing = true
        try {
            const resp = await fetch('/api/pg/staging/clear', { method: 'DELETE', headers: _headers() })
            if (resp.status === 401) { userLogout(auth); return }
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail) }
            const data = await resp.json()
            alert(`已清空 ${data.deleted} 筆`)
            staging.selected = []
            loadStaging()
        } catch (e) { alert('清空失敗: ' + e.message) }
        finally { staging.clearing = false }
    }

    function toggleStagingSelect(id) {
        const idx = staging.selected.indexOf(id)
        if (idx === -1) staging.selected.push(id)
        else staging.selected.splice(idx, 1)
    }

    function toggleStagingSelectAll() {
        if (staging.selected.length === staging.data.length) {
            staging.selected = []
        } else {
            staging.selected = staging.data.map(r => r.id)
        }
    }

    function pagedTbData() {
        const start = tbDerive.page * tbDerive.pageSize
        return tbDerive.data.slice(start, start + tbDerive.pageSize)
    }
    function tbTotalPages() { return Math.max(1, Math.ceil(tbDerive.data.length / tbDerive.pageSize)) }
    function tbPrevPage() { if (tbDerive.page > 0) tbDerive.page-- }
    function tbNextPage() { if (tbDerive.page < tbTotalPages() - 1) tbDerive.page++ }

    function pagedPgData() {
        const start = pgDerive.page * pgDerive.pageSize
        return pgDerive.data.slice(start, start + pgDerive.pageSize)
    }
    function pgTotalPages() { return Math.max(1, Math.ceil(pgDerive.data.length / pgDerive.pageSize)) }
    function pgPrevPage() { if (pgDerive.page > 0) pgDerive.page-- }
    function pgNextPage() { if (pgDerive.page < pgTotalPages() - 1) pgDerive.page++ }

    async function deriveTB() {
        tbDerive.loading = true
        try {
            const resp = await fetch('/api/pg/derive/tb', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({ tb_status: 'pending' })
            })
            if (!resp.ok) throw new Error('Derive failed')
            const data = await resp.json()
            tbDerive.data = data.data || []
            tbDerive.page = 0
        } catch (e) { console.error('TB 推導失敗:', e) }
        finally { tbDerive.loading = false }
    }

    async function derivePG() {
        pgDerive.loading = true
        try {
            const resp = await fetch('/api/pg/derive/pg', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({ pg_status: 'pending' })
            })
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}))
                throw new Error(err.detail || `HTTP ${resp.status}`)
            }
            const data = await resp.json()
            pgDerive.data = data.data || []
            pgDerive.page = 0
        } catch (e) {
            console.error('PG 推導失敗:', e)
            alert('PG 推導失敗: ' + e.message)
        }
        finally { pgDerive.loading = false }
    }

    async function executeTB() {
        if (!tbDerive.data.length) return
        if (kwGw.status !== 'connected') { alert('請先連線 Kepware Gateway'); return }
        const chainMsg = importSettings.autoChain ? '（含 PG 匯入）' : ''
        if (!confirm(`確定要建立 ${tbDerive.data.length} 筆 Tag 到 Kepware？${chainMsg}`)) return
        tbDerive.executing = true
        tbDerive.result = null
        try {
            const resp = await fetch('/api/pg/execute/tb', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({
                    kw_gw_url: kwGw.url, kw_gw_username: kwGw.username, kw_gw_password: kwGw.password,
                    delay: importSettings.delay, batch_size: importSettings.batchSize, batch_pause: importSettings.batchPause,
                })
            })
            if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || 'Execute failed') }
            tbDerive.result = await resp.json()
            await deriveTB()
            if (importSettings.autoChain) {
                await derivePG()
                if (pgDerive.data.length) { await executePG(true) }
            }
        } catch (e) {
            tbDerive.result = { message: `建點失敗: ${e.message}`, failed: 1 }
        } finally { tbDerive.executing = false }
    }

    async function executePG(skipConfirm = false) {
        if (!pgDerive.data.length) return
        if (!skipConfirm && !confirm(`確定要將 ${pgDerive.data.length} 筆資料寫入 PG 正式表（含 Collector）？`)) return
        pgDerive.executing = true
        pgDerive.result = null
        try {
            const resp = await fetch('/api/pg/execute/pg', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({})
            })
            if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || 'Execute failed') }
            const data = await resp.json()
            console.log('[executePG] 回應:', JSON.stringify(data, null, 2))
            if (data.errors && data.errors.length > 0) {
                console.error('[executePG] 錯誤明細:', data.errors)
            }
            pgDerive.result = data
            await derivePG()
        } catch (e) {
            pgDerive.result = { message: `寫入失敗: ${e.message}`, errors: [1] }
        } finally { pgDerive.executing = false }
    }

    async function testKwGwConnection() {
        if (!kwGw.url) { alert('請輸入 Kepware Gateway URL'); return }
        kwGw.testing = true
        try {
            const resp = await fetch('/api/kw-gw/test', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ kw_gw_url: kwGw.url, kw_gw_username: kwGw.username, kw_gw_password: kwGw.password })
            })
            if (!resp.ok) { const err = await resp.json().catch(() => ({})); throw new Error(err.detail || `HTTP ${resp.status}`) }
            kwGw.status = 'connected'; kwGw.statusText = '已連線'
            localStorage.setItem('kw_gw_url', kwGw.url)
            localStorage.setItem('kw_gw_username', kwGw.username)
        } catch (e) {
            kwGw.status = 'error'; kwGw.statusText = `連線失敗: ${e.message}`
        } finally { kwGw.testing = false }
    }

    function pagedScaleData() {
        const start = scaleDerive.page * scaleDerive.pageSize
        return scaleDerive.data.slice(start, start + scaleDerive.pageSize)
    }
    function scaleTotalPages() { return Math.max(1, Math.ceil(scaleDerive.data.length / scaleDerive.pageSize)) }
    function scalePrevPage() { if (scaleDerive.page > 0) scaleDerive.page-- }
    function scaleNextPage() { if (scaleDerive.page < scaleTotalPages() - 1) scaleDerive.page++ }

    async function deriveScale() {
        scaleDerive.loading = true
        try {
            const resp = await fetch('/api/pg/derive/scale', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({ scale_status: 'pending' })
            })
            if (!resp.ok) { const err = await resp.json().catch(() => ({})); throw new Error(err.detail || `HTTP ${resp.status}`) }
            const data = await resp.json()
            scaleDerive.data = data.data || []
            scaleDerive.page = 0
        } catch (e) {
            console.error('Scale 推導失敗:', e)
            alert('Scale 推導失敗: ' + e.message)
        } finally { scaleDerive.loading = false }
    }

    async function executeScale() {
        if (!scaleDerive.data.length) return
        if (kwGw.status !== 'connected') { alert('請先連線 Kepware Gateway'); return }
        if (!confirm(`確定要設定 ${scaleDerive.data.length} 筆 Tag 的 Scale？`)) return
        scaleDerive.executing = true
        scaleDerive.result = null
        try {
            const resp = await fetch('/api/pg/execute/scale', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({
                    kw_gw_url: kwGw.url, kw_gw_username: kwGw.username, kw_gw_password: kwGw.password,
                    delay: scaleSettings.delay, batch_size: scaleSettings.batchSize, batch_pause: scaleSettings.batchPause,
                })
            })
            if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || 'Execute failed') }
            scaleDerive.result = await resp.json()
            await deriveScale()
        } catch (e) {
            scaleDerive.result = { message: `Scale 設定失敗: ${e.message}`, errors: [1] }
        } finally { scaleDerive.executing = false }
    }

    async function executeCollectorReload() {
        if (!confirm('確定要呼叫 Collector Reload？請確認 tags 表已寫入完成。')) return
        collectorReload.loading = true
        collectorReload.result = null
        try {
            const resp = await fetch('/api/collector/reload', {
                method: 'POST', headers: _headers(),
            })
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}))
                throw new Error(err.detail || `HTTP ${resp.status}`)
            }
            const data = await resp.json()
            collectorReload.result = {
                success: true,
                message: `Collector 重載成功，共 ${data.total_tags || '?'} 筆 tag`,
            }
        } catch (e) {
            collectorReload.result = { success: false, message: `重載失敗: ${e.message}` }
        } finally { collectorReload.loading = false }
    }

    return {
        importSubTab, pgConn, imp, staging, tbDerive, pgDerive, importSettings,
        kwGw, scaleDerive, scaleSettings,
        collectorConn, collectorReload,
        testPGConnection, testCollectorConnection,
        handleImportFile, handleImportDrop, importToStaging,
        loadStaging, deleteSelectedStaging, clearAllStaging, toggleStagingSelect, toggleStagingSelectAll,
        deriveTB, derivePG, executeTB, executePG,
        pagedTbData, tbTotalPages, tbPrevPage, tbNextPage,
        pagedPgData, pgTotalPages, pgPrevPage, pgNextPage,
        testKwGwConnection, deriveScale, executeScale,
        pagedScaleData, scaleTotalPages, scalePrevPage, scaleNextPage,
        executeCollectorReload,
    }
}
