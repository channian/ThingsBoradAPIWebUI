// Tab: Database Search — cascading multi-condition filter
const { useState: useStateP4 } = React;

const FIELD_DEFS = [
  { id: 'building', label: '棟別',     placeholder: '例：A 棟 / 1F / B1' },
  { id: 'system',   label: '系統',     placeholder: '例：HVAC / Boiler / AHU' },
  { id: 'tagname',  label: 'Tag Name', placeholder: '例：IFIX_1F_T01_*' },
  { id: 'project',  label: '專案名稱', placeholder: '例：FactoryA_2026Q2' },
  { id: 'owner',    label: 'Owner',    placeholder: '例：channian' },
];

const OPS = [
  { id: 'eq',       label: '等於',       sym: '=' },
  { id: 'neq',      label: '不等於',     sym: '≠' },
  { id: 'contains', label: '包含',       sym: '⊃' },
  { id: 'startsw',  label: '開頭為',     sym: '^' },
  { id: 'endsw',    label: '結尾為',     sym: '$' },
  { id: 'in',       label: '屬於 (多選)', sym: '∈' },
  { id: 'isnull',   label: '為空',       sym: '∅' },
  { id: 'notnull',  label: '不為空',     sym: '!∅' },
];

function TabDbSearch() {
  // 條件鏈：每筆 = { field, op, value, join } join 表示與「上一條」的關係 (AND/OR)
  const [conds, setConds] = useStateP4([
    { field: 'building', op: 'eq',       value: 'A 棟', join: null },
    { field: 'system',   op: 'eq',       value: 'HVAC', join: 'AND' },
    { field: 'tagname',  op: 'startsw',  value: 'IFIX_1F_', join: 'AND' },
  ]);
  const [phase, setPhase] = useStateP4('idle'); // idle | running | done

  const updateCond = (idx, patch) => setConds(cs => cs.map((c, i) => i === idx ? { ...c, ...patch } : c));
  const addCond  = () => setConds(cs => [...cs, { field: 'owner', op: 'eq', value: '', join: 'AND' }]);
  const removeCond = (idx) => setConds(cs => cs.filter((_, i) => i !== idx).map((c, i) => i === 0 ? { ...c, join: null } : c));
  const dupCond  = (idx) => setConds(cs => {
    const base = cs[idx];
    const next = [...cs];
    next.splice(idx + 1, 0, { ...base, join: 'AND' });
    return next;
  });

  // 模擬：每加一個條件 → 命中數遞減
  const startCounts = [4128, 1240, 312, 86, 28];
  const cumulative = conds.map((_, i) => startCounts[Math.min(i, startCounts.length - 1)]);

  const sample = [
    { tag: 'IFIX_1F_AHU01_RetTemp',     building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'channian', dt: 'Float', updated: '2026-05-08 14:22:11' },
    { tag: 'IFIX_1F_AHU01_SupTemp',     building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'channian', dt: 'Float', updated: '2026-05-08 14:22:11' },
    { tag: 'IFIX_1F_AHU01_FanSpd',      building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'channian', dt: 'Float', updated: '2026-05-08 14:22:11' },
    { tag: 'IFIX_1F_AHU02_RetTemp',     building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'admin',    dt: 'Float', updated: '2026-05-07 09:14:02' },
    { tag: 'IFIX_1F_AHU02_SupTemp',     building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'admin',    dt: 'Float', updated: '2026-05-07 09:14:02' },
    { tag: 'IFIX_1F_AHU02_FanSpd',      building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'admin',    dt: 'Float', updated: '2026-05-07 09:14:02' },
    { tag: 'IFIX_1F_VAV01_DamperPos',   building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'channian', dt: 'Float', updated: '2026-05-06 18:01:30' },
    { tag: 'IFIX_1F_VAV02_DamperPos',   building: 'A 棟', floor: '1F', system: 'HVAC',   project: 'FactoryA_2026Q2', owner: 'channian', dt: 'Float', updated: '2026-05-06 18:01:30' },
  ];

  const runSearch = () => { setPhase('running'); setTimeout(() => setPhase('done'), 600); };
  const reset = () => { setConds([{ field: 'building', op: 'eq', value: '', join: null }]); setPhase('idle'); };

  // 預覽 SQL-like 語法
  const sqlPreview = conds.map((c, i) => {
    const f = FIELD_DEFS.find(x => x.id === c.field)?.label || c.field;
    const op = OPS.find(x => x.id === c.op);
    const v = (c.op === 'isnull' || c.op === 'notnull') ? '' : `"${c.value || '?'}"`;
    return `${i === 0 ? '' : c.join + ' '}${f} ${op?.sym} ${v}`.trim();
  }).join('\n  ');

  return (
    <>
      {/* —— 條件建構器 —— */}
      <Card title="條件鏈 · CASCADING FILTER" icon={<Icon.filter />} actions={
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-3)' }}>
          {conds.length} 個條件 · 預估命中 {cumulative[cumulative.length - 1]} 筆
        </span>
      }>
        <div className="cond-list">
          {conds.map((c, i) => {
            const fieldDef = FIELD_DEFS.find(f => f.id === c.field);
            const opDef    = OPS.find(o => o.id === c.op);
            const noValue  = c.op === 'isnull' || c.op === 'notnull';
            return (
              <div className="cond-row" key={i}>
                <div className="cond-step">
                  <div className="cond-step-num">{i + 1}</div>
                  {i < conds.length - 1 && <div className="cond-step-line" />}
                </div>

                <div className="cond-body">
                  <div className="cond-grid">
                    {/* JOIN */}
                    <div className="cond-cell cond-join">
                      {i === 0
                        ? <span className="cond-where">WHERE</span>
                        : (
                          <div className="cond-join-toggle">
                            <button className={c.join === 'AND' ? 'on' : ''} onClick={() => updateCond(i, { join: 'AND' })}>AND</button>
                            <button className={c.join === 'OR' ? 'on' : ''}  onClick={() => updateCond(i, { join: 'OR' })}>OR</button>
                          </div>
                        )
                      }
                    </div>

                    {/* FIELD */}
                    <div className="cond-cell">
                      <div className="field-label">欄位</div>
                      <select className="select" value={c.field} onChange={e => updateCond(i, { field: e.target.value })}>
                        {FIELD_DEFS.map(f => <option key={f.id} value={f.id}>{f.label}</option>)}
                      </select>
                    </div>

                    {/* OP */}
                    <div className="cond-cell">
                      <div className="field-label">運算子</div>
                      <select className="select" value={c.op} onChange={e => updateCond(i, { op: e.target.value })}>
                        {OPS.map(o => <option key={o.id} value={o.id}>{o.sym}  {o.label}</option>)}
                      </select>
                    </div>

                    {/* VALUE */}
                    <div className="cond-cell cond-val">
                      <div className="field-label">值</div>
                      {noValue
                        ? <input className="input" disabled placeholder="—" />
                        : <input className="input" placeholder={fieldDef?.placeholder} value={c.value} onChange={e => updateCond(i, { value: e.target.value })} />
                      }
                    </div>

                    {/* HIT COUNT */}
                    <div className="cond-cell cond-hit">
                      <div className="field-label">命中</div>
                      <div className="cond-hit-num">{cumulative[i].toLocaleString()}</div>
                    </div>

                    {/* ROW ACTIONS */}
                    <div className="cond-cell cond-acts">
                      <button className="cond-iconbtn" title="複製" onClick={() => dupCond(i)}>⧉</button>
                      <button className="cond-iconbtn danger" title="移除" onClick={() => removeCond(i)} disabled={conds.length === 1}>×</button>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="cond-add">
          <Button variant="outline" size="sm" icon={<Icon.plus />} onClick={addCond}>新增條件</Button>
          <Button variant="ghost" size="sm" onClick={reset}>清除全部</Button>
          <span style={{ flex: 1 }} />
          <Button variant="ghost" size="sm" icon={<Icon.download />}>儲存為查詢樣板</Button>
        </div>
      </Card>

      {/* —— SQL 預覽 —— */}
      <Card title="查詢預覽 · SQL-LIKE" icon={<Icon.database />} actions={
        <Button variant="ghost" size="sm">複製</Button>
      }>
        <pre className="sql-preview">{`SELECT tag, building, system, project, owner
FROM   tags
WHERE  ${sqlPreview.replace(/\n  /g, '\n       ')}
ORDER  BY updated DESC
LIMIT  100;`}</pre>
      </Card>

      {/* —— 執行列 —— */}
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', margin: '16px 0', flexWrap: 'wrap' }}>
        <Button variant="primary" icon={<Icon.search />} onClick={runSearch}>
          {phase === 'running' ? '查詢中…' : '執行查詢'}
        </Button>
        <Button variant="outline" size="sm" icon={<Icon.download />}>匯出結果 CSV</Button>
        <span style={{ flex: 1 }} />
        {phase === 'done' && (
          <>
            <Pill kind="info">命中 28 筆</Pill>
            <Pill kind="muted">查詢時間 0.42s</Pill>
            <Pill kind="muted">3 個條件</Pill>
          </>
        )}
      </div>

      {/* —— 結果區 —— */}
      {phase === 'idle' && (
        <div className="empty-block">
          <Icon.search />
          <div className="t">設定條件後點擊「執行查詢」</div>
          <div className="s">CASCADING FILTER · 後一條件作用於前一條件結果</div>
        </div>
      )}

      {phase !== 'idle' && (
        <>
          <div className="filter-bar">
            <div className="search grow"><Icon.search /><input className="input" placeholder="在結果中搜尋…" /></div>
            <select className="select"><option>每頁 20</option><option>每頁 50</option><option>每頁 100</option></select>
            <select className="select"><option>排序：更新時間 ↓</option><option>排序：Tag ↑</option><option>排序：Owner</option></select>
          </div>

          <div className="tbl-wrap">
            <table>
              <thead><tr>
                <th style={{ width: 36 }}><input type="checkbox" /></th>
                <th>Tag Name</th>
                <th>棟別 / 樓層</th>
                <th>系統</th>
                <th>專案名稱</th>
                <th>Owner</th>
                <th>DataType</th>
                <th>更新時間</th>
              </tr></thead>
              <tbody>
                {sample.map((r, i) => (
                  <tr key={i}>
                    <td><input type="checkbox" /></td>
                    <td className="td-mono" style={{ fontWeight: 600, color: 'var(--fg-1)' }}>
                      <Hl text={r.tag} match="IFIX_1F_" />
                    </td>
                    <td className="td-mono"><Hl text={`${r.building} · ${r.floor}`} match="A 棟" /></td>
                    <td className="td-mono"><Hl text={r.system} match="HVAC" /></td>
                    <td className="td-mono">{r.project}</td>
                    <td className="td-mono">{r.owner}</td>
                    <td className="td-mono">{r.dt}</td>
                    <td className="td-mono" style={{ color: 'var(--fg-3)' }}>{r.updated}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="pager">
            <Button variant="ghost" size="sm">‹ 上一頁</Button>
            <span className="info">1 / 2 頁 · 共 28 筆</span>
            <Button variant="ghost" size="sm">下一頁 ›</Button>
          </div>
        </>
      )}
    </>
  );
}

// 命中字串高亮
function Hl({ text, match }) {
  if (!match || !text.includes(match)) return text;
  const idx = text.indexOf(match);
  return <>{text.slice(0, idx)}<mark className="hl">{match}</mark>{text.slice(idx + match.length)}</>;
}

Object.assign(window, { TabDbSearch });
