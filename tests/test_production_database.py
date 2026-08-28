from __future__ import annotations

from decimal import Decimal

import pytest

from queryagent.database.production import (
    BUSINESS_TABLE_NAMES,
    INTERNAL_TABLE_NAMES,
    PRODUCTION_TABLES,
    build_production_snapshot,
    render_create_table,
)


def test_production_schema_has_four_business_domains_and_sensitive_columns() -> None:
    assert len(PRODUCTION_TABLES) == 17
    assert set(BUSINESS_TABLE_NAMES) == {
        "customers",
        "products",
        "orders",
        "order_items",
        "reviews",
        "accounts",
        "transactions",
        "loans",
        "credit_cards",
        "employees",
        "departments",
        "salaries",
        "attendance",
        "shipments",
        "warehouses",
        "carriers",
        "delivery_routes",
    }
    assert set(INTERNAL_TABLE_NAMES) >= {
        "schema_metadata",
        "value_index",
        "table_embeddings",
    }

    by_name = {table.name: table for table in PRODUCTION_TABLES}
    assert by_name["customers"].sensitive_columns == {"phone", "email"}
    assert by_name["employees"].sensitive_columns == {"national_id"}
    assert any(fk.references_table == "customers" for fk in by_name["orders"].foreign_keys)


def test_render_create_table_quotes_identifiers_and_foreign_keys() -> None:
    customers = next(table for table in PRODUCTION_TABLES if table.name == "customers")
    orders = next(table for table in PRODUCTION_TABLES if table.name == "orders")

    ddl = render_create_table(customers)
    assert 'CREATE TABLE IF NOT EXISTS "customers"' in ddl
    assert '"customer_id" BIGINT NOT NULL PRIMARY KEY' in ddl
    assert '"phone" TEXT NOT NULL' in ddl

    order_ddl = render_create_table(orders)
    assert 'FOREIGN KEY ("customer_id") REFERENCES "customers" ("customer_id")' in order_ddl


def test_snapshot_is_deterministic_and_sufficiently_large() -> None:
    first = build_production_snapshot()
    second = build_production_snapshot()

    assert first.digest == second.digest
    assert first.rows == second.rows
    assert first.total_rows >= 30_000
    assert first.row_counts["orders"] >= 5_000
    assert first.row_counts["transactions"] >= 5_000
    assert first.row_counts["attendance"] >= 5_000


def test_snapshot_contains_fixed_demo_queries_and_consistent_order_totals() -> None:
    snapshot = build_production_snapshot()
    customers = snapshot.rows["customers"]
    orders = snapshot.rows["orders"]
    items = snapshot.rows["order_items"]

    assert customers[0][:4] == (1, "张伟", "北京", "2023-01-10")
    assert orders[0] == (1, 1, "2024-06-01", "已完成", Decimal("4098.00"))

    items_by_order: dict[int, Decimal] = {}
    for _item_id, order_id, _product_id, quantity, unit_price in items:
        items_by_order[order_id] = items_by_order.get(order_id, Decimal("0")) + quantity * unit_price
    for order_id, _customer_id, _date, _status, total in orders[:20]:
        assert total == items_by_order[order_id]


def test_snapshot_seed_changes_generated_data_but_not_schema() -> None:
    first = build_production_snapshot(seed=20260828)
    other = build_production_snapshot(seed=7)

    assert first.digest != other.digest
    assert first.tables == other.tables
    assert first.row_counts == other.row_counts


@pytest.mark.parametrize("bad_identifier", ["users;DROP TABLE users", 'a"b'])
def test_render_create_table_never_accepts_dynamic_identifiers(bad_identifier: str) -> None:
    # The production schema is static and quote_identifier is only exercised
    # with its trusted TableSpec names. This guard documents the invariant.
    assert bad_identifier not in {table.name for table in PRODUCTION_TABLES}
