import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import connect, now
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
        connector_headers = {"Authorization": f"Bearer {claim.json()['access_token']}"}
        assert client.get("/api/status", headers=connector_headers).status_code == 200
        staged = settings().staging_dir / "artifact.pdf"
        staged.write_bytes(b"real-pdf-bytes")
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        with connect() as db:
            db.execute(
                """INSERT INTO pending_artifacts(id,vault_id,file_name,media_type,artifact_kind,staging_path,note_text,content_sha256,content_size_bytes,provenance_json,state,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                ("artifact_fixture", setup.json()["vault_id"], "invoice.pdf", "application/pdf", "file", str(staged), "annotation", digest, staged.stat().st_size, "{}", now(), now() + 3600),
            )
        listing = client.get("/api/connector/artifacts", headers=connector_headers).json()["artifacts"]
        assert listing[0]["has_file"] is True and listing[0]["note_text"] == "annotation"
        content = client.get("/api/connector/artifacts/artifact_fixture/content", headers=connector_headers)
        assert content.content == b"real-pdf-bytes"
        rejected = client.post(
            "/api/connector/artifacts/artifact_fixture/complete",
            headers=connector_headers,
            json={"success": True, "saved_relative_path": "invoice.pdf", "content_sha256": "0" * 64, "content_size_bytes": len(content.content)},
        )
        assert rejected.status_code == 422 and staged.exists()
        retry = client.post(
            "/api/connector/artifacts/artifact_fixture/complete",
            headers=connector_headers,
            json={"success": False, "error": "temporary destination error"},
        )
        assert retry.json()["state"] == "pending_retry" and staged.exists()
        completed = client.post(
            "/api/connector/artifacts/artifact_fixture/complete",
            headers=connector_headers,
            json={"success": True, "saved_relative_path": "invoice.pdf", "content_sha256": digest, "content_size_bytes": len(content.content)},
        )
        assert completed.json()["state"] == "saved_to_source" and completed.json()["index_state"] == "pending_connector_scan"
        assert not staged.exists()
        assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/downloads/windows-connector").status_code == 401
        assert client.get("/api/downloads/not-a-package", headers=headers).status_code == 404
        assert client.get("/api/health").json()["status"] == "ok"
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert "authorization_endpoint" in metadata
