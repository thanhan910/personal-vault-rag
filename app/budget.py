from __future__ import annotations

import uuid
from datetime import datetime, timezone
from dataclasses import dataclass

from .db import connect, now, transaction

MICRO_USD_PER_M_TEXT_EMBED = 120_000
MICRO_USD_PER_M_RERANK = 50_000


@dataclass
class Reservation:
    id: str
    microusd: int


class BudgetUnavailable(RuntimeError):
    pass


def estimate_text_embedding(tokens: int) -> int:
    return max(1, (tokens * MICRO_USD_PER_M_TEXT_EMBED + 999_999) // 1_000_000)


def estimate_rerank(tokens: int) -> int:
    return max(1, (tokens * MICRO_USD_PER_M_RERANK + 999_999) // 1_000_000)


def reserve(vault_id: str, model: str, operation: str, units: int, microusd: int, request_key: str) -> Reservation:
    stamp = now()
    current = datetime.fromtimestamp(stamp, timezone.utc)
    month_start = datetime(current.year, current.month, 1, tzinfo=timezone.utc).timestamp()
    with connect() as db, transaction(db, immediate=True):
        existing = db.execute(
            "SELECT id,reserved_microusd,state FROM usage_ledger WHERE vault_id=? AND request_key=?",
            (vault_id, request_key),
        ).fetchone()
        if existing:
            return Reservation(existing["id"], existing["reserved_microusd"])
        vault = db.execute("SELECT monthly_budget_microusd FROM vaults WHERE id=?", (vault_id,)).fetchone()
        spent = db.execute(
            """SELECT COALESCE(SUM(COALESCE(actual_microusd,reserved_microusd)),0) amount
               FROM usage_ledger WHERE vault_id=? AND created_at>=? AND state IN ('reserved','complete')""",
            (vault_id, month_start),
        ).fetchone()["amount"]
        if not vault or vault["monthly_budget_microusd"] <= 0:
            raise BudgetUnavailable("Paid provider calls are disabled; set a monthly allowance in Vault Settings")
        if spent + microusd > vault["monthly_budget_microusd"]:
            raise BudgetUnavailable("This call would exceed the vault's local monthly spending allowance")
        reservation_id = "use_" + uuid.uuid4().hex
        db.execute(
            """INSERT INTO usage_ledger(id,vault_id,provider,model,operation,estimated_units,reserved_microusd,state,created_at,request_key)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (reservation_id, vault_id, "voyage", model, operation, units, microusd, "reserved", stamp, request_key),
        )
    return Reservation(reservation_id, microusd)


def complete(reservation_id: str, actual_microusd: int | None = None) -> None:
    with connect() as db:
        db.execute(
            """UPDATE usage_ledger SET state='complete',actual_microusd=COALESCE(?,reserved_microusd),completed_at=? WHERE id=?""",
            (actual_microusd, now(), reservation_id),
        )


def release(reservation_id: str) -> None:
    with connect() as db:
        db.execute("UPDATE usage_ledger SET state='released',completed_at=? WHERE id=?", (now(), reservation_id))
