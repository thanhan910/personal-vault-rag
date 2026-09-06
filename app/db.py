from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from .config import settings


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS vaults (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at REAL NOT NULL,
  quota_bytes INTEGER NOT NULL,
  min_free_bytes INTEGER NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0,
  monthly_budget_microusd INTEGER NOT NULL DEFAULT 0,
  provider_ciphertext BLOB,
  embedding_model TEXT NOT NULL DEFAULT 'voyage-4-large',
  embedding_dimensions INTEGER NOT NULL DEFAULT 1024,
  visual_model TEXT NOT NULL DEFAULT 'voyage-multimodal-3.5',
  rerank_model TEXT NOT NULL DEFAULT 'rerank-2.5'
  ,last_job_claim_at REAL
);

CREATE TABLE IF NOT EXISTS access_tokens (
  id TEXT PRIMARY KEY,
  vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  scopes TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  revoked_at REAL
);

CREATE TABLE IF NOT EXISTS pairing_codes (
  code_hash TEXT PRIMARY KEY,
  vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
  expires_at REAL NOT NULL,
  used_at REAL
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT NOT NULL,
  vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  platform TEXT NOT NULL,
  root_label TEXT NOT NULL,
  last_seen_at REAL,
  last_successful_sync_at REAL,
  online INTEGER NOT NULL DEFAULT 0,
  scan_generation INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (vault_id, id)
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT NOT NULL,
  vault_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  display_name TEXT NOT NULL,
  media_type TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  revision_mtime_ns INTEGER NOT NULL DEFAULT 0,
  content_sha256 TEXT,
  current_version_id TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  availability TEXT NOT NULL DEFAULT 'source_online',
  last_seen_generation INTEGER NOT NULL DEFAULT 0,
  missing_confirmations INTEGER NOT NULL DEFAULT 0,
  last_seen_at REAL,
  deleted_at REAL,
  error TEXT,
  PRIMARY KEY (vault_id, id),
  UNIQUE (vault_id, source_id, relative_path),
  FOREIGN KEY (vault_id, source_id) REFERENCES sources(vault_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS versions (
  id TEXT NOT NULL,
  vault_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  source_mtime_ns INTEGER NOT NULL,
  extraction_version TEXT NOT NULL,
  embedding_model TEXT,
  visual_model TEXT,
  created_at REAL NOT NULL,
  indexed_at REAL,
  raw_staging_path TEXT,
  raw_expires_at REAL,
  text_bytes INTEGER NOT NULL DEFAULT 0,
  preview_bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (vault_id, id),
  FOREIGN KEY (vault_id, document_id) REFERENCES documents(vault_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT NOT NULL,
  vault_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'text',
  heading_path TEXT,
  locator_json TEXT NOT NULL,
  text TEXT NOT NULL,
  contextual_text TEXT,
  token_estimate INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (vault_id, id),
  FOREIGN KEY (vault_id, document_id) REFERENCES documents(vault_id, id) ON DELETE CASCADE,
  FOREIGN KEY (vault_id, version_id) REFERENCES versions(vault_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS table_cells (
  vault_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  sheet_name TEXT NOT NULL,
  cell_ref TEXT NOT NULL,
  row_number INTEGER NOT NULL,
  column_number INTEGER NOT NULL,
  raw_value TEXT,
  numeric_value REAL,
  formula TEXT,
  PRIMARY KEY (vault_id, version_id, sheet_name, cell_ref),
  FOREIGN KEY (vault_id, version_id) REFERENCES versions(vault_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS previews (
  id TEXT NOT NULL,
  vault_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  path TEXT NOT NULL,
  media_type TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (vault_id, id),
  FOREIGN KEY (vault_id, version_id) REFERENCES versions(vault_id, id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, contextual_text, heading_path,
  vault_id UNINDEXED, chunk_id UNINDEXED, document_id UNINDEXED, version_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  priority INTEGER NOT NULL DEFAULT 100,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  not_before REAL NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS usage_ledger (
  id TEXT PRIMARY KEY,
  vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  operation TEXT NOT NULL,
  estimated_units INTEGER NOT NULL,
  reserved_microusd INTEGER NOT NULL,
  actual_microusd INTEGER,
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  completed_at REAL,
  request_key TEXT,
  UNIQUE (vault_id, request_key)
);

CREATE TABLE IF NOT EXISTS pending_artifacts (
  id TEXT PRIMARY KEY,
  vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
  file_name TEXT NOT NULL,
  media_type TEXT,
  staging_path TEXT,
  note_text TEXT,
  provenance_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  claimed_at REAL,
  completed_at REAL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS temporary_inspections (
  id TEXT PRIMARY KEY,
  vault_id TEXT NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
  file_id TEXT NOT NULL,
  file_name TEXT NOT NULL,
  staging_path TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  extracted_text TEXT,
  warnings_json TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  completed_at REAL
);

CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY,
  redirect_uris_json TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  vault_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'search',
  expires_at REAL NOT NULL,
  used_at REAL
);

CREATE INDEX IF NOT EXISTS idx_documents_vault_state ON documents(vault_id, state);
CREATE INDEX IF NOT EXISTS idx_versions_doc ON versions(vault_id, document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(vault_id, document_id, version_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_table_cells_range ON table_cells(vault_id,document_id,version_id,sheet_name,row_number,column_number);
CREATE INDEX IF NOT EXISTS idx_previews_version ON previews(vault_id,version_id,ordinal);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, not_before, priority, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_unique ON jobs(vault_id,kind,payload_json) WHERE state IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_usage_month ON usage_ledger(vault_id, created_at, state);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or settings().db_path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def init_db(path: Path | None = None) -> None:
    with connect(path) as db:
        db.executescript(SCHEMA)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(vaults)")}
        if "last_job_claim_at" not in columns:
            db.execute("ALTER TABLE vaults ADD COLUMN last_job_claim_at REAL")
        oauth_columns = {row["name"] for row in db.execute("PRAGMA table_info(oauth_codes)")}
        if "scope" not in oauth_columns:
            db.execute("ALTER TABLE oauth_codes ADD COLUMN scope TEXT NOT NULL DEFAULT 'search'")


@contextlib.contextmanager
def transaction(db: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


def now() -> float:
    return time.time()
