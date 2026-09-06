from __future__ import annotations

import json
import secrets
import time
import uuid
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, HttpUrl

from .config import settings
from .db import connect, now, transaction
from .security import authenticate_token, new_token, pkce_s256, token_hash

router = APIRouter()


class ClientRegistration(BaseModel):
    redirect_uris: list[HttpUrl]
    client_name: str | None = None
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] = ["authorization_code", "refresh_token"]
    response_types: list[str] = ["code"]


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
def authorization_metadata() -> dict:
    base = settings().remote_base_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["search", "write"],
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata() -> dict:
    base = settings().remote_base_url.rstrip("/")
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["search", "write"],
    }


@router.post("/oauth/register", status_code=201)
def register_client(body: ClientRegistration) -> dict:
    if body.token_endpoint_auth_method != "none":
        raise HTTPException(400, "Only public PKCE clients are supported")
    if not body.redirect_uris or len(body.redirect_uris) > 10:
        raise HTTPException(400, "One to ten redirect URIs are required")
    client_id = "client_" + secrets.token_urlsafe(24)
    with connect() as db:
        db.execute(
            "INSERT INTO oauth_clients(client_id,redirect_uris_json,created_at) VALUES(?,?,?)",
            (client_id, json.dumps([str(value) for value in body.redirect_uris]), now()),
        )
    return {
        "client_id": client_id,
        "client_id_issued_at": int(now()),
        "redirect_uris": [str(value) for value in body.redirect_uris],
        "client_name": body.client_name or "Personal Vault client",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def _validated_authorize(client_id: str, redirect_uri: str, response_type: str, code_challenge: str, code_challenge_method: str) -> None:
    with connect() as db:
        row = db.execute("SELECT redirect_uris_json FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
    if not row or redirect_uri not in json.loads(row["redirect_uris_json"]):
        raise HTTPException(400, "Unknown client or redirect URI")
    if response_type != "code" or code_challenge_method != "S256" or len(code_challenge) < 43:
        raise HTTPException(400, "Authorization code with PKCE S256 is required")


@router.get("/oauth/authorize", response_class=HTMLResponse)
def authorize_form(client_id: str, redirect_uri: str, response_type: str, code_challenge: str, code_challenge_method: str, state: str = "", scope: str = "search write") -> HTMLResponse:
    _validated_authorize(client_id, redirect_uri, response_type, code_challenge, code_challenge_method)
    fields = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": response_type, "code_challenge": code_challenge, "code_challenge_method": code_challenge_method, "state": state, "scope": scope}
    hidden = "".join(f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in fields.items())
    html = f"""<!doctype html><html><head><meta charset=utf-8><title>Authorize Personal Vault</title>
    <style>body{{font:16px system-ui;max-width:32rem;margin:4rem auto;padding:1rem}}input,button{{box-sizing:border-box;width:100%;padding:.8rem;margin:.4rem 0}}.note{{color:#555}}</style></head>
    <body><h1>Authorize Personal Vault</h1><p>Enter a vault access token from the private Vault setup page. The token is submitted only to this server.</p>
    <form method=post>{hidden}<label>Vault access token<input name=vault_token type=password required autocomplete=off></label><button>Authorize</button></form>
    <p class=note>Requested scopes: {escape(scope)}. Never paste this token into a chat message.</p></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


@router.post("/oauth/authorize")
def authorize_submit(
    client_id: str = Form(), redirect_uri: str = Form(), response_type: str = Form(),
    code_challenge: str = Form(), code_challenge_method: str = Form(), state: str = Form(default=""),
    scope: str = Form(default="search write"), vault_token: str = Form(),
) -> RedirectResponse:
    _validated_authorize(client_id, redirect_uri, response_type, code_challenge, code_challenge_method)
    principal = authenticate_token(vault_token)
    requested = [item for item in scope.split() if item in {"search", "write"}]
    if "search" not in requested:
        requested.insert(0, "search")
    if any(item not in principal.scopes and "admin" not in principal.scopes for item in requested):
        raise HTTPException(403, "Vault token does not grant requested scopes")
    code = new_token("code")
    with connect() as db:
        db.execute(
            """INSERT INTO oauth_codes(code_hash,client_id,vault_id,redirect_uri,code_challenge,scope,expires_at)
               VALUES(?,?,?,?,?,?,?)""",
            (token_hash(code), client_id, principal.vault_id, redirect_uri, code_challenge, " ".join(requested), now() + 300),
        )
    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(params)}", status_code=303)


def _issue_oauth_tokens(vault_id: str, client_id: str, scopes: list[str]) -> dict:
    access, refresh = new_token("oauth"), new_token("refresh")
    stamp = now()
    with connect() as db, transaction(db, immediate=True):
        db.execute(
            """INSERT INTO access_tokens(id,vault_id,token_hash,label,scopes,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("tok_" + uuid.uuid4().hex, vault_id, token_hash(access), f"OAuth access {client_id}", " ".join(scopes), stamp, stamp + 3600),
        )
        db.execute(
            """INSERT INTO access_tokens(id,vault_id,token_hash,label,scopes,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("tok_" + uuid.uuid4().hex, vault_id, token_hash(refresh), f"OAuth refresh {client_id}", "refresh " + " ".join(scopes), stamp, stamp + 30 * 86400),
        )
    return {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh, "scope": " ".join(scopes)}


@router.post("/oauth/token")
def token_endpoint(
    grant_type: str = Form(), client_id: str = Form(), code: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None), redirect_uri: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
) -> JSONResponse:
    with connect() as db:
        if not db.execute("SELECT 1 FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone():
            raise HTTPException(401, "Unknown client")
    if grant_type == "authorization_code":
        if not code or not code_verifier or not redirect_uri:
            raise HTTPException(400, "code, code_verifier and redirect_uri are required")
        with connect() as db, transaction(db, immediate=True):
            row = db.execute("SELECT * FROM oauth_codes WHERE code_hash=?", (token_hash(code),)).fetchone()
            if not row or row["used_at"] or row["expires_at"] < now() or row["client_id"] != client_id or row["redirect_uri"] != redirect_uri or pkce_s256(code_verifier) != row["code_challenge"]:
                raise HTTPException(400, "Invalid or expired authorization code")
            db.execute("UPDATE oauth_codes SET used_at=? WHERE code_hash=?", (now(), token_hash(code)))
        return JSONResponse(_issue_oauth_tokens(row["vault_id"], client_id, row["scope"].split()), headers={"Cache-Control": "no-store"})
    if grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(400, "refresh_token is required")
        principal = authenticate_token(refresh_token)
        if "refresh" not in principal.scopes or principal.label != f"OAuth refresh {client_id}":
            raise HTTPException(400, "Invalid refresh token")
        with connect() as db:
            db.execute("UPDATE access_tokens SET revoked_at=? WHERE id=?", (now(), principal.token_id))
        scopes = [item for item in principal.scopes if item != "refresh"]
        return JSONResponse(_issue_oauth_tokens(principal.vault_id, client_id, scopes), headers={"Cache-Control": "no-store"})
    raise HTTPException(400, "Unsupported grant_type")
