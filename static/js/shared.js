// shared.js — 共用狀態初始值與工具函式

export const AUTH_INITIAL = {
    token: localStorage.getItem('kep_token') || '',
    user: localStorage.getItem('kep_user') || '',
    role: localStorage.getItem('kep_role') || '',
    displayName: localStorage.getItem('kep_display') || '',
    username: '', password: '', logging: false, error: '',
}

export function authHeaders(auth) {
    return auth.token
        ? { 'Authorization': `Bearer ${auth.token}`, 'Content-Type': 'application/json' }
        : { 'Content-Type': 'application/json' }
}

export async function userLogin(auth) {
    auth.logging = true; auth.error = ''
    try {
        const resp = await fetch('/api/user/login', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: auth.username, password: auth.password })
        })
        if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || '登入失敗') }
        const data = await resp.json()
        auth.token = data.token; auth.user = data.username
        auth.role = data.role; auth.displayName = data.display_name || ''
        localStorage.setItem('kep_token', data.token)
        localStorage.setItem('kep_user', data.username)
        localStorage.setItem('kep_role', data.role)
        localStorage.setItem('kep_display', data.display_name || '')
        auth.username = ''; auth.password = ''
    } catch (e) { auth.error = e.message }
    finally { auth.logging = false }
}

export function userLogout(auth) {
    auth.token = ''; auth.user = ''; auth.role = ''; auth.displayName = ''
    localStorage.removeItem('kep_token'); localStorage.removeItem('kep_user')
    localStorage.removeItem('kep_role'); localStorage.removeItem('kep_display')
}

export function formatTime(ts) { if (!ts) return '-'; return new Date(ts).toLocaleString('zh-TW') }
export function formatISOTime(iso) { if (!iso) return '-'; return new Date(iso).toLocaleString('zh-TW') }
export function typeLabel(t) { return { create: '新增', delete: '刪除', delete_direct: '直接刪除' }[t] || t }
export function truncate(str, len) { if (!str) return '-'; return str.length > len ? str.substring(0, len) + '...' : str }
