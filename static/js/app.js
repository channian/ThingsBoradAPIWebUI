import {
    AUTH_INITIAL,
    authHeaders as _authHeaders,
    userLogin as _userLogin,
    userLogout as _userLogout,
    formatTime, formatISOTime, typeLabel, truncate,
} from './shared.js'
import { useTbDirect } from './components/tb-direct.js'
import { useIoMapping } from './components/io-mapping.js'
import { useQuery } from './components/query.js'
import { useHistory } from './components/history.js'
import { useImport } from './components/import.js'
import { useSettings } from './components/settings.js'

const { createApp, reactive, ref, computed, watch } = Vue

createApp({
    setup() {
        const auth = reactive({ ...AUTH_INITIAL })
        const conn = reactive({ url: '', username: '', password: '', token: null, status: 'disconnected' })
        const activeTab = ref('create')

        const connStatusText = computed(() => ({
            disconnected: '未連線', connecting: '連線中...', connected: '已連線', error: '連線失敗',
        })[conn.status])
        const canKepwareImport = computed(() => auth.role === 'admin' || auth.role === 'operator')

        const userLogin = () => _userLogin(auth)
        const userLogout = () => _userLogout(auth)
        const authHeaders = () => _authHeaders(auth)

        const dragTarget = ref(null)
        const importComp = useImport(auth, conn, dragTarget)
        const tbDirect = useTbDirect(auth, conn, importComp.kwGw, dragTarget)
        const ioMapping = useIoMapping(auth)
        const queryComp = useQuery(conn, tbDirect.connectSSE)
        const historyComp = useHistory()
        const settings = useSettings(auth, conn)

        const confirmDeleteSelected = () => queryComp.confirmDeleteSelected(tbDirect.modal)

        const saved = localStorage.getItem('tb_conn')
        if (saved) { try { const s = JSON.parse(saved); conn.url = s.url || ''; conn.username = s.username || '' } catch (e) {} }
        watch(() => [conn.url, conn.username], () => {
            localStorage.setItem('tb_conn', JSON.stringify({ url: conn.url, username: conn.username }))
        })

        return {
            auth, userLogin, userLogout, authHeaders, canKepwareImport,
            conn, connStatusText, activeTab, dragTarget,
            formatTime, formatISOTime, typeLabel, truncate,
            ...tbDirect,
            ...ioMapping,
            ...queryComp,
            confirmDeleteSelected,
            ...historyComp,
            ...importComp,
            ...settings,
        }
    }
}).mount('#app')
