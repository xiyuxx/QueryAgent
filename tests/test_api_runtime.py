from __future__ import annotations

import time

from fastapi.testclient import TestClient

from queryagent.api.app import create_app
from queryagent.api.runtime import AppServices
from queryagent.llm import LLMClient, LLMResponse, ProviderConfig, ProviderRegistry, SQLCandidate, Usage
from queryagent.tools.access import load_access_config
from queryagent.tools.db import QueryResult


class FakeSchema:
    last_tables = ["customers"]

    def context_for(self, question: str, *, role: str = "readonly") -> str:
        return "CREATE TABLE customers (id integer);"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = []

    def list_tables(self, role: str):
        return {"tables": [{"name": "customers", "row_count": 1}], "error": None}

    def browse_table(self, table, role, page, page_size):
        return {"table": table, "columns": ["id"], "rows": [[1]], "page": page, "page_size": page_size, "total_rows": 1, "total_pages": 1, "error": None}

    def search_table(self, table, term, role, page, page_size):
        return self.browse_table(table, role, page, page_size)

    def export_table_csv(self, table, role, page, page_size):
        return {"csv": "id\n1\n", "filename": f"{table}.csv", "error": None}

    def execute(self, sql, role=None):
        self.calls.append((sql, role))
        return QueryResult(columns=["count"], rows=[(1,)])

    def close(self):
        pass


class EvalLLM(LLMClient):
    def generate(self, prompt, *, system="", response_model=None):
        return LLMResponse("", Usage(), parsed=SQLCandidate(sql="SELECT 1", explanation="test"))

    def generate_sql(self, question, schema_ddl, feedback, strategy="standard", history=None):
        return LLMResponse("", Usage(), parsed=SQLCandidate(sql="SELECT 1", explanation="test"))


def registry() -> ProviderRegistry:
    config = ProviderConfig("deepseek", "key", "http://example", "model")
    empty = ProviderConfig("qwen", "", "http://example", "model")
    openai = ProviderConfig("openai", "", "http://example", "model")
    return ProviderRegistry(configs={"deepseek": config, "qwen": empty, "openai": openai}, clients={"deepseek": EvalLLM()})


def services() -> AppServices:
    return AppServices(
        registry=registry(),
        executor=FakeExecutor(),
        schema_retriever=FakeSchema(),
        access_config=load_access_config(),
    )


def test_data_endpoints_forward_role_and_csv() -> None:
    runtime = services()
    with TestClient(create_app(services=runtime)) as client:
        assert client.get("/api/data/tables?role=analyst").json()["tables"][0]["name"] == "customers"
        assert client.get("/api/data/table/customers?role=analyst").status_code == 200
        csv = client.get("/api/data/table/customers/csv?role=analyst")
    assert csv.headers["content-type"].startswith("text/csv")
    assert "id" in csv.text


def test_evaluation_runs_in_background_with_admin_role() -> None:
    runtime = services()
    with TestClient(create_app(services=runtime)) as client:
        response = client.post("/api/evaluations", json={"dataset": "mini", "provider": "deepseek"})
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        current = None
        for _ in range(20):
            current = client.get(f"/api/evaluations/{run_id}").json()
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
    assert current is not None
    assert current["status"] in {"completed", "failed"}
    if current["status"] == "completed":
        assert current["summary"]["total"] > 0
