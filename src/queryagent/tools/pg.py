"""PostgreSQL 执行器：只读会话 + 超时 + 行数上限 + EXPLAIN 成本估算。

只读双重防护：进程层 check_sql（只放行 SELECT）+ 会话层 default_transaction_read_only。
connect() 带重试：本机防火墙会间歇性拦截 127.0.0.1，需容错。
"""
from __future__ import annotations

import time

import psycopg

from .db import QueryResult, check_sql


def connect(dsn: str, *, autocommit: bool = True, retries: int = 6, delay: float = 1.0, **kwargs):
    """带重试的 PG 连接（应对 Windows 防火墙间歇性拦截 127.0.0.1）。"""
    last = None
    for i in range(retries):
        try:
            return psycopg.connect(dsn, autocommit=autocommit, **kwargs)
        except psycopg.OperationalError as e:
            last = e
            if i < retries - 1:
                time.sleep(delay * (i + 1))
    raise last


def estimate_cost(conn, sql: str) -> float:
    """用 EXPLAIN 估算查询代价（Total Cost），用于拦全表扫描/高代价查询。"""
    try:
        cur = conn.execute("EXPLAIN (FORMAT JSON) " + sql)
        plans = cur.fetchone()[0]
        plan = plans[0]
        return float(plan.get("Total Cost", plan.get("Plan", {}).get("Total Cost", 0.0)))
    except Exception:
        return float("inf")


class PgExecutor:
    def __init__(
        self,
        dsn: str,
        *,
        max_rows: int = 100,
        timeout_s: float = 10.0,
        max_cost: float | None = None,
    ) -> None:
        self.dsn = dsn
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self.max_cost = max_cost

    def close(self) -> None:
        """PgExecutor creates one connection per call and requires no cleanup."""

    def execute(self, sql: str) -> QueryResult:
        guard = check_sql(sql, dialect="postgres")
        if guard is not None:
            return QueryResult(error=guard)

        try:
            conn = connect(self.dsn, connect_timeout=int(self.timeout_s))
            try:
                conn.execute("SET default_transaction_read_only = on")
                if self.max_cost is not None:
                    cost = estimate_cost(conn, sql)
                    if cost > self.max_cost:
                        return QueryResult(error=f"query too expensive (estimated cost {cost:.1f} > {self.max_cost})")
                cur = conn.execute(sql)
                columns = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchmany(self.max_rows + 1)
                truncated = len(rows) > self.max_rows
                if truncated:
                    rows = rows[: self.max_rows]
                return QueryResult(columns=columns, rows=[tuple(r) for r in rows], truncated=truncated)
            finally:
                conn.close()
        except psycopg.Error as e:
            return QueryResult(error=f"{type(e).__name__}: {e}")
