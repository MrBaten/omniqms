from .conftest import register  # noqa: F401


def test_register_errors_are_readable(client):
    r = client.post("/api/auth/register", json={"email": "baten", "password": "short", "name": "B", "workspace_name": ""})
    assert r.status_code == 422
    d = r.json()["detail"]
    assert isinstance(d, str)
    assert "Email: enter a valid address" in d and "Password: must be at least 8 characters" in d
    assert "Business / workspace name" in d
