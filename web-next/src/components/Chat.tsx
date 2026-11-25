"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, askQuestion, checkHealth, deleteSession, listSessions, myPrefs, myTrips, sessionHistory,
  type ChatRequest, type ChatResult, type SessionSummary, type TripRecord, type UserMemoryItem,
} from "@/lib/api";
import Icon from "./Icon";
import type { Itinerary } from "./TripTimeline";
import type { InsightsPayload } from "../app/insights/page";
import styles from "./Chat.module.css";

interface Message {
  role: "user" | "assistant";
  content: string;
  error?: boolean;
  retryRequest?: ChatRequest;
  agents?: ChatResult["agents_called"];
  intents?: NonNullable<ChatResult["intention"]>["intents"];
  itinerary?: Itinerary | null;
  insights?: InsightsPayload | null;
  dataKey?: string; // 该消息的可视化数据在 localStorage 的 key（卡片链接携带）
  animate?: boolean; // 仅实时对话中新出现的消息做入场动画，历史回放不播
}

/** localStorage 治理：trip:/insights: 前缀 key 只保留最近 20 个（容量约 5MB 上限） */
function pruneVisualizationStorage(prefix: "trip" | "insights") {
  try {
    const keys: { k: string; t: number }[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(`${prefix}:`) && !k.endsWith(":latest_itinerary") && k !== `${prefix}:latest`) {
        // key 尾部是时间戳（新格式）或无时间戳（回放补的）——用 localStorage 元数据拿不到时间，
        // 简化：遍历时收集，超过 25 个删最旧的（FIFO 近似）
        keys.push({ k, t: 0 });
      }
    }
    while (keys.length > 25) {
      localStorage.removeItem(keys.shift()!.k);
    }
  } catch { /* ignore */ }
}

/** 从聚合结果里提取可可视化 skill 的结构化输出（费用/冲突/比价/天气） */
function extractInsights(result: unknown): InsightsPayload | null {
  const r = result as { results?: { agent_name?: string; data?: Record<string, unknown> }[] } | null;
  const rs = r?.results || [];
  const pick = (name: string) => rs.find(x => x.agent_name === name)?.data || null;
  const payload: InsightsPayload = {
    expense: pick("expense_estimate") as InsightsPayload["expense"],
    conflict: pick("conflict_check") as InsightsPayload["conflict"],
    compare: pick("compare_fares") as InsightsPayload["compare"],
    weather: pick("weather_alert") as InsightsPayload["weather"],
  };
  return Object.values(payload).some(Boolean) ? payload : null;
}

/** 从聚合结果里提取行程规划的结构化 itinerary（无则 null） */
function extractItinerary(result: unknown): Itinerary | null {
  const r = result as { results?: { agent_name?: string; data?: { itinerary?: Itinerary } }[] } | null;
  const hit = r?.results?.find((x) => x.agent_name === "itinerary_planning");
  return hit?.data?.itinerary || null;
}

const SUGGESTIONS = [
  "出差住宿标准是多少？",
  "如何报销差旅费用？需要哪些材料？",
  "我要去上海出差5天，帮我规划详细行程",
  "航班延误了怎么办？",
];

const AGENT_LABELS: Record<string, string> = {
  rag_knowledge: "知识库问答",
  memory_query: "记忆查询",
  preference: "偏好管理",
  information_query: "信息查询",
  itinerary_planning: "行程规划",
  budget_check: "预算标准",
  compare_fares: "价格对比",
  itinerary_lookup: "行程查询",
  weather_alert: "天气建议",
  expense_estimate: "费用预估",
  trip_compare: "方案对比",
  conflict_check: "冲突检测",
};

function makeSessionId() {
  return typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString();
}

// 会话回放时 assistant 消息已由后端渲染为展示文本；此函数仅兜底
function prettyAssistant(raw: string): string {
  return raw;
}

export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [online, setOnline] = useState<"ready" | "model_unavailable" | "offline" | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [userId, setUserId] = useState("default_user");
  const [userIdLocked, setUserIdLocked] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [historyError, setHistoryError] = useState("");
  const [activeSession, setActiveSession] = useState<string | null>(null);
  // 每会话独立的消息缓存：切换会话时先显示缓存（含正在生成的问题），不闪没
  const sessionMsgCache = useRef<Record<string, Message[]>>({});
  // 会话级 busy：动画只显示在真正有请求在跑的那个会话
  const pendingSessions = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [memories, setMemories] = useState<UserMemoryItem[]>([]);
  const [trips, setTrips] = useState<TripRecord[]>([]);
  const listRef = useRef<HTMLDivElement>(null);

  const welcome = "你好，我是商旅助手。我可以查询差旅政策与报销标准、记住你的出行偏好、规划完整的出差行程。";

  useEffect(() => { checkHealth().then(setOnline); }, []);
  // 恢复上次会话：默认用户直接锁定并拉会话列表，自动回到最近的会话
  useEffect(() => {
    const savedUid = window.localStorage.getItem("shanglv_uid");
    const uid = savedUid || "default_user";
    setUserId(uid);
    setUserIdLocked(true);
    listSessions(uid).then(ss => {
      setSessions(ss);
      // 有历史会话则恢复到最近的一个；没有则留欢迎页
      if (ss.length > 0) loadSessionRef.current?.(ss[0].session_id, uid);
    }).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" }); }, [messages, busy]);

  // 用户 ID 变化时加载会话首条（锁定后不可改，长期记忆按它隔离）
  const applyUserId = useCallback((uid: string) => {
    const v = uid.trim() || "default_user";
    window.localStorage.setItem("shanglv_uid", v);
    setUserId(v);
    setUserIdLocked(true)
    setSessionId(null)
    setActiveSession(null)
    setMessages([{ role: "assistant", content: welcome }])
    listSessions(v).then(setSessions).catch(() => undefined)
  }, [welcome])

  const refreshSessions = useCallback(() => {
    if (!userIdLocked) return;
    listSessions(userId).then(setSessions).catch(() => undefined);
  }, [userId, userIdLocked]);

  const startNewChat = useCallback(() => {
    setSessionId(makeSessionId());
    setActiveSession(null);
    setMessages([{ role: "assistant", content: welcome }]);
  }, [welcome]);

  const loadSession = useCallback(async (sid: string, uidOverride?: string) => {
    const uid = uidOverride ?? userId;
    setSessionId(sid);
    setActiveSession(sid);
    // 立即显示该会话的本地缓存（如有），避免切换瞬间闪没/白屏
    const cached = sessionMsgCache.current[sid];
    if (cached && cached.length > 0) {
      setMessages(cached);
    } else {
      setMessages([{ role: "assistant", content: welcome }]);
    }
    try {
      setHistoryError("");
      const h = await sessionHistory(uid, sid);
      const msgs: Message[] = h.map(m => ({
        role: m.role,
        content: m.role === "assistant" ? prettyAssistant(m.content) : m.content,
        itinerary: (m as { itinerary?: Itinerary }).itinerary || null,
        dataKey: (m as { dataKey?: string }).dataKey,
      }));
      const finalMsgs: Message[] = h.length ? msgs : [{ role: "assistant", content: welcome }];
      // 回放恢复：每条带可视化数据的消息都按当时的 key 回填（历史卡片链接仍指向自己的数据）
      finalMsgs.forEach((m, idx) => {
        if (m.itinerary && m.dataKey) {
          try { localStorage.setItem(`trip:${m.dataKey}`, JSON.stringify(m.itinerary)); } catch {}
        } else if (m.itinerary && !m.dataKey) {
          // 旧消息无 key（修复前落库的）：补一个确定性 key 并回填
          const k = `${sid}:msg${idx}`;
          m.dataKey = k;
          try { localStorage.setItem(`trip:${k}`, JSON.stringify(m.itinerary)); } catch {}
        }
        if (m.insights && m.dataKey) {
          try { localStorage.setItem(`insights:${m.dataKey}`, JSON.stringify(m.insights)); } catch {}
        } else if (m.insights && !m.dataKey) {
          const k = `${sid}:msg${idx}`;
          m.dataKey = k;
          try { localStorage.setItem(`insights:${k}`, JSON.stringify(m.insights)); } catch {}
        }
      });
      const lastTrip = [...finalMsgs].reverse().find(m => m.itinerary);
      if (lastTrip?.itinerary) {
        try { localStorage.setItem("trip:latest_itinerary", JSON.stringify(lastTrip.itinerary)); } catch {}
      }
      // 只在仍是当前会话时更新视图，避免竞态（快速切换会话时旧响应覆盖新会话）
      setSessionId(cur => {
        if (cur === sid) {
          const latest = sessionMsgCache.current[sid];
          const keepLocal = latest && (latest !== cached ||
            pendingSessions.current.has(sid) || latest.some(message => message.retryRequest));
          const visible = keepLocal ? latest : finalMsgs;
          setMessages(visible);
          sessionMsgCache.current[sid] = visible;
        }
        return cur;
      });
    } catch {
      setHistoryError("历史对话加载失败，请稍后重试。");
    }
  }, [userId, welcome]);
  const loadSessionRef = useRef<{ (sid: string, uid?: string): void } | null>(null);
  loadSessionRef.current = loadSession;

  const removeSession = useCallback(async (sid: string) => {
    try {
      await deleteSession(userId, sid);
      setHistoryError("");
    } catch {
      setHistoryError("删除失败，对话仍保留，请稍后重试。");
      return;
    }
    delete sessionMsgCache.current[sid];
    setSessions(ss => ss.filter(x => x.session_id !== sid));
    if (sid === sessionId) startNewChat();
  }, [userId, sessionId, startNewChat]);

  const loadMemory = useCallback(async () => {
    try {
      const [p, t] = await Promise.all([myPrefs(userId), myTrips(userId)]);
      setMemories(Object.entries(p.preferences || {}).map(([key, value]) => ({ key, value })));
      setTrips(t.history || []);
    } catch {
      setMemories([]); setTrips([]);
    }
  }, [userId]);
  useEffect(() => { if (memoryOpen) loadMemory(); }, [memoryOpen, loadMemory]);

  const send = useCallback(async (forced?: string, retryRequest?: ChatRequest) => {
    const query = (retryRequest?.message ?? forced ?? input).trim();
    if (!query || pendingSessions.current.size > 0) return;
    if (!userIdLocked) applyUserId(userId);
    const sid = retryRequest?.session_id ?? sessionId ?? makeSessionId();
    const request = retryRequest ?? {
      message: query,
      user_id: userId,
      session_id: sid,
      request_id: Array.from(crypto.getRandomValues(new Uint8Array(16)),
        value => value.toString(16).padStart(2, "0")).join(""),
    };
    if (!retryRequest) {
      const userMsg: Message = { role: "user", content: query, animate: true };
      setSessionId(sid);
      setMessages(m => [...m, userMsg]);
      sessionMsgCache.current[sid] = [...(sessionMsgCache.current[sid] || []), userMsg];
      setInput("");
    }

    function showResult(message: Message) {
      const cached = sessionMsgCache.current[sid] || [];
      const index = retryRequest ? cached.findIndex(item => item.retryRequest === retryRequest) : -1;
      const updated = [...cached];
      if (index < 0) updated.push(message);
      else updated[index] = message;
      sessionMsgCache.current[sid] = updated;
      setSessionId(cur => {
        if (cur === sid) setMessages(updated);
        return cur;
      });
    }

    pendingSessions.current.add(sid);
    setBusy(true);
    forceRender(n => n + 1);
    try {
      const r = await askQuestion(request);
      const replyMsg: Message = {
        role: "assistant",
        content: r.display_text || "（无输出）",
        agents: r.agents_called,
        intents: r.intention?.intents,
        itinerary: extractItinerary(r.result),
        insights: extractInsights(r.result),
        animate: true,
      };
      // 行程/洞察数据按消息粒度存 localStorage（卡片链接带 key，多轮不互相覆盖）
      const msgKey = `${r.session_id || sid}:${Date.now()}`;
      if (replyMsg.itinerary) {
        replyMsg.dataKey = msgKey;
        try {
          localStorage.setItem(`trip:${msgKey}`, JSON.stringify(replyMsg.itinerary));
          localStorage.setItem("trip:latest_itinerary", JSON.stringify(replyMsg.itinerary)); // 兜底（直接访问 /trip 无参数时）
          pruneVisualizationStorage("trip");
        } catch { /* 容量满等异常忽略 */ }
      }
      // 洞察数据存入 localStorage（/insights 可视化页读取）
      const insights = extractInsights(r.result);
      if (insights) {
        replyMsg.dataKey = msgKey;
        try {
          localStorage.setItem(`insights:${msgKey}`, JSON.stringify(insights));
          localStorage.setItem("insights:latest", JSON.stringify(insights));
          pruneVisualizationStorage("insights");
        } catch { /* ignore */ }
      }
      showResult(replyMsg);
      refreshSessions();
    } catch (e) {
      showResult({
        role: "assistant",
        content: `请求失败：${e instanceof Error ? e.message : String(e)}`,
        error: true,
        retryRequest: !(e instanceof ApiError) || e.status >= 500 ? request : undefined,
      });
    } finally {
      pendingSessions.current.delete(sid);
      setBusy(pendingSessions.current.size > 0);
      forceRender(n => n + 1);
    }
  }, [input, userId, userIdLocked, sessionId, applyUserId, refreshSessions]);

  const hasConversation = messages.some(m => m.role === "user");

  return <div className={styles.app}>
    <aside className={`${styles.sidebar} ${sidebarOpen ? "" : styles.sidebarClosed}`}>
      <div className={styles.sideBrand}><span>商</span><div><b>商旅助手</b><small>TRAVEL COPILOT</small></div></div>
      <button className={styles.newChatBtn} onClick={startNewChat}><Icon name="plus" size={17} />开启新会话</button>
      <div className={styles.sidebarBody}>
        <div className={styles.sectionTitle}>用户身份 <em>{userIdLocked ? "已锁定" : "未锁定"}</em></div>
        <div className={styles.sessionItem} style={{ cursor: userIdLocked ? "default" : "text" }}>
          <Icon name="agent" size={14} />
          <input
            className={styles.sessionTitle}
            style={{ background: "transparent", border: 0, outline: 0, color: "inherit", font: "inherit", width: "100%" }}
            value={userId}
            disabled={userIdLocked}
            placeholder="输入用户ID"
            onChange={e => setUserId(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") applyUserId(userId); }}
          />
        </div>
        {!userIdLocked && <div className={styles.empty}>输入用户 ID 后回车锁定；长期记忆（偏好/行程）按用户 ID 隔离存储。当前默认 default_user。</div>}
        {userIdLocked && <>
          <div className={styles.sectionTitle}>最近会话 <em>{sessions.length}</em></div>
          {historyError && <div className={styles.empty} role="alert">{historyError}</div>}
          {sessions.length === 0 && <div className={styles.empty}>问一个问题，开始积累差旅记忆</div>}
          {sessions.map(s => <div key={s.session_id}
            className={`${styles.sessionItem} ${activeSession === s.session_id ? styles.sessionActive : ""}`}
            onClick={() => loadSession(s.session_id)}>
            <Icon name="chat" size={14} />
            <span className={styles.sessionTitle}>{s.title || "新对话"}</span>
            <button onClick={e => { e.stopPropagation(); removeSession(s.session_id); }}><Icon name="trash" size={13} /></button>
          </div>)}
        </>}
      </div>
      <a className={styles.memoryEntry} href="/history" target="_blank" rel="noopener" style={{ textDecoration: "none", marginBottom: 8 }}>
        <span><Icon name="memory" size={17} /></span>
        <div><b>差旅足迹</b><small>历史目的地地图与统计</small></div>
        <Icon name="arrow" size={14} />
      </a>
      <button className={styles.memoryEntry} onClick={() => setMemoryOpen(true)}>
        <span><Icon name="memory" size={17} /></span>
        <div><b>我的长期记忆</b><small>偏好与差旅历史（PostgreSQL）</small></div>
        <Icon name="arrow" size={14} />
      </button>
      <div className={styles.sideUser}>
        <div>{userId.slice(0, 1).toUpperCase()}</div>
        <span><b>{userId}</b><small>商旅用户 · {userIdLocked ? "记忆已连接" : "未锁定"}</small></span>
        {userIdLocked && <button title="切换用户" onClick={() => { setUserIdLocked(false); setSessions([]); setActiveSession(null); }}><Icon name="logout" size={15} /></button>}
      </div>
    </aside>
    <section className={styles.main}>
      <header className={styles.header}>
        <div className={styles.left}>
          <button className={styles.collapseBtn} onClick={() => setSidebarOpen(v => !v)}><Icon name="menu" size={18} /></button>
          <div><b>商旅助手工作台</b><small>差旅政策 · 偏好记忆 · 行程规划</small></div>
        </div>
        <div className={styles.right}>
          <span className={`${styles.status} ${online === "ready" ? styles.ok : online !== null ? styles.err : ""}`}>
            <i />{online === null ? "连接中" : online === "ready" ? "Agent 服务正常" : online === "model_unavailable" ? "后端在线 · 模型未就绪" : "后端不可用"}
          </span>
          <span className={styles.engine}>MULTI-AGENT</span>
        </div>
      </header>
      <main className={`${styles.chat} ${!hasConversation ? styles.chatEmpty : ""}`} ref={listRef}>
        {!hasConversation && <div className={styles.welcome}>
          <div className={styles.welcomeMark}><Icon name="spark" size={25} /></div>
          <span className={styles.welcomeKicker}>ASK ANYTHING ABOUT TRAVEL</span>
          <h1>今天要安排哪趟差旅？</h1>
          <p>{welcome}</p>
          <div className={styles.suggestions}>
            {SUGGESTIONS.map((s, i) => <button key={s} onClick={() => send(s)}><span>0{i + 1}</span>{s}<Icon name="arrow" size={14} /></button>)}
          </div>
          <div className={styles.flow}>
            <span><Icon name="search" size={14} />混合检索</span><i>→</i>
            <span><Icon name="memory" size={14} />偏好记忆</span><i>→</i>
            <span><Icon name="agent" size={14} />多智能体编排</span><i>→</i>
            <span><Icon name="send" size={14} />行程生成</span>
          </div>
        </div>}
        {hasConversation && messages.map((m, i) => <article key={i} className={`${styles.msgRow} ${m.animate ? "" : styles.noAnim} ${m.role === "user" ? styles.userRow : styles.assistantRow}`}>
          <div className={styles.roleAvatar}>{m.role === "user" ? "你" : <Icon name="spark" size={17} />}</div>
          <div className={`${styles.msg} ${m.error ? styles.errorText : ""}`}>
            {m.itinerary && (
              <a className="handbook-link" href={`/trip?key=${encodeURIComponent(m.dataKey || "latest_itinerary")}`} target="_blank" rel="noopener">
                <span className="hl-icon">🗺️</span>
                <div><b>查看行程手册</b><small>地图路线 · 每日时间轴 · 住宿建议</small></div>
                <span className="hl-arrow">→</span>
              </a>
            )}
            {m.insights && (
              <a className="handbook-link insights-link" href={`/insights?key=${encodeURIComponent(m.dataKey || "latest")}`} target="_blank" rel="noopener">
                <span className="hl-icon">📊</span>
                <div><b>查看数据洞察</b><small>费用对比 · 冲突时间轴 · 方案比价 · 出行建议</small></div>
                <span className="hl-arrow">→</span>
              </a>
            )}
            <div className={styles.msgHead}>
              <b>{m.role === "user" ? "你的问题" : "商旅助手回答"}</b>
              {m.intents?.length ? <span className={styles.verified}><Icon name="activity" size={13} />{m.intents[0].type}</span> : null}
            </div>
            <div className={styles.content}>{m.content}</div>
            {m.retryRequest && <div className={styles.rating}>
              <button type="button" disabled={busy} onClick={() => send(undefined, m.retryRequest)}>重试这条请求</button>
            </div>}
            {m.agents?.length ? <div className={styles.reasoning}>
              {m.agents.map((a, j) => <span key={j}><Icon name="agent" size={13} />{AGENT_LABELS[a.agent_name] || a.agent_name} {a.status === "success" ? "✓" : "✗"}</span>)}
            </div> : null}
            {m.role === "assistant" && !m.error && <div className={styles.rating}>
              <button onClick={() => navigator.clipboard.writeText(m.content)}><Icon name="copy" size={13} />复制</button>
            </div>}
          </div>
        </article>)}
        {sessionId && pendingSessions.current.has(sessionId) && <div className={styles.thinking}>
          <span className={styles.thinkingMark}><Icon name="spark" size={17} /></span>
          <div><b>正在协同处理</b><small>意图识别 · 并行调度子智能体 · 聚合生成</small><i><em /><em /><em /></i></div>
        </div>}
      </main>
      <footer className={styles.composer}>
        <div className={styles.composerBox}>
          <textarea value={input} rows={1} placeholder="描述你的差旅需求、政策疑问，或让我规划行程…"
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
          <button onClick={() => send()} disabled={busy || !input.trim()}><Icon name="send" size={18} /></button>
        </div>
        <p>回答基于企业差旅知识库 · 偏好跨会话记忆 · Shift + Enter 换行</p>
      </footer>
    </section>

    {memoryOpen && <div className={styles.modalBackdrop} onClick={() => setMemoryOpen(false)}>
      <section className={styles.memoryPanel} onClick={e => e.stopPropagation()}>
        <header>
          <div>
            <span>USER LONG-TERM MEMORY</span>
            <h2>我的长期记忆</h2>
            <p>偏好与差旅历史持久化在 PostgreSQL，跨会话可用；侧栏「切换用户」按钮可更换身份。</p>
          </div>
          <button onClick={() => setMemoryOpen(false)}>×</button>
        </header>
        <div className={styles.memoryList}>
          {memories.length === 0
            ? <div className={styles.memoryEmpty}><Icon name="memory" size={25} /><b>还没有偏好记录</b><small>对话中说「我喜欢住万豪，坐国航，请记住」即可写入长期偏好。</small></div>
            : memories.map(m => <div key={m.key}>
              <span><Icon name="memory" size={15} /></span>
              <div><b>{m.key}</b><p>{Array.isArray(m.value) ? m.value.join("、") : String(m.value)}</p><small>长期偏好</small></div>
            </div>)}
          {trips.length > 0 && <div className={styles.sectionTitle} style={{ marginTop: 10 }}>差旅历史 <em>{trips.length}</em></div>}
          {trips.slice().reverse().map((t, i) => <div key={i}>
            <span><Icon name="send" size={15} /></span>
            <div><b>{t.origin || "?"} → {t.destination || "?"}</b><p>{t.start_date || t.timestamp || ""}{t.end_date ? ` 至 ${t.end_date}` : ""} · {t.purpose || "出差"}</p><small>行程记录</small></div>
          </div>)}
        </div>
      </section>
    </div>}
  </div>;
}
