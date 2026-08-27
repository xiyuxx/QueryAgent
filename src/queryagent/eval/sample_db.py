"""构造评测用的示例销售库（确定性数据，保证数字可复现）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE customers (
  customer_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  city TEXT NOT NULL,
  signup_date TEXT NOT NULL
);
CREATE TABLE products (
  product_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  price REAL NOT NULL
);
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL,
  order_date TEXT NOT NULL
);
"""

_ROWS: dict[str, list[tuple]] = {
    "customers": [
        (1, "张三", "北京", "2023-01-15"),
        (2, "李四", "上海", "2023-03-22"),
        (3, "王五", "北京", "2023-05-30"),
        (4, "赵六", "深圳", "2024-01-10"),
        (5, "孙七", "上海", "2024-02-14"),
    ],
    "products": [
        (1, "手机", "电子", 3999.0),
        (2, "笔记本", "电子", 8999.0),
        (3, "T恤", "服饰", 99.0),
        (4, "咖啡杯", "家居", 59.0),
        (5, "耳机", "电子", 499.0),
    ],
    "orders": [
        (1, 1, 1, 1, "2024-06-01"),
        (2, 2, 2, 1, "2024-06-05"),
        (3, 1, 3, 3, "2024-06-12"),
        (4, 3, 5, 2, "2024-07-01"),
        (5, 4, 1, 1, "2024-07-10"),
        (6, 5, 4, 5, "2024-07-15"),
        (7, 2, 3, 1, "2024-06-20"),
        (8, 1, 2, 1, "2024-06-25"),
    ],
}


def build_sample_db(path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        for table, rows in _ROWS.items():
            placeholders = ",".join("?" * len(rows[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        conn.commit()
    finally:
        conn.close()
    return str(path)
