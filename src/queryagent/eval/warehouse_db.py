"""构造多领域大库（17 张表，4 个领域），用于压出 schema 相关性选择的收益。

每个表/列带中文描述，作为 embedding 语义检索的语料（模拟真实数据目录）。
数据确定性生成，保证评测数字可复现。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ColumnSpec:
    name: str
    type: str
    description: str


@dataclass
class TableSpec:
    name: str
    description: str
    columns: list[ColumnSpec] = field(default_factory=list)


def _t(name: str, description: str, columns: list[tuple[str, str, str]]) -> TableSpec:
    return TableSpec(name, description, [ColumnSpec(n, ty, d) for n, ty, d in columns])


WAREHOUSE_TABLES: list[TableSpec] = [
    # ---- 电商 ----
    _t("customers", "电商平台的注册客户，含姓名、所在城市与注册日期", [
        ("customer_id", "INTEGER PRIMARY KEY", "客户唯一标识"),
        ("name", "TEXT", "客户姓名"),
        ("city", "TEXT", "所在城市"),
        ("register_date", "TEXT", "注册日期"),
    ]),
    _t("products", "电商平台在售商品，含价格、品类与品牌", [
        ("product_id", "INTEGER PRIMARY KEY", "商品唯一标识"),
        ("name", "TEXT", "商品名称"),
        ("category", "TEXT", "商品品类"),
        ("price", "REAL", "单价"),
        ("brand", "TEXT", "品牌"),
    ]),
    _t("orders", "客户下单记录，含下单日期、订单状态与总金额", [
        ("order_id", "INTEGER PRIMARY KEY", "订单唯一标识"),
        ("customer_id", "INTEGER", "下单客户标识"),
        ("order_date", "TEXT", "下单日期"),
        ("status", "TEXT", "订单状态"),
        ("total_amount", "REAL", "订单总金额"),
    ]),
    _t("order_items", "订单中每个商品的明细行，含数量与单价", [
        ("item_id", "INTEGER PRIMARY KEY", "明细唯一标识"),
        ("order_id", "INTEGER", "所属订单标识"),
        ("product_id", "INTEGER", "商品标识"),
        ("quantity", "INTEGER", "购买数量"),
        ("unit_price", "REAL", "成交单价"),
    ]),
    _t("reviews", "客户对商品的评分与评价", [
        ("review_id", "INTEGER PRIMARY KEY", "评价唯一标识"),
        ("product_id", "INTEGER", "被评价商品标识"),
        ("customer_id", "INTEGER", "评价客户标识"),
        ("rating", "INTEGER", "评分 1-5"),
        ("review_date", "TEXT", "评价日期"),
    ]),
    # ---- 金融 ----
    _t("accounts", "银行账户信息，含账户类型与当前余额", [
        ("account_id", "INTEGER PRIMARY KEY", "账户唯一标识"),
        ("customer_id", "INTEGER", "账户持有人客户标识"),
        ("account_type", "TEXT", "账户类型（储蓄/信用）"),
        ("balance", "REAL", "当前余额"),
        ("open_date", "TEXT", "开户日期"),
    ]),
    _t("transactions", "账户交易流水，含金额、日期与交易类型", [
        ("transaction_id", "INTEGER PRIMARY KEY", "流水唯一标识"),
        ("account_id", "INTEGER", "所属账户标识"),
        ("amount", "REAL", "交易金额（正收入负支出）"),
        ("txn_date", "TEXT", "交易日期"),
        ("txn_type", "TEXT", "交易类型（收入/支出）"),
    ]),
    _t("loans", "贷款记录，含金额、利率、到期日与还款状态", [
        ("loan_id", "INTEGER PRIMARY KEY", "贷款唯一标识"),
        ("customer_id", "INTEGER", "借款人客户标识"),
        ("amount", "REAL", "贷款金额"),
        ("interest_rate", "REAL", "年利率"),
        ("due_date", "TEXT", "到期日"),
        ("status", "TEXT", "还款状态"),
    ]),
    _t("credit_cards", "信用卡信息，含额度、欠款余额与卡类型", [
        ("card_id", "INTEGER PRIMARY KEY", "卡唯一标识"),
        ("customer_id", "INTEGER", "持卡人客户标识"),
        ("credit_limit", "REAL", "信用额度"),
        ("outstanding_balance", "REAL", "欠款余额"),
        ("card_type", "TEXT", "卡类型（金卡/白金卡/普卡）"),
    ]),
    # ---- 人力资源 ----
    _t("employees", "公司员工信息，含所属部门、入职日期与职位", [
        ("employee_id", "INTEGER PRIMARY KEY", "员工唯一标识"),
        ("name", "TEXT", "员工姓名"),
        ("department_id", "INTEGER", "所属部门标识"),
        ("hire_date", "TEXT", "入职日期"),
        ("title", "TEXT", "职位"),
    ]),
    _t("departments", "公司部门信息，含部门名称、地点与负责人", [
        ("department_id", "INTEGER PRIMARY KEY", "部门唯一标识"),
        ("name", "TEXT", "部门名称"),
        ("location", "TEXT", "办公地点"),
        ("manager_id", "INTEGER", "负责人员工标识"),
    ]),
    _t("salaries", "员工薪资记录，含基本工资、奖金与发放月份", [
        ("salary_id", "INTEGER PRIMARY KEY", "薪资记录唯一标识"),
        ("employee_id", "INTEGER", "员工标识"),
        ("base_salary", "REAL", "基本工资"),
        ("bonus", "REAL", "奖金"),
        ("pay_month", "TEXT", "发放月份"),
    ]),
    _t("attendance", "员工考勤记录，含出勤日期与迟到/缺勤状态", [
        ("attendance_id", "INTEGER PRIMARY KEY", "考勤记录唯一标识"),
        ("employee_id", "INTEGER", "员工标识"),
        ("work_date", "TEXT", "出勤日期"),
        ("check_in", "TEXT", "打卡时间"),
        ("status", "TEXT", "出勤状态（正常/迟到/缺勤）"),
    ]),
    # ---- 物流 ----
    _t("shipments", "货物运输单，含仓库、承运商、状态与重量", [
        ("shipment_id", "INTEGER PRIMARY KEY", "运输单唯一标识"),
        ("warehouse_id", "INTEGER", "发货仓库标识"),
        ("carrier_id", "INTEGER", "承运商标识"),
        ("status", "TEXT", "运输状态"),
        ("ship_date", "TEXT", "发货日期"),
        ("weight", "REAL", "货物重量（kg）"),
    ]),
    _t("warehouses", "仓库信息，含位置、容量与当前库存量", [
        ("warehouse_id", "INTEGER PRIMARY KEY", "仓库唯一标识"),
        ("name", "TEXT", "仓库名称"),
        ("city", "TEXT", "所在城市"),
        ("capacity", "REAL", "容量（吨）"),
        ("stock_quantity", "INTEGER", "当前库存量"),
    ]),
    _t("carriers", "承运商信息，含联系方式与车辆数量", [
        ("carrier_id", "INTEGER PRIMARY KEY", "承运商唯一标识"),
        ("name", "TEXT", "承运商名称"),
        ("contact", "TEXT", "联系电话"),
        ("vehicle_count", "INTEGER", "车辆数量"),
    ]),
    _t("delivery_routes", "配送路线，含起点、终点与里程", [
        ("route_id", "INTEGER PRIMARY KEY", "路线唯一标识"),
        ("origin", "TEXT", "起点城市"),
        ("destination", "TEXT", "终点城市"),
        ("distance_km", "REAL", "里程（公里）"),
    ]),
]


WAREHOUSE_ROWS: dict[str, list[tuple]] = {
    "customers": [
        (1, "张伟", "北京", "2023-01-10"),
        (2, "李娜", "上海", "2023-02-14"),
        (3, "王强", "深圳", "2023-03-20"),
        (4, "刘洋", "北京", "2023-04-05"),
        (5, "陈静", "上海", "2023-05-18"),
    ],
    "products": [
        (1, "手机", "电子", 3999.0, "华为"),
        (2, "笔记本", "电子", 8999.0, "联想"),
        (3, "T恤", "服饰", 99.0, "优衣库"),
        (4, "咖啡杯", "家居", 59.0, "无品牌"),
        (5, "耳机", "电子", 499.0, "索尼"),
    ],
    "orders": [
        (1, 1, "2024-06-01", "已完成", 4098.0),
        (2, 2, "2024-06-05", "已完成", 8999.0),
        (3, 1, "2024-06-12", "已完成", 297.0),
        (4, 3, "2024-07-01", "已完成", 998.0),
        (5, 4, "2024-07-10", "已发货", 3999.0),
        (6, 5, "2024-07-15", "已完成", 295.0),
    ],
    "order_items": [
        (1, 1, 1, 1, 3999.0),
        (2, 1, 3, 1, 99.0),
        (3, 2, 2, 1, 8999.0),
        (4, 3, 3, 3, 99.0),
        (5, 4, 5, 2, 499.0),
        (6, 5, 1, 1, 3999.0),
        (7, 6, 4, 5, 59.0),
    ],
    "reviews": [
        (1, 1, 1, 5, "2024-06-03"),
        (2, 2, 2, 4, "2024-06-06"),
        (3, 3, 1, 3, "2024-06-14"),
        (4, 4, 3, 2, "2024-07-02"),
        (5, 5, 4, 5, "2024-07-11"),
    ],
    "accounts": [
        (1, 1, "储蓄", 50000.0, "2023-01-11"),
        (2, 2, "储蓄", 120000.0, "2023-02-15"),
        (3, 3, "信用", 8000.0, "2023-03-21"),
        (4, 4, "储蓄", 30000.0, "2023-04-06"),
        (5, 5, "储蓄", 65000.0, "2023-05-19"),
    ],
    "transactions": [
        (1, 1, 5000.0, "2024-06-10", "收入"),
        (2, 1, -2000.0, "2024-06-15", "支出"),
        (3, 2, 10000.0, "2024-06-20", "收入"),
        (4, 3, -1500.0, "2024-06-25", "支出"),
        (5, 4, 3000.0, "2024-07-01", "收入"),
        (6, 5, -800.0, "2024-07-05", "支出"),
    ],
    "loans": [
        (1, 1, 100000.0, 0.045, "2025-01-10", "未还清"),
        (2, 2, 50000.0, 0.038, "2024-12-20", "已还清"),
        (3, 3, 200000.0, 0.052, "2025-03-15", "未还清"),
        (4, 4, 80000.0, 0.041, "2025-02-01", "未还清"),
        (5, 5, 150000.0, 0.048, "2024-11-30", "已还清"),
    ],
    "credit_cards": [
        (1, 1, 50000.0, 12000.0, "金卡"),
        (2, 2, 80000.0, 5000.0, "白金卡"),
        (3, 3, 30000.0, 20000.0, "普卡"),
        (4, 4, 60000.0, 0.0, "金卡"),
        (5, 5, 100000.0, 30000.0, "白金卡"),
    ],
    "employees": [
        (1, "赵云", 1, "2022-01-10", "工程师"),
        (2, "孙尚香", 1, "2022-03-15", "工程师"),
        (3, "关羽", 2, "2021-06-01", "经理"),
        (4, "张飞", 2, "2022-08-20", "销售"),
        (5, "黄忠", 3, "2023-02-14", "分析师"),
        (6, "马超", 3, "2023-05-30", "分析师"),
    ],
    "departments": [
        (1, "研发部", "北京", 1),
        (2, "销售部", "上海", 3),
        (3, "财务部", "深圳", 5),
    ],
    "salaries": [
        (1, 1, 18000.0, 3000.0, "2024-06"),
        (2, 2, 16000.0, 2000.0, "2024-06"),
        (3, 3, 25000.0, 5000.0, "2024-06"),
        (4, 4, 12000.0, 1000.0, "2024-06"),
        (5, 5, 15000.0, 1500.0, "2024-06"),
        (6, 6, 14000.0, 1200.0, "2024-06"),
    ],
    "attendance": [
        (1, 1, "2024-06-01", "08:30", "正常"),
        (2, 1, "2024-06-02", "09:15", "迟到"),
        (3, 1, "2024-06-03", "09:30", "迟到"),
        (4, 2, "2024-06-01", "08:45", "正常"),
        (5, 2, "2024-06-03", "10:00", "迟到"),
        (6, 3, "2024-06-01", "08:20", "正常"),
        (7, 5, "2024-06-05", "09:30", "迟到"),
    ],
    "shipments": [
        (1, 1, 1, "已送达", "2024-06-10", 120.0),
        (2, 2, 1, "运输中", "2024-06-15", 300.0),
        (3, 1, 2, "已送达", "2024-06-20", 80.0),
        (4, 3, 3, "已送达", "2024-07-01", 500.0),
        (5, 2, 2, "待发货", "2024-07-05", 45.0),
    ],
    "warehouses": [
        (1, "北京仓", "北京", 1000.0, 600),
        (2, "上海仓", "上海", 2000.0, 1500),
        (3, "深圳仓", "深圳", 1500.0, 900),
    ],
    "carriers": [
        (1, "顺丰速运", "400-811-1111", 500),
        (2, "京东物流", "400-606-5500", 300),
        (3, "德邦物流", "95353", 200),
    ],
    "delivery_routes": [
        (1, "北京", "上海", 1200.0),
        (2, "北京", "深圳", 2100.0),
        (3, "上海", "深圳", 1450.0),
        (4, "上海", "杭州", 170.0),
    ],
}


def warehouse_catalog() -> dict:
    """导出 {table: {description, columns: {col: description}}} 供 embedding 检索。"""
    return {
        t.name: {
            "description": t.description,
            "columns": {c.name: c.description for c in t.columns},
        }
        for t in WAREHOUSE_TABLES
    }


def build_warehouse_db(path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    conn = sqlite3.connect(path)
    try:
        for t in WAREHOUSE_TABLES:
            cols = ", ".join(f"{c.name} {c.type}" for c in t.columns)
            conn.execute(f"CREATE TABLE {t.name} ({cols});")
        for table, rows in WAREHOUSE_ROWS.items():
            placeholders = ",".join("?" * len(rows[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        conn.commit()
    finally:
        conn.close()
    return str(path)
