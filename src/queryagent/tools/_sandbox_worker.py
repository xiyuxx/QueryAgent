"""沙箱 worker：在隔离进程/容器内执行 SQL，输出 JSON。

独立运行（只依赖 stdlib），供 SandboxExecutor 通过 subprocess 或 Docker 调用。
输入：argv[1]=db 路径, argv[2]=SQL, argv[3]=最大行数；输出：单行 JSON。
"""
from __future__ import annotations

import json
import sqlite3
import sys


def main() -> None:
    db_path = sys.argv[1]
    sql = sys.argv[2]
    max_rows = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    out: dict = {"columns": [], "rows": [], "truncated": False, "error": None}
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            cur = conn.execute(sql)
            out["columns"] = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
            out["truncated"] = len(rows) > max_rows
            out["rows"] = rows[:max_rows]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 任何执行错误都作为 JSON 错误返回
        out["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
