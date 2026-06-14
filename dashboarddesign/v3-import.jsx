// Kep It Simple v3 — Kepware Import: stepper container + Step 1 (CSV) + Step 2 (暫存)
const { useState: useStateI3 } = React;

const IMPORT_STEPS = [
  { id: 1, label: 'CSV 上傳',         code: 'UPLOAD' },
  { id: 2, label: '暫存資料',         code: 'STAGING' },
  { id: 3, label: 'Kepware 建點',     code: 'KW · CREATE' },
  { id: 4, label: 'PG 匯入',          code: 'PG · IMPORT' },
  { id: 5, label: 'Scale 設定',       code: 'SCALE' },
  { id: 6, label: 'Collector Reload', code: 'RELOAD' },
];

function TabImportKW({ gw, setGw }) {
  const [step, setStep] = useStateI3(1);
  const [doneSteps, setDoneSteps] = useStateI3([]);
  // 資料庫中的流程狀態（每 tag）：kwCreated = 已建點的 tag 清單
  const [kwCreated, setKwCreated] = useStateI3([]);
  const [pgDone, setPgDone] = useStateI3(false);
  const [scaleDone, setScaleDone] = useStateI3(false);
  const markDone = (id) => setDoneSteps(d => (d.includes(id) ? d : [...d, id]));
  const goNext = (id) => { markDone(id); setStep(id + 1); };

  return (
    <>
      <div className="stepper">
        {IMPORT_STEPS.map((s, i) => (
          <React.Fragment key={s.id}>
            {i > 0 && <span className="step-sep"></span>}
            <button
              className={`step-btn ${step === s.id ? 'active' : ''} ${doneSteps.includes(s.id) ? 'done' : ''}`}
              onClick={() => setStep(s.id)}
              data-screen-label={`Step ${s.id} ${s.label}`}
            >
              <span className="step-num">{doneSteps.includes(s.id) && step !== s.id ? '✓' : s.id}</span>
              <span className="step-meta">
                <span className="step-label">{s.label}</span>
                <span className="step-code">{s.code}</span>
              </span>
            </button>
          </React.Fragment>
        ))}
      </div>

      {step === 1 && <Step1Csv onNext={() => goNext(1)} />}
      {step === 2 && <Step2Staging onNext={() => goNext(2)} kwCreated={kwCreated} pgDone={pgDone} scaleDone={scaleDone} />}
      {step === 3 && <Step3Kepware gw={gw} setGw={setGw} onNext={() => goNext(3)} onExecuted={setKwCreated} />}
      {step === 4 && <Step4Pg onNext={() => goNext(4)} onDone={() => setPgDone(true)} />}
      {step === 5 && <Step5Scale onNext={() => goNext(5)} onApplied={() => setScaleDone(true)} />}
      {step === 6 && <Step6Reload onDone={() => markDone(6)} />}
    </>
  );
}

// ---------------- Step 1 · CSV 上傳 ----------------
function Step1Csv({ onNext }) {
  const [file, setFile] = useStateI3(null);
  const pick = () => setFile({ name: 'tags_202606.csv', rows: STAGED_ROWS.length });

  const required = [
    ['name', true], ['address', true], ['description', true],
    ['data_type', true], ['scan_rate', false],
  ];

  return (
    <>
      <Card title="上傳點位 CSV" icon={<Icon.upload />}>
        <a className="template-link" href="#" onClick={e => e.preventDefault()} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--azure)', marginBottom: 12, textDecoration: 'none' }}>
          <Icon.download />下載 CSV 範本
        </a>
        <div className="upload" onClick={pick}>
          <Icon.upload />
          <h4>{file ? '已選擇檔案，點擊可重新選擇' : '拖曳 CSV 至此，或點擊選擇檔案'}</h4>
          <p>UTF-8 編碼 · 需包含 name / address / description 欄位</p>
          {file && <span className="file-name"><Icon.check />{file.name} · {file.rows} 筆</span>}
        </div>
      </Card>

      {file && (
        <Card title="欄位檢查" icon={<Icon.shield_check />}>
          <div className="dp-grid" style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {required.map(([col, req]) => (
              <span key={col} className="pill pill-ok">✓ {col}{req ? '' : ' (選填)'}</span>
            ))}
          </div>
          <div style={{ marginTop: 14, display: 'flex', gap: 10, alignItems: 'center' }}>
            <Pill kind="info">解析成功 {file.rows} 筆</Pill>
            <Pill kind="muted">0 筆格式錯誤</Pill>
            <span style={{ flex: 1 }}></span>
            <Button variant="primary" icon={<Icon.play />} onClick={onNext}>寫入暫存，前往 Step 2</Button>
          </div>
        </Card>
      )}
    </>
  );
}

// ---------------- Step 2 · 暫存資料 ----------------
function Step2Staging({ onNext, kwCreated = [], pgDone = false, scaleDone = false }) {
  const [checked, setChecked] = useStateI3([]);
  const [rows, setRows] = useStateI3(STAGED_ROWS);
  const toggle = (i) => setChecked(c => (c.includes(i) ? c.filter(x => x !== i) : [...c, i]));
  const toggleAll = () => setChecked(c => (c.length === rows.length ? [] : rows.map((_, i) => i)));
  const removeChecked = () => { setRows(r => r.filter((_, i) => !checked.includes(i))); setChecked([]); };

  // 從資料庫讀回的流程狀態：Step 3 建點 / Step 4 PG / Step 5 Scale
  const stKw = (r) => kwCreated.includes(r.tag);
  const stPg = (r) => pgDone && stKw(r);
  const stScale = (r) => scaleDone && stKw(r);
  const StPill = ({ done }) => <Pill kind={done ? 'ok' : 'muted'}>{done ? 'DONE' : 'PENDING'}</Pill>;
  const nKw = rows.filter(stKw).length;

  return (
    <>
      <div className="filter-bar">
        <Pill kind="info">暫存 {rows.length} 筆</Pill>
        <Pill kind="muted">來源 tags_202606.csv</Pill>
        <Pill kind={nKw > 0 ? 'ok' : 'muted'}>已建點 {nKw} / {rows.length}</Pill>
        <span className="grow"></span>
        <Button variant="danger" size="sm" icon={<Icon.trash />} onClick={removeChecked}>刪除選取 ({checked.length})</Button>
        <Button variant="outline" size="sm" icon={<Icon.refresh />} onClick={() => { setRows(STAGED_ROWS); setChecked([]); }}>還原</Button>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th style={{ width: 40 }}><input type="checkbox" checked={checked.length === rows.length && rows.length > 0} onChange={toggleAll} /></th>
              <th>Tag Name</th><th>Address</th><th>Description</th>
              <th style={{ width: 96 }}>建點 (S3)</th><th style={{ width: 96 }}>PG (S4)</th><th style={{ width: 96 }}>Scale (S5)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.tag}>
                <td><input type="checkbox" checked={checked.includes(i)} onChange={() => toggle(i)} /></td>
                <td className="td-mono" style={{ fontWeight: 600, color: 'var(--ink-1)' }}>{r.tag}</td>
                <td className="td-mono" style={{ color: 'var(--ink-3)' }}>{r.address}</td>
                <td>{r.desc}</td>
                <td><StPill done={stKw(r)} /></td>
                <td><StPill done={stPg(r)} /></td>
                <td><StPill done={stScale(r)} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
        <Button variant="primary" icon={<Icon.play />} onClick={onNext}>前往 Step 3 · Kepware 建點</Button>
      </div>
    </>
  );
}

Object.assign(window, { TabImportKW, Step1Csv, Step2Staging });
