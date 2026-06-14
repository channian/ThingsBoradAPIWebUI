// AI Agent 客服浮動視窗
const { useState: useChatState, useRef: useChatRef, useEffect: useChatEffect } = React;

function ChatWidget() {
  const [open, setOpen] = useChatState(false);
  const [messages, setMessages] = useChatState([
    { role: 'assistant', text: '您好！我是 KIS 助手，可以協助您操作 Kepware 點位管理系統。有什麼需要幫忙的嗎？', time: '14:30' }
  ]);
  const [input, setInput] = useChatState('');
  const [typing, setTyping] = useChatState(false);
  const scrollRef = useChatRef(null);

  useChatEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, open]);

  const send = () => {
    if (!input.trim()) return;
    const now = new Date();
    const time = `${now.getHours()}:${String(now.getMinutes()).padStart(2, '0')}`;
    
    setMessages(m => [...m, { role: 'user', text: input, time }]);
    setInput('');
    setTyping(true);

    // 模擬 AI 回應
    setTimeout(() => {
      const replies = [
        '我了解您的問題。您可以在「批次新增」分頁上傳 CSV 檔案，系統會自動驗證 DeviceProfile。',
        '建議您先執行 DRY RUN 模式確認無誤後，再切換到 LIVE 模式執行。',
        '若要查詢現有裝置，可以使用「資料庫進階搜尋」分頁，支援多條件篩選。',
        '您可以在「操作歷史」分頁查看所有執行紀錄，包含成功、略過、失敗的統計。',
        'IO Mapping 功能可以協助您比對需求表與 iFIX IO 表，找出未匹配的點位。'
      ];
      const reply = replies[Math.floor(Math.random() * replies.length)];
      setMessages(m => [...m, { role: 'assistant', text: reply, time }]);
      setTyping(false);
    }, 800 + Math.random() * 400);
  };

  const quickActions = [
    { icon: Icon.plus, label: '如何批次新增？' },
    { icon: Icon.search, label: '怎麼查詢裝置？' },
    { icon: Icon.alert, label: 'DRY RUN 是什麼？' },
  ];

  return (
    <>
      {/* 浮動按鈕 */}
      {!open && (
        <button className="chat-fab" onClick={() => setOpen(true)} title="AI 助手">
          <Icon.bot />
          <span className="chat-pulse"></span>
        </button>
      )}

      {/* 對話視窗 */}
      {open && (
        <div className="chat-window">
          <div className="chat-header">
            <div className="chat-header-info">
              <Icon.bot />
              <div>
                <div className="chat-title">KIS 助手</div>
                <div className="chat-status"><span className="chat-dot"></span>線上服務中</div>
              </div>
            </div>
            <div className="chat-actions">
              <button className="chat-btn-icon" onClick={() => setMessages([messages[0]])} title="清除對話">
                <Icon.refresh style={{ width: 14, height: 14 }} />
              </button>
              <button className="chat-btn-icon" onClick={() => setOpen(false)} title="關閉">
                <Icon.x style={{ width: 14, height: 14 }} />
              </button>
            </div>
          </div>

          <div className="chat-body" ref={scrollRef}>
            {messages.map((msg, i) => (
              <div key={i} className={`chat-msg ${msg.role}`}>
                {msg.role === 'assistant' && <div className="chat-avatar"><Icon.bot /></div>}
                <div className="chat-bubble">
                  <div className="chat-text">{msg.text}</div>
                  <div className="chat-time">{msg.time}</div>
                </div>
                {msg.role === 'user' && <div className="chat-avatar chat-avatar-user">你</div>}
              </div>
            ))}
            {typing && (
              <div className="chat-msg assistant">
                <div className="chat-avatar"><Icon.bot /></div>
                <div className="chat-bubble">
                  <div className="chat-typing">
                    <span></span><span></span><span></span>
                  </div>
                </div>
              </div>
            )}

            {messages.length === 1 && (
              <div className="chat-quick">
                <div className="chat-quick-title">常見問題</div>
                {quickActions.map((q, i) => {
                  const Ico = q.icon;
                  return (
                    <button key={i} className="chat-quick-btn" onClick={() => { setInput(q.label); }}>
                      <Ico />{q.label}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="chat-footer">
            <input
              className="chat-input"
              placeholder="輸入您的問題…"
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') send(); }}
            />
            <button className="chat-send" onClick={send} disabled={!input.trim()}>
              <Icon.send />
            </button>
          </div>
        </div>
      )}
    </>
  );
}

Object.assign(window, { ChatWidget });
