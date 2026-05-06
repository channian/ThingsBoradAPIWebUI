import { authHeaders, userLogout } from '../shared.js'

export function useSettings(auth, conn) {
    const { reactive, ref, computed } = Vue

    function _headers() { return authHeaders(auth) }

    // ── Password Change ──
    const showPwDialog = ref(false)
    const pwForm = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })
    const pwMsg = reactive({ text: '', error: false })

    async function changeMyPassword() {
        pwMsg.text = ''; pwMsg.error = false
        if (!pwForm.newPassword) { pwMsg.text = '請輸入新密碼'; pwMsg.error = true; return }
        if (pwForm.newPassword !== pwForm.confirmPassword) { pwMsg.text = '兩次密碼不一致'; pwMsg.error = true; return }
        try {
            const resp = await fetch('/api/user/change-password', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({ old_password: pwForm.oldPassword, new_password: pwForm.newPassword })
            })
            if (resp.status === 401) { userLogout(auth); return }
            if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || '變更失敗') }
            pwMsg.text = '密碼已變更'; pwMsg.error = false
            pwForm.oldPassword = ''; pwForm.newPassword = ''; pwForm.confirmPassword = ''
        } catch (e) { pwMsg.text = e.message; pwMsg.error = true }
    }

    // ── User Management ──
    const userList = ref([])
    const userForm = reactive({ username: '', password: '', displayName: '', role: 'operator' })

    async function loadUsers() {
        try {
            const resp = await fetch('/api/admin/users', { headers: _headers() })
            if (resp.status === 401) { userLogout(auth); return }
            if (resp.ok) userList.value = await resp.json()
        } catch (e) { console.error(e) }
    }

    async function createUser() {
        if (!userForm.username || !userForm.password) { alert('帳號和密碼為必填'); return }
        try {
            const resp = await fetch('/api/admin/users', {
                method: 'POST', headers: _headers(),
                body: JSON.stringify({ username: userForm.username, password: userForm.password, display_name: userForm.displayName, role: userForm.role })
            })
            if (!resp.ok) { const e = await resp.json(); alert(e.detail || '建立失敗'); return }
            userForm.username = ''; userForm.password = ''; userForm.displayName = ''; userForm.role = 'operator'
            loadUsers()
        } catch (e) { alert(e.message) }
    }

    async function updateUserRole(userId, newRole) {
        await fetch(`/api/admin/users/${userId}`, { method: 'PUT', headers: _headers(), body: JSON.stringify({ role: newRole }) })
        loadUsers()
    }

    async function toggleUserActive(userId, active) {
        await fetch(`/api/admin/users/${userId}`, { method: 'PUT', headers: _headers(), body: JSON.stringify({ is_active: active }) })
        loadUsers()
    }

    async function resetUserPassword(userId, username) {
        const newPass = prompt(`請輸入 ${username} 的新密碼:`)
        if (!newPass) return
        await fetch(`/api/admin/users/${userId}/reset-password`, { method: 'POST', headers: _headers(), body: JSON.stringify({ new_password: newPass }) })
        alert('密碼已重設')
    }

    async function deleteUser(userId, username) {
        if (!confirm(`確定刪除帳號 ${username}？`)) return
        const resp = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE', headers: _headers() })
        if (!resp.ok) { const e = await resp.json(); alert(e.detail || '刪除失敗'); return }
        loadUsers()
    }

    // ── Activity Log ──
    const actLog = reactive({ rows: [], total: 0, page: 1, pageSize: 30, filterUser: '', filterAction: '', loading: false })
    const actLogTotalPages = computed(() => Math.max(1, Math.ceil(actLog.total / actLog.pageSize)))

    async function loadActivityLogs() {
        actLog.loading = true
        try {
            const body = { page: actLog.page, page_size: actLog.pageSize }
            if (actLog.filterUser) body.username = actLog.filterUser
            if (actLog.filterAction) body.action = actLog.filterAction
            const resp = await fetch('/api/admin/logs/query', { method: 'POST', headers: _headers(), body: JSON.stringify(body) })
            if (resp.status === 401) { userLogout(auth); return }
            if (resp.ok) {
                const data = await resp.json()
                actLog.rows = data.rows; actLog.total = data.total
            }
        } catch (e) { console.error(e) }
        finally { actLog.loading = false }
    }

    async function cleanupLogs() {
        if (!confirm('確定清理超過保留期限的日誌？')) return
        try {
            const resp = await fetch('/api/admin/logs/cleanup', { method: 'POST', headers: _headers() })
            if (resp.ok) { const d = await resp.json(); alert(`已清理 ${d.deleted} 筆日誌`); loadActivityLogs() }
        } catch (e) { alert(e.message) }
    }

    // ── Config (Dropdowns, Defaults, Mapping) ──
    const settingsSubTab = ref('profiles')
    const configData = reactive({ dropdown_options: {}, defaults: {}, mapping_rules: {} })
    const newTagValues = reactive({})
    const newFieldName = ref('')
    const newDefaultField = ref('')
    const newDefaultValue = ref('')
    const newMappingEntries = reactive({})
    const newRuleName = ref('')

    async function loadConfig() {
        try {
            const resp = await fetch('/api/config')
            if (!resp.ok) throw new Error('Failed to load config')
            const data = await resp.json()
            configData.dropdown_options = data.dropdown_options || {}
            configData.defaults = data.defaults || {}
            configData.mapping_rules = data.mapping_rules || {}
        } catch (e) { console.error('載入設定失敗:', e) }
    }

    async function addDropdownValue(field) {
        const val = (newTagValues[field] || '').trim()
        if (!val) return
        try {
            const resp = await fetch('/api/config/dropdown/add', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ field, value: val })
            })
            if (!resp.ok) { const err = await resp.json(); alert(err.detail || '新增失敗'); return }
            configData.dropdown_options[field].push(val)
            newTagValues[field] = ''
        } catch (e) { alert('新增失敗: ' + e.message) }
    }

    async function removeDropdownValue(field, val) {
        try {
            const resp = await fetch('/api/config/dropdown/remove', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ field, value: val })
            })
            if (!resp.ok) return
            const idx = configData.dropdown_options[field].indexOf(val)
            if (idx !== -1) configData.dropdown_options[field].splice(idx, 1)
        } catch (e) { alert('移除失敗: ' + e.message) }
    }

    async function addNewField() {
        const name = newFieldName.value.trim()
        if (!name) return
        if (configData.dropdown_options[name]) { alert('欄位已存在'); return }
        try {
            const resp = await fetch('/api/config/dropdown', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ field: name, values: [] })
            })
            if (!resp.ok) return
            configData.dropdown_options[name] = []
            newFieldName.value = ''
        } catch (e) { alert('新增欄位失敗: ' + e.message) }
    }

    async function saveDefaults() {
        try {
            await fetch('/api/config/defaults', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ defaults: configData.defaults })
            })
        } catch (e) { console.error('儲存預設值失敗:', e) }
    }

    async function addNewDefault() {
        const field = newDefaultField.value.trim()
        const val = newDefaultValue.value.trim()
        if (!field) return
        configData.defaults[field] = val
        await saveDefaults()
        newDefaultField.value = ''
        newDefaultValue.value = ''
    }

    async function addMappingEntry(ruleName) {
        const key = (newMappingEntries[ruleName + '_key'] || '').trim()
        const val = (newMappingEntries[ruleName + '_val'] || '').trim()
        if (!key || !val) return
        configData.mapping_rules[ruleName][key] = val
        try {
            await fetch('/api/config/mapping', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ rule_name: ruleName, mapping: configData.mapping_rules[ruleName] })
            })
            newMappingEntries[ruleName + '_key'] = ''
            newMappingEntries[ruleName + '_val'] = ''
        } catch (e) { alert('新增映射失敗: ' + e.message) }
    }

    async function removeMappingEntry(ruleName, key) {
        delete configData.mapping_rules[ruleName][key]
        configData.mapping_rules[ruleName] = { ...configData.mapping_rules[ruleName] }
        try {
            await fetch('/api/config/mapping', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ rule_name: ruleName, mapping: configData.mapping_rules[ruleName] })
            })
        } catch (e) { alert('移除映射失敗: ' + e.message) }
    }

    async function addNewMappingRule() {
        const name = newRuleName.value.trim()
        if (!name) return
        if (configData.mapping_rules[name]) { alert('規則已存在'); return }
        configData.mapping_rules[name] = {}
        try {
            await fetch('/api/config/mapping', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ rule_name: name, mapping: {} })
            })
            newRuleName.value = ''
        } catch (e) { alert('新增規則失敗: ' + e.message) }
    }

    async function deleteMappingRule(ruleName) {
        if (!confirm(`確定要刪除映射規則「${ruleName}」嗎？`)) return
        try {
            const resp = await fetch('/api/config/mapping/' + encodeURIComponent(ruleName), { method: 'DELETE' })
            if (resp.ok) {
                delete configData.mapping_rules[ruleName]
                configData.mapping_rules = { ...configData.mapping_rules }
            }
        } catch (e) { alert('刪除失敗: ' + e.message) }
    }

    // ── DeviceProfile ──
    const dpList = reactive({ syncing: false, syncResult: null, loading: false, loaded: false, profiles: [] })

    async function loadDeviceProfiles() {
        if (conn.status !== 'connected') { alert('請先連線 ThingsBoard'); return }
        dpList.loading = true
        try {
            const resp = await fetch('/api/device-profiles/list', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tb_url: conn.url, tb_token: conn.token })
            })
            if (!resp.ok) throw new Error('Failed to load profiles')
            const data = await resp.json()
            dpList.profiles = data.profiles || []
            dpList.loaded = true
        } catch (e) { alert('載入 DeviceProfile 失敗: ' + e.message) }
        finally { dpList.loading = false }
    }

    async function syncTbProfilesToPG() {
        if (conn.status !== 'connected') { alert('請先連線 ThingsBoard'); return }
        dpList.syncing = true
        dpList.syncResult = null
        try {
            const resp = await fetch('/api/pg/ref/tb-profiles/sync', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tb_url: conn.url, tb_username: conn.username, tb_password: conn.password })
            })
            if (!resp.ok) { const err = await resp.json().catch(() => ({})); throw new Error(err.detail || `HTTP ${resp.status}`) }
            dpList.syncResult = await resp.json()
            loadRefTables()
        } catch (e) {
            dpList.syncResult = { message: `同步失敗: ${e.message}`, errors: [1] }
        } finally { dpList.syncing = false }
    }

    // ── Reference Tables ──
    const refTab = ref('location')
    const refData = reactive({ locations: [], ownerships: [], devices: [], systems: [], tbProfiles: [] })
    const refForm = reactive({
        loc_bu: '', loc_site: '', loc_zone: '',
        own_dept: '', own_owner: '',
        dev_name: '', dev_driver: '', dev_site: '', dev_system: '',
        sys_code: '', sys_name: '',
        tbp_name: '', tbp_desc: '',
    })

    async function loadRefTables() {
        try {
            const [loc, own, dev, sys, tbp] = await Promise.all([
                fetch('/api/pg/ref/locations').then(r => r.json()),
                fetch('/api/pg/ref/ownerships').then(r => r.json()),
                fetch('/api/pg/ref/devices').then(r => r.json()),
                fetch('/api/pg/ref/systems').then(r => r.json()),
                fetch('/api/pg/ref/tb-profiles').then(r => r.json()),
            ])
            refData.locations = loc; refData.ownerships = own
            refData.devices = dev; refData.systems = sys; refData.tbProfiles = tbp
        } catch (e) { console.error('載入參照表失敗:', e) }
    }

    async function addRefLocation() {
        if (!refForm.loc_bu || !refForm.loc_site || !refForm.loc_zone) return
        try {
            await fetch('/api/pg/ref/locations', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ bu: refForm.loc_bu, site: refForm.loc_site, zone: refForm.loc_zone }) })
            refForm.loc_bu = ''; refForm.loc_site = ''; refForm.loc_zone = ''
            loadRefTables()
        } catch (e) { alert('新增失敗: ' + e.message) }
    }

    async function addRefOwnership() {
        if (!refForm.own_dept || !refForm.own_owner) return
        try {
            await fetch('/api/pg/ref/ownerships', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ department: refForm.own_dept, data_owner: refForm.own_owner }) })
            refForm.own_dept = ''; refForm.own_owner = ''
            loadRefTables()
        } catch (e) { alert('新增失敗: ' + e.message) }
    }

    async function addRefDevice() {
        if (!refForm.dev_name || !refForm.dev_driver) return
        try {
            const resp = await fetch('/api/pg/ref/devices', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_name: refForm.dev_name, driver_type: refForm.dev_driver,
                    site: refForm.dev_site || null, system_code: refForm.dev_system || null }) })
            if (!resp.ok) { const err = await resp.json().catch(() => ({})); throw new Error(err.detail || `HTTP ${resp.status}`) }
            refForm.dev_name = ''; refForm.dev_driver = ''; refForm.dev_site = ''; refForm.dev_system = ''
            loadRefTables()
        } catch (e) { alert('Device 新增失敗: ' + e.message) }
    }

    async function addRefSystem() {
        if (!refForm.sys_code) return
        try {
            await fetch('/api/pg/ref/systems', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ system_code: refForm.sys_code, system_name: refForm.sys_name || null }) })
            refForm.sys_code = ''; refForm.sys_name = ''
            loadRefTables()
        } catch (e) { alert('新增失敗: ' + e.message) }
    }

    async function addRefTbProfile() {
        if (!refForm.tbp_name) return
        try {
            await fetch('/api/pg/ref/tb-profiles', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: refForm.tbp_name, description: refForm.tbp_desc || null }) })
            refForm.tbp_name = ''; refForm.tbp_desc = ''
            loadRefTables()
        } catch (e) { alert('新增失敗: ' + e.message) }
    }

    async function deleteRef(table, id) {
        if (!confirm('確定要刪除？')) return
        try {
            await fetch('/api/pg/ref/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ table, id }) })
            loadRefTables()
        } catch (e) { alert('刪除失敗: ' + e.message) }
    }

    return {
        showPwDialog, pwForm, pwMsg, changeMyPassword,
        userList, userForm, loadUsers, createUser, updateUserRole, toggleUserActive, resetUserPassword, deleteUser,
        actLog, actLogTotalPages, loadActivityLogs, cleanupLogs,
        settingsSubTab, configData, newTagValues, newFieldName, newDefaultField, newDefaultValue,
        newMappingEntries, newRuleName,
        loadConfig, addDropdownValue, removeDropdownValue, addNewField,
        saveDefaults, addNewDefault,
        addMappingEntry, removeMappingEntry, addNewMappingRule, deleteMappingRule,
        dpList, loadDeviceProfiles, syncTbProfilesToPG,
        refTab, refData, refForm, loadRefTables,
        addRefLocation, addRefOwnership, addRefDevice, addRefSystem, addRefTbProfile, deleteRef,
    }
}
