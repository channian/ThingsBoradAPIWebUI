export function useIoMapping(auth) {
    const { reactive, computed } = Vue

    const ioMap = reactive({
        aFile: null, bFile: null, aFileName: '', bFileName: '',
        running: false, mappingId: '', columns: [],
        data: [], total: 0, matched: 0, unmatched: 0,
        filter: 'all', page: 1,
    })

    function ioMapPickFile(event, which) {
        const file = event.dataTransfer ? event.dataTransfer.files[0] : event.target.files[0]
        if (!file) return
        if (which === 'a') { ioMap.aFile = file; ioMap.aFileName = file.name }
        else { ioMap.bFile = file; ioMap.bFileName = file.name }
    }

    const filteredIoMap = computed(() => {
        if (ioMap.filter === 'matched') return ioMap.data.filter(r => r._matched)
        if (ioMap.filter === 'unmatched') return ioMap.data.filter(r => !r._matched)
        return ioMap.data
    })
    const ioMapTotalPages = computed(() => Math.max(1, Math.ceil(filteredIoMap.value.length / 50)))
    const pagedIoMap = computed(() => {
        const start = (ioMap.page - 1) * 50
        return filteredIoMap.value.slice(start, start + 50)
    })

    async function executeIoMapping() {
        if (!ioMap.aFile || !ioMap.bFile) return
        ioMap.running = true; ioMap.data = []; ioMap.mappingId = ''; ioMap.page = 1
        try {
            const form = new FormData()
            form.append('a_file', ioMap.aFile)
            form.append('b_file', ioMap.bFile)
            const headers = auth.token ? { 'Authorization': `Bearer ${auth.token}` } : {}
            const resp = await fetch('/api/io-mapping/execute', { method: 'POST', headers, body: form })
            if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || 'Mapping 失敗') }
            const result = await resp.json()
            ioMap.mappingId = result.mapping_id
            ioMap.columns = result.columns
            ioMap.data = result.data
            ioMap.total = result.total
            ioMap.matched = result.matched
            ioMap.unmatched = result.unmatched
        } catch (e) { alert('Mapping 失敗: ' + e.message) }
        finally { ioMap.running = false }
    }

    function downloadIoMapping() {
        if (!ioMap.mappingId) return
        window.open(`/api/io-mapping/download/${ioMap.mappingId}`, '_blank')
    }

    return {
        ioMap, ioMapPickFile, filteredIoMap, ioMapTotalPages, pagedIoMap,
        executeIoMapping, downloadIoMapping,
    }
}
