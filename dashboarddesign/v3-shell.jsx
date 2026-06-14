// Kep It Simple v3 — shell components + shared Kepware mock data.
// Reuses Icon / Button / Card / Pill from primitives.jsx.
const { useState: useStateS3, useEffect: useEffectS3 } = React;

// ---- Logo (light/azure edition) ----
function KisMark3({ size = 38, dark = false }) {
  return (
    <svg viewBox="0 0 64 64" width={size} height={size}>
      <rect x="2" y="2" width="60" height="60" rx="14" fill={dark ? '#2C3040' : '#FFFFFF'} stroke="#4FACE5" strokeOpacity="0.7" strokeWidth="1.5"></rect>
      <rect x="2" y="2" width="60" height="60" rx="14" fill="url(#kis3G)" opacity="0.5"></rect>
      <text x="32" y="40" textAnchor="middle" fontFamily="'JetBrains Mono', monospace" fontWeight="800" fontSize="17" fill={dark ? '#EAF1FC' : '#353A4C'} letterSpacing="-0.01em">KIS</text>
      <circle className="logo-pulse" cx="50" cy="50" r="3.6" fill="#367ADF"></circle>
      <defs>
        <radialGradient id="kis3G" cx="0.8" cy="0.2" r="0.9">
          <stop offset="0" stopColor="#AED5F5" stopOpacity="0.9"></stop>
          <stop offset="1" stopColor="#FFFFFF" stopOpacity="0"></stop>
        </radialGradient>
      </defs>
    </svg>
  );
}

// ---- clock ----
function useClock3() {
  const [now, setNow] = useStateS3(new Date());
  useEffectS3(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}
const pad03 = (n) => String(n).padStart(2, '0');

// ---- Matrix-style falling code (blue palette) ----
function MatrixRain3() {
  const ref = React.useRef(null);
  useEffectS3(() => {
    const cv = ref.current; if (!cv) return;
    const ctx = cv.getContext('2d');
    let raf, W, H, cols, drops, fontSize = 14;
    const glyphs = 'ｱｲｳｴｵｶｷｸ0123456789KISTAGCHSPWRFACMFGENV<>=∈∅'.split('');
    const resize = () => {
      W = cv.width = cv.offsetWidth; H = cv.height = cv.offsetHeight;
      cols = Math.floor(W / fontSize);
      drops = Array.from({ length: cols }, () => Math.random() * -H / fontSize);
    };
    resize();
    const ro = new ResizeObserver(resize); ro.observe(cv);
    let t = 0;
    const draw = () => {
      // self-heal: 登入面板在 mount 時 offsetWidth 可能為 0，尺寸一變就重建
      if (cv.offsetWidth && cv.width !== cv.offsetWidth) resize();
      ctx.fillStyle = 'rgba(44, 48, 64, 0.10)';
      ctx.fillRect(0, 0, W, H);
      ctx.font = `${fontSize}px 'JetBrains Mono', monospace`;
      for (let i = 0; i < cols; i++) {
        const x = i * fontSize, y = drops[i] * fontSize;
        const ch = glyphs[(Math.floor(Math.random() * glyphs.length))];
        // leading glyph bright, trail in icy/azure
        ctx.fillStyle = Math.random() > 0.975 ? 'rgba(174, 213, 245, 0.95)' : 'rgba(79, 172, 229, 0.55)';
        ctx.fillText(ch, x, y);
        if (y > H && Math.random() > 0.975) drops[i] = 0;
        drops[i] += 0.5 + (i % 3) * 0.12;
      }
      t++;
      raf = requestAnimationFrame(draw);
    };
    draw();
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, []);
  return <canvas ref={ref} className="lv3-rain"></canvas>;
}

// ---- Top bar ----
function TopBar3({ tabs, tab, setTab, gw, user, onLogout }) {
  const now = useClock3();
  return (
    <header className="v3-topbar">
      <div className="v3-brand">
        <KisMark3 size={34} dark={true} />
        <div>
          <h1>Kep It Simple</h1>
          <div className="sub">Kepware Tag Management · v3</div>
        </div>
      </div>
      <nav className="v3-nav">
        {tabs.map(t => {
          const Ico = Icon[t.icon];
          return (
            <button key={t.id} className={`v3-nav-item ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)} data-screen-label={t.label}>
              {Ico && <Ico />}
              <span>{t.label}</span>
            </button>
          );
        })}
      </nav>
      <div className="v3-top-right">
        <span className={`v3-conn-pill ${gw.status === 'connected' ? '' : 'off'}`}>
          <span className="dot"></span>
          KEPWARE GW · {gw.status === 'connected' ? 'ONLINE' : 'OFFLINE'}
        </span>
        <span className="v3-clock">{pad03(now.getHours())}:{pad03(now.getMinutes())}:{pad03(now.getSeconds())}</span>
        <div className="v3-user">
          <div className="av">{(user || '?').slice(0, 1).toUpperCase()}</div>
          <span className="nm">{user}</span>
          <button className="icon-btn" title="登出" onClick={onLogout}><Icon.logout /></button>
        </div>
      </div>
    </header>
  );
}

// ---- Login (split, daylight) ----
function Login3({ onLogin }) {
  const [u, setU] = useStateS3('');
  const [p, setP] = useStateS3('');
  const [err, setErr] = useStateS3('');
  const [busy, setBusy] = useStateS3(false);
  const submit = (e) => {
    e.preventDefault();
    if (!u || !p) { setErr('請輸入帳號與密碼'); return; }
    setErr(''); setBusy(true);
    setTimeout(() => { setBusy(false); onLogin(u); }, 400);
  };
  return (
    <div className="lv3">
      <div className="lv3-left">
        <MatrixRain3 />
        <div className="lv3-scan"></div>
        <div className="lv3-grid"></div>
        <div className="lv3-brand">
          <KisMark3 size={50} dark={true} />
          <div>
            <h1>Kep It Simple</h1>
            <div className="sub">Kepware · Tag Management</div>
          </div>
        </div>
        <div className="lv3-mid">
          <h2>推導、比對、確認 —— 建點不再盲推。</h2>
          <p>CSV 上傳 → 推導 + 結構驗證 → 可編輯預覽 → 確認執行。純 Kepware 路線的點位管理主控台。</p>
        </div>
        <div className="lv3-readout">
          <div className="rr"><span className="rk">GATEWAY</span><span className="rv">10.11.64.70:57412</span></div>
          <div className="rr"><span className="rk">ENVIRONMENT</span><span className="rv">Production · 工廠 A</span></div>
          <div className="rr"><span className="rk">PLATFORM</span><span className="rv ok">● Kepware API Gateway · READY</span></div>
        </div>
      </div>
      <div className="lv3-right">
        <form className="lv3-form" onSubmit={submit}>
          <h3>登入主控台</h3>
          <p className="hint">請以您的系統帳號登入以繼續操作。</p>
          <div className="lf-row">
            <label>帳號</label>
            <input className="input" autoComplete="username" value={u} onChange={e => setU(e.target.value)} />
          </div>
          <div className="lf-row">
            <label>密碼</label>
            <input className="input" type="password" autoComplete="current-password" value={p} onChange={e => setP(e.target.value)} />
          </div>
          {err && <div className="err">{err}</div>}
          <Button variant="primary" icon={<Icon.shield />} type="submit">{busy ? '登入中…' : '登入'}</Button>
          <div className="lv3-meta">KIS v3 · build 2026.06.13</div>
        </form>
      </div>
    </div>
  );
}

// ---- Modal ----
function Modal3({ title, body, danger, confirmLabel, onConfirm, onCancel }) {
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-box" onClick={e => e.stopPropagation()}>
        <h3>{danger && <Icon.alert />}{title}</h3>
        <p>{body}</p>
        <div className="actions">
          <Button variant="ghost" onClick={onCancel}>取消</Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>{confirmLabel || '確認'}</Button>
        </div>
      </div>
    </div>
  );
}

// ---- Kepware Gateway zone presets ----
const GW_ZONES = {
  Zone1: { label: 'Zone 1 · 廠區 A',  url: 'https://10.11.64.70:57412', username: 'kw_admin' },
  Zone2: { label: 'Zone 2 · 廠區 B',  url: 'https://10.11.72.40:57412', username: 'kw_oper' },
};

// ---- Kepware Gateway connection card (shared by Step 3 + 批次刪除) ----
function GwConnCard({ gw, setGw }) {
  const test = () => {
    setGw(g => ({ ...g, status: 'connecting' }));
    setTimeout(() => setGw(g => ({ ...g, status: 'connected' })), 700);
  };
  const pickZone = (z) => {
    const p = GW_ZONES[z];
    setGw(g => ({ ...g, zone: z, url: p.url, username: p.username, status: 'connecting' }));
    setTimeout(() => setGw(g => ({ ...g, status: 'connected' })), 700);
  };
  const statusCls = { connected: 'ok', connecting: 'busy' }[gw.status] || 'idle';
  const statusTxt = { connected: '● 已連線', connecting: '◌ 測試中…' }[gw.status] || '○ 未連線';
  return (
    <Card title="Kepware Gateway 連線" icon={<Icon.link />}
      actions={
        <div className="zone-switch">
          {Object.keys(GW_ZONES).map(z => (
            <button key={z} className={`zone-btn ${gw.zone === z ? 'on' : ''}`} onClick={() => pickZone(z)}>{z}</button>
          ))}
        </div>
      }>
      <div className="gw-grid">
        <div>
          <div className="field-label">Gateway</div>
          <select className="select" value={gw.zone || 'Zone1'} onChange={e => pickZone(e.target.value)}>
            {Object.entries(GW_ZONES).map(([z, p]) => <option key={z} value={z}>{p.label}</option>)}
          </select>
        </div>
        <div>
          <div className="field-label">GW URL</div>
          <input className="input" value={gw.url} onChange={e => setGw(g => ({ ...g, url: e.target.value, zone: null }))} />
        </div>
        <div>
          <div className="field-label">帳號</div>
          <input className="input" value={gw.username} onChange={e => setGw(g => ({ ...g, username: e.target.value }))} />
        </div>
        <Button variant="outline" icon={<Icon.link />} onClick={test}>{gw.status === 'connecting' ? '測試中…' : '測試連線'}</Button>
        <span className={`gw-status ${statusCls}`}>{statusTxt}</span>
      </div>
    </Card>
  );
}

// ================= Mock data =================

// Kepware 結構快取（5 Channels / 12 Devices / 28 Groups）
const KW_STRUCTURE = {
  K8CHS: { CHS: ['B1.CHS', '1F.CHS', '2F.CHS', '3F.CHS'], AHU: ['2F.AHU', '3F.AHU', 'RF.AHU'], FCU: ['1F.FCU', '2F.FCU'] },
  K8PWR: { PMS: ['B1.PMS', '1F.PMS', '2F.PMS', '3F.PMS'], UPS: ['B1.UPS'], GEN: ['B1.GEN'] },
  K8FAC: { FAC: ['B1.FAC', '3F.FAC'], LGT: ['1F.LGT', '2F.LGT', '3F.LGT'] },
  K8MFG: { MFG: ['2F.MFG', '2F.MFG.L1', '2F.MFG.L2'], CNV: ['2F.CNV'] },
  K8ENV: { ENV: ['RF.ENV', 'RF.ENV.AQ'], WTR: ['B1.WTR', 'RF.WTR'] },
};
const kwCounts = (s) => {
  const channels = Object.keys(s).length;
  let devices = 0, groups = 0;
  Object.values(s).forEach(devs => { devices += Object.keys(devs).length; Object.values(devs).forEach(g => groups += g.length); });
  return { channels, devices, groups };
};

// CSV 暫存資料（推導前的原始列）
const STAGED_ROWS = [
  ['K8_1F_CHS_CH01_SWT',  'K8CHS', 'CHS', '1F.CHS',    '冰水主機 1 出水溫度'],
  ['K8_1F_CHS_CH01_RWT',  'K8CHS', 'CHS', '1F.CHS',    '冰水主機 1 回水溫度'],
  ['K8_2F_CHS_AHU01_SAT', 'K8CHS', 'AHU', '2F.AHU',    'AHU-01 送風溫度'],
  ['K8_2F_CHS_AHU01_RAT', 'K8CHS', 'AHU', '2F.AHU',    'AHU-01 回風溫度'],
  ['K8_3F_CHS_AHU02_SAT', 'K8CHS', 'AHU', '3F.AHU',    'AHU-02 送風溫度'],
  ['K8_B1_PWR_PMS01_KW',  'K8PWR', 'PMS', 'B1.PMS',    '電表 PMS-01 即時功率'],
  ['K8_B1_PWR_PMS01_PF',  'K8PWR', 'PMS', 'B1.PMS',    '電表 PMS-01 功率因數'],
  ['K8_B1_PWR_UPS01_SOC', 'K8PWR', 'UPS', 'B1.UPS',    'UPS-01 電池電量'],
  ['K8_2F_MFG_L1_CNT',    'K8MFG', 'MFG', '2F.MFG.L1', '產線 1 產量計數'],
  ['K8_2F_MFG_L1_STAT',   'K8MFG', 'MFG', '2F.MFG.L1', '產線 1 運轉狀態'],
  ['K8_3F_FAC_FAN01_SPD', 'K8FAC', 'FAC', '3F.FAC',    '排風機 1 轉速'],
  ['K8_RF_ENV_AQ01_PM25', 'K8ENV', 'ENV', 'RF.ENV.AQ', '空品 AQ-01 PM2.5'],
  ['K8_4F_CHS_AHU03_SAT', 'K8CHS', 'AHU', '4F.AHU',    'AHU-03 送風溫度'],
  ['K8_4F_CHS_AHU03_RAT', 'K8CHS', 'AHU', '4F.AHU',    'AHU-03 回風溫度'],
  ['K8_4F_PWR_PMS02_KW',  'K8PWR', 'PMS', '4F.PMS',    '電表 PMS-02 即時功率'],
  ['K8_1F_MFG_L2_CNT',    'K8MFG', 'MFG', '1F.MFG.L2', '產線 2 產量計數'],
  ['K8_B1_WTR_PMP01_FLW', 'K8ENV', 'WTR', 'B1.WTR.PMP','補水泵 1 流量'],
  ['K8_5F_BMS_VAV01_TMP', 'K8BMS', 'BMS', '5F.VAV',    'VAV-01 室內溫度'],
  ['K8_B1_FAC_PMP01_STA', 'K8FAC', 'PMP', 'B1.PMP',    '污水泵 1 狀態'],
].map(([tag, channel, device, groups, desc]) => ({
  tag, channel, device, groups, desc,
  address: 'ns=2;s=' + tag.replace(/_/g, '.'),
}));

// 驗證：比對 kepware_structure 快取
function validateRow(row, structure) {
  const chOk = !!structure[row.channel];
  const devOk = chOk && !!structure[row.channel][row.device];
  const grpOk = devOk && structure[row.channel][row.device].includes(row.groups);
  return {
    channel_exists: chOk,
    device_exists: devOk,
    group_exists: grpOk,
    group_auto_create: devOk && !grpOk,
  };
}
// 狀態代碼：ok = 路徑已存在 / add = 自動建 Group / warn = Channel 或 Device 不存在
function rowStatus(v) {
  if (!v.channel_exists || !v.device_exists) return 'warn';
  if (!v.group_exists) return 'add';
  return 'ok';
}

Object.assign(window, {
  KisMark3, TopBar3, Login3, Modal3, GwConnCard, MatrixRain3, GW_ZONES, useClock3, pad03,
  KW_STRUCTURE, kwCounts, STAGED_ROWS, validateRow, rowStatus,
});
