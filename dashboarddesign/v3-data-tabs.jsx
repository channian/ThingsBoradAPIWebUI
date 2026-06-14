// Kep It Simple v3 — 查詢 / 歷史 / 設定（Kepware 語彙）
const { useState: useStateT3 } = React;

// ---------------- 查詢 ----------------
function TabQueryV3() {
  const all = STAGED_ROWS.map((r, i) => ({
    ...r,
    dt: i % 3 === 0 ? 'Float' : i % 3 === 1 ? 'Word' : 'Boolean',
    updated: `2026-06-${pad03(10 + (i % 3))} ${pad03(9 + (i % 8))}:${pad03((i * 7) % 60)}:${pad03((i * 13) % 60)}`,
    st: i === 17 || i === 18 ? 'warn' : 'ok',
  }));
  const [q, setQ] = useStateT3('');
  const [ch, setCh] = useStateT3('all');
  const channels = ['all', ...Object.keys(KW_STRUCTURE)];
  const rows = all.filter(r =>
    (ch === 'all' || r.channel === ch) &&
    (!q || r.tag.toLowerCase().includes(q.toLowerCase()) || r.desc.includes(q))
  );
  return (
    <>
      <div className="filter-bar">
        <div className="search grow"><Icon.search /><input className="input" placeholder="搜尋 Tag Name / 描述 …" value={q} onChange={e => setQ(e.target.value)} /></div>
        <select className="select" value={ch} onChange={e => setCh(e.target.value)}>
          {channels.map(c => <option key={c} value={c}>{c === 'all' ? '全部 Channel' : c}</option>)}
        </select>
        <select className="select"><option>每頁 20</option><option>每頁 50</option><option>每頁 100</option></select>
        <Button variant="outline" size="sm" icon={<Icon.download />}>匯出 CSV</Button>
      </div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 12, alignItems: 'center' }}>
        <Pill kind="info">查詢結果 {rows.length} 筆</Pill>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>Tag Name</th><th>Channel</th><th>Device</th><th>Groups</th><th>DataType</th><th>Kepware 狀態</th><th>更新時間</th></tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.tag}>
                <td className="td-mono" style={{ fontWeight: 600, color: 'var(--ink-1)' }}><Dot kind={r.st} />{r.tag}</td>
                <td className="td-mono">{r.channel}</td>
                <td className="td-mono">{r.device}</td>
                <td className="td-mono">{r.groups}</td>
                <td className="td-mono">{r.dt}</td>
                <td><Pill kind={r.st}>{r.st === 'ok' ? 'CREATED' : 'PENDING'}</Pill></td>
                <td className="td-mono" style={{ color: 'var(--ink-3)' }}>{r.updated}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pager">
        <Button variant="ghost" size="sm">‹ 上一頁</Button>
        <span className="info">1 / 1 頁 · 共 {rows.length} 筆</span>
        <Button variant="ghost" size="sm">下一頁 ›</Button>
      </div>
    </>
  );
}

// ---------------- 歷史 ----------------
function TabHistoryV3() {
  const rows = [
    ['2026-06-13 10:22:41', 'kw_create', 17, 17, 0, 0, 'channian'],
    ['2026-06-13 10:21:05', 'sync',      45, 45, 0, 0, 'channian'],
    ['2026-06-12 17:42:18', 'reload',    2,  2,  0, 0, 'admin'],
    ['2026-06-12 17:40:02', 'scale',     6,  6,  0, 0, 'admin'],
    ['2026-06-12 17:35:50', 'pg_import', 412, 410, 0, 2, 'channian'],
    ['2026-06-11 14:08:33', 'kw_delete', 24, 22, 1, 1, 'admin'],
    ['2026-06-10 09:15:27', 'kw_create', 168, 163, 3, 2, 'channian'],
  ];
  const opLabel = { kw_create: 'Kepware 建點', kw_delete: '批次刪除', pg_import: 'PG 匯入', scale: 'Scale 設定', reload: 'Collector Reload', sync: '結構同步' };
  const opKind = { kw_delete: 'b-delete' };
  return (
    <>
      <div className="filter-bar">
        <div className="search grow"><Icon.search /><input className="input" placeholder="搜尋操作類型 / 使用者 …" /></div>
        <select className="select"><option>全部操作</option>{Object.values(opLabel).map(l => <option key={l}>{l}</option>)}</select>
        <Button variant="ghost" size="sm" icon={<Icon.refresh />}>重新整理</Button>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>時間</th><th>操作類型</th><th className="td-num">總數</th><th className="td-num">成功</th><th className="td-num">略過</th><th className="td-num">失敗</th><th>使用者</th><th></th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="td-mono" style={{ color: 'var(--ink-2)' }}>{r[0]}</td>
                <td><span className={`b-badge ${opKind[r[1]] || 'b-create'}`}>{opLabel[r[1]]}</span></td>
                <td className="td-num">{r[2]}</td>
                <td className="td-num" style={{ color: 'var(--ok)' }}>{r[3]}</td>
                <td className="td-num" style={{ color: r[4] > 0 ? 'var(--warn)' : 'var(--ink-3)' }}>{r[4]}</td>
                <td className="td-num" style={{ color: r[5] > 0 ? 'var(--err)' : 'var(--ink-3)' }}>{r[5]}</td>
                <td className="td-mono">{r[6]}</td>
                <td><Button variant="ghost" size="sm" icon={<Icon.download />}>明細</Button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ---------------- 設定 ----------------
function TabSettingsV3() {
  const [gateways, setGateways] = useStateT3([
    { zone: 'Zone1', label: '廠區 A', url: 'https://10.11.64.70:57412', username: 'kw_admin', verifySsl: false, isDefault: true,  status: 'connected' },
    { zone: 'Zone2', label: '廠區 B', url: 'https://10.11.72.40:57412', username: 'kw_oper',  verifySsl: true,  isDefault: false, status: 'connected' },
  ]);
  const [editing, setEditing] = useStateT3(null); // index or 'new'
  const [draft, setDraft] = useStateT3(null);

  const blank = { zone: '', label: '', url: 'https://', username: '', password: '', verifySsl: true, isDefault: false, status: 'idle' };
  const startNew = () => { setEditing('new'); setDraft({ ...blank }); };
  const startEdit = (i) => { setEditing(i); setDraft({ ...gateways[i] }); };
  const cancel = () => { setEditing(null); setDraft(null); };
  const save = () => {
    setGateways(gs => {
      let next = editing === 'new' ? [...gs, draft] : gs.map((g, j) => j === editing ? draft : g);
      if (draft.isDefault) next = next.map((g, j) => ({ ...g, isDefault: (editing === 'new' ? j === next.length - 1 : j === editing) }));
      return next;
    });
    cancel();
  };
  const remove = (i) => setGateways(gs => gs.filter((_, j) => j !== i));
  const makeDefault = (i) => setGateways(gs => gs.map((g, j) => ({ ...g, isDefault: j === i })));
  const testOne = (i) => {
    setGateways(gs => gs.map((g, j) => j === i ? { ...g, status: 'connecting' } : g));
    setTimeout(() => setGateways(gs => gs.map((g, j) => j === i ? { ...g, status: 'connected' } : g)), 700);
  };
  const stTxt = (s) => ({ connected: '● 已連線', connecting: '◌ 測試中', idle: '○ 未測試' }[s] || '○');
  const stCls = (s) => ({ connected: 'ok', connecting: 'warn' }[s] || 'muted');

  return (
    <>
      <Card title="Kepware Gateway 管理" icon={<Icon.link />}
        actions={<Button variant="primary" size="sm" icon={<Icon.plus />} onClick={startNew}>新增 Gateway</Button>}>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 80 }}>Zone</th><th>名稱</th><th>GW URL</th><th>帳號</th>
                <th style={{ width: 80 }}>SSL 驗證</th><th style={{ width: 90 }}>狀態</th><th style={{ width: 70 }}>預設</th><th style={{ width: 150 }}></th>
              </tr>
            </thead>
            <tbody>
              {gateways.map((g, i) => (
                <tr key={i}>
                  <td className="td-mono" style={{ fontWeight: 700, color: 'var(--azure-deep)' }}>{g.zone}</td>
                  <td>{g.label}</td>
                  <td className="td-mono" style={{ color: 'var(--ink-3)' }}>{g.url}</td>
                  <td className="td-mono">{g.username}</td>
                  <td>{g.verifySsl ? <Pill kind="ok">開啟</Pill> : <Pill kind="muted">關閉</Pill>}</td>
                  <td><span className={`gw-status ${stCls(g.status)}`} style={{ padding: '3px 8px', fontSize: 11 }}>{stTxt(g.status)}</span></td>
                  <td>{g.isDefault ? <Pill kind="info">預設</Pill> : <button className="ed-back" style={{ width: 'auto', padding: '3px 8px', fontFamily: 'var(--font-mono)', fontSize: 11 }} onClick={() => makeDefault(i)}>設為預設</button>}</td>
                  <td>
                    <div style={{ display: 'flex', gap: 4 }}>
                      <button className="cond-iconbtn" title="測試連線" onClick={() => testOne(i)}><Icon.link /></button>
                      <button className="cond-iconbtn" title="編輯" onClick={() => startEdit(i)}><Icon.settings /></button>
                      <button className="cond-iconbtn danger" title="刪除" onClick={() => remove(i)} disabled={gateways.length <= 1 || g.isDefault}><Icon.trash /></button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {editing !== null && draft && (
          <div style={{ marginTop: 16, padding: 16, border: '1px solid var(--azure-line)', borderRadius: 10, background: 'var(--azure-soft)' }}>
            <div className="kis-card-title" style={{ marginBottom: 12 }}><Icon.link />{editing === 'new' ? '新增 Gateway' : `編輯 ${draft.zone || 'Gateway'}`}</div>
            <div style={{ display: 'grid', gridTemplateColumns: '90px 1fr 1.4fr 1fr', gap: 12, alignItems: 'end' }}>
              <div><div className="field-label">Zone</div><input className="input" placeholder="Zone3" value={draft.zone} onChange={e => setDraft(d => ({ ...d, zone: e.target.value }))} /></div>
              <div><div className="field-label">名稱</div><input className="input" placeholder="廠區 C" value={draft.label} onChange={e => setDraft(d => ({ ...d, label: e.target.value }))} /></div>
              <div><div className="field-label">GW URL</div><input className="input" value={draft.url} onChange={e => setDraft(d => ({ ...d, url: e.target.value }))} /></div>
              <div><div className="field-label">帳號</div><input className="input" value={draft.username} onChange={e => setDraft(d => ({ ...d, username: e.target.value }))} /></div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
              <div><div className="field-label">密碼</div><input className="input" type="password" placeholder="••••••" value={draft.password || ''} onChange={e => setDraft(d => ({ ...d, password: e.target.value }))} /></div>
              <div style={{ display: 'flex', gap: 18, alignItems: 'center', paddingBottom: 4 }}>
                <label className="checkbox-row"><input type="checkbox" checked={draft.verifySsl} onChange={e => setDraft(d => ({ ...d, verifySsl: e.target.checked }))} />驗證 SSL 憑證</label>
                <label className="checkbox-row"><input type="checkbox" checked={draft.isDefault} onChange={e => setDraft(d => ({ ...d, isDefault: e.target.checked }))} />設為預設</label>
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 14 }}>
              <Button variant="ghost" onClick={cancel}>取消</Button>
              <Button variant="primary" icon={<Icon.check />} onClick={save}>{editing === 'new' ? '新增' : '儲存'}</Button>
            </div>
          </div>
        )}
        <div className="legend" style={{ marginTop: 12 }}>
          <span>預設 Gateway 會成為建點 / 刪除頁面的初始 Zone 選項。</span>
        </div>
      </Card>

      <Card title="推導規則" icon={<Icon.mapping />}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <div>
            <div className="field-label">Tag 命名規則</div>
            <input className="input" defaultValue="{site}_{floor}_{system}_{device}_{point}" />
          </div>
          <div>
            <div className="field-label">Address 前綴</div>
            <input className="input" defaultValue="ns=2;s=" />
          </div>
        </div>
        <div className="legend" style={{ marginTop: 12 }}>
          <span>推導順序：site_prefix → floor → system → channel / device / tag_groups → address</span>
        </div>
      </Card>

      <Card title="結構快取維護" icon={<Icon.database />}
        actions={<Pill kind="muted">最後同步 2026-06-13 10:21</Pill>}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <Button variant="outline" icon={<Icon.refresh />}>立即同步結構</Button>
          <Button variant="danger" icon={<Icon.trash />}>清空 kepware_structure 快取</Button>
          <span className="sync-meta">清空後需重新同步，否則 Step 3 無法進行路徑驗證。</span>
        </div>
      </Card>
    </>
  );
}

Object.assign(window, { TabQueryV3, TabHistoryV3, TabSettingsV3 });
