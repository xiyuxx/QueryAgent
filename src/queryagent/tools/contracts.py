"""Typed contracts exchanged by the MCP data tools."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class QueryToolResponse(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    truncated: bool = False
    error: ToolError | None = None
    query_id: str | None = None
    tables: list[str] = Field(default_factory=list)


class SQLValidationResponse(BaseModel):
    ok: bool
    code: str
    message: str = ""
    normalized_sql: str = ""
    tables: list[str] = Field(default_factory=list)


class SchemaToolResponse(BaseModel):
    ddl: str
    tables: list[str] = Field(default_factory=list)
    sensitive_columns: dict[str, list[str]] = Field(default_factory=dict)
    error: ToolError | None = None


class TableSummary(BaseModel):
    name: str
    columns: list[dict] = Field(default_factory=list)
    column_count: int = 0
    row_count: int = 0


class TableListResponse(BaseModel):
    tables: list[TableSummary] = Field(default_factory=list)
    total_rows: int = 0
    error: ToolError | None = None


class TablePageResponse(BaseModel):
    table: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    page: int = 1
    page_size: int = 50
    total_rows: int = 0
    total_pages: int = 0
    sensitive_columns: list[str] = Field(default_factory=list)
    search_term: str | None = None
    csv: str | None = None
    filename: str | None = None
    error: ToolError | None = None
