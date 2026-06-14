// Primitives: Button, Card, Pill, Dot, Icon set
const Icon = {
  plus:    () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M8 3v10M3 8h10"/></svg>,
  trash:   () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M3 5h10M6 5V3.5C6 3 6.5 2.5 7 2.5h2c.5 0 1 .5 1 1V5M5 5l.5 8c0 .5.5 1 1 1h3c.5 0 1-.5 1-1L11 5"/></svg>,
  search:  () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L13.5 13.5"/></svg>,
  download:() => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M8 2v8m0 0L5 7m3 3l3-3M3 13h10"/></svg>,
  upload:  () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M8 11V3m0 0L5 6m3-3l3 3M3 13h10"/></svg>,
  refresh: () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M3 8a5 5 0 018-3.5L13 6M13 3v3h-3M13 8a5 5 0 01-8 3.5L3 10M3 13v-3h3"/></svg>,
  play:    () => <svg viewBox="0 0 16 16" fill="currentColor"><path d="M4 3l9 5-9 5z"/></svg>,
  check:   () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M3 8l3 3 7-7"/></svg>,
  x:       () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>,
  alert:   () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M8 2L1.5 13.5h13L8 2z"/><path d="M8 6v3.5M8 11.5v.5"/></svg>,
  shield:  () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M8 1.5L2.5 4v4c0 3 2.5 5.5 5.5 6.5C11 13.5 13.5 11 13.5 8V4L8 1.5z"/></svg>,
  shield_check: () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M8 1.5L2.5 4v4c0 3 2.5 5.5 5.5 6.5C11 13.5 13.5 11 13.5 8V4L8 1.5z"/><path d="M5.5 8l1.7 1.7L10.5 6.5"/></svg>,
  link:    () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M6.5 9.5L9.5 6.5M6 5L4 7a2.5 2.5 0 003.5 3.5L9 9M10 11l2-2a2.5 2.5 0 00-3.5-3.5L7 7"/></svg>,
  logout:  () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M9 3H4v10h5M9 8H14M11 5l3 3-3 3"/></svg>,
  settings:() => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="8" cy="8" r="2.2"/><path d="M8 1v2M8 13v2M3.5 3.5l1.5 1.5M11 11l1.5 1.5M1 8h2M13 8h2M3.5 12.5L5 11M11 5l1.5-1.5"/></svg>,
  history: () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 5v3.5L10.5 10"/></svg>,
  mapping: () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="3.5" cy="4" r="1.5"/><circle cx="3.5" cy="12" r="1.5"/><circle cx="12.5" cy="8" r="1.5"/><path d="M5 4h2.5L11 8M5 12h2.5L11 8"/></svg>,
  kepware: () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2 6h12M5 9h2M5 11h4"/></svg>,
  users:   () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="6" cy="6" r="2.5"/><path d="M2 13c0-2 2-3.5 4-3.5s4 1.5 4 3.5"/><circle cx="11" cy="5" r="1.8"/><path d="M11 9c1.5 0 3 1 3 2.5"/></svg>,
  filter:  () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"><path d="M2 3h12l-4.5 5.5V13l-3-1.5V8.5L2 3z"/></svg>,
  database:() => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><ellipse cx="8" cy="3.5" rx="5.5" ry="1.8"/><path d="M2.5 3.5v9c0 1 2.5 1.8 5.5 1.8s5.5-.8 5.5-1.8v-9"/><path d="M2.5 8c0 1 2.5 1.8 5.5 1.8s5.5-.8 5.5-1.8"/></svg>,
  bot:     () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="3" y="5" width="10" height="8" rx="2"/><circle cx="6" cy="9" r="0.8" fill="currentColor"/><circle cx="10" cy="9" r="0.8" fill="currentColor"/><path d="M8 2v3M5 5L3.5 3.5M11 5l1.5-1.5M3 13v1.5M13 13v1.5"/></svg>,
  send:    () => <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2L7 9M14 2l-4 12-3-7-7-3 14-2z"/></svg>,
};

function Button({ children, variant = 'outline', size, icon, onClick, type = 'button' }) {
  const cls = `btn btn-${variant} ${size === 'sm' ? 'btn-sm' : ''}`;
  return <button type={type} className={cls} onClick={onClick}>{icon}{children}</button>;
}

function Card({ title, icon, actions, children }) {
  return (
    <div className="kis-card">
      <div className="kis-card-head">
        <div className="kis-card-title">{icon}{title}</div>
        {actions}
      </div>
      {children}
    </div>
  );
}

function Pill({ kind = 'muted', children }) {
  return <span className={`pill pill-${kind}`}>{children}</span>;
}

function Dot({ kind = 'muted' }) {
  return <span className={`kis-dot kis-dot-${kind}`} />;
}

Object.assign(window, { Icon, Button, Card, Pill, Dot });
