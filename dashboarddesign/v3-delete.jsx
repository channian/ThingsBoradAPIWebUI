// Kep It Simple v3 — 批次刪除（Kepware API，獨立一級分頁）
const { useState: useStateD3 } = React;

function TabDeleteKW({ gw, setGw }) {
  const [file, setFile] = useStateD3(null);
  const [phase, setPhase] = useStateD3('idle'); // idle | running | done
  const [modal, setModal] = useStateD3(null);

  const delRows = STAGED_ROWS.slice(0, 8).map(r => ({ tag: r.tag, channel: r.channel, device: r.device, groups: r.groups }));

  const pick = () => { setFile({ name: 'delete_list_202606.csv', rows: delRows.length }); setPhase('idle'); };

  const execute = () => {
    setModal({
      title: '確認批次刪除',
      body: `此操作將直接從 Kepware 刪除 ${delRows.length} 筆 Tag，無法復原。確定要執行嗎？`,
      danger: true,
      confirmLabel: `我已確認，刪除 ${delRows.length} 筆`,
      onConfirm: () => {
        setModal(null); setPhase('running');
        setTimeout(() => setPhase('done'), 1100);
      },
    });
  };

  return (
    <>
      <GwConnCard gw={gw} setGw={setGw} />

      <Card title="上傳刪除清單" icon={<Icon.upload />}>
        <div className="upload compact" onClick={pick}>
          <Icon.upload />
          <h4>{file ? '已選擇檔案，點擊可重新選擇' : '拖曳 CSV 至此，或點擊選擇檔案'}</h4>
          <p>需包含 name + type 欄位</p>
          {file && <span className="file-name"><Icon.check />{file.name} · {file.rows} 筆</span>}
        </div>
      </Card>

      {file && (
        <>
          <Card title="刪除預覽" icon={<Icon.trash />}
            actions={<Pill kind="err">{delRows.length} 筆待刪除</Pill>}>
            <div className="tbl-wrap">
              <table>
                <thead><tr><th>Tag Name</th><th>Channel</th><th>Device</th><th>Groups</th></tr></thead>
                <tbody>
                  {delRows.map(r => (
                    <tr key={r.tag}>
                      <td className="td-mono" style={{ fontWeight: 600, color: 'var(--ink-1)' }}>{r.tag}</td>
                      <td className="td-mono">{r.channel}</td>
                      <td className="td-mono">{r.device}</td>
                      <td className="td-mono">{r.groups}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          {phase !== 'done' && (
            <div className="banner warn">
              <div className="banner-icon"><Icon.alert /></div>
              <div className="banner-body">
                <h4>此操作將直接從 Kepware 刪除 Tag，無法復原！</h4>
                <p>請確認清單內容無誤。刪除後 Collector 仍需 Reload 才會停止輪詢這些點位。</p>
              </div>
            </div>
          )}

          {phase === 'done' && (
            <div className="banner ok">
              <div className="banner-icon"><Icon.check /></div>
              <div className="banner-body"><h4>已刪除 {delRows.length} 筆 Tag</h4><p>建議前往 Kepware Import → Step 6 執行 Collector Reload。</p></div>
            </div>
          )}

          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button variant="danger" icon={<Icon.trash />} onClick={execute}>
              {phase === 'running' ? '刪除中…' : `執行刪除 (${delRows.length} 筆)`}
            </Button>
          </div>
        </>
      )}

      {modal && <Modal3 {...modal} onCancel={() => setModal(null)} />}
    </>
  );
}

Object.assign(window, { TabDeleteKW });
