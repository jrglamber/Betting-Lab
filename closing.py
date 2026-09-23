from __future__ import annotations

from typing import Any, Dict

from db import Database
from research import finalize_closing_lines


def update_closing_lines(db: Database) -> int:
    # Backwards-compatible route name; semantics corrected in v0.5.
    return finalize_closing_lines(db)


def settle_signal(
    db: Database,
    signal_id: int,
    result: str,
) -> Dict[str, Any]:
    result = result.upper()
    if result not in {"WIN", "LOSS", "PUSH", "VOID"}:
        raise ValueError("result must be WIN, LOSS, PUSH or VOID")
    sig = db.fetchone("SELECT * FROM signals WHERE id=?", (signal_id,))
    if not sig:
        raise ValueError("signal not found")

    if result == "WIN":
        pnl = float(sig["offered_odds"]) - 1.0
    elif result == "LOSS":
        pnl = -1.0
    else:
        pnl = 0.0

    db.execute(
        """
        UPDATE signals
        SET result=?, pnl_units=?, status='SETTLED'
        WHERE id=?
        """,
        (result, pnl, signal_id),
    )
    return {"signal_id": signal_id, "result": result, "pnl_units": pnl}
