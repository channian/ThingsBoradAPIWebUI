export function useHistory() {
    const { ref } = Vue

    const history = ref([])

    async function loadHistory() {
        try { const resp = await fetch('/api/history'); history.value = await resp.json() } catch (e) {}
    }

    async function clearHistory() {
        if (!confirm('確定要清除所有歷史紀錄嗎？')) return
        try { await fetch('/api/history', { method: 'DELETE' }); history.value = [] } catch (e) {}
    }

    function hasTaskInMemory(taskId) {
        return true
    }

    return { history, loadHistory, clearHistory, hasTaskInMemory }
}
