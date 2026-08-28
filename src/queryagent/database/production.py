"""Deterministic PostgreSQL demo-production schema and synthetic data.

The Web Demo uses this module as its single source of truth for the business
schema. It intentionally does not write a database file to the repository:
``build_production_snapshot`` creates the same data on every initialization
using a fixed seed, and the PostgreSQL initializer inserts that snapshot.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable


DEFAULT_SEED = 20260828
DEFAULT_DB_NAME = "queryagent_demo"


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    pg_type: str
    description: str
    nullable: bool = False
    primary_key: bool = False
    sensitive: bool = False


@dataclass(frozen=True)
class ForeignKeySpec:
    columns: tuple[str, ...]
    references_table: str
    references_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSpec:
    name: str
    description: str
    columns: tuple[ColumnSpec, ...]
    foreign_keys: tuple[ForeignKeySpec, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def sensitive_columns(self) -> frozenset[str]:
        return frozenset(column.name for column in self.columns if column.sensitive)


@dataclass
class ProductionSnapshot:
    """A complete, deterministic set of business rows."""

    seed: int
    tables: tuple[TableSpec, ...]
    rows: dict[str, list[tuple]]

    @property
    def row_counts(self) -> dict[str, int]:
        return {table.name: len(self.rows.get(table.name, [])) for table in self.tables}

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())

    @property
    def digest(self) -> str:
        payload = {
            "seed": self.seed,
            "rows": {name: values for name, values in sorted(self.rows.items())},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()


# The order is also the dependency-safe insertion order. departments and
# warehouses/carriers are created before the fact tables that reference them.
PRODUCTION_TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "customers",
        "电商平台注册客户，含姓名、城市、注册日期和合成联系方式",
        (
            ColumnSpec("customer_id", "BIGINT", "客户唯一标识", primary_key=True),
            ColumnSpec("name", "TEXT", "客户姓名"),
            ColumnSpec("city", "TEXT", "所在城市"),
            ColumnSpec("register_date", "DATE", "注册日期"),
            ColumnSpec("phone", "TEXT", "客户手机号（合成敏感字段）", sensitive=True),
            ColumnSpec("email", "TEXT", "客户邮箱（合成敏感字段）", sensitive=True),
        ),
    ),
    TableSpec(
        "products",
        "电商平台在售商品，含价格、品类和品牌",
        (
            ColumnSpec("product_id", "BIGINT", "商品唯一标识", primary_key=True),
            ColumnSpec("name", "TEXT", "商品名称"),
            ColumnSpec("category", "TEXT", "商品品类"),
            ColumnSpec("price", "NUMERIC(12,2)", "商品单价"),
            ColumnSpec("brand", "TEXT", "商品品牌"),
        ),
    ),
    TableSpec(
        "departments",
        "公司组织部门，含部门名称、办公地点和负责人",
        (
            ColumnSpec("department_id", "BIGINT", "部门唯一标识", primary_key=True),
            ColumnSpec("name", "TEXT", "部门名称"),
            ColumnSpec("location", "TEXT", "办公地点"),
            ColumnSpec("manager_id", "BIGINT", "负责人员工标识", nullable=True),
        ),
    ),
    TableSpec(
        "warehouses",
        "物流仓库，含位置、容量和当前库存量",
        (
            ColumnSpec("warehouse_id", "BIGINT", "仓库唯一标识", primary_key=True),
            ColumnSpec("name", "TEXT", "仓库名称"),
            ColumnSpec("city", "TEXT", "所在城市"),
            ColumnSpec("capacity", "NUMERIC(12,2)", "容量（吨）"),
            ColumnSpec("stock_quantity", "INTEGER", "当前库存量"),
        ),
    ),
    TableSpec(
        "carriers",
        "物流承运商，含联系方式和车辆数量",
        (
            ColumnSpec("carrier_id", "BIGINT", "承运商唯一标识", primary_key=True),
            ColumnSpec("name", "TEXT", "承运商名称"),
            ColumnSpec("contact", "TEXT", "联系电话"),
            ColumnSpec("vehicle_count", "INTEGER", "车辆数量"),
        ),
    ),
    TableSpec(
        "orders",
        "客户订单记录，含下单日期、状态和订单总金额",
        (
            ColumnSpec("order_id", "BIGINT", "订单唯一标识", primary_key=True),
            ColumnSpec("customer_id", "BIGINT", "下单客户标识"),
            ColumnSpec("order_date", "DATE", "下单日期"),
            ColumnSpec("status", "TEXT", "订单状态"),
            ColumnSpec("total_amount", "NUMERIC(14,2)", "订单总金额"),
        ),
        (ForeignKeySpec(("customer_id",), "customers", ("customer_id",)),),
    ),
    TableSpec(
        "order_items",
        "订单商品明细，含商品、购买数量和成交单价",
        (
            ColumnSpec("item_id", "BIGINT", "明细唯一标识", primary_key=True),
            ColumnSpec("order_id", "BIGINT", "所属订单标识"),
            ColumnSpec("product_id", "BIGINT", "商品标识"),
            ColumnSpec("quantity", "INTEGER", "购买数量"),
            ColumnSpec("unit_price", "NUMERIC(12,2)", "成交单价"),
        ),
        (
            ForeignKeySpec(("order_id",), "orders", ("order_id",)),
            ForeignKeySpec(("product_id",), "products", ("product_id",)),
        ),
    ),
    TableSpec(
        "reviews",
        "客户对商品的评分记录",
        (
            ColumnSpec("review_id", "BIGINT", "评价唯一标识", primary_key=True),
            ColumnSpec("product_id", "BIGINT", "被评价商品标识"),
            ColumnSpec("customer_id", "BIGINT", "评价客户标识"),
            ColumnSpec("rating", "INTEGER", "评分 1-5"),
            ColumnSpec("review_date", "DATE", "评价日期"),
        ),
        (
            ForeignKeySpec(("product_id",), "products", ("product_id",)),
            ForeignKeySpec(("customer_id",), "customers", ("customer_id",)),
        ),
    ),
    TableSpec(
        "accounts",
        "客户银行账户，含账户类型和当前余额",
        (
            ColumnSpec("account_id", "BIGINT", "账户唯一标识", primary_key=True),
            ColumnSpec("customer_id", "BIGINT", "账户持有人客户标识"),
            ColumnSpec("account_type", "TEXT", "账户类型"),
            ColumnSpec("balance", "NUMERIC(14,2)", "当前余额"),
            ColumnSpec("open_date", "DATE", "开户日期"),
        ),
        (ForeignKeySpec(("customer_id",), "customers", ("customer_id",)),),
    ),
    TableSpec(
        "transactions",
        "账户交易流水，含金额、日期和交易类型",
        (
            ColumnSpec("transaction_id", "BIGINT", "流水唯一标识", primary_key=True),
            ColumnSpec("account_id", "BIGINT", "所属账户标识"),
            ColumnSpec("amount", "NUMERIC(14,2)", "交易金额（正收入负支出）"),
            ColumnSpec("txn_date", "DATE", "交易日期"),
            ColumnSpec("txn_type", "TEXT", "交易类型"),
        ),
        (ForeignKeySpec(("account_id",), "accounts", ("account_id",)),),
    ),
    TableSpec(
        "loans",
        "客户贷款记录，含金额、利率、到期日和还款状态",
        (
            ColumnSpec("loan_id", "BIGINT", "贷款唯一标识", primary_key=True),
            ColumnSpec("customer_id", "BIGINT", "借款人客户标识"),
            ColumnSpec("amount", "NUMERIC(14,2)", "贷款金额"),
            ColumnSpec("interest_rate", "NUMERIC(6,4)", "年利率"),
            ColumnSpec("due_date", "DATE", "到期日"),
            ColumnSpec("status", "TEXT", "还款状态"),
        ),
        (ForeignKeySpec(("customer_id",), "customers", ("customer_id",)),),
    ),
    TableSpec(
        "credit_cards",
        "客户信用卡，含额度、欠款余额和卡类型",
        (
            ColumnSpec("card_id", "BIGINT", "信用卡唯一标识", primary_key=True),
            ColumnSpec("customer_id", "BIGINT", "持卡人客户标识"),
            ColumnSpec("credit_limit", "NUMERIC(14,2)", "信用额度"),
            ColumnSpec("outstanding_balance", "NUMERIC(14,2)", "欠款余额"),
            ColumnSpec("card_type", "TEXT", "卡类型"),
        ),
        (ForeignKeySpec(("customer_id",), "customers", ("customer_id",)),),
    ),
    TableSpec(
        "employees",
        "公司员工，含部门、入职日期、职位和合成身份证号",
        (
            ColumnSpec("employee_id", "BIGINT", "员工唯一标识", primary_key=True),
            ColumnSpec("name", "TEXT", "员工姓名"),
            ColumnSpec("department_id", "BIGINT", "所属部门标识"),
            ColumnSpec("hire_date", "DATE", "入职日期"),
            ColumnSpec("title", "TEXT", "职位"),
            ColumnSpec("national_id", "TEXT", "员工身份证号（合成敏感字段）", sensitive=True),
        ),
        (ForeignKeySpec(("department_id",), "departments", ("department_id",)),),
    ),
    TableSpec(
        "salaries",
        "员工月度薪资记录，含基本工资、奖金和发放月份",
        (
            ColumnSpec("salary_id", "BIGINT", "薪资记录唯一标识", primary_key=True),
            ColumnSpec("employee_id", "BIGINT", "员工标识"),
            ColumnSpec("base_salary", "NUMERIC(12,2)", "基本工资"),
            ColumnSpec("bonus", "NUMERIC(12,2)", "奖金"),
            ColumnSpec("pay_month", "DATE", "发放月份"),
        ),
        (ForeignKeySpec(("employee_id",), "employees", ("employee_id",)),),
    ),
    TableSpec(
        "attendance",
        "员工考勤记录，含日期、打卡时间和出勤状态",
        (
            ColumnSpec("attendance_id", "BIGINT", "考勤记录唯一标识", primary_key=True),
            ColumnSpec("employee_id", "BIGINT", "员工标识"),
            ColumnSpec("work_date", "DATE", "出勤日期"),
            ColumnSpec("check_in", "TIME", "打卡时间"),
            ColumnSpec("status", "TEXT", "出勤状态"),
        ),
        (ForeignKeySpec(("employee_id",), "employees", ("employee_id",)),),
    ),
    TableSpec(
        "shipments",
        "货物运输单，含仓库、承运商、状态、日期和重量",
        (
            ColumnSpec("shipment_id", "BIGINT", "运输单唯一标识", primary_key=True),
            ColumnSpec("warehouse_id", "BIGINT", "发货仓库标识"),
            ColumnSpec("carrier_id", "BIGINT", "承运商标识"),
            ColumnSpec("status", "TEXT", "运输状态"),
            ColumnSpec("ship_date", "DATE", "发货日期"),
            ColumnSpec("weight", "NUMERIC(12,2)", "货物重量（kg）"),
        ),
        (
            ForeignKeySpec(("warehouse_id",), "warehouses", ("warehouse_id",)),
            ForeignKeySpec(("carrier_id",), "carriers", ("carrier_id",)),
        ),
    ),
    TableSpec(
        "delivery_routes",
        "配送路线，含起点、终点和里程",
        (
            ColumnSpec("route_id", "BIGINT", "路线唯一标识", primary_key=True),
            ColumnSpec("origin", "TEXT", "起点城市"),
            ColumnSpec("destination", "TEXT", "终点城市"),
            ColumnSpec("distance_km", "NUMERIC(12,2)", "里程（公里）"),
        ),
    ),
)

BUSINESS_TABLE_NAMES = tuple(table.name for table in PRODUCTION_TABLES)
INTERNAL_TABLE_NAMES = ("schema_metadata", "value_index", "table_embeddings", "queryagent_state")


def quote_identifier(identifier: str) -> str:
    """Quote a static SQL identifier without accepting SQL syntax."""
    return '"' + identifier.replace('"', '""') + '"'


def render_create_table(table: TableSpec, *, if_not_exists: bool = True) -> str:
    prefix = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
    definitions: list[str] = []
    for column in table.columns:
        definition = f"{quote_identifier(column.name)} {column.pg_type}"
        if not column.nullable:
            definition += " NOT NULL"
        if column.primary_key:
            definition += " PRIMARY KEY"
        definitions.append(definition)
    for index, fk in enumerate(table.foreign_keys, start=1):
        local = ", ".join(quote_identifier(column) for column in fk.columns)
        remote = ", ".join(quote_identifier(column) for column in fk.references_columns)
        definitions.append(
            f"CONSTRAINT {quote_identifier(f'fk_{table.name}_{index}')} "
            f"FOREIGN KEY ({local}) REFERENCES {quote_identifier(fk.references_table)} ({remote})"
        )
    body = ",\n  ".join(definitions)
    return f"{prefix} {quote_identifier(table.name)} (\n  {body}\n);"


def _date_from_offset(start: date, offset: int) -> str:
    return (start + timedelta(days=offset)).isoformat()


def _month_from_offset(start_year: int, start_month: int, offset: int) -> str:
    month_index = (start_month - 1) + offset
    year = start_year + month_index // 12
    month = month_index % 12 + 1
    return f"{year:04d}-{month:02d}-01"


def _money(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def _names(count: int) -> list[str]:
    surnames = ("张", "李", "王", "刘", "陈", "杨", "黄", "赵", "周", "吴", "徐", "孙", "胡", "朱", "高", "林")
    given = ("伟", "娜", "强", "洋", "静", "磊", "敏", "婷", "杰", "倩", "鹏", "欣", "浩", "琳", "晨")
    return [surnames[i % len(surnames)] + given[(i * 7) % len(given)] + (str(i // 240) if i >= 240 else "") for i in range(count)]


def _build_rows(seed: int, tables: tuple[TableSpec, ...]) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    rows: dict[str, list[tuple]] = {}
    cities = ("北京", "上海", "深圳", "广州", "杭州", "成都", "武汉", "南京", "西安", "苏州")
    categories = ("电子", "服饰", "家居", "食品", "运动", "图书")
    brands = ("华为", "联想", "索尼", "小米", "优衣库", "无品牌", "耐克", "星巴克")

    customer_names = _names(240)
    customers = [
        (1, "张伟", "北京", "2023-01-10", "13800000001", "customer1@example.com"),
        (2, "李娜", "上海", "2023-02-14", "13800000002", "customer2@example.com"),
        (3, "王强", "深圳", "2023-03-20", "13800000003", "customer3@example.com"),
        (4, "刘洋", "北京", "2023-04-05", "13800000004", "customer4@example.com"),
        (5, "陈静", "上海", "2023-05-18", "13800000005", "customer5@example.com"),
    ]
    for customer_id in range(6, 241):
        customers.append(
            (
                customer_id,
                customer_names[customer_id - 1],
                cities[rng.randrange(len(cities))],
                _date_from_offset(date(2023, 1, 1), rng.randrange(730)),
                f"138{customer_id:08d}",
                f"customer{customer_id}@example.com",
            )
        )
    rows["customers"] = customers

    products = [
        (1, "手机", "电子", _money(3999), "华为"),
        (2, "笔记本", "电子", _money(8999), "联想"),
        (3, "T恤", "服饰", _money(99), "优衣库"),
        (4, "咖啡杯", "家居", _money(59), "无品牌"),
        (5, "耳机", "电子", _money(499), "索尼"),
    ]
    product_nouns = ("智能手表", "平板电脑", "运动鞋", "保温杯", "机械键盘", "办公椅", "咖啡豆", "绘图本")
    for product_id in range(6, 161):
        products.append(
            (
                product_id,
                f"{product_nouns[(product_id - 6) % len(product_nouns)]}{product_id}",
                categories[rng.randrange(len(categories))],
                _money(rng.uniform(29, 12999)),
                brands[rng.randrange(len(brands))],
            )
        )
    rows["products"] = products

    departments = [
        (1, "研发部", "北京", 1),
        (2, "销售部", "上海", 3),
        (3, "财务部", "深圳", 5),
    ]
    department_names = ("市场部", "人力资源部", "客服部", "供应链部", "法务部", "数据部", "运营部", "采购部", "审计部")
    for department_id, name in enumerate(department_names, start=4):
        departments.append((department_id, name, cities[(department_id + 1) % len(cities)], None))
    rows["departments"] = departments

    warehouses = [
        (1, "北京仓", "北京", _money(1000), 600),
        (2, "上海仓", "上海", _money(2000), 1500),
        (3, "深圳仓", "深圳", _money(1500), 900),
    ]
    for warehouse_id in range(4, 13):
        city = cities[(warehouse_id + 2) % len(cities)]
        warehouses.append((warehouse_id, f"{city}仓", city, _money(rng.uniform(500, 5000)), rng.randrange(200, 5000)))
    rows["warehouses"] = warehouses

    carriers = [
        (1, "顺丰速运", "400-811-1111", 500),
        (2, "京东物流", "400-606-5500", 300),
        (3, "德邦物流", "95353", 200),
    ]
    carrier_names = ("中通快递", "圆通速递", "申通快递", "极兔速递", "安能物流")
    for carrier_id in range(4, 17):
        carriers.append((carrier_id, f"{carrier_names[(carrier_id - 4) % len(carrier_names)]}{carrier_id}", f"400-{carrier_id:03d}-{carrier_id * 137 % 10000:04d}", rng.randrange(80, 600)))
    rows["carriers"] = carriers

    # Generate order items first so order totals are internally consistent.
    order_items: list[tuple] = [
        (1, 1, 1, 1, _money(3999)),
        (2, 1, 3, 1, _money(99)),
        (3, 2, 2, 1, _money(8999)),
        (4, 3, 3, 3, _money(99)),
        (5, 4, 5, 2, _money(499)),
        (6, 5, 1, 1, _money(3999)),
        (7, 6, 4, 5, _money(59)),
    ]
    order_rows: list[tuple] = []
    fixed_orders = (
        (1, 1, "2024-06-01", "已完成"),
        (2, 2, "2024-06-05", "已完成"),
        (3, 1, "2024-06-12", "已完成"),
        (4, 3, "2024-07-01", "已完成"),
        (5, 4, "2024-07-10", "已发货"),
        (6, 5, "2024-07-15", "已完成"),
    )
    for order_id, customer_id, order_date, status in fixed_orders:
        total = sum((quantity * unit_price for item_id, oid, pid, quantity, unit_price in order_items if oid == order_id), Decimal(0))
        order_rows.append((order_id, customer_id, order_date, status, total))
    statuses = ("已完成", "已发货", "待付款", "已取消")
    next_item_id = 8
    for order_id in range(7, 5001):
        customer_id = rng.randrange(1, 241)
        order_date = _date_from_offset(date(2024, 1, 1), rng.randrange(730))
        status = statuses[rng.randrange(len(statuses))]
        total = Decimal(0)
        # Keep the generated volume stable across seeds; the seed changes
        # values, while the demo's pagination/benchmark size remains fixed.
        for _ in range(1 + (order_id % 3)):
            product_id = rng.randrange(1, 161)
            quantity = 1 + rng.randrange(5)
            product_price = products[product_id - 1][3]
            unit_price = _money(float(product_price) * rng.uniform(0.88, 1.0))
            order_items.append((next_item_id, order_id, product_id, quantity, unit_price))
            next_item_id += 1
            total += quantity * unit_price
        order_rows.append((order_id, customer_id, order_date, status, total))
    rows["orders"] = order_rows
    rows["order_items"] = order_items

    reviews: list[tuple] = [
        (1, 1, 1, 5, "2024-06-03"),
        (2, 2, 2, 4, "2024-06-06"),
        (3, 3, 1, 3, "2024-06-14"),
        (4, 4, 3, 2, "2024-07-02"),
        (5, 5, 4, 5, "2024-07-11"),
    ]
    for review_id in range(6, 801):
        reviews.append((review_id, rng.randrange(1, 161), rng.randrange(1, 241), 1 + rng.randrange(5), _date_from_offset(date(2024, 1, 1), rng.randrange(730))))
    rows["reviews"] = reviews

    accounts: list[tuple] = [
        (1, 1, "储蓄", _money(50000), "2023-01-11"),
        (2, 2, "储蓄", _money(120000), "2023-02-15"),
        (3, 3, "信用", _money(8000), "2023-03-21"),
        (4, 4, "储蓄", _money(30000), "2023-04-06"),
        (5, 5, "储蓄", _money(65000), "2023-05-19"),
    ]
    account_types = ("储蓄", "信用", "投资")
    for account_id in range(6, 241):
        accounts.append((account_id, account_id, account_types[rng.randrange(len(account_types))], _money(rng.uniform(1000, 300000)), _date_from_offset(date(2023, 1, 1), rng.randrange(365))))
    rows["accounts"] = accounts

    transactions: list[tuple] = [
        (1, 1, _money(5000), "2024-06-10", "收入"),
        (2, 1, _money(-2000), "2024-06-15", "支出"),
        (3, 2, _money(10000), "2024-06-20", "收入"),
        (4, 3, _money(-1500), "2024-06-25", "支出"),
        (5, 4, _money(3000), "2024-07-01", "收入"),
        (6, 5, _money(-800), "2024-07-05", "支出"),
    ]
    for transaction_id in range(7, 5001):
        amount = _money(rng.uniform(50, 30000))
        txn_type = "收入" if rng.random() < 0.48 else "支出"
        if txn_type == "支出":
            amount = -amount
        transactions.append((transaction_id, rng.randrange(1, 241), amount, _date_from_offset(date(2024, 1, 1), rng.randrange(730)), txn_type))
    rows["transactions"] = transactions

    loans: list[tuple] = [
        (1, 1, _money(100000), Decimal("0.0450"), "2025-01-10", "未还清"),
        (2, 2, _money(50000), Decimal("0.0380"), "2024-12-20", "已还清"),
        (3, 3, _money(200000), Decimal("0.0520"), "2025-03-15", "未还清"),
        (4, 4, _money(80000), Decimal("0.0410"), "2025-02-01", "未还清"),
        (5, 5, _money(150000), Decimal("0.0480"), "2024-11-30", "已还清"),
    ]
    for loan_id in range(6, 181):
        loans.append((loan_id, rng.randrange(1, 241), _money(rng.uniform(20000, 500000)), Decimal(f"{rng.uniform(0.028, 0.068):.4f}"), _date_from_offset(date(2025, 1, 1), rng.randrange(730)), "未还清" if rng.random() < 0.62 else "已还清"))
    rows["loans"] = loans

    credit_cards: list[tuple] = [
        (1, 1, _money(50000), _money(12000), "金卡"),
        (2, 2, _money(80000), _money(5000), "白金卡"),
        (3, 3, _money(30000), _money(20000), "普卡"),
        (4, 4, _money(60000), _money(0), "金卡"),
        (5, 5, _money(100000), _money(30000), "白金卡"),
    ]
    card_types = ("普卡", "金卡", "白金卡")
    for card_id in range(6, 241):
        limit = _money(rng.choice((30000, 50000, 80000, 100000, 150000)))
        credit_cards.append((card_id, card_id, limit, _money(rng.uniform(0, float(limit))), card_types[rng.randrange(len(card_types))]))
    rows["credit_cards"] = credit_cards

    employee_names = _names(180)
    employees: list[tuple] = [
        (1, "赵云", 1, "2022-01-10", "工程师", "110101199001010011"),
        (2, "孙尚香", 1, "2022-03-15", "工程师", "110101199102020022"),
        (3, "关羽", 2, "2021-06-01", "经理", "310101198803030033"),
        (4, "张飞", 2, "2022-08-20", "销售", "310101199004040044"),
        (5, "黄忠", 3, "2023-02-14", "分析师", "440101199105050055"),
        (6, "马超", 3, "2023-05-30", "分析师", "440101199206060066"),
    ]
    titles = ("工程师", "销售", "分析师", "专员", "主管", "经理")
    for employee_id in range(7, 181):
        employees.append((employee_id, employee_names[employee_id - 1], rng.randrange(1, 13), _date_from_offset(date(2021, 1, 1), rng.randrange(1200)), titles[rng.randrange(len(titles))], f"110101{1980 + employee_id % 25:04d}{employee_id:06d}"))
    rows["employees"] = employees

    salaries: list[tuple] = []
    salary_id = 1
    fixed_salary = {
        1: (18000, 3000), 2: (16000, 2000), 3: (25000, 5000), 4: (12000, 1000), 5: (15000, 1500), 6: (14000, 1200)
    }
    for employee_id in range(1, 181):
        for month_offset in range(12):
            if employee_id in fixed_salary and month_offset == 0:
                base, bonus = fixed_salary[employee_id]
            else:
                base = rng.randrange(8000, 36001, 500)
                bonus = rng.randrange(0, 8001, 500)
            salaries.append((salary_id, employee_id, _money(base), _money(bonus), _month_from_offset(2024, 6, month_offset)))
            salary_id += 1
    rows["salaries"] = salaries

    attendance: list[tuple] = [
        (1, 1, "2024-06-01", "08:30:00", "正常"),
        (2, 1, "2024-06-02", "09:15:00", "迟到"),
        (3, 1, "2024-06-03", "09:30:00", "迟到"),
        (4, 2, "2024-06-01", "08:45:00", "正常"),
        (5, 2, "2024-06-03", "10:00:00", "迟到"),
        (6, 3, "2024-06-01", "08:20:00", "正常"),
        (7, 5, "2024-06-05", "09:30:00", "迟到"),
    ]
    attendance_statuses = ("正常", "迟到", "缺勤")
    for attendance_id in range(8, 6001):
        status = rng.choices(attendance_statuses, weights=(80, 15, 5), k=1)[0]
        hour = 8 if status == "正常" else 9 + rng.randrange(2)
        minute = rng.randrange(0, 60)
        attendance.append((attendance_id, rng.randrange(1, 181), _date_from_offset(date(2024, 1, 1), rng.randrange(365)), f"{hour:02d}:{minute:02d}:00", status))
    rows["attendance"] = attendance

    shipments: list[tuple] = [
        (1, 1, 1, "已送达", "2024-06-10", _money(120)),
        (2, 2, 1, "运输中", "2024-06-15", _money(300)),
        (3, 1, 2, "已送达", "2024-06-20", _money(80)),
        (4, 3, 3, "已送达", "2024-07-01", _money(500)),
        (5, 2, 2, "待发货", "2024-07-05", _money(45)),
    ]
    shipment_statuses = ("已送达", "运输中", "待发货", "已取消")
    for shipment_id in range(6, 3001):
        shipments.append((shipment_id, rng.randrange(1, 13), rng.randrange(1, 17), shipment_statuses[rng.randrange(len(shipment_statuses))], _date_from_offset(date(2024, 1, 1), rng.randrange(730)), _money(rng.uniform(10, 1000))))
    rows["shipments"] = shipments

    routes: list[tuple] = [
        (1, "北京", "上海", _money(1200)),
        (2, "北京", "深圳", _money(2100)),
        (3, "上海", "深圳", _money(1450)),
        (4, "上海", "杭州", _money(170)),
    ]
    for route_id in range(5, 41):
        origin = cities[rng.randrange(len(cities))]
        destination = cities[rng.randrange(len(cities))]
        while destination == origin:
            destination = cities[rng.randrange(len(cities))]
        routes.append((route_id, origin, destination, _money(rng.uniform(50, 2800))))
    rows["delivery_routes"] = routes

    # Ensure every declared table has a row collection, even if a future schema
    # adds an intentionally empty dimension table.
    for table in tables:
        rows.setdefault(table.name, [])
    return rows


def build_production_snapshot(seed: int = DEFAULT_SEED) -> ProductionSnapshot:
    """Build the fixed synthetic production dataset in memory."""
    tables = PRODUCTION_TABLES
    rows = _build_rows(seed, tables)
    return ProductionSnapshot(seed=seed, tables=tables, rows=rows)


def table_doc(table: TableSpec) -> str:
    columns = "；".join(f"{column.name}：{column.description}" for column in table.columns)
    return f"表 {table.name}：{table.description}。字段：{columns}。"


def iter_table_docs(tables: Iterable[TableSpec] = PRODUCTION_TABLES) -> list[tuple[str, str]]:
    return [(table.name, table_doc(table)) for table in tables]


# Keep the public database lifecycle names discoverable without importing the
# psycopg-dependent initializer until the operation is actually called.
def ensure_production_database(*args, **kwargs):
    from .initializer import ensure_production_database as ensure

    return ensure(*args, **kwargs)


def initialize_production_database(*args, **kwargs):
    from .initializer import initialize_production_database as initialize

    return initialize(*args, **kwargs)


def reset_production_database(*args, **kwargs):
    from .initializer import reset_production_database as reset

    return reset(*args, **kwargs)
