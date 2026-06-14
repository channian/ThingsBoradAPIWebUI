// Kep It Simple v3 — Step 4 PG 匯入 / Step 5 Scale / Step 6 Collector Reload
const { useState: useStateQ3 } = React;

// ---------------- Step 4 · PG 匯入 ----------------
function Step4Pg({ onNext, onDone }) {
  const [phase, setPhase] = useStateQ3('idle');
  const rows = STAGED_ROWS.slice(0, 6);
  const pgPreview = rows.map(r => ({
    ...r,
    bu: 'FAC', zone: r.groups.split('.')[0], owner: 'channian', dept: 'FM', scan: 'NORMAL',
  }));

  const run = () => { setPhase('running'); setTimeout(() => { setPhase('done'); onDone && onDone(); }, 900); };

  return (
    <>
      <div className="summary-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, marginBottom: 16 }}>
        <div className="summary-stat"><div className="v">17</div><div className="l">已完成建點</div></div>
        <div className="summary-stat"><div className="v">17</div><div className="l">待匯入 PG</div></div>
        <div className="summary-stat s-ok"><div className="v">{phase === 'done' ? 17 : 0}</div><div className="l">已匯入</div></div>
        <div className="summary-stat s-err"><div className="v">0</div><div className="l">失敗</div></div>
      </div>

      <Card title="PG 欄位推導預覽" icon={<Icon.database />}
        actions={<Pill kind="muted">顯示前 6 筆</Pill>}>
        <div className="tbl-wrap">
          <table>
            <thead><tr><th>Tag Name</th><th>BU</th><th>Zone</th><th>Owner</th><th>Dept</th><th>Scan Group</th><th>Kepware 狀態</th></tr></thead>
            <tbody>
              {pgPreview.map(r => (
                <tr key={r.tag}>
                  <td className="td-mono" style={{ fontWeight: 600, color: 'var(--ink-1)' }}>{r.tag}</td>
                  <td className="td-mono">{r.bu}</td>
                  <td className="td-mono">{r.zone}</td>
                  <td className="td-mono">{r.owner}</td>
                  <td className="td-mono">{r.dept}</td>
                  <td className="td-mono">{r.scan}</td>
                  <td><Pill kind="ok">CREATED</Pill></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {phase === 'done' && (
        <div className="banner ok">
          <div className="banner-icon"><Icon.check /></div>
          <div className="banner-body"><h4>PG 匯入完成</h4><p>17 筆已寫入正式表並更新 Collector tags 表。</p></div>
        </div>
      )}

      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        {phase === 'done'
          ? <Button variant="primary" icon={<Icon.play />} onClick={onNext}>前往 Step 5 · Scale 設定</Button>
          : <Button variant="primary" icon={<Icon.play />} onClick={run}>{phase === 'running' ? '匯入中…' : '執行 PG 匯入'}</Button>}
      </div>
    </>
  );
}

// ---------------- Step 5 · Scale 設定 ----------------
function Step5Scale({ onNext, onApplied }) {
  const init = [
    { tag: 'K8_1F_CHS_CH01_SWT', rawLo: 0, rawHi: 32767, engLo: 0, engHi: 50, unit: '°C' },
    { tag: 'K8_1F_CHS_CH01_RWT', rawLo: 0, rawHi: 32767, engLo: 0, engHi: 50, unit: '°C' },
    { tag: 'K8_2F_CHS_AHU01_SAT', rawLo: 0, rawHi: 32767, engLo: 0, engHi: 60, unit: '°C' },
    { tag: 'K8_B1_PWR_PMS01_KW', rawLo: 0, rawHi: 65535, engLo: 0, engHi: 2000, unit: 'kW' },
    { tag: 'K8_B1_PWR_UPS01_SOC', rawLo: 0, rawHi: 100, engLo: 0, engHi: 100, unit: '%' },
    { tag: 'K8_RF_ENV_AQ01_PM25', rawLo: 0, rawHi: 1024, engLo: 0, engHi: 500, unit: 'µg/m³' },
  ];
  const [rows, setRows] = useStateQ3(init);
  const [applied, setApplied] = useStateQ3(false);
  const set = (i, k, v) => { setApplied(false); setRows(rs => rs.map((r, j) => j === i ? { ...r, [k]: v } : r)); };

  return (
    <>
      <Card title="Scale 參數（可編輯）" icon={<Icon.settings />}
        actions={<Pill kind={applied ? 'ok' : 'muted'}>{applied ? '已套用' : '未套用'}</Pill>}>
        <div className="tbl-wrap">
          <table>
            <thead><tr><th>Tag Name</th><th style={{ width: 100 }}>Raw Lo</th><th style={{ width: 100 }}>Raw Hi</th><th style={{ width: 100 }}>Eng Lo</th><th style={{ width: 100 }}>Eng Hi</th><th style={{ width: 90 }}>單位</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.tag}>
                  <td className="td-mono" style={{ fontWeight: 600, color: 'var(--ink-1)' }}>{r.tag}</td>
                  <td className="ed-cell"><input className="input" type="number" value={r.rawLo} onChange={e => set(i, 'rawLo', e.target.value)} /></td>
                  <td className="ed-cell"><input className="input" type="number" value={r.rawHi} onChange={e => set(i, 'rawHi', e.target.value)} /></td>
                  <td className="ed-cell"><input className="input" type="number" value={r.engLo} onChange={e => set(i, 'engLo', e.target.value)} /></td>
                  <td className="ed-cell"><input className="input" type="number" value={r.engHi} onChange={e => set(i, 'engHi', e.target.value)} /></td>
                  <td className="ed-cell"><input className="input" value={r.unit} onChange={e => set(i, 'unit', e.target.value)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="legend"><span>線性轉換：Eng = EngLo + (Raw − RawLo) × (EngHi − EngLo) / (RawHi − RawLo)</span></div>
      </Card>

      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
        <Button variant="outline" icon={<Icon.check />} onClick={() => { setApplied(true); onApplied && onApplied(); }}>套用 Scale 設定</Button>
        <Button variant="primary" icon={<Icon.play />} onClick={onNext}>前往 Step 6 · Collector Reload</Button>
      </div>
    </>
  );
}

// ---------------- Step 6 · Collector Reload ----------------
function Step6Reload({ onDone }) {
  const [phase, setPhase] = useStateQ3('idle'); // idle | running | done
  const [lines, setLines] = useStateQ3([]);

  const SCRIPT = [
    ['t', '12:00:01'], ['i', '開始 Collector Reload …'],
    ['t', '12:00:02'], ['o', 'collector-a · 停止輪詢 (drain mode)'],
    ['t', '12:00:03'], ['o', 'collector-a · 重新載入 tags 表 · 1,247 點'],
    ['t', '12:00:04'], ['o', 'collector-b · 重新載入 tags 表 · 982 點'],
    ['t', '12:00:05'], ['o', 'collector-a / collector-b · 恢復輪詢'],
    ['t', '12:00:05'], ['i', 'Reload 完成 · 全部點位上線'],
  ];

  const run = () => {
    setPhase('running'); setLines([]);
    let i = 0;
    const t = setInterval(() => {
      i += 2;
      setLines(SCRIPT.slice(0, i));
      if (i >= SCRIPT.length) { clearInterval(t); setPhase('done'); onDone(); }
    }, 450);
  };

  const collectors = [
    { id: 'collector-a', tags: 1247, last: '2026-06-12 17:42' },
    { id: 'collector-b', tags: 982, last: '2026-06-12 17:42' },
  ];

  return (
    <>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 16 }}>
        {collectors.map(c => (
          <div className="kis-card" key={c.id} style={{ marginBottom: 0 }}>
            <div className="kis-card-head" style={{ marginBottom: 8 }}>
              <div className="kis-card-title"><Icon.kepware />{c.id}</div>
              <Pill kind={phase === 'running' ? 'warn' : 'ok'}>{phase === 'running' ? 'RELOADING' : 'RUNNING'}</Pill>
            </div>
            <div style={{ display: 'flex', gap: 18, fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--ink-3)' }}>
              <span>點位 <b style={{ color: 'var(--ink-1)' }}>{c.tags.toLocaleString()}</b></span>
              <span>上次 Reload <b style={{ color: 'var(--ink-1)' }}>{phase === 'done' ? '剛剛' : c.last}</b></span>
            </div>
          </div>
        ))}
      </div>

      {lines.length > 0 && (
        <div className="log" style={{ marginBottom: 16 }}>
          {Array.from({ length: lines.length / 2 }, (_, i) => (
            <div key={i}><span className="t">[{lines[i * 2][1]}]</span> <span className={lines[i * 2 + 1][0]}>{lines[i * 2 + 1][1]}</span></div>
          ))}
        </div>
      )}

      {phase === 'done' && (
        <div className="banner ok">
          <div className="banner-icon"><Icon.check /></div>
          <div className="banner-body"><h4>Reload 完成</h4><p>本次匯入流程全部完成：建點 17 → PG 匯入 17 → Scale 6 → Reload 2 collectors。</p></div>
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button variant="primary" icon={<Icon.refresh />} onClick={run}>{phase === 'running' ? 'Reload 中…' : 'Reload Collectors'}</Button>
      </div>
    </>
  );
}

Object.assign(window, { Step4Pg, Step5Scale, Step6Reload });
