from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from scripts.core.models import parse_ts


STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

AGENT_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "snapshot_id",
        "decision_time",
        "data_cutoff_time",
        "ticker",
        "market_session",
        "market_data",
        "technical_signals",
        "available_news",
        "source_metadata",
    ],
    "properties": {
        "snapshot_id": {"type": "string", "minLength": 1},
        "decision_time": {"type": "string", "format": "date-time"},
        "data_cutoff_time": {"type": "string", "format": "date-time"},
        "ticker": {"type": "string", "pattern": "^[A-Z][A-Z0-9.-]{0,9}$"},
        "market_session": {"enum": ["pre_market", "regular", "after_hours", "closed"]},
        "market_data": {"type": "object"},
        "technical_signals": {"type": "object"},
        "available_news": {"type": "array", "items": {"type": "object"}},
        "source_metadata": {"type": "array", "items": {"type": "object"}},
        "agent_context": {"type": "object"},
    },
}

NEWS_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "headline",
        "published_at",
        "first_seen_at",
        "source",
        "source_tier",
        "ticker_relevance",
        "direction",
        "novelty",
        "already_priced_in",
        "confidence",
    ],
    "properties": {
        "headline": {"type": "string"},
        "published_at": {"type": "string", "format": "date-time"},
        "first_seen_at": {"type": "string", "format": "date-time"},
        "source": {"type": "string"},
        "source_tier": {"type": "integer", "minimum": 1, "maximum": 4},
        "ticker_relevance": {"type": "number", "minimum": 0, "maximum": 1},
        "direction": {"enum": ["positive", "negative", "neutral", "mixed"]},
        "novelty": {"type": "number", "minimum": 0, "maximum": 1},
        "already_priced_in": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

NEWS_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "events",
        "published_at",
        "first_seen_at",
        "source",
        "source_tier",
        "ticker_relevance",
        "direction",
        "novelty",
        "already_priced_in",
        "confidence",
        "data_gaps",
    ],
    "properties": {
        "events": {"type": "array", "items": NEWS_EVENT_SCHEMA},
        "published_at": {"type": ["string", "null"], "format": "date-time"},
        "first_seen_at": {"type": ["string", "null"], "format": "date-time"},
        "source": {"type": ["string", "null"]},
        "source_tier": {"type": ["integer", "null"], "minimum": 1, "maximum": 4},
        "ticker_relevance": {"type": "number", "minimum": 0, "maximum": 1},
        "direction": {"enum": ["positive", "negative", "neutral", "mixed", "none"]},
        "novelty": {"type": "number", "minimum": 0, "maximum": 1},
        "already_priced_in": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "data_gaps": STRING_ARRAY,
    },
}

CHALLENGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "objections",
        "contradictions",
        "missing_evidence",
        "stale_evidence",
        "chase_risk",
        "event_risk",
        "recommendation",
        "confidence_adjustment",
        "veto_recommended",
    ],
    "properties": {
        "objections": STRING_ARRAY,
        "contradictions": STRING_ARRAY,
        "missing_evidence": STRING_ARRAY,
        "stale_evidence": STRING_ARRAY,
        "chase_risk": {"enum": ["low", "medium", "high"]},
        "event_risk": {"enum": ["low", "medium", "high"]},
        "recommendation": {"enum": ["proceed", "reduce_confidence", "no_trade"]},
        "confidence_adjustment": {"type": "number", "minimum": -1, "maximum": 0.25},
        "veto_recommended": {"type": "boolean"},
    },
}

DECISION_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action",
        "ticker",
        "thesis",
        "supporting_evidence",
        "contrary_evidence",
        "entry_condition",
        "invalidation_condition",
        "exit_condition",
        "confidence",
        "max_holding_period",
        "no_trade_reason",
    ],
    "properties": {
        "action": {"enum": ["buy", "hold", "exit", "no_trade"]},
        "ticker": {"type": "string"},
        "thesis": {"type": "string"},
        "supporting_evidence": STRING_ARRAY,
        "contrary_evidence": STRING_ARRAY,
        "entry_condition": {"type": "string"},
        "invalidation_condition": {"type": "string"},
        "exit_condition": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "max_holding_period": {"type": "string"},
        "no_trade_reason": {"type": ["string", "null"]},
    },
}

CANDIDATE_EXTRACTOR_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates", "data_gaps"],
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ticker", "company_name", "event_indices", "discovery_score", "reason"],
                "properties": {
                    "ticker": {"type": "string", "pattern": "^[A-Z][A-Z0-9.-]{0,9}$"},
                    "company_name": {"type": ["string", "null"]},
                    "event_indices": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                    "discovery_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
            },
        },
        "data_gaps": STRING_ARRAY,
    },
}

CANDIDATE_RANKING_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["ranked_candidates", "data_gaps"],
    "properties": {
        "ranked_candidates": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "ticker",
                    "score",
                    "direction",
                    "catalyst_strength",
                    "evidence_quality",
                    "market_confirmation",
                    "instrument_preference",
                    "rationale",
                    "risk_flags",
                ],
                "properties": {
                    "ticker": {"type": "string", "pattern": "^[A-Z][A-Z0-9.-]{0,9}$"},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "direction": {"enum": ["bullish", "bearish", "mixed", "unclear"]},
                    "catalyst_strength": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_quality": {"type": "number", "minimum": 0, "maximum": 1},
                    "market_confirmation": {"type": "number", "minimum": 0, "maximum": 1},
                    "instrument_preference": {"enum": ["equity", "call", "put", "none"]},
                    "rationale": {"type": "string"},
                    "risk_flags": STRING_ARRAY,
                },
            },
        },
        "data_gaps": STRING_ARRAY,
    },
}

CATALYST_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ticker",
        "catalyst_summary",
        "direction",
        "materiality",
        "event_time",
        "event_time_basis",
        "bull_case",
        "supporting_facts",
        "source_urls",
        "assumptions",
        "data_gaps",
        "already_priced_in",
        "confidence",
        "instrument_preference",
    ],
    "properties": {
        "ticker": {"type": "string"},
        "catalyst_summary": {"type": "string"},
        "direction": {"enum": ["positive", "negative", "mixed", "unclear"]},
        "materiality": {"type": "number", "minimum": 0, "maximum": 1},
        "event_time": {"type": ["string", "null"], "format": "date-time"},
        "event_time_basis": {"enum": ["source_explicit", "model_inference", "unknown"]},
        "bull_case": {"type": "string"},
        "supporting_facts": STRING_ARRAY,
        "source_urls": {"type": "array", "items": {"type": "string"}},
        "assumptions": STRING_ARRAY,
        "data_gaps": STRING_ARRAY,
        "already_priced_in": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "instrument_preference": {"enum": ["equity", "call", "put", "none"]},
    },
}

CATALYST_DECISION_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action",
        "instrument",
        "ticker",
        "thesis",
        "supporting_evidence",
        "contrary_evidence",
        "entry_condition",
        "invalidation_condition",
        "exit_condition",
        "confidence",
        "max_holding_period",
        "option_preference",
        "no_trade_reason",
    ],
    "properties": {
        "action": {"enum": ["buy", "buy_to_open", "no_trade"]},
        "instrument": {"enum": ["equity", "call", "put", "none"]},
        "ticker": {"type": "string"},
        "thesis": {"type": "string"},
        "supporting_evidence": STRING_ARRAY,
        "contrary_evidence": STRING_ARRAY,
        "entry_condition": {"type": "string"},
        "invalidation_condition": {"type": "string"},
        "exit_condition": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "max_holding_period": {"type": "string"},
        "option_preference": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["target_dte", "target_abs_delta"],
            "properties": {
                "target_dte": {"type": "integer", "minimum": 1, "maximum": 365},
                "target_abs_delta": {"type": "number", "minimum": 0.05, "maximum": 0.95},
            },
        },
        "no_trade_reason": {"type": ["string", "null"]},
    },
}

OUTPUT_SCHEMAS = {
    "news_agent": NEWS_OUTPUT_SCHEMA,
    "challenge_agent": CHALLENGE_OUTPUT_SCHEMA,
    "decision_manager": DECISION_OUTPUT_SCHEMA,
    "catalyst_candidate_extractor": CANDIDATE_EXTRACTOR_OUTPUT_SCHEMA,
    "catalyst_ranker": CANDIDATE_RANKING_OUTPUT_SCHEMA,
    "catalyst_bull_news_agent": CATALYST_RESEARCH_OUTPUT_SCHEMA,
    "catalyst_challenge_agent": CHALLENGE_OUTPUT_SCHEMA,
    "catalyst_decision_manager": CATALYST_DECISION_OUTPUT_SCHEMA,
}


def validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)


def validate_agent_input(data: dict[str, Any]) -> None:
    validate_schema(data, AGENT_INPUT_SCHEMA)
    decision_time = parse_ts(data["decision_time"])
    cutoff = parse_ts(data["data_cutoff_time"])
    if cutoff > decision_time:
        raise ValueError("data_cutoff_time cannot be after decision_time")
    quote = data["market_data"].get("quote")
    if quote and quote.get("asof") and parse_ts(quote["asof"]) > cutoff:
        raise ValueError("quote timestamp exceeds data cutoff")
    for item in data["available_news"]:
        if item.get("published_at") and parse_ts(item["published_at"]) > cutoff:
            raise ValueError("news publication timestamp exceeds data cutoff")
        if item.get("first_seen_at") and parse_ts(item["first_seen_at"]) > decision_time:
            raise ValueError("news first_seen_at exceeds decision time")


def schema_for_provider(schema: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(schema)
    result.pop("$schema", None)
    return result
