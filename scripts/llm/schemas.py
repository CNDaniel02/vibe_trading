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

OUTPUT_SCHEMAS = {
    "news_agent": NEWS_OUTPUT_SCHEMA,
    "challenge_agent": CHALLENGE_OUTPUT_SCHEMA,
    "decision_manager": DECISION_OUTPUT_SCHEMA,
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
