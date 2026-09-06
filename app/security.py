from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet
from fastapi import Header, HTTPException

from .config import settings
from .db import connect, now


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(prefix: str = "pvr") -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _fernet() -> Fernet:
    path = settings().secret_dir / "master.key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
    return Fernet(path.read_bytes())


def encrypt_secret(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt_secret(value: bytes | None) -> str | None:
    return _fernet().decrypt(value).decode() if value else None


@dataclass(frozen=True)
class Principal:
    vault_id: str
    token_id: str
    scopes: frozenset[str]
    label: str

    def require(self, scope: str) -> None:
        if scope not in self.scopes and "admin" not in self.scopes:
            raise HTTPException(403, f"Token lacks {scope!r} scope")


def authenticate_token(token: str) -> Principal:
    with connect() as db:
        row = db.execute(
            """SELECT id,vault_id,scopes,label,expires_at,revoked_at FROM access_tokens
               WHERE token_hash=?""",
            (token_hash(token),),
        ).fetchone()
    if not row or row["revoked_at"] or (row["expires_at"] and row["expires_at"] < now()):
        raise HTTPException(401, "Invalid or expired access token", headers={"WWW-Authenticate": "Bearer"})
    return Principal(row["vault_id"], row["id"], frozenset(row["scopes"].split()), row["label"])


def require_principal(authorization: str | None = Header(default=None)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    return authenticate_token(authorization.split(" ", 1)[1].strip())


def sign_view(vault_id: str, document_id: str, version_id: str, expires: int) -> str:
    key = _fernet()._signing_key  # stable HMAC key derived from local master key
    message = f"{vault_id}\n{document_id}\n{version_id}\n{expires}".encode()
    return base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode().rstrip("=")


def check_view_signature(vault_id: str, document_id: str, version_id: str, expires: int, signature: str) -> bool:
    if expires < int(now()):
        return False
    return hmac.compare_digest(sign_view(vault_id, document_id, version_id, expires), signature)


def pkce_s256(value: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode().rstrip("=")
