import os
import tempfile

import pytest

TEST_DATA = tempfile.mkdtemp(prefix="pvr-tests-")
os.environ["PVR_DATA_DIR"] = TEST_DATA
os.environ["PVR_QDRANT_URL"] = "http://127.0.0.1:1"
os.environ["PVR_PUBLIC_BASE_URL"] = "http://testserver/vault"
os.environ["PVR_REMOTE_BASE_URL"] = "http://testserver/vault-mcp"


@pytest.fixture(autouse=True)
def clean_database():
    from app.config import settings
    from app.db import connect, init_db

    settings().ensure_dirs()
    init_db()
    with connect() as db:
        preferred_order = (
            "oauth_codes", "oauth_clients", "temporary_inspections", "pending_artifacts", "usage_ledger", "chunks_fts",
            "previews", "table_cells", "chunks", "jobs", "versions", "documents", "sources",
            "pairing_codes", "access_tokens", "vaults",
        )
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for table in preferred_order:
            if table in tables:
                db.execute(f"DELETE FROM {table}")
    bootstrap = settings().secret_dir / "bootstrap-code"
    bootstrap.unlink(missing_ok=True)
    yield
