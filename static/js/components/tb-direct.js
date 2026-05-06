import { authHeaders } from '../shared.js'

export function useTbDirect(auth, conn, kwGw, dragTarget) {
    const { reactive, ref, computed, nextTick } = Vue

    function newOpState(defaults = {}) {
        return reactive({
            uploadId: null, fileName: null, headers: [], preview: [], totalRows: 0,
            uniqueTypes: [], duplicateNames: [],
            dryRun: true, running: false, taskId: null, showSettings: false,
            progress: { current: 0, total: 0, success: 0, fail: 0, skip: 0 },
            logs: [], summary: null,
            settings: { delay: 0.2, batchSize: 50, batchPause: 5 },
            ...defaults
        })
    }
    const create = newOpState()
    const del = newOpState()

    const modal = reactive({ show: false, title: '', message: '', onConfirm: () => {} })
    const createLogArea = ref(null)
    const deleteLogArea = ref(null)

    const createProgress = computed(() => create.progress.total === 0 ? 0 : Math.round(create.progress.current / create.progress.total * 100))
    const deleteProgress = computed(() => del.progress.total === 0 ? 0 : Math.round(del.progress.current / del.progress.total * 100))
    const hasTypeHeader = computed(() => create.uniqueTypes.length > 0)

    const dpCheck = reactive({
        loading: false, checked: false,
        results: {}, validCount: 0, invalidCount: 0,
    })

    async function testConnection() {
        conn.status = 'connecting'
        try {
            const resp = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tb_url: conn.url, username: conn.username, password: conn.password }) })
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail || 'Login failed') }
            const data = await resp.json()
            conn.token = data.token
            conn.status = 'connected'
        } catch (e) { conn.status = 'error'; alert('連線失敗: ' + e.message) }
    }

    async function uploadCSV(operation, file) {
        const state = operation === 'create' ? create : del
        const formData = new FormData()
        formData.append('file', file)
        try {
            const resp = await fetch('/api/csv/upload', { method: 'POST', body: formData })
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail || 'Upload failed') }
            const data = await resp.json()
            state.uploadId = data.upload_id; state.headers = data.headers; state.preview = data.preview
            state.totalRows = data.total_rows; state.fileName = data.filename
            state.uniqueTypes = data.unique_types || []
            state.duplicateNames = data.duplicate_names || []
            state.taskId = null; state.logs = []; state.summary = null
            state.progress = { current: 0, total: 0, success: 0, fail: 0, skip: 0 }
            dpCheck.checked = false
        } catch (e) { alert('CSV 上傳失敗: ' + e.message) }
    }

    function handleFileSelect(op, event) { const file = event.target.files[0]; if (file) uploadCSV(op, file) }
    function handleDrop(op, event) { dragTarget.value = null; const file = event.dataTransfer.files[0]; if (file && file.name.endsWith('.csv')) { uploadCSV(op, file) } else { alert('請上傳 .csv 檔案') } }

    function connectSSE(taskId, state, logAreaRef) {
        const evtSource = new EventSource('/api/tasks/' + taskId + '/stream')
        evtSource.onmessage = (event) => {
            const data = JSON.parse(event.data)
            if (data.type === 'log') {
                state.logs.push({ level: data.level, message: data.message })
                nextTick(() => { if (logAreaRef.value) logAreaRef.value.scrollTop = logAreaRef.value.scrollHeight })
            } else if (data.type === 'progress') {
                state.progress = { current: data.current, total: data.total, success: data.success, fail: data.fail, skip: data.skip }
            } else if (data.type === 'complete') {
                state.summary = { total: data.total, success: data.success, fail: data.fail, skip: data.skip }
                state.running = false; evtSource.close()
            }
        }
        evtSource.onerror = () => { state.running = false; evtSource.close() }
    }

    async function startTask(operation) {
        const state = operation === 'create' ? create : del
        if (!state.uploadId || conn.status !== 'connected') return
        state.running = true; state.logs = []; state.summary = null
        state.progress = { current: 0, total: 0, success: 0, fail: 0, skip: 0 }
        try {
            const resp = await fetch('/api/tasks/execute', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_id: state.uploadId, operation, dry_run: state.dryRun,
                    tb_url: conn.url, tb_username: conn.username, tb_password: conn.password,
                    delay: state.settings.delay, batch_size: state.settings.batchSize, batch_pause: state.settings.batchPause,
                    csv_filename: state.fileName || '' }) })
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail || 'Execute failed') }
            const data = await resp.json()
            state.taskId = data.task_id
            connectSSE(data.task_id, state, operation === 'create' ? createLogArea : deleteLogArea)
        } catch (e) { state.running = false; alert('啟動任務失敗: ' + e.message) }
    }

    async function startKwDelete() {
        if (!del.uploadId) return
        if (kwGw.status !== 'connected') { alert('請先連線 Kepware Gateway'); return }
        del.running = true; del.logs = []; del.summary = null
        del.progress = { current: 0, total: 0, success: 0, fail: 0, skip: 0 }
        try {
            const resp = await fetch('/api/kw/delete-batch', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': auth.token ? `Bearer ${auth.token}` : '' },
                body: JSON.stringify({ upload_id: del.uploadId, dry_run: del.dryRun,
                    kw_gw_url: kwGw.url, kw_gw_username: kwGw.username, kw_gw_password: kwGw.password,
                    delay: del.settings.delay, batch_size: del.settings.batchSize, batch_pause: del.settings.batchPause }) })
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail || 'Execute failed') }
            const data = await resp.json()
            del.taskId = data.task_id
            connectSSE(data.task_id, del, deleteLogArea)
        } catch (e) { del.running = false; alert('啟動任務失敗: ' + e.message) }
    }

    function confirmDelete() {
        if (del.dryRun) { startKwDelete(); return }
        modal.title = '確認刪除'; modal.message = `即將以 LIVE 模式刪除 ${del.totalRows} 筆 Tag，此操作無法復原！確定要繼續嗎？`
        modal.onConfirm = () => startKwDelete(); modal.show = true
    }

    async function checkDeviceProfiles() {
        if (conn.status !== 'connected') { alert('請先連線 ThingsBoard'); return }
        dpCheck.loading = true; dpCheck.checked = false
        const types = create.uniqueTypes || []
        if (types.length === 0) { dpCheck.loading = false; alert('CSV 中無 type 欄位資料'); return }
        try {
            const resp = await fetch('/api/device-profiles/check', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tb_url: conn.url, tb_token: conn.token, type_names: types })
            })
            if (!resp.ok) { const err = await resp.json(); throw new Error(err.detail || 'Check failed') }
            const data = await resp.json()
            dpCheck.results = data.check_results
            dpCheck.validCount = data.valid_count
            dpCheck.invalidCount = data.invalid_count
            dpCheck.checked = true
        } catch (e) { alert('DeviceProfile 驗證失敗: ' + e.message) }
        finally { dpCheck.loading = false }
    }

    return {
        create, del, modal, createLogArea, deleteLogArea,
        createProgress, deleteProgress, hasTypeHeader, dpCheck,
        testConnection, uploadCSV, handleFileSelect, handleDrop,
        startTask, confirmDelete, checkDeviceProfiles, connectSSE,
    }
}
