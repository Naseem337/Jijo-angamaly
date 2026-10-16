from fastapi.testclient import TestClient


def test_health_endpoint() -> None:
    from app.main import create_app

    client = TestClient(create_app())
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "truechoice" in body["clients"]
