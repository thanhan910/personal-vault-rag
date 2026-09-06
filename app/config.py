from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PVR_", case_sensitive=False)

    data_dir: Path = Path("/data")
    bind_port: int = 5001
    public_base_url: str = "http://127.0.0.1:5001/vault"
    remote_base_url: str = "http://127.0.0.1:5001/vault-mcp"
    qdrant_url: str = "http://qdrant:6333"
    max_upload_bytes: int = 256 * 1024 * 1024
    staging_ttl_hours: int = 24
    preview_ttl_days: int = 14
    log_ttl_days: int = 14
    min_free_bytes: int = 10 * 1024**3
    default_vault_quota_bytes: int = 20 * 1024**3
    worker_poll_seconds: float = 2.0
    reconcile_delete_confirmations: int = 2
    frame_interval_seconds: int = 30
    max_video_frames: int = 120

    @property
    def db_path(self) -> Path:
        return self.data_dir / "catalog.sqlite3"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def secret_dir(self) -> Path:
        return self.data_dir / "secrets"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.staging_dir, self.preview_dir, self.secret_dir):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)


@lru_cache
def settings() -> Settings:
    value = Settings()
    value.ensure_dirs()
    return value
