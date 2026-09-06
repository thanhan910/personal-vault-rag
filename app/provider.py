from __future__ import annotations

from typing import Sequence

import httpx
from PIL import Image
import voyageai

from .budget import BudgetUnavailable, complete, estimate_multimodal, estimate_rerank, estimate_text_embedding, mark_uncertain, release, reserve
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
    key = _key(vault_id)
    conservative_tokens = sum(max(1, len(text.encode("utf-8"))) for text in texts)
    reservation = reserve(vault_id, model, "text_embedding", conservative_tokens, estimate_text_embedding(conservative_tokens), request_key)
    call_started = False
    try:
        call_started = True
        response = httpx.post(
            f"{VOYAGE_URL}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": list(texts), "input_type": input_type, "output_dimension": dimensions, "truncation": False},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        vectors = body["data"]
        actual_tokens = int(body["usage"]["total_tokens"])
        complete(reservation.id, estimate_text_embedding(actual_tokens))
        return [item["embedding"] for item in sorted(vectors, key=lambda item: item["index"])]
    except Exception as exc:
        if call_started:
            mark_uncertain(reservation.id, f"{type(exc).__name__}: {exc}")
        else:
            release(reservation.id)
        raise


def rerank(vault_id: str, query: str, documents: Sequence[str], request_key: str, top_k: int) -> list[tuple[int, float]]:
    row = provider_settings(vault_id)
    model = row["rerank_model"]
    key = _key(vault_id)
    conservative_tokens = max(1, len(query.encode("utf-8"))) * len(documents) + sum(max(1, len(doc.encode("utf-8"))) for doc in documents)
    reservation = reserve(vault_id, model, "rerank", conservative_tokens, estimate_rerank(conservative_tokens), request_key)
    call_started = False
    try:
        call_started = True
        response = httpx.post(
            f"{VOYAGE_URL}/rerank",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "query": query, "documents": list(documents), "top_k": top_k, "truncation": False},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        complete(reservation.id, estimate_rerank(int(body["usage"]["total_tokens"])))
        return [(item["index"], item["relevance_score"]) for item in body["data"]]
    except Exception as exc:
        if call_started:
            mark_uncertain(reservation.id, f"{type(exc).__name__}: {exc}")
        else:
            release(reservation.id)
        raise


def embed_visual_inputs(vault_id: str, inputs: Sequence[list[object]], input_type: str, request_key: str) -> list[list[float]]:
    row = provider_settings(vault_id)
    model = row["visual_model"]
    key = _key(vault_id)
    text_tokens = 0
    charged_pixels = 0
    for parts in inputs:
        for part in parts:
            if isinstance(part, Image.Image):
                charged_pixels += min(2_000_000, max(50_000, part.width * part.height))
            elif isinstance(part, str):
                text_tokens += max(1, len(part.encode("utf-8")))
    reserved_microusd = estimate_multimodal(text_tokens, charged_pixels)
    reservation = reserve(vault_id, model, "multimodal_embedding", text_tokens + charged_pixels, reserved_microusd, request_key)
    call_started = False
    try:
        client = voyageai.Client(api_key=key)
        call_started = True
        result = client.multimodal_embed(inputs=list(inputs), model=model, input_type=input_type, truncation=False)
        complete(reservation.id, estimate_multimodal(result.text_tokens, result.image_pixels, result.video_pixels))
        return result.embeddings
    except Exception as exc:
        if call_started:
            mark_uncertain(reservation.id, f"{type(exc).__name__}: {exc}")
        else:
            release(reservation.id)
        raise
