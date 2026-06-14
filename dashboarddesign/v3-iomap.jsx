// Kep It Simple v3 — IO Mapping(需求表 × iFIX IO 表 比對)
const { useState: useStateM3 } = React;

function TabIoMapV3() {
  const [fileA, setFileA] = useStateM3(null);
  const [fileB, setFileB] = useStateM3(null);
  const [phase, setPhase] = useStateM3('idle'); // idle | running | done
  const [flt, setFlt] = useStateM3('all');

  const data = [
    { matched: true,  tag: 'K8_1F_CHS_CH01_SWT',  addr: 'D00100', dt: 'Float', dev: 'iFIX1', node: 'IFIXNode' },
    { matched: true,  tag: 'K8_1F_CHS_CH01_RWT',  addr: 'D00102', dt: 'Float', dev: 'iFIX1', node: 'IFIXNode' },
    { matched: true,  tag: 'K8_2F_CHS_AHU01_SAT', addr: 'D00200', dt: 'Float', dev: 'iFIX2', node: 'IFIXNode' },
    { matched: false, tag: 'K8_2F_CHS_AHU01_RAT', addr: '—',      dt: '—',     dev: '—',     node: '—' },
    { matched: true,  tag: 'K8_B1_PWR_PMS01_KW',  addr: 'E00050', dt: 'Float', dev: 'PMS01', node: 'OPCNode' },
    { matched: true,  tag: 'K8_2F_MFG_L1_CNT',    addr: 'D00310', dt: 'Word',  dev: 'iFIX2', node: 'IFIXNode' },
    { matched: false, tag: 'K8_B1_WTR_PMP01_FLW', addr: '—',      dt: '—',     dev: '—',     node: '—' },
    { matched: true,  tag: 'K8_3F_FAC_FAN01_SPD', addr: 'E00072', dt: 'Float', dev: 'FAC01', node: 'OPCNode' },
  ];
  const rows = phase === 'done' ? data.filter(r => flt === 'all' || (flt === 'ok' ? r.matched : !r.matched)) : [];
  const nOk = data.filter(r => r.matched).length;
  const nMiss = data.length - nOk;

  const run = () => { setPhase('running'); setTimeout(() => setPhase('done'), 700); };
  const clear = () => { setFileA(null); setFileB(null); setPhase('idle'); setFlt('all'); };

  const Zone = ({ file, onPick, hint, name }) => (
    <div className="upload compact" onClick={onPick}>
      <Icon.upload />
      <h4>{file ? '已選擇檔案,點擊可重新選擇' : '拖曳或點擊上傳 CSV'}</h4>
      <p>{hint}</p>
      {file && <span className="file-name"><Icon.check />{name}</span>}
    </div>
  );

  return (
    <>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 16 }}>
        <div>
          <div className="field-label" style={{ marginBottom: 8 }}>A 檔案 · 需求表</div>
          <Zone file={fileA} onPick={() => setFileA(true)} hint="需含 Tag Name 欄位" name="requirement_v3.csv" />
        </div>
        <div>
          <div className="field-label" style={{ marginBottom: 8 }}>B 檔案 · iFIX IO 表</div>
          <Zone file={fileB} onPick={() => setFileB(true)} hint="需含 Tag Name / Address / DataType" name="ifix_iolist.csv" />
        </div>
      </div>

      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 14, flexWrap: 'wrap' }}>
        <Button variant="primary" icon={<Icon.mapping />} onClick={run}>
          {phase === 'running' ? 'Mapping 中…' : '執行 Mapping'}
        </Button>
        <Button variant="outline" size="sm" icon={<Icon.download />}>下載結果 CSV</Button>
        <Button variant="ghost" size="sm" onClick={clear}>清除</Button>
        <span style={{ flex: 1 }}></span>
        {phase === 'done' && (
          <>
            <Pill kind="info">需求 {data.length}</Pill>
            <Pill kind="ok">匹配 {nOk}</Pill>
            <Pill kind="err">未匹配 {nMiss}</Pill>
          </>
        )}
      </div>

      {phase !== 'done' ? (
        <div className="empty-block">
          <Icon.mapping />
          <div className="t">上傳 A / B 兩份 CSV 後點擊「執行 Mapping」</div>
          <div className="s">以 TAG NAME 為鍵,從 IO 表帶回 ADDRESS / DATATYPE / DEVICE / NODE</div>
        </div>
      ) : (
        <>
          <div className="filter-bar">
            <select className="select" value={flt} onChange={e => setFlt(e.target.value)}>
              <option value="all">全部</option>
              <option value="ok">已匹配</option>
              <option value="miss">未匹配</option>
            </select>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--ink-3)' }}>顯示 {rows.length} / {data.length} 筆</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead><tr><th style={{ width: 40 }}>#</th><th>狀態</th><th>Tag Name</th><th>Address</th><th>DataType</th><th>Device</th><th>Node</th></tr></thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={r.tag} className={!r.matched ? 'row-warn' : ''}>
                    <td className="td-mono" style={{ color: 'var(--ink-3)' }}>{i + 1}</td>
                    <td><Pill kind={r.matched ? 'ok' : 'err'}>{r.matched ? '已匹配' : '未匹配'}</Pill></td>
                    <td className="td-mono" style={{ color: 'var(--ink-1)', fontWeight: 600 }}>{r.tag}</td>
                    <td className="td-mono">{r.addr}</td>
                    <td className="td-mono">{r.dt}</td>
                    <td className="td-mono">{r.dev}</td>
                    <td className="td-mono">{r.node}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

Object.assign(window, { TabIoMapV3 });
