import { useEffect, useState } from "react";

type Health = {
  status: string;
  service: string;
  version: string;
  time: string;
};

async function loadHealth(): Promise<Health> {
  const response = await fetch("/api/health");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<Health>;
}

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadHealth().then(setHealth).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "后端暂不可用");
    });
  }, []);

  return (
    <main className="app-shell">
      <header className="topbar">
        <strong>QueryAgent</strong>
        <span className="phase-badge">本地 Web Demo · Phase 0</span>
      </header>
      <section className="welcome-card">
        <p className="eyebrow">工程基础已就绪</p>
        <h1>Text-to-SQL 数据分析 Agent</h1>
        <p className="description">
          查询工作台、数据浏览和评测控制台将在后续阶段接入。当前页面用于验证 React、Nginx
          与 FastAPI 的连通性。
        </p>
        <div className={`status ${health ? "status-ok" : "status-waiting"}`}>
          <span className="status-dot" />
          {health ? `后端已连接 · ${health.service} ${health.version}` : "正在检查后端连接..."}
        </div>
        {error && <p className="error">后端连接失败：{error}</p>}
      </section>
    </main>
  );
}
