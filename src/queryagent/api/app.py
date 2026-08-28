"""Minimal HTTP health surface for the Phase 0 Web Demo foundation.

Business routes are added in later phases. Keeping the application importable
now lets Docker and frontend proxy checks run before the full agent API exists.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="QueryAgent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Return a process-level liveness response.

    Database and embedding readiness checks belong to the startup/status layer
    and will be added with the PostgreSQL initialization phase.
    """
    return {
        "status": "ok",
        "service": "queryagent-api",
        "version": app.version,
        "time": datetime.now(UTC).isoformat(),
    }


@app.get("/api/system/status")
def system_status() -> dict[str, object]:
    """Expose the Phase 0 configuration shape without making model calls."""
    providers = {
        name: bool(os.environ.get(f"{name.upper()}_API_KEY", "").strip())
        for name in ("deepseek", "qwen", "openai")
    }
    return {
        "status": "foundation",
        "providers": providers,
        "database": {"status": "pending", "backend": "postgresql"},
        "embedding": {"status": "pending"},
    }
