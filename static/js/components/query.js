export function useQuery(conn, connectSSE) {
    const { reactive, ref, computed } = Vue

    const query = reactive({
        search: '', devices: [], page: 0, pageSize: 20,
        totalPages: 0, totalElements: 0, hasNext: false,
        loading: false, searched: false, selected: [],
        taskId: null, logs: [], summary: null,
    })
    const queryLogArea = ref(null)
    const isAllSelected = computed(() => query.devices.length > 0 && query.devices.every(d => query.selected.includes(d.id.id)))

    async function queryDevices(page) {
        if (conn.status !== 'connected') { alert('請先連線 ThingsBoard'); return }
        query.loading = true; query.page = Math.max(0, page)
        try {
            const resp = await fetch('/api/devices/query', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tb_url: conn.url, tb_token: conn.token, page: query.page, page_size: query.pageSize, text_search: query.search || null }) })
            if (!resp.ok) throw new Error('Query failed')
            const data = await resp.json()
            query.devices = data.data || []; query.totalPages = data.totalPages || 0
            query.totalElements = data.totalElements || 0; query.hasNext = data.hasNext || false
            query.searched = true; query.selected = []
        } catch (e) { alert('查詢失敗: ' + e.message) } finally { query.loading = false }
    }

    function toggleSelectAll() { if (isAllSelected.value) { query.selected = [] } else { query.selected = query.devices.map(d => d.id.id) } }

    function confirmDeleteSelected(modal) {
        modal.title = '確認刪除已選裝置'; modal.message = `即將刪除 ${query.selected.length} 筆裝置，此操作無法復原！`
        modal.onConfirm = () => deleteSelected(); modal.show = true
    }

    async function deleteSelected() {
        if (!query.selected.length) return
        query.logs = []; query.summary = null
        try {
            const resp = await fetch('/api/devices/delete-direct', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tb_url: conn.url, tb_username: conn.username, tb_password: conn.password, device_ids: query.selected, dry_run: false }) })
            if (!resp.ok) throw new Error('Delete failed')
            const data = await resp.json(); query.taskId = data.task_id
            connectSSE(data.task_id, query, queryLogArea)
        } catch (e) { alert('刪除失敗: ' + e.message) }
    }

    async function exportDevices() {
        try {
            const resp = await fetch('/api/devices/export', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tb_url: conn.url, tb_token: conn.token, text_search: query.search || null }) })
            if (!resp.ok) throw new Error('Export failed')
            const blob = await resp.blob()
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a'); a.href = url
            a.download = resp.headers.get('content-disposition')?.match(/filename="(.+)"/)?.[1] || 'devices_export.csv'
            a.click(); URL.revokeObjectURL(url)
        } catch (e) { alert('匯出失敗: ' + e.message) }
    }

    return {
        query, queryLogArea, isAllSelected,
        queryDevices, toggleSelectAll, confirmDeleteSelected, deleteSelected, exportDevices,
    }
}
