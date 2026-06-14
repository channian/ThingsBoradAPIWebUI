// Kep It Simple v3 — Step 3 · Kepware 建點（同步結構 / 推導比對 / 可編輯預覽 / 限速 / 執行）
const { useState: useStateK3 } = React;

function Step3Kepware({ gw, setGw, onNext, onExecuted }) {
  // 結構快取
  const [synced, setSynced] = useStateK3(false);
  const [syncing, setSyncing] = useStateK3(false);
  const [syncedAt, setSyncedAt] = useStateK3(null);
  // 推導結果（可編輯）
  const [rows, setRows] = useStateK3(STAGED_ROWS.map(r => ({ ...r })));
  const [deriving, setDeriving] = useStateK3(false);
  // 自訂輸入模式：{ "i:channel": true }
  const [customCell, setCustomCell] = useStateK3({});
  // 限速
  const [rate, setRate] = useStateK3({ delay: 0.2, batch: 50, pause: 5, withPg: false });
  // 執行
  const [phase, setPhase] = useStateK3('idle'); // idle | running | done
  const [prog, setProg] = useStateK3(0);
  const [modal, setModal] = useStateK3(null);

  const structure = KW_STRUCTURE;
  const counts = kwCounts(structure);
  const channels = Object.keys(structure);

  const sync = () => {
    setSyncing(true);
    setTimeout(() => {
      setSyncing(false); setSynced(true);
      const n = new Date();
      setSyncedAt(`${n.getFullYear()}-${pad03(n.getMonth() + 1)}-${pad03(n.getDate())} ${pad03(n.getHours())}:${pad03(n.getMinutes())}`);
    }, 900);
  };

  const reDerive = () => {
    setDeriving(true);
    setTimeout(() => { setRows(STAGED_ROWS.map(r => ({ ...r }))); setCustomCell({}); setDeriving(false); }, 600);
  };

  const vrows = rows.map(r => { const v = validateRow(r, structure); return { ...r, v, st: rowStatus(v) }; });
  const nOk = vrows.filter(r => r.st === 'ok').length;
  const nAdd = vrows.filter(r => r.st === 'add').length;
  const nWarn = vrows.filter(r => r.st === 'warn').length;

  const setCell = (i, key, val) => {
    setRows(rs => rs.map((r, j) => {
      if (j !== i) return r;
      const next = { ...r, [key]: val };
      // 換 channel 時，若 device 不屬於新 channel，預選第一個
      if (key === 'channel' && structure[val] && !structure[val][r.device]) {
        next.device = Object.keys(structure[val])[0];
      }
      return next;
    }));
  };
  const cellKey = (i, k) => `${i}:${k}`;
  const setCustom = (i, k, on) => setCustomCell(c => ({ ...c, [cellKey(i, k)]: on }));

  const execute = () => {
    const run = () => {
      setModal(null); setPhase('running'); setProg(0);
      const total = vrows.length - nWarn;
      let done = 0;
      const t = setInterval(() => {
        done += 2;
        if (done >= total) {
          done = total; clearInterval(t); setPhase('done');
          onExecuted && onExecuted(vrows.filter(r => r.st !== 'warn').map(r => r.tag));
        }
        setProg(done);
      }, 120);
    };
    if (nWarn > 0) {
      setModal({
        title: '部分路徑無法驗證',
        body: `仍有 ${nWarn} 筆的 Channel / Device 不存在於 Kepware 結構快取中，執行時將略過這些 Tag。建議先修正後再執行。確定要繼續嗎？`,
        danger: true, confirmLabel: `略過 ${nWarn} 筆並執行`, onConfirm: run,
      });
    } else run();
  };

  const stChip = (st) => st === 'ok' ? <span className="st-chip st-ok">✓</span>
    : st === 'add' ? <span className="st-chip st-add">+G</span>
    : <span className="st-chip st-warn">▲</span>;

  return (
    <>
      <GwConnCard gw={gw} setGw={setGw} />

      <Card title="Kepware 結構快取" icon={<Icon.database />}
        actions={synced && <Pill kind="ok">已同步</Pill>}>
        <div className="sync-row">
          <Button variant="primary" icon={<Icon.refresh />} onClick={sync}>
            {syncing ? '同步中…' : synced ? '重新同步結構' : '同步 Kepware 結構'}
          </Button>
          {synced ? (
            <>
              <span className="sync-meta">最後同步：{syncedAt}</span>
              <div className="sync-counts">
                <span className="sync-chip"><b>{counts.channels}</b> Channels</span>
                <span className="sync-chip"><b>{counts.devices}</b> Devices</span>
                <span className="sync-chip"><b>{counts.groups}</b> Groups</span>
              </div>
            </>
          ) : (
            <span className="sync-meta">尚未同步 — 同步後下拉選單才會載入現有 Channel / Device</span>
          )}
        </div>
      </Card>

      <div style={{ display: 'flex', gap: 10, marginBottom: 14, alignItems: 'center' }}>
        <Button variant="outline" icon={<Icon.refresh />} onClick={reDerive}>{deriving ? '推導中…' : '重新推導'}</Button>
        <span className="sync-meta">推導來源：暫存 {rows.length} 筆 · 命名規則 + 結構比對</span>
      </div>

      <Card title="推導結果（可編輯）" icon={<Icon.mapping />}>
        <div className="vsum">
          <span className="vs-chip vs-ok"><span className="d"></span>{nOk} 筆路徑完全匹配</span>
          <span className="vs-chip vs-add"><span className="d"></span>{nAdd} 筆需新建 Group</span>
          <span className="vs-chip vs-warn"><span className="d"></span>{nWarn} 筆 Channel / Device 不存在</span>
        </div>

        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Tag Name</th><th style={{ width: 130 }}>Channel</th><th style={{ width: 120 }}>Device</th>
                <th style={{ width: 140 }}>Groups</th><th>Address</th><th>Description</th><th style={{ width: 60 }}>狀態</th>
              </tr>
            </thead>
            <tbody>
              {vrows.map((r, i) => (
                <tr key={r.tag} className={r.st === 'warn' ? 'row-warn' : r.st === 'add' ? 'row-add' : ''}>
                  <td className="td-mono" style={{ fontWeight: 600, color: 'var(--ink-1)' }}>{r.tag}</td>
                  <td className="ed-cell">
                    {customCell[cellKey(i, 'channel')] || (!channels.includes(r.channel) && synced === false) || !channels.includes(r.channel) ? (
                      <span className="ed-custom">
                        <input className="input" value={r.channel} onChange={e => setCell(i, 'channel', e.target.value)} />
                        {synced && <button className="ed-back" title="改用下拉選單" onClick={() => { setCustom(i, 'channel', false); setCell(i, 'channel', channels[0]); }}>▾</button>}
                      </span>
                    ) : (
                      <select className="select" value={r.channel}
                        onChange={e => e.target.value === '__custom' ? setCustom(i, 'channel', true) : setCell(i, 'channel', e.target.value)}>
                        {channels.map(c => <option key={c} value={c}>{c}</option>)}
                        <option value="__custom">自訂…</option>
                      </select>
                    )}
                  </td>
                  <td className="ed-cell">
                    {customCell[cellKey(i, 'device')] || !(structure[r.channel] && structure[r.channel][r.device]) ? (
                      <span className="ed-custom">
                        <input className="input" value={r.device} onChange={e => setCell(i, 'device', e.target.value)} />
                        {structure[r.channel] && <button className="ed-back" title="改用下拉選單" onClick={() => { setCustom(i, 'device', false); setCell(i, 'device', Object.keys(structure[r.channel])[0]); }}>▾</button>}
                      </span>
                    ) : (
                      <select className="select" value={r.device}
                        onChange={e => e.target.value === '__custom' ? setCustom(i, 'device', true) : setCell(i, 'device', e.target.value)}>
                        {Object.keys(structure[r.channel] || {}).map(d => <option key={d} value={d}>{d}</option>)}
                        <option value="__custom">自訂…</option>
                      </select>
                    )}
                  </td>
                  <td className="ed-cell"><input className="input" value={r.groups} onChange={e => setCell(i, 'groups', e.target.value)} /></td>
                  <td className="td-mono" style={{ color: 'var(--ink-3)', fontSize: 11.5 }}>{r.address}</td>
                  <td style={{ fontSize: 12.5 }}>{r.desc}</td>
                  <td>{stChip(r.st)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="legend">
          <span><span className="st-chip st-ok">✓</span> 路徑已存在</span>
          <span><span className="st-chip st-add">+G</span> 將自動建立 Group</span>
          <span><span className="st-chip st-warn">▲</span> Channel / Device 不存在（需確認）</span>
          <span className="dot-sep">·</span>
          <span>Channel / Device 可下拉或自訂 · Groups 以 . 分隔層級</span>
        </div>
      </Card>

      <Card title="限速設定" icon={<Icon.settings />}>
        <div className="rate-grid">
          <div>
            <div className="field-label">單筆延遲 (s)</div>
            <input className="input" type="number" step="0.1" min="0" value={rate.delay} onChange={e => setRate(r => ({ ...r, delay: e.target.value }))} />
          </div>
          <div>
            <div className="field-label">批次大小</div>
            <input className="input" type="number" min="1" value={rate.batch} onChange={e => setRate(r => ({ ...r, batch: e.target.value }))} />
          </div>
          <div>
            <div className="field-label">批次暫停 (s)</div>
            <input className="input" type="number" min="0" value={rate.pause} onChange={e => setRate(r => ({ ...r, pause: e.target.value }))} />
          </div>
          <label className="checkbox-row">
            <input type="checkbox" checked={rate.withPg} onChange={e => setRate(r => ({ ...r, withPg: e.target.checked }))} />
            Kepware 建點 + PG 匯入一次完成
          </label>
        </div>
      </Card>

      {phase === 'running' && (
        <div style={{ marginBottom: 14 }}>
          <div className="progress">
            <div className="progress-fill" style={{ width: `${(prog / (vrows.length - nWarn)) * 100}%` }}></div>
            <div className="progress-text">建點中 · {prog} / {vrows.length - nWarn}</div>
          </div>
        </div>
      )}

      {phase === 'done' && (
        <div className="banner ok">
          <div className="banner-icon"><Icon.check /></div>
          <div className="banner-body">
            <h4>成功建立 {vrows.length - nWarn} 筆 Tag</h4>
            <p>{nAdd > 0 && `自動建立 ${nAdd} 個 Tag Group · `}{nWarn > 0 ? `略過 ${nWarn} 筆未驗證路徑 · ` : ''}{rate.withPg ? '已連動 PG 匯入。' : '可前往 Step 4 進行 PG 匯入。'}</p>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        {phase === 'done'
          ? <Button variant="primary" icon={<Icon.play />} onClick={onNext}>前往 Step 4 · PG 匯入</Button>
          : <Button variant="primary" icon={<Icon.play />} onClick={execute}>{phase === 'running' ? '建點中…' : '執行 Kepware 建點'}</Button>}
      </div>

      {modal && <Modal3 {...modal} onCancel={() => setModal(null)} />}
    </>
  );
}

Object.assign(window, { Step3Kepware });
