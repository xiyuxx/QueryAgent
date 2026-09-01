import { useCallback, useEffect, useState } from "react";
import { HashRouter, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import {
  BarChartOutlined,
  LeftOutlined,
  RightOutlined,
  CloseOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  MessageOutlined,
  PlusOutlined,
  ReloadOutlined,
  SendOutlined,
  SettingOutlined,
  TableOutlined,
} from "@ant-design/icons";

type Provider = { name: string; model: string; base_url: string; configured: boolean };
type Role = { name: string; allowed_tables: string[] | null };
type Stage = {
  stage?: string;
  status?: string;
  intent?: string;
  tables?: string[];
  rows?: number;
  error?: string;
  provider?: string;
  model?: string;
};
type Result = {
  status: string;
  question: string;
  intent: string;
  role: string;
  answer?: string | null;
  sql?: string | null;
  columns?: string[] | null;
  rows?: unknown[][] | null;
  provider?: string | null;
  model?: string | null;
  summary_fallback?: boolean;
  total_tokens?: number;
  total_cost_usd?: number;
  steps?: number;
  corrections?: number;
  error?: string | null;
};
type Turn = {
  id: string;
  role: string;
  question: string;
  answer: string;
  sql?: string;
  result?: Result;
  stages?: Stage[];
  createdAt: string;
};
type Session = { id: string; title: string; turns: Turn[]; updatedAt: string };

const STORAGE_KEY = "queryagent.sessions.v1";
const ROLE_KEY = "queryagent.role";
const PROVIDER_KEY = "queryagent.provider";
const SESSION_KEY = "queryagent.activeSession";
const API_URL_KEY = "queryagent.apiUrl";

function uid() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
function blankSession(): Session {
  return { id: uid(), title: "新会话", turns: [], updatedAt: new Date().toISOString() };
}
function loadSessions(): Session[] {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]") as Session[];
  } catch {
    return [];
  }
}
function saveSessions(sessions: Session[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
}

function apiUrl(path: string) {
  const configured = localStorage.getItem(API_URL_KEY)?.trim();
  return configured ? `${configured.replace(/\/$/, "")}${path}` : path;
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(url), init);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail?.message || body.detail || message;
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new Error(String(message));
  }
  return response.json() as Promise<T>;
}

function Shell({
  children,
  role,
  provider,
  roles,
  providers,
  onRole,
  onProvider,
}: {
  children: React.ReactNode;
  role: string;
  provider: string;
  roles: Role[];
  providers: Provider[];
  onRole: (value: string) => void;
  onProvider: (value: string) => void;
}) {
  const navigate = useNavigate();
  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => navigate("/")} title="返回查询工作台">
          <span className="brand-mark"><BarChartOutlined /></span>
          <span>QueryAgent</span>
        </button>
        <nav className="main-nav">
          <NavLink className={({ isActive }) => isActive ? "nav-link active" : "nav-link"} to="/"><MessageOutlined />查询</NavLink>
          <NavLink className={({ isActive }) => isActive ? "nav-link active" : "nav-link"} to="/data"><TableOutlined />数据</NavLink>
          <NavLink className={({ isActive }) => isActive ? "nav-link active" : "nav-link"} to="/console"><DashboardOutlined />评测</NavLink>
        </nav>
        <div className="top-controls">
          <label className="api-url-control">后端
            <input aria-label="后端地址" defaultValue={localStorage.getItem(API_URL_KEY) || ""} placeholder="本机默认" onBlur={(event) => { const value = event.target.value.trim(); if (value) localStorage.setItem(API_URL_KEY, value); else localStorage.removeItem(API_URL_KEY); window.location.reload(); }} />
          </label>
          <label>模型
            <select value={provider} onChange={(event) => onProvider(event.target.value)} disabled={!providers.length}>
              <option value="">未配置</option>
              {providers.map((item) => <option key={item.name} value={item.name}>{item.name} · {item.model}</option>)}
            </select>
          </label>
          <label>角色
            <select value={role} onChange={(event) => onRole(event.target.value)}>
              {roles.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
            </select>
          </label>
        </div>
        <div className={`connection-dot ${providers.length ? "online" : "offline"}`} title={providers.length ? "模型已配置" : "请配置模型 API Key"} />
      </header>
      <main className="page">{children}</main>
    </div>
  );
}

function SessionsSidebar({
  sessions,
  activeId,
  onSelect,
  onNew,
  onDelete,
}: {
  sessions: Session[];
  activeId: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}) {
  return (
    <aside className="session-sidebar">
      <div className="sidebar-heading"><span>会话</span><button className="icon-button" onClick={onNew} title="新建会话"><PlusOutlined /></button></div>
      <div className="session-list">
        {sessions.map((session) => (
          <div key={session.id} className={session.id === activeId ? "session-row selected" : "session-row"}>
            <button className="session-select" onClick={() => onSelect(session.id)}>
              <strong>{session.title}</strong><span>{session.turns.length} 条查询</span>
            </button>
            <button className="row-delete" onClick={() => onDelete(session.id)} title="删除会话"><DeleteOutlined /></button>
          </div>
        ))}
      </div>
      <div className="sidebar-footer">本地会话 · 浏览器存储</div>
    </aside>
  );
}

const stageLabels: Record<string, string> = {
  route: "识别意图", schema: "读取 Schema", generate: "生成 SQL", execute: "MCP 执行",
  validate: "结果校验", correct: "自动纠正", audit: "语义复核", summary: "生成总结",
  answer: "生成回答", model: "模型响应",
};
function StageTimeline({ stages }: { stages: Stage[] }) {
  return <div className="stage-list">{stages.map((stage, index) => <div className={`stage-item ${stage.status === "error" ? "stage-error" : ""}`} key={`${stage.stage}-${index}`}>
    <span className={`stage-marker ${stage.status === "done" ? "done" : stage.status === "error" ? "error" : "running"}`} />
    <div><strong>{stageLabels[stage.stage || ""] || stage.stage || "处理中"}</strong><span>{stage.status === "fallback" ? "已降级" : stage.status === "error" ? stage.error || "执行失败" : stage.status === "running" ? "处理中" : stage.intent || (stage.provider ? `${stage.provider} · ${stage.model || ""}` : stage.rows !== undefined ? `${stage.rows} 行` : "完成")}</span></div>
  </div>)}</div>;
}

function ResultPanel({ turn }: { turn: Turn }) {
  const [tab, setTab] = useState<"result" | "sql" | "metrics">("result");
  const result = turn.result;
  return <section className="result-panel">
    <div className="panel-tabs">
      <button className={tab === "result" ? "tab active" : "tab"} onClick={() => setTab("result")}>结果 {result?.rows ? `(${result.rows.length})` : ""}</button>
      <button className={tab === "sql" ? "tab active" : "tab"} onClick={() => setTab("sql")}>SQL</button>
      <button className={tab === "metrics" ? "tab active" : "tab"} onClick={() => setTab("metrics")}>指标</button>
    </div>
    {tab === "result" && <div className="result-content">
      {result?.answer && <div className="answer-box">{result.answer}{result.summary_fallback && <span className="fallback-note"> · 固定模板</span>}</div>}
      {result?.error && <div className="error-box">{result.error}</div>}
      {result?.rows && result.rows.length > 0 ? <div className="table-wrap"><table><thead><tr>{(result.columns || []).map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((value, cellIndex) => <td key={cellIndex}>{String(value ?? "")}</td>)}</tr>)}</tbody></table></div> : result?.status === "done" && !result.answer ? <div className="empty-result">查询完成，没有返回行。</div> : null}
    </div>}
    {tab === "sql" && <div className="code-view"><div className="code-header"><span>只读查询</span>{result?.sql && <button className="icon-button" title="复制 SQL" onClick={() => navigator.clipboard?.writeText(result.sql || "")}><DownloadOutlined /></button>}</div><pre>{result?.sql || "此请求未生成 SQL。"}</pre></div>}
    {tab === "metrics" && <div className="metrics-grid"><Metric label="Provider" value={result?.provider || "-"} /><Metric label="模型" value={result?.model || "-"} /><Metric label="Token" value={String(result?.total_tokens ?? 0)} /><Metric label="成本" value={`$${(result?.total_cost_usd ?? 0).toFixed(5)}`} /><Metric label="步骤" value={String(result?.steps ?? 0)} /><Metric label="纠正" value={String(result?.corrections ?? 0)} /></div>}
  </section>;
}
function Metric({ label, value }: { label: string; value: string }) { return <div className="metric"><span>{label}</span><strong>{value}</strong></div>; }

function QueryPage({ role, provider, sessions, activeId, onSessions, onSelectSession }: { role: string; provider: string; sessions: Session[]; activeId: string; onSessions: (sessions: Session[]) => void; onSelectSession: (id: string) => void }) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const active = sessions.find((session) => session.id === activeId) || sessions[0];
  const turns = active?.turns || [];
  const updateTurn = (nextTurn: Turn) => onSessions(sessions.map((session) => session.id === active?.id ? { ...session, turns: session.turns.map((item) => item.id === nextTurn.id ? nextTurn : item), updatedAt: new Date().toISOString() } : session));
  const send = async () => {
    const text = question.trim();
    if (!text || busy || !provider || !active) return;
    setQuestion(""); setBusy(true);
    const turn: Turn = { id: uid(), role, question: text, answer: "", stages: [], createdAt: new Date().toISOString() };
    const history = turns.map((item) => ({ role: item.role, question: item.question, answer: item.answer, sql: item.sql, result_summary: item.result?.answer || "" }));
    onSessions(sessions.map((session) => session.id === active.id ? { ...session, title: session.turns.length ? session.title : text.slice(0, 24), turns: [...session.turns, turn], updatedAt: new Date().toISOString() } : session));
    try {
      const response = await fetch(apiUrl("/api/chat/stream"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: text, role, provider, history }) });
      if (!response.ok || !response.body) throw new Error(`查询请求失败 (${response.status})`);
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let answer = "";
      const apply = (event: string, data: any) => {
        if (event === "stage") turn.stages = [...(turn.stages || []), data];
        if (event === "token") { answer += data.text || ""; turn.answer = answer; }
        if (event === "result") { turn.result = data.result; turn.answer = data.result.answer || answer; turn.sql = data.result.sql || ""; }
        if (event === "error") turn.result = { ...(turn.result || {}), status: "failed", question: text, intent: "query", role, error: data.error || data.message } as Result;
        updateTurn({ ...turn });
      };
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        buffer += decoder.decode(value, { stream: true }); const blocks = buffer.split("\n\n"); buffer = blocks.pop() || "";
        for (const block of blocks) { const eventLine = block.split("\n").find((line) => line.startsWith("event:")); const dataLine = block.split("\n").find((line) => line.startsWith("data:")); if (eventLine && dataLine) apply(eventLine.slice(6).trim(), JSON.parse(dataLine.slice(5).trim())); }
      }
    } catch (error) {
      turn.result = { status: "failed", question: text, intent: "query", role, error: error instanceof Error ? error.message : "查询失败" };
      updateTurn({ ...turn });
    }
    setBusy(false);
  };
  return <div className="workspace">
    <SessionsSidebar sessions={sessions} activeId={active?.id || ""} onSelect={onSelectSession} onNew={() => onSessions([...sessions, blankSession()])} onDelete={(id) => { const remaining = sessions.filter((session) => session.id !== id); onSessions(remaining.length ? remaining : [blankSession()]); }} />
    <section className="chat-column"><div className="workspace-heading"><div><span className="section-kicker">数据分析工作台</span><h1>{active?.title || "新会话"}</h1></div><span className="role-chip"><SettingOutlined /> {role}</span></div>
      <div className="messages">{!turns.length && <div className="empty-chat"><div className="empty-icon"><DatabaseOutlined /></div><h2>从一个数据问题开始</h2><p>查询业务数据、了解表结构，或直接和助手对话。</p><div className="quick-prompts"><button onClick={() => setQuestion("统计各城市的客户数量")}>统计各城市的客户数量</button><button onClick={() => setQuestion("有哪些可用的数据表？")}>有哪些可用的数据表？</button><button onClick={() => setQuestion("你好，介绍一下你自己")}>你好，介绍一下你自己</button></div></div>}
        {turns.map((turn) => <div className="turn" key={turn.id}><div className="user-message"><span className="avatar user-avatar">{role.slice(0, 1).toUpperCase()}</span><div><span className="message-meta">{role} · {new Date(turn.createdAt).toLocaleTimeString()}</span><p>{turn.question}</p></div></div>{turn.stages && turn.stages.length > 0 && <StageTimeline stages={turn.stages} />}{turn.result && <ResultPanel turn={turn} />}</div>)}
        {busy && <div className="thinking"><span className="avatar assistant-avatar"><BarChartOutlined /></span><span className="typing"><i /><i /><i /></span><span>正在分析</span></div>}
      </div>
      <div className="composer"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={provider ? "输入问题，按 Enter 发送" : "请先在右上角配置 Provider"} disabled={busy || !provider} rows={2} /><button className="send-button" onClick={() => void send()} disabled={busy || !question.trim() || !provider} title="发送查询"><SendOutlined /></button></div>
    </section>
  </div>;
}

function DataPage({ role }: { role: string }) {
  const [tables, setTables] = useState<any[]>([]); const [selected, setSelected] = useState(""); const [data, setData] = useState<any>(null); const [search, setSearch] = useState(""); const [page, setPage] = useState(1); const [loading, setLoading] = useState(false); const [error, setError] = useState("");
  const loadTables = useCallback(async () => { try { const body = await jsonFetch<any>(`/api/data/tables?role=${encodeURIComponent(role)}`); setTables(body.tables || []); if (!selected && body.tables?.length) setSelected(body.tables[0].name); } catch (reason) { setError(reason instanceof Error ? reason.message : "表目录加载失败"); } }, [role, selected]);
  const loadData = useCallback(async () => { if (!selected) return; setLoading(true); try { const body = await jsonFetch<any>(`/api/data/table/${encodeURIComponent(selected)}?role=${encodeURIComponent(role)}&page=${page}&page_size=50&search=${encodeURIComponent(search)}`); setData(body); setError(""); } catch (reason) { setError(reason instanceof Error ? reason.message : "数据加载失败"); } finally { setLoading(false); } }, [role, selected, page, search]);
  useEffect(() => { void loadTables(); }, [loadTables]); useEffect(() => { void loadData(); }, [loadData]);
  const exportCsv = async () => { if (!selected) return; const response = await fetch(apiUrl(`/api/data/table/${encodeURIComponent(selected)}/csv?role=${encodeURIComponent(role)}&page=${page}&page_size=50`)); if (!response.ok) return; const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `${selected}.csv`; link.click(); URL.revokeObjectURL(url); };
  return <div className="data-layout"><aside className="data-sidebar"><div className="sidebar-heading"><span>数据表</span><button className="icon-button" onClick={() => void loadTables()} title="刷新表目录"><ReloadOutlined /></button></div>{tables.map((table) => <button key={table.name} className={selected === table.name ? "table-link selected" : "table-link"} onClick={() => { setSelected(table.name); setPage(1); }}>{table.name}<span>{table.row_count ?? "-"}</span></button>)}</aside><section className="data-content"><div className="workspace-heading"><div><span className="section-kicker">数据浏览</span><h1>{selected || "选择一张表"}</h1></div><div className="toolbar"><div className="search-input"><input value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} placeholder="搜索整张表" /><CloseOutlined onClick={() => setSearch("")} /></div><button className="secondary-button" onClick={() => void exportCsv()} disabled={!selected}><DownloadOutlined />导出 CSV</button></div></div>{error && <div className="error-box">{error}</div>}{data && <><div className="data-summary">第 {data.page} / {data.total_pages || 1} 页 · 共 {data.total_rows} 行{loading && " · 加载中"}</div><div className="table-wrap large-table"><table><thead><tr>{(data.columns || []).map((column: string) => <th key={column}>{column}</th>)}</tr></thead><tbody>{(data.rows || []).map((row: any[], rowIndex: number) => <tr key={rowIndex}>{row.map((value, cellIndex) => <td key={cellIndex}>{String(value ?? "")}</td>)}</tr>)}</tbody></table>{!data.rows?.length && <div className="empty-result">没有匹配数据。</div>}</div><div className="pagination"><button className="icon-button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)} title="上一页"><LeftOutlined /></button><span>{page}</span><button className="icon-button" disabled={page >= (data.total_pages || 1)} onClick={() => setPage((value) => value + 1)} title="下一页"><RightOutlined /></button></div></>}</section></div>;
}

function ConsolePage({ provider }: { provider: string }) {
  const [dataset, setDataset] = useState("mini"); const [run, setRun] = useState<any>(null); const [busy, setBusy] = useState(false);
  const start = async () => { setBusy(true); try { const created = await jsonFetch<any>("/api/evaluations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dataset, provider }) }); setRun(created); const timer = window.setInterval(async () => { const current = await jsonFetch<any>(`/api/evaluations/${created.run_id}`); setRun(current); if (["completed", "failed"].includes(current.status)) { window.clearInterval(timer); setBusy(false); } }, 800); } catch (error) { setBusy(false); setRun({ status: "failed", error: error instanceof Error ? error.message : "评测启动失败" }); } };
  return <div className="console-page"><div className="workspace-heading"><div><span className="section-kicker">可靠性验证</span><h1>评测控制台</h1></div><div className="toolbar"><select value={dataset} onChange={(event) => setDataset(event.target.value)}><option value="mini">mini · 快速</option><option value="warehouse">warehouse · 完整</option></select><button className="primary-button" onClick={() => void start()} disabled={busy || !provider}><ReloadOutlined className={busy ? "spin" : ""} />{busy ? "运行中" : "开始评测"}</button></div></div>{!run && <div className="console-empty"><BarChartOutlined /><p>选择数据集后运行实时评测。</p><span>评测固定使用 admin 角色，结果只保留在当前后端进程。</span></div>}{run && <><div className="eval-overview"><Metric label="状态" value={run.status === "completed" ? "已完成" : run.status === "running" ? "运行中" : run.status === "queued" ? "排队中" : "失败"} /><Metric label="执行准确率" value={run.summary ? `${(run.summary.exec_accuracy * 100).toFixed(1)}%` : "-"} /><Metric label="Exact Match" value={run.summary ? `${(run.summary.exact_match_rate * 100).toFixed(1)}%` : "-"} /><Metric label="平均延迟" value={run.summary ? `${run.summary.avg_latency_ms.toFixed(0)} ms` : "-"} /></div>{run.error && <div className="error-box">{run.error}</div>}<div className="table-wrap eval-table"><table><thead><tr><th>ID</th><th>状态</th><th>执行</th><th>Exact</th><th>步骤</th><th>纠正</th><th>SQL</th><th>Gold SQL</th></tr></thead><tbody>{(run.cases || []).map((item: any) => <tr key={item.case_id}><td>{item.case_id}</td><td>{item.status}</td><td>{item.exec_match == null ? "-" : item.exec_match ? "通过" : "失败"}</td><td>{item.exact_match ? "通过" : "-"}</td><td>{item.steps}</td><td>{item.corrections}</td><td><code>{item.sql}</code></td><td><code>{item.gold_sql}</code></td></tr>)}</tbody></table></div></>}</div>;
}

function App() {
  const [providers, setProviders] = useState<Provider[]>([]); const [roles, setRoles] = useState<Role[]>([]); const [role, setRole] = useState(localStorage.getItem(ROLE_KEY) || "readonly"); const [provider, setProvider] = useState(localStorage.getItem(PROVIDER_KEY) || ""); const [sessions, setSessions] = useState<Session[]>(() => { const loaded = loadSessions(); return loaded.length ? loaded : [blankSession()]; }); const [activeId, setActiveId] = useState(() => { const saved = localStorage.getItem(SESSION_KEY); const loaded = loadSessions(); return saved && loaded.some((session) => session.id === saved) ? saved : ""; });
  useEffect(() => { void Promise.all([jsonFetch<any>("/api/config/providers"), jsonFetch<any>("/api/roles")]).then(([providerConfig, roleConfig]) => { setProviders(providerConfig.providers || []); setRoles(roleConfig.roles || []); if (!provider && providerConfig.default_provider) setProvider(providerConfig.default_provider); if (!role && roleConfig.default_role) setRole(roleConfig.default_role); }).catch(() => undefined); }, []);
  useEffect(() => { if (!sessions.find((session) => session.id === activeId)) setActiveId(sessions[0]?.id || ""); saveSessions(sessions); }, [sessions, activeId]);
  useEffect(() => { if (activeId) localStorage.setItem(SESSION_KEY, activeId); }, [activeId]); useEffect(() => { localStorage.setItem(ROLE_KEY, role); }, [role]); useEffect(() => { localStorage.setItem(PROVIDER_KEY, provider); }, [provider]);
  const updateSessions = (next: Session[]) => { setSessions(next); if (!next.find((session) => session.id === activeId)) setActiveId(next[0]?.id || ""); };
  return <HashRouter><Shell role={role} provider={provider} roles={roles} providers={providers} onRole={setRole} onProvider={setProvider}><Routes><Route path="/" element={<QueryPage role={role} provider={provider} sessions={sessions} activeId={activeId || sessions[0]?.id || ""} onSessions={updateSessions} onSelectSession={setActiveId} />} /><Route path="/data" element={<DataPage role={role} />} /><Route path="/console" element={<ConsolePage provider={provider} />} /></Routes></Shell></HashRouter>;
}

export { App };
export default App;
