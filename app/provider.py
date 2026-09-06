from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence

import httpx
from PIL import Image
import voyageai

from .budget import BudgetUnavailable, complete, estimate_rerank, estimate_text_embedding, release, reserve
from .db import connect
from .security import decrypt_secret

VOYAGE_URL = "https://api.voyageai.com/v1"


def provider_settings(vault_id: str):
    with connect() as db:
        return db.execute(
            """SELECT provider_ciphertext,embedding_model,embedding_dimensions,visual_model,rerank_model
               FROM vaults WHERE id=?""", (vault_id,)
        ).fetchone()


def _key(vault_id: str) -> str:
    row = provider_settings(vault_id)
    key = decrypt_secret(row["provider_ciphertext"] if row else None)
    if not key:
        raise BudgetUnavailable("Voyage API key is not configured for this vault")
    return key


def embed_texts(vault_id: str, texts: Sequence[str], input_type: str, request_key: str) -> list[list[float]]:
    row = provider_settings(vault_id)
    model, dimensions = row["embedding_model"], row["embedding_dimensions"]
    tokens = sum(max(1, len(text) // 4) for text in texts)
    reservation = reserve(vault_id, model, "text_embedding", tokens, estimate_text_embedding(tokens), request_key)
    try:
        response = httpx.post(
            f"{VOYAGE_URL}/embeddings",
            headers={"Authorization": f"Bearer {_key(vault_id)}"},
            json={"model": model, "input": list(texts), "input_type": input_type, "output_dimension": dimensions, "truncation": False},
            timeout=60,
        )
        response.raise_for_status()
        vectors = response.json()["data"]
        complete(reservation.id)
        return [item["embedding"] for item in sorted(vectors, key=lambda item: item["index"])]
    except Exception:
        release(reservation.id)
        raise


def rerank(vault_id: str, query: str, documents: Sequence[str], request_key: str, top_k: int) -> list[tuple[int, float]]:
    row = provider_settings(vault_id)
    model = row["rerank_model"]
    tokens = max(1, len(query) // 4) * len(documents) + sum(max(1, len(doc) // 4) for doc in documents)
    reservation = reserve(vault_id, model, "rerank", tokens, estimate_rerank(tokens), request_key)
    try:
        response = httpx.post(
            f"{VOYAGE_URL}/rerank",
            headers={"Authorization": f"Bearer {_key(vault_id)}"},
            json={"model": model, "query": query, "documents": list(documents), "top_k": top_k, "truncation": False},
            timeout=60,
        )
        response.raise_for_status()
        complete(reservation.id)
        return [(item["index"], item["relevance_score"]) for item in response.json()["data"]]
    except Exception:
        release(reservation.id)
        raise


def embed_visual_inputs(vault_id: str, inputs: Sequence[list[object]], input_type: str, request_key: str) -> list[list[float]]:
    row = provider_settings(vault_id)
    model = row["visual_model"]
    pixel_count = 0
    for parts in inputs:
        for part in parts:
            if isinstance(part, Image.Image):
                pixel_count += part.width * part.height
    # Current list price is $0.0006 per 1M pixels up to the documented cap.
    reserved_microusd = max(1, (pixel_count * 600 + 999_999) // 1_000_000)
    reservation = reserve(vault_id, model, "multimodal_embedding", pixel_count, reserved_microusd, request_key)
    try:
        client = voyageai.Client(api_key=_key(vault_id))
        result = client.multimodal_embed(inputs=list(inputs), model=model, input_type=input_type, truncation=False)
        complete(reservation.id)
        return result.embeddings
    except Exception:
        release(reservation.id)
        raise
