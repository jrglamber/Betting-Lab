from __future__ import annotations

"""Safety guards for adaptive_winner_research model freezing.

The adaptive bootstrap can come up before CLV-First or TPRED4 historical rows have
finished their first population pass. Never freeze an empty/partial historical
model merely because startup won the race. Retraining is allowed only when more
pre-forward rows become available; the forward cutoff itself never moves.
"""

import adaptive_winner_research as core

VERSION = "ADAPTIVE_WINNER_RESEARCH_SAFETY_V1"
_INSTALLED = False


def install(db):
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    original_w1_model = core._w1_model
    original_tennis_w1_model = core._tennis_w1_model

    def guarded_w1_model(db_arg, start):
        model = original_w1_model(db_arg, start)
        try:
            eligible = sum(
                1 for row in core._all_w1_candidates(db_arg)
                if (core._dt(row.get("created_at")) or start) < start
                and row.get("result") in {"WIN", "LOSS"}
            )
            if eligible > int(model.get("training_samples") or 0):
                model = core._train_w1(db_arg, start)
        except Exception:
            pass
        return model

    def guarded_tennis_w1_model(db_arg, start):
        model = original_tennis_w1_model(db_arg, start)
        try:
            row = db_arg.fetchone(
                """SELECT COUNT(*) AS n FROM tennis_predictive4_walkforward
                   WHERE model_version=? AND evidence_ready=1 AND commence_time<?""",
                (core.TPRED4_MODEL_VERSION, start.isoformat()),
            ) or {}
            eligible = int(row.get("n") or 0)
            if eligible > int(model.get("training_samples") or 0):
                model = core._train_tennis_w1(db_arg, start)
        except Exception:
            pass
        return model

    core._w1_model = guarded_w1_model
    core._tennis_w1_model = guarded_tennis_w1_model
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "forward_cutoffs_move": False,
        "retrain_condition": "new_pre_forward_history_only",
        "betting_authority": False,
    }
