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
