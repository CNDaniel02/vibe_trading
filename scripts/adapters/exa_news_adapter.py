from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from scripts.adapters.errors import AdapterConfigurationError, AdapterDataError
from scripts.adapters.http_json import request_json
from scripts.core.models import parse_ts, utc_now


class ExaNewsAdapter:
    """Read-only Exa search adapter that returns grounded news evidence."""

    TIER_ONE_DOMAINS = {
        "sec.gov",
        "investor.gov",
        "federalreserve.gov",
        "justice.gov",
        "ftc.gov",
        "fda.gov",
        "federalregister.gov",
    }
    TIER_TWO_DOMAINS = {"reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "ft.com", "cnbc.com"}

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.api_key_env = str(config.get("api_key_env", "EXA_API_KEY"))

    def readiness(self) -> dict[str, Any]:
        missing = [] if os.getenv(self.api_key_env) else [self.api_key_env]
        return {"ready": bool(self.config.get("enabled", False)) and not missing, "enabled": bool(self.config.get("enabled", False)), "missing_env": missing}

    def search(self, ticker: str, decision_time: str, company_name: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        query_name = company_name.strip() if company_name else ticker.upper()
        return self.search_query(
            f"{query_name} {ticker.upper()} stock company latest material news catalyst earnings filing",
            decision_time,
            ticker=ticker,
        )

    def search_market_events(self, decision_time: str, queries: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        max_searches = int(self.config.get("max_market_searches", len(queries)))
        events: list[dict[str, Any]] = []
        metadata: list[dict[str, Any]] = []
        for query in queries[:max_searches]:
            found, sources = self.search_query(query, decision_time)
            events.extend(found)
            metadata.extend(sources)
        return self._deduplicate(events), self._deduplicate_sources(metadata)

    def search_query(
        self,
        query: str,
        decision_time: str,
        *,
        ticker: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        readiness = self.readiness()
        if not readiness["enabled"]:
            raise AdapterConfigurationError("Exa news adapter is disabled")
        if readiness["missing_env"]:
            raise AdapterConfigurationError(f"missing Exa environment variable: {self.api_key_env}")
        cutoff = parse_ts(decision_time)
        start = cutoff - timedelta(hours=float(self.config.get("lookback_hours", 48)))
        body: dict[str, Any] = {
            "query": query,
            "type": str(self.config.get("search_type", "fast")),
            "category": "news",
            "numResults": int(self.config.get("num_results", 6)),
            "startPublishedDate": start.isoformat(),
            "endPublishedDate": cutoff.isoformat(),
            "contents": {"highlights": {"maxCharacters": 1200}},
            "systemPrompt": "Prefer primary company, regulator, filing, and reputable wire sources. Avoid duplicate syndicated stories.",
        }
        if self.config.get("include_domains"):
            body["includeDomains"] = list(self.config["include_domains"])
        if self.config.get("exclude_domains"):
            body["excludeDomains"] = list(self.config["exclude_domains"])
        response = request_json(
            str(self.config.get("endpoint", "https://api.exa.ai/search")),
            method="POST",
            headers={"x-api-key": str(os.environ[self.api_key_env])},
            payload=body,
            timeout_seconds=float(self.config.get("timeout_seconds", 30)),
            max_retries=int(self.config.get("max_retries", 2)),
        )
        retrieved_at = parse_ts(utc_now()).isoformat()
        events: list[dict[str, Any]] = []
        sources: dict[str, dict[str, Any]] = {}
        for item in response.get("results", []):
            if not isinstance(item, dict):
                continue
            published_at = item.get("publishedDate") or item.get("published_at")
            if not published_at:
                continue
            published = parse_ts(str(published_at))
            if published > cutoff:
                continue
            event_at = self._optional_timestamp(
                item.get("eventDate") or item.get("event_at") or item.get("eventTime"),
                cutoff,
            )
            url = self._canonical_url(str(item.get("url", "")))
            domain = self._domain(url)
            source_tier = self._source_tier(domain)
            age_hours = max(0.0, (cutoff - published).total_seconds() / 3600)
            novelty = max(0.0, min(1.0, 1 - age_hours / max(1.0, float(self.config.get("lookback_hours", 48)))))
            headline = str(item.get("title") or "").strip()
            if not headline:
                continue
            events.append(
                {
                    "headline": headline,
                    "ticker": ticker.upper() if ticker else None,
                    "published_at": published.isoformat(),
                    "event_at": event_at,
                    "first_seen_at": retrieved_at,
                    "retrieved_at": retrieved_at,
                    "source": domain or "unknown",
                    "source_tier": source_tier,
                    "ticker_relevance": 0.7,
                    "direction": "neutral",
                    "novelty": round(novelty, 3),
                    "already_priced_in": False,
                    "confidence": 0.55 if source_tier <= 2 else 0.4,
                    "url": url,
                    "highlights": item.get("highlights", []),
                }
            )
            sources[url or domain or "unknown"] = {
                "source": domain or "unknown",
                "source_tier": source_tier,
                "url": url,
                "retrieved_at": retrieved_at,
            }
        if response.get("results") and not events:
            raise AdapterDataError("Exa results had no publication timestamps usable at the cutoff")
        return self._deduplicate(events), list(sources.values())

    @staticmethod
    def _optional_timestamp(value: Any, cutoff: datetime) -> str | None:
        if not value:
            return None
        try:
            parsed = parse_ts(str(value))
        except (TypeError, ValueError):
            return None
        return parsed.isoformat() if parsed <= cutoff else None

    @staticmethod
    def _canonical_url(url: str) -> str:
        if not url:
            return ""
        parts = urlsplit(url.strip())
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))

    @staticmethod
    def _deduplicate(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for event in events:
            key = (
                str(event.get("url", "")),
                str(event.get("published_at", "")),
                str(event.get("headline", "")).strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
        return unique

    @staticmethod
    def _deduplicate_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for source in sources:
            key = str(source.get("url") or source.get("source") or "unknown")
            unique[key] = source
        return list(unique.values())

    @staticmethod
    def _domain(url: str) -> str:
        host = urlparse(url).hostname or ""
        return host.lower().removeprefix("www.")

    @classmethod
    def _source_tier(cls, domain: str) -> int:
        if any(domain == item or domain.endswith("." + item) for item in cls.TIER_ONE_DOMAINS):
            return 1
        if any(domain == item or domain.endswith("." + item) for item in cls.TIER_TWO_DOMAINS):
            return 2
        return 3 if domain else 4
