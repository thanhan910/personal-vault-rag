import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.budget import BudgetUnavailable, estimate_multimodal, estimate_text_embedding, mark_uncertain, reserve
from app.catalog import cleanup_expired, enqueue_missing_embedding_jobs, register_upload, stable_version_id
from app.config import settings
from app.db import connect, now
from app.extraction import ExtractedPreview, ExtractionResult, extract_document
from app.provider import embed_texts, embed_visual_inputs
from app.retrieval import _lexical, _load_chunks, _qdrant_filter
from app.security import encrypt_secret
from app.worker import _replace_visual_evidence


def seed_vault(name: str = "review") -> tuple[str, str]:
    vault_id = "vault_" + uuid.uuid4().hex
    source_id = "src_" + uuid.uuid4().hex
    with connect() as db:
        db.execute(
            "INSERT INTO vaults(id,name,created_at,quota_bytes,min_free_bytes) VALUES(?,?,?,?,?)",
            (vault_id, name, now(), 10**9, 0),
        )
        db.execute(
            "INSERT INTO sources(id,vault_id,name,platform,root_label) VALUES(?,?,?,?,?)",
            (source_id, vault_id, "review", "linux", "fixture"),
        )
    return vault_id, source_id


def seed_version(vault_id: str, source_id: str, document_id: str, version_id: str, path: str, *, current: bool = True) -> None:
    with connect() as db:
        if not db.execute("SELECT 1 FROM documents WHERE vault_id=? AND id=?", (vault_id, document_id)).fetchone():
            db.execute(
                """INSERT INTO documents(id,vault_id,source_id,relative_path,display_name,media_type,current_version_id,state)
                   VALUES(?,?,?,?,?,'text/plain',?,'indexed')""",
                (document_id, vault_id, source_id, path, Path(path).name, version_id if current else None),
            )
        db.execute(
            """INSERT INTO versions(id,vault_id,document_id,content_sha256,source_mtime_ns,extraction_version,created_at,indexed_at,lexical_indexed_at)
               VALUES(?,?,?,?,?,'test',?,?,?)""",
            (version_id, vault_id, document_id, "c" * 64, 1, now(), now(), now()),
        )


def add_chunk(vault_id: str, document_id: str, version_id: str, chunk_id: str, text: str, locator: dict) -> dict:
    record = {
        "id": chunk_id,
        "vault_id": vault_id,
        "document_id": document_id,
        "version_id": version_id,
        "ordinal": 0,
        "kind": "text",
        "heading_path": "",
        "locator_json": json.dumps(locator),
        "text": text,
        "contextual_text": None,
        "token_estimate": max(1, len(text) // 4),
    }
    with connect() as db:
        db.execute(
            """INSERT INTO chunks(id,vault_id,document_id,version_id,ordinal,kind,heading_path,locator_json,text,contextual_text,token_estimate,created_at)
               VALUES(:id,:vault_id,:document_id,:version_id,:ordinal,:kind,:heading_path,:locator_json,:text,:contextual_text,:token_estimate,:created_at)""",
            {**record, "created_at": now()},
        )
        db.execute(
            "INSERT INTO chunks_fts(text,contextual_text,heading_path,vault_id,chunk_id,document_id,version_id) VALUES(?,?,?,?,?,?,?)",
            (text, "", "", vault_id, chunk_id, document_id, version_id),
        )
    return record


def test_budget_rechecks_zero_allowance_and_counts_uncertain_attempts():
    vault_id, _ = seed_vault()
    with connect() as db:
        db.execute("UPDATE vaults SET monthly_budget_microusd=10 WHERE id=?", (vault_id,))
    first = reserve(vault_id, "model", "operation", 1, 8, "same-logical-batch")
    mark_uncertain(first.id, "response lost after request")
    with pytest.raises(BudgetUnavailable):
        reserve(vault_id, "model", "operation", 1, 3, "same-logical-batch")
    with connect() as db:
        db.execute("UPDATE vaults SET monthly_budget_microusd=0 WHERE id=?", (vault_id,))
    with pytest.raises(BudgetUnavailable):
        reserve(vault_id, "model", "operation", 1, 1, "same-logical-batch")


def test_provider_settles_reported_usage_and_does_not_call_at_zero_budget(monkeypatch):
    vault_id, _ = seed_vault()
    with connect() as db:
        db.execute(
            "UPDATE vaults SET provider_ciphertext=?,monthly_budget_microusd=? WHERE id=?",
            (encrypt_secret("test-provider-key"), 1_000_000, vault_id),
        )
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"total_tokens": 100}}

    monkeypatch.setattr("app.provider.httpx.post", lambda *args, **kwargs: calls.append((args, kwargs)) or Response())
    assert embed_texts(vault_id, ["tiny"], "document", "batch") == [[0.1, 0.2]]
    with connect() as db:
        row = db.execute("SELECT state,actual_microusd FROM usage_ledger WHERE vault_id=?", (vault_id,)).fetchone()
        assert row["state"] == "complete"
        assert row["actual_microusd"] == estimate_text_embedding(100)
        db.execute("UPDATE vaults SET monthly_budget_microusd=0 WHERE id=?", (vault_id,))
    with pytest.raises(BudgetUnavailable):
        embed_texts(vault_id, ["again"], "document", "batch")
    assert len(calls) == 1


def test_multimodal_provider_settles_reported_text_and_pixel_usage(monkeypatch):
    from PIL import Image

    vault_id, _ = seed_vault()
    with connect() as db:
        db.execute(
            "UPDATE vaults SET provider_ciphertext=?,monthly_budget_microusd=? WHERE id=?",
            (encrypt_secret("test-provider-key"), 1_000_000, vault_id),
        )

    class Client:
        def __init__(self, api_key: str):
            assert api_key == "test-provider-key"

        def multimodal_embed(self, **kwargs):
            assert kwargs["inputs"][0][0] == "visual query context"
            return SimpleNamespace(
                embeddings=[[0.3, 0.4]],
                text_tokens=40,
                image_pixels=50_000,
                video_pixels=0,
            )

    monkeypatch.setattr("app.provider.voyageai.Client", Client)
    image = Image.new("RGB", (20, 20), "white")
    assert embed_visual_inputs(vault_id, [["visual query context", image]], "query", "visual-batch") == [[0.3, 0.4]]
    with connect() as db:
        row = db.execute(
            "SELECT state,estimated_units,actual_microusd FROM usage_ledger WHERE vault_id=?",
            (vault_id,),
        ).fetchone()
    assert row["state"] == "complete"
    assert row["estimated_units"] >= 50_000 + len("visual query context")
    assert row["actual_microusd"] == estimate_multimodal(40, 50_000)


def test_provider_activation_queues_missing_text_and_visual_backfill(tmp_path: Path):
    vault_id, source_id = seed_vault()
    document_id, version_id = "doc_" + "a" * 32, "ver_" + "b" * 32
    seed_version(vault_id, source_id, document_id, version_id, "folder/file.pdf")
    text_record = add_chunk(vault_id, document_id, version_id, "chk_" + "c" * 32, "retained text", {"page": 1})
    preview = tmp_path / "page.jpg"
    preview.write_bytes(b"image")
    _replace_visual_evidence(vault_id, document_id, version_id, [ExtractedPreview(preview, {"page": 1})], [text_record])
    with connect() as db:
        db.execute(
            "UPDATE vaults SET provider_ciphertext=?,monthly_budget_microusd=1000000 WHERE id=?",
            (b"configured", vault_id),
        )
    assert enqueue_missing_embedding_jobs(vault_id) == {"text": 1, "visual": 1}
    with connect() as db:
        kinds = {row["kind"] for row in db.execute("SELECT kind FROM jobs WHERE vault_id=? AND state='queued'", (vault_id,))}
    assert {"embed_text", "embed_visual"}.issubset(kinds)


def test_visual_locator_does_not_inherit_unrelated_page_text(tmp_path: Path):
    vault_id, source_id = seed_vault()
    document_id, version_id = "doc_" + "d" * 32, "ver_" + "e" * 32
    seed_version(vault_id, source_id, document_id, version_id, "two-pages.pdf")
    page_one = add_chunk(vault_id, document_id, version_id, "chk_" + "f" * 32, "page one words", {"page": 1})
    previews = []
    for page in (1, 2):
        path = tmp_path / f"page-{page}.jpg"
        path.write_bytes(b"image")
        previews.append(ExtractedPreview(path, {"page": page}))
    _replace_visual_evidence(vault_id, document_id, version_id, previews, [page_one])
    with connect() as db:
        row = db.execute(
            """SELECT p.locator_json,c.locator_json chunk_locator,c.contextual_text
               FROM previews p JOIN chunks c ON c.vault_id=p.vault_id AND c.id=p.text_chunk_id
               WHERE p.vault_id=? AND p.version_id=? AND p.ordinal=1""",
            (vault_id, version_id),
        ).fetchone()
    assert json.loads(row["locator_json"]) == {"page": 2}
    assert json.loads(row["chunk_locator"]) == {"page": 2}
    assert row["contextual_text"] is None


def test_visual_only_image_is_valid_extraction(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "photo.png"
    from PIL import Image

    Image.new("RGB", (32, 32), "blue").save(image_path)
    monkeypatch.setattr("app.extraction._docling", lambda path: (_ for _ in ()).throw(RuntimeError("no text")))
    monkeypatch.setattr("app.extraction._image_ocr", lambda path: ExtractionResult([]))
    result = extract_document(image_path, image_path.name, tmp_path / "previews")
    assert result.chunks == []
    assert result.previews[0].locator == {"image": 1}


def test_default_lexical_search_is_current_and_filters_are_defensive():
    vault_id, source_id = seed_vault()
    document_id = "doc_" + "1" * 32
    old_version, new_version = "ver_" + "2" * 32, "ver_" + "3" * 32
    seed_version(vault_id, source_id, document_id, old_version, "allowed/current.txt", current=False)
    seed_version(vault_id, source_id, document_id, new_version, "allowed/current.txt", current=False)
    with connect() as db:
        db.execute("UPDATE documents SET current_version_id=? WHERE vault_id=? AND id=?", (new_version, vault_id, document_id))
    old_chunk = "chk_" + "4" * 32
    new_chunk = "chk_" + "5" * 32
    add_chunk(vault_id, document_id, old_version, old_chunk, "reimbursement cap old", {"line": 1})
    add_chunk(vault_id, document_id, new_version, new_chunk, "reimbursement cap current", {"line": 1})
    assert _lexical(vault_id, "reimbursement cap", 10, {}) == [new_chunk]
    assert set(_lexical(vault_id, "reimbursement cap", 10, {"include_history": True})) == {old_chunk, new_chunk}
    assert _load_chunks(vault_id, [new_chunk], {"path_prefix": "elsewhere"}) == {}
    encoded_filter = json.dumps(_qdrant_filter(vault_id, {"source_id": source_id, "media_type": "text", "path_prefix": "allowed"}).model_dump(), default=str)
    assert source_id in encoded_filter and "media_filters" in encoded_filter and "path_prefixes" in encoded_filter and "current" in encoded_filter


def test_preview_expiry_disables_visual_index_and_requests_source_recovery(tmp_path: Path):
    vault_id, source_id = seed_vault()
    document_id, version_id = "doc_" + "6" * 32, "ver_" + "7" * 32
    seed_version(vault_id, source_id, document_id, version_id, "photo.png")
    preview = tmp_path / "old.webp"
    preview.write_bytes(b"preview")
    with connect() as db:
        db.execute(
            "UPDATE versions SET visual_indexed_at=?,visual_model='voyage-multimodal-3.5',preview_bytes=? WHERE vault_id=? AND id=?",
            (now(), preview.stat().st_size, vault_id, version_id),
        )
        db.execute(
            """INSERT INTO previews(id,vault_id,document_id,version_id,ordinal,path,media_type,locator_json,created_at)
               VALUES(?,?,?,?,0,?,'image/webp','{}',?)""",
            ("preview_old", vault_id, document_id, version_id, str(preview), now() - (settings().preview_ttl_days + 1) * 86400),
        )
        db.execute(
            "INSERT INTO embedding_batches(vault_id,version_id,modality,batch_start,model,item_count,completed_at) VALUES(?,?,'visual',0,'voyage-multimodal-3.5',1,?)",
            (vault_id, version_id, now()),
        )
    cleanup_expired()
    with connect() as db:
        version = db.execute("SELECT visual_indexed_at,visual_recovery_needed,preview_bytes FROM versions WHERE vault_id=? AND id=?", (vault_id, version_id)).fetchone()
        job = db.execute("SELECT kind FROM jobs WHERE vault_id=? AND kind='delete_version_vectors'", (vault_id,)).fetchone()
        batch = db.execute("SELECT 1 FROM embedding_batches WHERE vault_id=? AND version_id=?", (vault_id, version_id)).fetchone()
    assert version["visual_indexed_at"] is None and version["visual_recovery_needed"] == 1 and version["preview_bytes"] == 0
    assert job and not batch and not preview.exists()


def test_unchanged_reupload_queues_visual_regeneration(tmp_path: Path):
    vault_id, source_id = seed_vault()
    document_id = "doc_" + "8" * 32
    raw = b"same source bytes"
    digest = hashlib.sha256(raw).hexdigest()
    version_id = stable_version_id(document_id, digest, 1)
    seed_version(vault_id, source_id, document_id, version_id, "photo.png")
    with connect() as db:
        db.execute(
            "UPDATE documents SET content_sha256=?,revision_mtime_ns=? WHERE vault_id=? AND id=?",
            (digest, 1, vault_id, document_id),
        )
        db.execute(
            "UPDATE versions SET content_sha256=?,source_mtime_ns=1,visual_recovery_needed=1 WHERE vault_id=? AND id=?",
            (digest, vault_id, version_id),
        )
    staged = tmp_path / "photo.png"
    staged.write_bytes(raw)
    returned_document, returned_version, state = register_upload(
        vault_id,
        source_id,
        "photo.png",
        "photo.png",
        digest,
        1,
        len(raw),
        staged,
        scan_generation=2,
        document_id=document_id,
    )
    assert returned_document == document_id and returned_version == version_id and state.startswith("job_")
    with connect() as db:
        job = db.execute("SELECT kind FROM jobs WHERE id=?", (state,)).fetchone()
        version = db.execute("SELECT raw_staging_path FROM versions WHERE vault_id=? AND id=?", (vault_id, version_id)).fetchone()
    assert job["kind"] == "render_visual" and version["raw_staging_path"] == str(staged)


def test_reusing_an_indexed_version_refreshes_current_vector_metadata(tmp_path: Path):
    vault_id, source_id = seed_vault()
    document_id = "doc_" + "9" * 32
    raw = b"known source bytes"
    digest = hashlib.sha256(raw).hexdigest()
    version_id = stable_version_id(document_id, digest, 1)
    seed_version(vault_id, source_id, document_id, version_id, "old-folder/file.txt")
    staged = tmp_path / "file.txt"
    staged.write_bytes(raw)
    returned = register_upload(
        vault_id,
        source_id,
        "new-folder/file.txt",
        "file.txt",
        digest,
        1,
        len(raw),
        staged,
        scan_generation=2,
        document_id=document_id,
    )
    assert returned == (document_id, version_id, "unchanged")
    with connect() as db:
        job = db.execute(
            "SELECT payload_json FROM jobs WHERE vault_id=? AND kind='mark_historical' AND state='queued'",
            (vault_id,),
        ).fetchone()
    assert json.loads(job["payload_json"]) == {"document_id": document_id, "current_version_id": version_id}
