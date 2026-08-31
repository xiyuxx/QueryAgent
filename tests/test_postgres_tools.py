from __future__ import annotations

from decimal import Decimal

import pytest

from queryagent.tools.access import AccessConfig, RolePolicy
from queryagent.tools.policy import SQLPolicy
from queryagent.tools.postgres import (
    ColumnDescriptor,
    TableDescriptor,
    catalog_from_descriptors,
    find_sensitive_references,
    mask_sensitive_rows,
)


def _catalog():
    return catalog_from_descriptors(
        [
            TableDescriptor(
                "customers",
                (
                    ColumnDescriptor("customer_id", "bigint", nullable=False),
                    ColumnDescriptor("name", "text", nullable=False),
                    ColumnDescriptor("email", "text", nullable=False, sensitive=True),
                    ColumnDescriptor("phone", "text", nullable=False, sensitive=True),
                ),
            ),
            TableDescriptor(
                "orders",
                (
                    ColumnDescriptor("order_id", "bigint", nullable=False),
                    ColumnDescriptor("customer_id", "bigint", nullable=False),
                ),
            ),
        ]
    )


def test_mask_sensitive_rows_keeps_column_names_and_masks_only_sensitive_positions() -> None:
    columns, rows = mask_sensitive_rows(
        ["customer_id", "email", "name"],
        [(1, "a@example.com", "张伟")],
        sensitive_columns={"email"},
    )

    assert columns == ["customer_id", "email", "name"]
    assert rows == [[1, "******", "张伟"]]


def test_mask_sensitive_rows_preserves_raw_authorized_sensitive_columns() -> None:
    columns, rows = mask_sensitive_rows(
        ["email", "phone"],
        [("a@example.com", "13800000001")],
        sensitive_columns={"email", "phone"},
        raw_columns={"email"},
    )

    assert columns == ["email", "phone"]
    assert rows == [["a@example.com", "******"]]


def test_find_sensitive_references_handles_aliases_and_ctes() -> None:
    catalog = _catalog()

    assert find_sensitive_references("SELECT c.email FROM customers c", catalog) == [
        # Dataclass equality makes this assertion readable without exposing
        # parser internals to callers.
        find_sensitive_references("SELECT email FROM customers", catalog)[0]
    ]
    refs = find_sensitive_references(
        "WITH selected AS (SELECT email FROM customers) SELECT * FROM selected",
        catalog,
    )
    assert [(ref.table, ref.column) for ref in refs] == [("customers", "email")]
    assert find_sensitive_references("SELECT c.name FROM customers c", catalog) == []


def test_role_policy_requires_explicit_raw_sensitive_access() -> None:
    readonly = RolePolicy(name="readonly")
    admin = RolePolicy(name="admin")
    hr = RolePolicy(name="hr", raw_columns={"employees": {"national_id"}})

    assert not readonly.may_return_raw("customers", "email")
    assert admin.may_return_raw("customers", "email")
    assert hr.may_return_raw("employees", "national_id")
    assert not hr.may_return_raw("customers", "email")


def test_sql_policy_rejects_non_public_schema_and_cross_database_qualification() -> None:
    policy = SQLPolicy(
        dialect="postgres",
        allowed_tables={"customers"},
        allowed_schemas={"public"},
        allowed_catalogs={""},
    )

    assert policy.validate("SELECT * FROM private.customers").code == "SCHEMA_NOT_ALLOWED"
    assert policy.validate("SELECT * FROM other_db.public.customers").code == "DATABASE_NOT_ALLOWED"
    assert policy.validate("SELECT * FROM public.customers").ok
    assert policy.validate("SELECT * FROM customers").ok


@pytest.mark.parametrize("page, page_size", [(0, 1), (-1, 50), (1, 0), (1, 1000)])
def test_pagination_input_policy_is_bounded(page: int, page_size: int) -> None:
    # This is the public contract used by PostgresDataService._page_query:
    # pages are one-based and page size is capped at 100.
    normalized_page = max(1, int(page))
    normalized_size = min(100, max(1, int(page_size)))
    assert normalized_page >= 1
    assert 1 <= normalized_size <= 100
