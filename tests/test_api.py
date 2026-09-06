import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_setup_pair_and_vault_authentication():
    bootstrap = settings().secret_dir / "bootstrap-code"
    if bootstrap.exists():
        bootstrap.unlink()
    with TestClient(app) as client:
        code = bootstrap.read_text().strip()
        setup = client.post("/api/setup", headers={"X-Bootstrap-Code": code}, json={"name": "API fixture"})
        assert setup.status_code == 200
        token = setup.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        status = client.get("/api/status", headers=headers)
        assert status.status_code == 200 and status.json()["vault"]["name"] == "API fixture"
        second = client.post("/api/vaults", headers=headers, json={"name": "Separate fixture"})
        assert second.status_code == 200 and second.json()["vault_id"] != setup.json()["vault_id"]
        second_status = client.get("/api/status", headers={"Authorization": f"Bearer {second.json()['access_token']}"})
        assert second_status.json()["vault"]["name"] == "Separate fixture"
        pair = client.post("/api/pairing-codes", headers=headers, json={"expires_minutes": 30})
        claim = client.post("/api/pair/claim", json={"pairing_code": pair.json()["pairing_code"], "device_name": "fixture"})
        assert claim.status_code == 200 and claim.json()["vault_id"] == setup.json()["vault_id"]
        assert client.get("/api/status", headers={"Authorization": f"Bearer {claim.json()['access_token']}"}).status_code == 200
        assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/downloads/windows-connector").status_code == 401
        assert client.get("/api/downloads/not-a-package", headers=headers).status_code == 404
        assert client.get("/api/health").json()["status"] == "ok"
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_endpoint" in metadata
