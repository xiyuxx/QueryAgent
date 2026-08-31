"""Role-based access control for the MCP data server.

Roles are defined in a YAML config file (default: queryagent_roles.yaml).
The server loads the config at startup; the caller passes a role name with
every tool call. The server enforces access — the caller cannot grant itself
permissions it was not given.

Config format (YAML):
    roles:
      analyst:
        allowed_tables:          # if absent: all tables visible
          - orders
          - customers
          - products
        blocked_columns:         # legacy option: remove columns from old adapters
          employees:
            - salary
            - national_id
        raw_columns:              # sensitive columns allowed without masking
          customers:
            - phone

      hr:
        allowed_tables:
          - employees
          - departments
          - salaries
        blocked_columns: {}

      readonly:                  # no allowed_tables = all tables visible
        blocked_columns: {}
        raw_columns: {}           # sensitive values are masked unless listed

    default_role: readonly       # used when no role is supplied

Policy rules:
- allowed_tables: None means the role may see every table.
- blocked_columns is retained for legacy adapters that remove columns;
- PostgreSQL Web Demo sensitive columns remain named and are masked as
  `******` unless the role lists them in raw_columns (admin is unrestricted);
- A role unknown to the config is rejected with ACCESS_DENIED.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────
# Config data model
# ─────────────────────────────────────────────

@dataclass
class RolePolicy:
    name: str
    allowed_tables: set[str] | None = None          # None = all
    blocked_columns: dict[str, set[str]] = field(default_factory=dict)
    # A blocked sensitive column remains visible in PostgreSQL schema/results
    # but is masked. ``raw_columns`` is an explicit opt-in for roles such as
    # HR; admin is treated as unrestricted by ``may_return_raw``.
    raw_columns: dict[str, set[str]] = field(default_factory=dict)

    def may_access_table(self, table: str) -> bool:
        if self.allowed_tables is None:
            return True
        return table.lower() in self.allowed_tables

    def visible_columns(self, table: str, columns: list[str]) -> list[str]:
        blocked = self.blocked_columns.get(table.lower(), set())
        return [c for c in columns if c.lower() not in blocked]

    def may_return_raw(self, table: str, column: str) -> bool:
        """Whether a sensitive column may be returned without masking.

        Sensitive fields are masked by default. A role must explicitly list a
        field in ``raw_columns`` or be ``admin`` to receive its raw value.
        ``blocked_columns`` remains useful for legacy callers that remove a
        field entirely, but it does not grant raw access.
        """
        table_name = table.lower()
        column_name = column.lower()
        if self.name == "admin":
            return True
        return column_name in self.raw_columns.get(table_name, set())


@dataclass
class AccessConfig:
    roles: dict[str, RolePolicy]
    default_role: str = "readonly"

    def get_role(self, name: str | None) -> RolePolicy | None:
        key = (name or self.default_role).lower()
        return self.roles.get(key)


# ─────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────

def _parse_config(raw: dict) -> AccessConfig:
    roles: dict[str, RolePolicy] = {}
    for role_name, spec in (raw.get("roles") or {}).items():
        spec = spec or {}
        allowed_raw = spec.get("allowed_tables")
        allowed = {str(t).lower() for t in allowed_raw} if allowed_raw is not None else None
        blocked_raw = spec.get("blocked_columns") or {}
        blocked = {
            str(table).lower(): {str(col).lower() for col in cols}
            for table, cols in blocked_raw.items()
        }
        raw_raw = spec.get("raw_columns") or {}
        raw = {
            str(table).lower(): {str(col).lower() for col in cols}
            for table, cols in raw_raw.items()
        }
        roles[role_name.lower()] = RolePolicy(
            name=role_name.lower(),
            allowed_tables=allowed,
            blocked_columns=blocked,
            raw_columns=raw,
        )
    default_role = (raw.get("default_role") or "readonly").lower()
    if default_role not in roles:
        roles.setdefault(default_role, RolePolicy(name=default_role))
    return AccessConfig(roles=roles, default_role=default_role)


def load_access_config(path: str | Path | None = None) -> AccessConfig:
    """Load role config from an explicit path or the project config file."""
    configured_path = path or os.environ.get("QUERYAGENT_ROLES_CONFIG", "")
    candidates = [Path(configured_path)] if configured_path else [
        Path.cwd() / "queryagent_roles.yaml",
        Path(__file__).resolve().parents[3] / "queryagent_roles.yaml",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8")
        try:
            import yaml  # optional dep
            raw = yaml.safe_load(text)
        except ImportError:
            raw = json.loads(text)
        return _parse_config(raw or {})

    # Safe default: one "readonly" role that can see every table.
    return AccessConfig(
        roles={"readonly": RolePolicy(name="readonly")},
        default_role="readonly",
    )


# ─────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────

@dataclass
class AuditRecord:
    timestamp: float
    role: str
    tool: str
    decision: str           # ALLOWED / DENIED
    tables: list[str]
    code: str = ""
    message: str = ""
    query_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "role": self.role,
            "tool": self.tool,
            "decision": self.decision,
            "tables": self.tables,
            "code": self.code,
            "message": self.message,
            "query_id": self.query_id,
        }


class AuditLog:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = os.environ.get("QUERYAGENT_AUDIT_LOG", "")
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, rec: AuditRecord) -> None:
        if self._path is None:
            return
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    def allowed(self, role: str, tool: str, tables: list[str], query_id: str = "") -> None:
        self.record(AuditRecord(
            timestamp=time.time(), role=role, tool=tool,
            decision="ALLOWED", tables=tables, query_id=query_id,
        ))

    def denied(self, role: str, tool: str, tables: list[str],
               code: str, message: str, query_id: str = "") -> None:
        self.record(AuditRecord(
            timestamp=time.time(), role=role, tool=tool,
            decision="DENIED", tables=tables,
            code=code, message=message, query_id=query_id,
        ))
