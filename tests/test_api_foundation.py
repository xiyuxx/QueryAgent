from fastapi.testclient import TestClient

from queryagent.api.app import app


client = TestClient(app)


def test_health_endpoint_reports_process_liveness() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "queryagent-api"


def test_system_status_only_reports_provider_configuration(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-for-test")
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = client.get("/api/system/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "foundation"
    assert body["providers"] == {
        "deepseek": True,
        "qwen": False,
        "openai": False,
    }
    assert body["database"] == {"status": "pending", "backend": "postgresql"}
    assert body["embedding"]["status"] == "pending"
