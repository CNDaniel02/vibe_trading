from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from scripts.adapters.errors import AdapterError
from scripts.adapters.exa_news_adapter import ExaNewsAdapter
from scripts.adapters.robinhood_discovery_adapter import RobinhoodDiscoveryAdapter
from scripts.agents.news_drift_headline_agent import NewsDriftHeadlineAgent
from scripts.core.audit import append_jsonl
from scripts.core.models import Quote, parse_ts, utc_now
from scripts.discovery.evidence_store import EvidenceSnapshotStore
from scripts.llm.base_provider import LLMProvider, ProviderError
from scripts.llm.usage_tracker import UsageTracker
from scripts.news_drift.event_store import NewsEventStore
from scripts.runtime.market_clock import UsEquityMarketClock
from scripts.simulation.fill_model import adverse_slippage


class NewsDriftPipeline:
    """Price-blind news discovery followed by deterministic shadow screening."""

    STRATEGY = "llm_news_drift_v1"
    LABEL_HORIZONS = {"plus_1m": 1, "plus_5m": 5, "plus_15m": 15}

    def __init__(
        self,
        root: str | Path,
        config: dict[str, Any],
        provider: LLMProvider,
        tracker: UsageTracker,
        *,
        discovery_adapter: RobinhoodDiscoveryAdapter | None = None,
        news_adapter: ExaNewsAdapter | None = None,
        clock: UsEquityMarketClock | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config
        self.profile = config.get("strategies", {}).get(self.STRATEGY, {})
        integrations = config.get("integrations", {})
        exa_config = dict(integrations.get("forward_data", {}).get("exa", {}))
        exa_config["lookback_hours"] = self.profile.get(
            "lookback_hours", exa_config.get("lookback_hours", 24)
        )
        exa_config["max_market_searches"] = int(
            self.profile.get("max_searches_per_cycle", 1)
        )
        discovery_config = {
            "minimum_market_cap_usd": self.profile.get("minimum_market_cap_usd", 0),
            "minimum_average_daily_volume_usd": self.profile.get(
                "minimum_average_daily_volume_usd", 0
            ),
            "maximum_spread_bps": self.profile.get("maximum_spread_bps", 50),
            "excluded_symbols": config.get("universe", {}).get("excluded_symbols", []),
        }
        self.discovery = discovery_adapter or RobinhoodDiscoveryAdapter(
            integrations.get("robinhood_mcp", {}), discovery_config, self.root
        )
        self.news = news_adapter or ExaNewsAdapter(exa_config)
        self.agent = NewsDriftHeadlineAgent(config, provider, tracker)
        self.tracker = tracker
        self.clock = clock or UsEquityMarketClock()
        self.evidence = EvidenceSnapshotStore(self.root, namespace="news_drift")
        self.database = self.root / "state" / "news_events.sqlite"

    def readiness(self) -> dict[str, Any]:
        return {
            "ready": bool(self.profile.get("enabled", False))
            and self.profile.get("execution") == "shadow_only"
            and self.news.readiness().get("ready", False)
            and self.discovery.readiness().get("ready", False),
            "enabled": bool(self.profile.get("enabled", False)),
            "execution": self.profile.get("execution"),
            "news": self.news.readiness(),
            "market_data": self.discovery.readiness(),
        }

    def run(self, now: str | None = None) -> dict[str, Any]:
        cycle_time = now or utc_now()
        clock = self.clock.status(cycle_time)
        calls_before = len(self.tracker.records)
        labels = self.resolve_due_labels(cycle_time)
        if not self.profile.get("enabled", False):
            return self._result("news_drift_skipped", cycle_time, reason="strategy disabled", labels=labels)
        if self.profile.get("execution") != "shadow_only":
            return self._result("news_drift_failed_closed", cycle_time, reason="execution must be shadow_only", labels=labels)
        if clock.market_session not in set(self.profile.get("allowed_market_sessions", [])):
            return self._result(
                "news_drift_skipped",
                cycle_time,
                reason=f"market session is {clock.market_session}",
                labels=labels,
                clock=clock.to_dict(),
            )
        if not self._inside_news_window(clock, cycle_time):
            return self._result(
                "news_drift_skipped",
                cycle_time,
                reason="outside configured premarket/after-hours discovery window",
                labels=labels,
                clock=clock.to_dict(),
            )

        queries = list(self.profile.get("market_queries", []))
        if not queries:
            return self._result("news_drift_failed_closed", cycle_time, reason="no market queries configured", labels=labels)
        retried_screened, retried_proposals = self._retry_transient_signals(
            clock.market_session,
            cycle_time,
            now is not None,
        )
        discovery_interval = max(1, int(self.profile.get("discovery_interval_seconds", 900)))
        if not self.evidence.claim_search_window("market_discovery", cycle_time, discovery_interval):
            return self._result(
                "news_drift_idle",
                cycle_time,
                strategy=self.STRATEGY,
                execution="shadow_only",
                reason="discovery cooldown active",
                labels=labels,
                screened=retried_screened,
                proposals=retried_proposals,
                model_calls=0,
            )

        cycle_id = f"ndr_{uuid4().hex}"
        query_index = int(parse_ts(cycle_time).timestamp() // discovery_interval) % len(queries)
        query = queries[query_index]
        try:
            raw_events, source_metadata = self.news.search_market_events(cycle_time, [query])
            normalized = self.evidence.normalize_events(raw_events)
            snapshot = self.evidence.write_snapshot(
                snapshot_type="raw",
                decision_time=cycle_time,
                payload={
                    "strategy": self.STRATEGY,
                    "cycle_id": cycle_id,
                    "query": query,
                    "events": normalized,
                    "source_metadata": source_metadata,
                },
            )
            unseen = self.evidence.unsent_model_events(
                "headline_agent",
                normalized,
                cycle_time,
                event_cooldown_hours=int(self.profile.get("event_cooldown_hours", 24)),
            )[: int(self.profile.get("max_events_per_cycle", 12))]
            if not unseen:
                return self._complete(
                    cycle_id,
                    cycle_time,
                    query,
                    snapshot,
                    normalized,
                    [],
                    retried_screened,
                    retried_proposals,
                    labels,
                    calls_before,
                )
            with NewsEventStore(self.database) as store:
                recent_events = store.list_events()[-20:]
            analysis = self.agent.analyze(
                snapshot_id=cycle_id,
                decision_time=cycle_time,
                events=unseen,
                recent_events=recent_events,
            )
            signal_time = cycle_time if now is not None else utc_now()
            signals = self._persist_signals(unseen, analysis, signal_time)
            self.evidence.mark_model_events_sent("headline_agent", unseen, signal_time)
            screened, proposals = self._screen_signals(signals, signal_time, now is not None)
            screened = retried_screened + screened
            proposals = retried_proposals + proposals
        except (AdapterError, ProviderError, RuntimeError, ValueError, OSError) as exc:
            result = self._result(
                "news_drift_failed_closed",
                cycle_time,
                cycle_id=cycle_id,
                stage="discovery_or_classification",
                reason=f"{type(exc).__name__}: {exc}",
                labels=labels,
                model_calls=len(self.tracker.records) - calls_before,
            )
            append_jsonl(self.root, "news_drift_cycles.jsonl", result)
            return result

        return self._complete(
            cycle_id,
            cycle_time,
            query,
            snapshot,
            normalized,
            signals,
            screened,
            proposals,
            labels,
            calls_before,
        )

    def _persist_signals(
        self,
        events: list[dict[str, Any]],
        analysis: dict[str, Any],
        signal_time: str,
    ) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []
        with NewsEventStore(self.database) as store:
            for signal in analysis.get("signals", []):
                ticker = str(signal.get("ticker") or "").upper()
                if not ticker:
                    continue
                raw = {**events[int(signal["event_index"])], "ticker": ticker}
                relation_type, related_id = self._validated_relation(store, ticker, signal)
                if signal.get("relation_type") == "duplicate" and related_id is None:
                    continue
                ingested = store.ingest_event(
                    raw,
                    event_type=relation_type,
                    related_event_id=related_id,
                    ingested_at=signal_time,
                )
                if not ingested["trigger_signal"]:
                    continue
                payload = {
                    **signal,
                    "strategy": self.STRATEGY,
                    "published_at": raw.get("published_at"),
                    "published_at_precision": raw.get("published_at_precision", "datetime"),
                    "first_seen_at": raw.get("first_seen_at"),
                    "event_at": raw.get("event_at"),
                    "source": raw.get("source"),
                    "source_tier": raw.get("source_tier"),
                    "url": raw.get("url"),
                }
                signal_record = store.record_signal(
                    ingested["event_id"],
                    signal_time=signal_time,
                    decision_time=signal_time,
                    payload=payload,
                    signal_type="headline_drift",
                    model=str(analysis.get("model", "unknown")),
                    prompt_version=self.agent.prompt_version,
                    direction=str(signal.get("direction")),
                    action="observe",
                    confidence=float(signal.get("confidence", 0)),
                )
                item = {
                    **payload,
                    "event_id": ingested["event_id"],
                    "signal_id": signal_record["signal_id"],
                    "signal_time": signal_time,
                }
                append_jsonl(self.root, "news_drift_signals.jsonl", item)
                saved.append(item)
        return saved

    def _inside_news_window(self, clock: Any, decision_time: str) -> bool:
        if clock.market_session == "regular":
            return True
        now = parse_ts(decision_time)
        if clock.market_session == "pre_market" and clock.open_time:
            minutes = (parse_ts(clock.open_time) - now).total_seconds() / 60
            return 0 < minutes <= float(self.profile.get("premarket_window_minutes", 120))
        if clock.market_session == "after_hours" and clock.close_time:
            minutes = (now - parse_ts(clock.close_time)).total_seconds() / 60
            return 0 <= minutes <= float(self.profile.get("after_hours_window_minutes", 120))
        return False

    def _retry_transient_signals(
        self,
        market_session: str,
        decision_time: str,
        deterministic_time: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if market_session != "regular":
            return [], []
        with NewsEventStore(self.database) as store:
            pending = store.list_unproposed_signals(
                limit=int(self.profile.get("max_events_per_cycle", 12))
            )
        transient = (
            "stale quote",
            "missing market context",
            "quote timestamp exceeds decision cutoff",
        )
        retryable = [
            item
            for item in pending
            if item.get("latest_rejection_reason") is None
            or str(item["latest_rejection_reason"]).startswith(transient)
        ]
        return self._screen_signals(retryable, decision_time, deterministic_time) if retryable else ([], [])

    @staticmethod
    def _validated_relation(
        store: NewsEventStore,
        ticker: str,
        signal: dict[str, Any],
    ) -> tuple[str, str | None]:
        relation = str(signal.get("relation_type", "new_event"))
        related_id = signal.get("related_event_id")
        related = store.get_event(str(related_id)) if related_id else None
        if related is None or str(related.get("ticker")) != ticker:
            return "new_event", None
        return relation, str(related_id)

    def _screen_signals(
        self,
        signals: list[dict[str, Any]],
        decision_time: str,
        deterministic_time: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        tickers = sorted({item["ticker"] for item in signals})
        valid: dict[str, dict[str, Any]] = {}
        rejected: dict[str, str] = {}
        for ticker in tickers:
            instrument = self.discovery.validate_instrument(ticker)
            if instrument.get("valid", False):
                valid[ticker] = instrument
            else:
                rejected[ticker] = str(instrument.get("reason", "instrument validation failed"))
        contexts = self.discovery.fetch_market_context(list(valid), decision_time) if valid else {}
        intraday = self._intraday_context(signals, list(contexts), decision_time)
        screened: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        for signal in signals:
            final_time = decision_time if deterministic_time else utc_now()
            context = contexts.get(signal["ticker"])
            verdict = self._tradability_verdict(
                signal,
                context,
                intraday.get(signal["ticker"], []),
                final_time,
                rejected.get(signal["ticker"]),
            )
            with NewsEventStore(self.database) as store:
                quote = verdict.get("quote", {})
                store.record_tradability_observation(
                    event_id=signal["event_id"],
                    ticker=signal["ticker"],
                    observed_at=final_time,
                    bid=quote.get("bid"),
                    ask=quote.get("ask"),
                    last=quote.get("last"),
                    spread_bps=verdict.get("spread_bps"),
                    volume=(context or {}).get("fundamentals", {}).get("average_daily_volume_usd"),
                    is_tradable=verdict["tradable"],
                    rejection_reason=verdict.get("reason"),
                    observation=verdict,
                )
                if verdict["proposal_eligible"]:
                    proposal = self._create_proposal(store, signal, verdict, final_time)
                    proposals.append(proposal)
            screened.append({"signal_id": signal["signal_id"], **verdict})
        return screened, proposals

    def _intraday_context(
        self,
        signals: list[dict[str, Any]],
        tickers: list[str],
        decision_time: str,
    ) -> dict[str, list[dict[str, Any]]]:
        if not tickers:
            return {}
        event_times = [
            self._effective_event_time(item)[0]
            for item in signals
            if item.get("event_at") or item.get("published_at")
        ]
        start = min(event_times, default=parse_ts(decision_time)) - timedelta(minutes=15)
        try:
            return self.discovery.fetch_intraday_bars(tickers, start.isoformat(), decision_time)
        except AdapterError:
            return {}

    def _tradability_verdict(
        self,
        signal: dict[str, Any],
        context: dict[str, Any] | None,
        bars: list[dict[str, Any]],
        decision_time: str,
        instrument_rejection: str | None,
    ) -> dict[str, Any]:
        base = {
            "ticker": signal["ticker"],
            "tradable": False,
            "proposal_eligible": False,
            "reason": None,
            "quote": (context or {}).get("quote", {}),
            "spread_bps": None,
            "initial_reaction_bps": None,
            "reference_price": None,
        }
        if instrument_rejection:
            return {**base, "reason": instrument_rejection}
        if context is None:
            return {**base, "reason": "missing market context"}
        quote = Quote(**context["quote"])
        base["spread_bps"] = quote.spread_bps()
        now = parse_ts(decision_time)
        quote_time = parse_ts(quote.asof)
        quote_age = (now - quote_time).total_seconds()
        if quote_time > now:
            return {**base, "reason": "quote timestamp exceeds decision cutoff"}
        if quote_age > float(self.profile.get("maximum_quote_age_seconds", 90)):
            return {**base, "reason": f"stale quote ({quote_age:.0f}s)"}
        if not context.get("eligible", False):
            return {**base, "reason": "liquidity, market-cap, or spread limit failed"}
        signal_time = parse_ts(str(signal.get("signal_time") or decision_time))
        latency = (signal_time - parse_ts(str(signal["first_seen_at"]))).total_seconds()
        if latency < 0 or latency > float(self.profile.get("maximum_signal_latency_seconds", 900)):
            return {**base, "reason": f"signal latency outside policy ({latency:.0f}s)"}
        event_time, event_time_basis = self._effective_event_time(signal)
        event_age = (now - event_time).total_seconds()
        if event_age < 0 or event_age > float(self.profile.get("maximum_event_age_seconds", 7200)):
            return {**base, "reason": f"event age outside policy ({event_age:.0f}s)"}
        reference = self._reference_price(signal, bars, quote, decision_time)
        reaction_bps = ((quote.mid / reference) - 1) * 10000 if reference and reference > 0 else None
        base.update(
            {
                "tradable": True,
                "reference_price": reference,
                "initial_reaction_bps": round(reaction_bps, 3) if reaction_bps is not None else None,
                "signal_latency_seconds": round(latency, 3),
                "event_age_seconds": round(event_age, 3),
                "event_time_basis": event_time_basis,
                "market_cap_usd": context.get("fundamentals", {}).get("market_cap"),
                "average_daily_volume_usd": context.get("fundamentals", {}).get("average_daily_volume_usd"),
            }
        )
        threshold_pass = (
            signal.get("direction") == "positive"
            and float(signal.get("materiality", 0)) >= float(self.profile.get("minimum_signal_materiality", 0.55))
            and float(signal.get("confidence", 0)) >= float(self.profile.get("minimum_signal_confidence", 0.60))
            and float(signal.get("ambiguity", 1)) <= float(self.profile.get("maximum_signal_ambiguity", 0.50))
            and reaction_bps is not None
            and reaction_bps <= float(self.profile.get("maximum_initial_reaction_bps", 500))
        )
        if threshold_pass:
            return {**base, "proposal_eligible": True, "reason": "positive drift thresholds passed"}
        if signal.get("direction") == "negative":
            reason = "negative event stored for isolated P2 experiments; base lane is long equity only"
        elif reaction_bps is None:
            reason = "pre-event reference price unavailable"
        elif reaction_bps > float(self.profile.get("maximum_initial_reaction_bps", 500)):
            reason = "initial reaction exceeds chase limit"
        else:
            reason = "headline materiality, confidence, direction, or ambiguity threshold failed"
        return {**base, "reason": reason}

    @staticmethod
    def _effective_event_time(signal: dict[str, Any]) -> tuple[Any, str]:
        if signal.get("event_at"):
            return parse_ts(str(signal["event_at"])), "event_at"
        if signal.get("published_at_precision") == "date" and signal.get("first_seen_at"):
            return parse_ts(str(signal["first_seen_at"])), "first_seen_at_for_date_precision"
        return parse_ts(str(signal["published_at"])), "published_at"

    @staticmethod
    def _reference_price(
        signal: dict[str, Any],
        bars: list[dict[str, Any]],
        quote: Quote,
        cutoff: str,
    ) -> float | None:
        event_time, _ = NewsDriftPipeline._effective_event_time(signal)
        cutoff_time = parse_ts(cutoff)
        eligible: list[tuple[Any, float]] = []
        for bar in bars:
            if not isinstance(bar, dict) or bar.get("interpolated", False):
                continue
            begins_at = bar.get("begins_at")
            close = bar.get("close_price")
            if begins_at is None or close is None:
                continue
            bar_end = parse_ts(str(begins_at)) + timedelta(minutes=5)
            if bar_end <= event_time and bar_end <= cutoff_time:
                eligible.append((bar_end, float(close)))
        if eligible:
            return max(eligible, key=lambda item: item[0])[1]
        return float(quote.previous_close) if quote.previous_close and quote.previous_close > 0 else None

    def _create_proposal(
        self,
        store: NewsEventStore,
        signal: dict[str, Any],
        verdict: dict[str, Any],
        decision_time: str,
    ) -> dict[str, Any]:
        quote = Quote(**verdict["quote"])
        costs = self.config.get("costs", {})
        slip = adverse_slippage(
            quote.ask,
            float(costs.get("slippage_bps", 0)),
            float(costs.get("minimum_slippage_usd", 0)),
        )
        entry = round(quote.ask + slip, 4)
        notional = float(self.profile.get("shadow_initial_cash_usd", 2000)) * float(
            self.profile.get("max_shadow_position_pct", 0.25)
        )
        raw_quantity = notional / entry if entry > 0 else 0
        if self.config.get("risk", {}).get("allow_fractional_shares", False):
            increment = float(self.config.get("risk", {}).get("fractional_share_increment", 0.001))
            quantity = round(math.floor(raw_quantity / increment) * increment, 6) if increment > 0 else 0
        else:
            quantity = math.floor(raw_quantity)
        label_targets = self._label_targets(decision_time)
        payload = {
            "strategy": self.STRATEGY,
            "execution": "shadow_only",
            "label_policy_version": "executable_preclose_v1",
            "quantity": quantity,
            "notional_usd": round(quantity * entry, 4),
            "entry_bid": quote.bid,
            "entry_ask": quote.ask,
            "entry_mid": quote.mid,
            "entry_slippage_usd_per_share": round(slip, 4),
            "entry_quote_asof": quote.asof,
            "label_targets": label_targets,
            "event_type": signal.get("event_type"),
            "source_tier": signal.get("source_tier"),
            "market_cap_usd": verdict.get("market_cap_usd"),
            "average_daily_volume_usd": verdict.get("average_daily_volume_usd"),
            "initial_reaction_bps": verdict.get("initial_reaction_bps"),
        }
        if quantity <= 0:
            raise ValueError("shadow proposal quantity is zero")
        saved = store.record_shadow_proposal(
            event_id=signal["event_id"],
            signal_id=signal["signal_id"],
            ticker=signal["ticker"],
            action="buy",
            direction="positive",
            decision_time=decision_time,
            entry_price=entry,
            payload=payload,
        )
        proposal = {**payload, "proposal_id": saved["proposal_id"], "event_id": signal["event_id"], "signal_id": signal["signal_id"], "ticker": signal["ticker"], "decision_time": decision_time}
        append_jsonl(self.root, "news_drift_proposals.jsonl", proposal)
        return proposal

    def _label_targets(self, decision_time: str) -> dict[str, str]:
        decision = parse_ts(decision_time)
        targets = {
            name: (decision + timedelta(minutes=minutes)).isoformat()
            for name, minutes in self.LABEL_HORIZONS.items()
        }
        day = pd.Timestamp(decision.astimezone(self.clock.new_york).date())
        session = self.clock.calendar.date_to_session(day, direction="next")
        preclose = timedelta(
            minutes=int(self.config.get("paper", {}).get("exit_before_close_minutes", 10))
        )
        close = self.clock.calendar.session_close(session).to_pydatetime() - preclose
        if decision < close:
            targets["same_day_close"] = close.isoformat()
        next_session = self.clock.calendar.next_session(session)
        targets["next_close"] = (
            self.clock.calendar.session_close(next_session).to_pydatetime() - preclose
        ).isoformat()
        second_session = self.clock.calendar.next_session(next_session)
        targets["second_close"] = (
            self.clock.calendar.session_close(second_session).to_pydatetime() - preclose
        ).isoformat()
        return targets

    def resolve_due_labels(self, now: str | None = None) -> dict[str, Any]:
        observed_at = now or utc_now()
        if not self.database.exists():
            return {"due": 0, "recorded": 0, "deferred": 0}
        current = parse_ts(observed_at)
        due: list[dict[str, Any]] = []
        with NewsEventStore(self.database) as store:
            existing = {
                (str(row["proposal_id"]), str(row["horizon"]))
                for row in store.connection.execute(
                    "SELECT proposal_id, horizon FROM outcome_labels WHERE proposal_id IS NOT NULL"
                ).fetchall()
            }
            for row in store.connection.execute("SELECT * FROM shadow_proposals ORDER BY decision_time").fetchall():
                payload = json.loads(row["payload_json"])
                for horizon, target in payload.get("label_targets", {}).items():
                    if (str(row["proposal_id"]), horizon) not in existing and parse_ts(target) <= current:
                        due.append({**dict(row), "payload": payload, "horizon": horizon, "target_time": target})
        if not due:
            return {"due": 0, "recorded": 0, "deferred": 0}
        recorded = 0
        deferred = 0
        quote_cache: dict[str, Quote] = {}
        for item in due:
            try:
                ticker = str(item["ticker"])
                if ticker not in quote_cache:
                    quote_cache[ticker] = self.discovery.fetch_current_quote(
                        ticker,
                        average_daily_volume_usd=item["payload"].get("average_daily_volume_usd"),
                    )
                quote = quote_cache[ticker]
                if parse_ts(quote.asof) > current:
                    deferred += 1
                    continue
                target_time = parse_ts(item["target_time"])
                quote_time = parse_ts(quote.asof)
                if quote_time < target_time:
                    deferred += 1
                    continue
                delay = (quote_time - target_time).total_seconds()
                tolerance = float(self.profile.get("label_delay_tolerance_seconds", 90))
                if delay > tolerance:
                    with NewsEventStore(self.database) as store:
                        store.record_outcome_label(
                            event_id=item["event_id"],
                            signal_id=item["signal_id"],
                            proposal_id=item["proposal_id"],
                            label="unavailable",
                            horizon=item["horizon"],
                            return_pct=None,
                            outcome_time=observed_at,
                            label_time=observed_at,
                            decision_time=item["decision_time"],
                            payload={
                                "target_time": item["target_time"],
                                "observation_delay_seconds": round(delay, 3),
                                "data_gap": "quote arrived outside label tolerance",
                            },
                        )
                    recorded += 1
                    continue
                if quote.spread_bps() > float(self.profile.get("maximum_spread_bps", 50)):
                    deferred += 1
                    continue
                entry = float(item["entry_price"])
                exit_slip = adverse_slippage(
                    quote.bid,
                    float(self.config.get("costs", {}).get("slippage_bps", 0)),
                    float(self.config.get("costs", {}).get("minimum_slippage_usd", 0)),
                )
                exit_price = max(0.0001, quote.bid - exit_slip)
                gross_return = (quote.mid / float(item["payload"]["entry_mid"]) - 1) * 100
                quantity = float(item["payload"].get("quantity", 0))
                commission = float(self.config.get("costs", {}).get("commission_per_order_usd", 0))
                if quantity <= 0:
                    raise ValueError("shadow proposal quantity is invalid")
                net_return = (
                    ((exit_price - entry) * quantity - 2 * commission)
                    / (entry * quantity)
                    * 100
                )
                with NewsEventStore(self.database) as store:
                    store.record_outcome_label(
                        event_id=item["event_id"],
                        signal_id=item["signal_id"],
                        proposal_id=item["proposal_id"],
                        label="profitable" if net_return > 0 else "not_profitable",
                        horizon=item["horizon"],
                        return_pct=net_return,
                        outcome_time=quote.asof,
                        label_time=observed_at,
                        decision_time=item["decision_time"],
                        payload={
                            "target_time": item["target_time"],
                            "observation_delay_seconds": round(delay, 3),
                            "quote": quote.to_dict(),
                            "gross_return_pct": round(gross_return, 6),
                            "net_return_pct": round(net_return, 6),
                            "exit_price": round(exit_price, 4),
                        },
                    )
                recorded += 1
            except (AdapterError, ValueError, KeyError):
                deferred += 1
        return {"due": len(due), "recorded": recorded, "deferred": deferred}

    def _complete(
        self,
        cycle_id: str,
        cycle_time: str,
        query: str,
        snapshot: dict[str, Any],
        raw_events: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        screened: list[dict[str, Any]],
        proposals: list[dict[str, Any]],
        labels: dict[str, Any],
        calls_before: int,
    ) -> dict[str, Any]:
        result = self._result(
            "news_drift_complete",
            cycle_time,
            strategy=self.STRATEGY,
            execution="shadow_only",
            cycle_id=cycle_id,
            query=query,
            raw_event_count=len(raw_events),
            signals=signals,
            screened=screened,
            proposals=proposals,
            labels=labels,
            evidence_snapshot=snapshot,
            model_calls=len(self.tracker.records) - calls_before,
            usage=self.tracker.summary(),
        )
        append_jsonl(self.root, "news_drift_cycles.jsonl", result)
        return result

    @staticmethod
    def _result(event: str, decision_time: str, **payload: Any) -> dict[str, Any]:
        return {
            "event": event,
            "decision_time": decision_time,
            **payload,
            "paper_orders_created": 0,
            "live_order_tools_called": False,
        }
