from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_plain_address_opens_the_dashboard_and_dead_pages_get_a_way_back(monkeypatch, tmp_path):
    monkeypatch.setenv("RESORT_WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("RESORT_DB_PATH", str(tmp_path / "r.sqlite3"))
    client = TestClient(create_app())  # no `with`: the startup jobs are not needed here
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 307 and home.headers["location"] == "/resort/admin/"
    assert client.get("/resort", follow_redirects=False).headers["location"] == "/resort/admin/"
    page = client.get("/resort/admin/nothing-here", headers={"accept": "text/html"})
    assert page.status_code == 404 and "Go to the dashboard" in page.text
    api = client.get("/resort/admin/nothing-here", headers={"accept": "application/json"})
    assert api.status_code == 404 and api.json() == {"detail": "Not Found"}  # API callers keep the JSON error

