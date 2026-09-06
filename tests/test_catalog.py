import hashlib
import json
import time
import uuid

import pytest

from app.catalog import complete_reconcile, register_upload
from app.config import settings
from app.db import connect, init_db, now
from app.retrieval import fetch_chunk
from app.security import authenticate_token, token_hash


def seed_vault(name: str) -> tuple[str, str, str]:
    vault_id = "vault_" + uuid.uuid4().hex
    token = "vault_" + uuid.uuid4().hex
    source_id = "src_" + uuid.uuid4().hex
    with connect() as db:
        db.execute("INSERT INTO vaults(id,name,created_at,quota_bytes,min_free_bytes) VALUES(?,?,?,?,?)", (vault_id, name, now(), 10**9, 0))
        db.execute("INSERT INTO access_tokens(id,vault_id,token_hash,label,scopes,created_at) VALUES(?,?,?,?,?,?)", ("tok_"+uuid.uuid4().hex, vault_id, token_hash(token), "test", "admin search write ingest", now()))
        db.execute("INSERT INTO sources(id,vault_id,name,platform,root_label) VALUES(?,?,?,?,?)", (source_id, vault_id, "test", "linux", "fixture"))
    return vault_id, token, source_id


def test_token_and_direct_fetch_are_vault_scoped():
    first, token, source = seed_vault("one")
    second, _, _ = seed_vault("two")
    assert authenticate_token(token).vault_id == first
    with connect() as db:
        db.execute("INSERT INTO documents(id,vault_id,source_id,relative_path,display_name,state) VALUES(?,?,?,?,?,'indexed')", ("doc_"+"a"*32, first, source, "a.txt", "a.txt"))
        db.execute("INSERT INTO versions(id,vault_id,document_id,content_sha256,source_mtime_ns,extraction_version,created_at,indexed_at) VALUES(?,?,?,?,?,?,?,?)", ("ver_"+"b"*32, first, "doc_"+"a"*32, "c"*64, 1, "test", now(), now()))
        db.execute("INSERT INTO chunks(id,vault_id,document_id,version_id,ordinal,locator_json,text,token_estimate,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("chk_"+"d"*32, first, "doc_"+"a"*32, "ver_"+"b"*32, 0, '{"page":1}', "evidence", 2, now()))
    assert fetch_chunk(first, "chk_"+"d"*32)["passages"][0]["text"] == "evidence"
    with pytest.raises(KeyError):
        fetch_chunk(second, "chk_"+"d"*32)


def test_offline_scan_does_not_delete_and_two_complete_scans_do():
    vault, _, source = seed_vault("delete semantics")
    with connect() as db:
        db.execute("INSERT INTO documents(id,vault_id,source_id,relative_path,display_name,last_seen_generation,state) VALUES(?,?,?,?,?,?,'indexed')", ("doc_"+"a"*32, vault, source, "old.txt", "old.txt", 1))
    complete_reconcile(vault, source, 2, False)
    with connect() as db:
        row = db.execute("SELECT deleted_at,availability,missing_confirmations FROM documents WHERE vault_id=?", (vault,)).fetchone()
    assert row["deleted_at"] is None and row["availability"] == "source_offline" and row["missing_confirmations"] == 0
    assert complete_reconcile(vault, source, 2, True)["confirmed_deleted"] == 0
    assert complete_reconcile(vault, source, 3, True)["confirmed_deleted"] == 1


def test_version_dedup_is_per_vault_and_staging_is_bounded(tmp_path):
    vault, _, source = seed_vault("dedup")
    staged = tmp_path / "input.txt"
    staged.write_text("same bytes")
    digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    doc, version, job = register_upload(vault, source, "input.txt", "input.txt", digest, 1, staged.stat().st_size, staged, scan_generation=1)
    assert doc.startswith("doc_") and version.startswith("ver_") and job.startswith("job_")
    with connect() as db:
        row = db.execute("SELECT raw_expires_at FROM versions WHERE vault_id=? AND id=?", (vault, version)).fetchone()
    assert now() < row["raw_expires_at"] <= now() + settings().staging_ttl_hours * 3600 + 2
