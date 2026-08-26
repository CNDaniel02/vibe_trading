from __future__ import annotations

from typing import Any


HARD_VETO_REASON_CODES = {
    "critical_fact_conflict",
    "out_of_snapshot_evidence",
    "temporal_integrity_failure",
    "stale_decision_critical_evidence",
    "missing_required_primary_source",
    "mandate_horizon_invalid",
}

SOFT_CONCERN_CODES = {
    "uncertainty",
    "partial_price_in",
    "valuation",
    "secondary_evidence_gap",
    "chase_risk",
    "event_risk",
    "price_action_conflict",
    "incomplete_context",
    "other",
}

COOLDOWN_PROFILE_KEYS = {
    "deep_research_no_trade": "deep_research_no_trade_minutes",
    "hard_veto": "hard_veto_minutes",
    "watch": "watch_minutes",
    "active_plan": "active_plan_minutes",
    "executed_trade": "executed_trade_minutes",
}


def normalize_allocator_challenge(
    challenge: dict[str, Any],
    *,
    legacy_fail_closed: bool = True,
) -> dict[str, Any]:
    """Derive the executable veto from structured reasons, not model wording."""
    value = dict(challenge)
    structured = "hard_veto_reasons" in value or "soft_concerns" in value
    hard_reasons = list(value.get("hard_veto_reasons") or [])
    soft_concerns = list(value.get("soft_concerns") or [])

    if structured:
        for reason in hard_reasons:
            if not isinstance(reason, dict) or reason.get("code") not in HARD_VETO_REASON_CODES:
                raise ValueError("allocator Challenge returned an invalid hard-veto reason")
            if not str(reason.get("detail") or "").strip():
                raise ValueError("allocator Challenge hard-veto detail is required")
        for concern in soft_concerns:
            if not isinstance(concern, dict) or concern.get("code") not in SOFT_CONCERN_CODES:
                raise ValueError("allocator Challenge returned an invalid soft concern")
            if not str(concern.get("detail") or "").strip():
                raise ValueError("allocator Challenge soft-concern detail is required")
        hard_veto = bool(hard_reasons)
    else:
        # Existing persisted records lack reason codes. They remain fail-closed
        # and are never retroactively promoted into executable proposals.
        hard_veto = legacy_fail_closed and bool(value.get("veto_recommended", False))

    if hard_veto:
        recommendation = "no_trade"
        concern_level = "hard_veto"
    elif soft_concerns:
        recommendation = "reduce_confidence"
        concern_level = "soft_concern"
    else:
        recommendation = (
            "reduce_confidence"
            if value.get("recommendation") == "reduce_confidence"
            else "proceed"
        )
        concern_level = "none"

    return {
        **value,
        "hard_veto_reasons": hard_reasons,
        "soft_concerns": soft_concerns,
        "hard_veto": hard_veto,
        "veto_recommended": hard_veto,
        "recommendation": recommendation,
        "concern_level": concern_level,
    }


def allocator_decision_outcome(analysis: dict[str, Any]) -> str:
    if analysis.get("fail_closed") or analysis.get("guardrail_actions"):
        return "hard_veto"
    challenge = normalize_allocator_challenge(
        dict(analysis.get("challenge") or {}),
        legacy_fail_closed=True,
    )
    if challenge["hard_veto"]:
        return "hard_veto"
    action = str((analysis.get("signal") or {}).get("action") or "no_trade")
    if action == "propose_trade":
        return "active_plan"
    if action == "watch":
        return "watch"
    return "deep_research_no_trade"


def allocator_cooldown_minutes(profile: dict[str, Any], outcome: str) -> int:
    key = COOLDOWN_PROFILE_KEYS.get(outcome)
    if key is None:
        return 0
    cooldowns = profile.get("cooldowns", {})
    if not isinstance(cooldowns, dict):
        cooldowns = {}
    fallback = int(profile.get("event_cooldown_hours", 24)) * 60
    return max(0, int(cooldowns.get(key, fallback)))
