from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Iterator
from unittest.mock import patch

from mcp import ClientSession

from scripts.broker.robinhood_readonly_adapter import (
    LiveOrderToolBlocked,
    RobinhoodReadonlyAdapter,
)
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.core.models import Quote
from scripts.discovery.ai_instrument_allocator_pipeline import (
    AiInstrumentAllocatorPipeline,
)
from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.llm.base_provider import ProviderRequest, ProviderResponse
from scripts.llm.mock_provider import MockProvider
from scripts.llm.schemas import validate_schema
from scripts.llm.usage_tracker import UsageTracker
from scripts.options.models import OptionContract, OptionQuote
from scripts.options.paper_broker import OptionPaperBroker
from scripts.replay.allocator_validation_contracts import (
    build_validation_manifest,
    detect_source_revision,
    validate_point_in_time_snapshot,
)
from scripts.simulation.paper_broker import PaperBroker


_NAMESPACE = "ai_instrument_allocator_v1"
_SCENARIO_IDS = ("bullish_equity", "bullish_call", "bearish_put")
_LIVE_ORDER_TOOL_NAMES = frozenset(
    {
        "cancel_equity_order",
        "cancel_option_order",
        "place_equity_order",
        "place_option_order",
        "review_equity_order",
        "review_option_order",
    }
)


@contextmanager
def _deny_live_broker_writes() -> Iterator[list[dict[str, str]]]:
    """Install a deny hook at both supported live-order boundaries."""
    attempts: list[dict[str, str]] = []
    original_call_tool = ClientSession.call_tool

    async def guarded_call_tool(
        session: ClientSession,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        normalized = str(name)
        if normalized in _LIVE_ORDER_TOOL_NAMES:
            attempts.append({"boundary": "mcp", "tool": normalized})
            raise LiveOrderToolBlocked(
                f"golden replay blocked live broker tool: {normalized}"
            )
        return await original_call_tool(session, name, *args, **kwargs)

    def blocked_adapter_call(method_name: str) -> Any:
        def blocked(_adapter: RobinhoodReadonlyAdapter, *_args: Any, **_kwargs: Any) -> None:
            attempts.append({"boundary": "readonly_adapter", "tool": method_name})
            raise LiveOrderToolBlocked(
                f"golden replay blocked live broker adapter method: {method_name}"
            )

        return blocked

    with (
        patch.object(ClientSession, "call_tool", guarded_call_tool),
        patch.object(
            RobinhoodReadonlyAdapter,
            "review_equity_order",
            blocked_adapter_call("review_equity_order"),
        ),
        patch.object(
            RobinhoodReadonlyAdapter,
            "place_equity_order",
            blocked_adapter_call("place_equity_order"),
        ),
        patch.object(
            RobinhoodReadonlyAdapter,
            "place_option_order",
            blocked_adapter_call("place_option_order"),
        ),
    ):
        yield attempts


class _FixtureProvider(MockProvider):
    def __init__(
        self,
        tracker: UsageTracker,
        probability_buckets: dict[str, float],
    ) -> None:
        super().__init__(tracker)
        self.probability_buckets = dict(probability_buckets)
        self.model = "allocator-golden-fixture-v1"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        response = super().generate(request)
        if request.agent_name not in {
            "ai_allocator_decision_manager",
            "ai_allocator_fast_decision_manager",
        }:
            return ProviderResponse(
                response.data,
                self.model,
                "golden_fixture",
                response.usage,
                response.response_id,
            )
        data = {
            **response.data,
            "signed_return_probability_buckets": dict(self.probability_buckets),
        }
        validate_schema(data, request.output_schema)
        return ProviderResponse(
            data,
            self.model,
            "golden_fixture",
            response.usage,
            response.response_id,
        )


class _FixtureDiscoveryAdapter:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = fixture
        self.phase = "entry"

    def collect_seed_candidates(
        self,
        _now: str,
        _watchlist: list[str],
    ) -> list[dict[str, Any]]:
        return [
            {
                "ticker": self.fixture["ticker"],
                "sources": ["golden_point_in_time_fixture"],
            }
        ]

    def fetch_market_context(
        self,
        _tickers: list[str],
        decision_time: str,
    ) -> dict[str, dict[str, Any]]:
        ticker = str(self.fixture["ticker"])
        quote = self.fixture["research_quote"]
        return {
            ticker: {
                "ticker": ticker,
                "eligible": True,
                "quote": {
                    "symbol": ticker,
                    "bid": quote["bid"],
                    "ask": quote["ask"],
                    "last": quote["last"],
                    "asof": decision_time,
                    "source": "golden_point_in_time_fixture",
                    "avg_daily_volume_usd": quote["avg_daily_volume_usd"],
                    "asset_class": "us_equity",
                    "is_otc": False,
                    "is_leveraged_etf": False,
                    "is_inverse_etf": False,
                    "halted": False,
                    "session_volume": 2_000_000,
                    "previous_close": 98.0,
                },
                "fundamentals": {"market_cap": 3_000_000_000_000},
                "technical_signals": {
                    "price_change_1d_pct": 2.0,
                    "price_change_5d_pct": 6.0,
                    "relative_strength_20d": 5.0,
                    "volume_ratio": 1.5,
                    "chase_score": 0.15,
                },
            }
        }

    def validate_instrument(self, symbol: str) -> dict[str, Any]:
        return {
            "valid": symbol.upper() == str(self.fixture["ticker"]).upper(),
            "name": self.fixture["company_name"],
            "symbol": symbol.upper(),
        }

    def fetch_current_quote(self, symbol: str, **_kwargs: Any) -> Quote:
        key = "exit_quote" if self.phase == "exit" else "entry_quote"
        quote = self.fixture[key]
        timestamp = self.fixture["times"][
            "exit" if self.phase == "exit" else "open_execution"
        ]
        return Quote(
            symbol=symbol.upper(),
            bid=float(quote["bid"]),
            ask=float(quote["ask"]),
            last=float(quote["last"]),
            asof=timestamp,
            source="golden_point_in_time_fixture",
            avg_daily_volume_usd=float(
                self.fixture["research_quote"]["avg_daily_volume_usd"]
            ),
        )


class _FixtureNewsAdapter:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = fixture

    def search(
        self,
        ticker: str,
        decision_time: str,
        company_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        del company_name
        event = {
            **self.fixture["event"],
            "ticker": ticker.upper(),
            "retrieved_at": decision_time,
        }
        return [event], [
            {
                "source": event["source"],
                "source_tier": event["source_tier"],
                "retrieved_at": decision_time,
            }
        ]


class _FixtureOptionDataAdapter:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = fixture
        self.phase = "entry"

    def _pair(self, phase: str, timestamp: str) -> tuple[OptionContract, OptionQuote] | None:
        option = self.fixture.get("option")
        if not isinstance(option, dict):
            return None
        contract = OptionContract.from_dict(dict(option["contract"]))
        raw = option[f"{phase}_quote"]
        quote = OptionQuote(
            option_id=contract.option_id,
            bid=float(raw["bid"]),
            ask=float(raw["ask"]),
            mark=float(raw["mark"]),
            updated_at=timestamp,
            source="golden_point_in_time_fixture",
            delta=float(raw["delta"]),
            gamma=float(raw["gamma"]),
            theta=float(raw["theta"]),
            vega=float(raw["vega"]),
            implied_volatility=float(raw["implied_volatility"]),
            volume=int(raw["volume"]),
            open_interest=int(raw["open_interest"]),
        )
        return contract, quote

    def fetch_contract_candidates(
        self,
        **kwargs: Any,
    ) -> tuple[list[tuple[OptionContract, OptionQuote]], dict[str, Any]]:
        timestamp = str(kwargs["now"])
        pair = self._pair("entry", timestamp)
        candidates = [pair] if pair is not None else []
        return candidates, {
            "candidate_count": len(candidates),
            "source": "golden_point_in_time_fixture",
        }

    def fetch_quotes(self, option_ids: list[str]) -> dict[str, OptionQuote]:
        timestamp = self.fixture["times"][
            "exit" if self.phase == "exit" else "open_execution"
        ]
        pair = self._pair(self.phase, timestamp)
        if pair is None or pair[0].option_id not in option_ids:
            return {}
        return {pair[0].option_id: pair[1]}


def _load_fixture(project_root: Path, scenario_id: str) -> tuple[Path, dict[str, Any]]:
    if scenario_id not in _SCENARIO_IDS:
        raise ValueError(f"unknown golden scenario: {scenario_id}")
    path = project_root / "fixtures" / "allocator_validation" / f"{scenario_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("scenario_id") != scenario_id:
        raise ValueError(f"golden scenario identity mismatch: {path}")
    return path, value


def _protected_hashes(root: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for directory in (
        root / "state" / "strategy_sleeves" / _NAMESPACE,
        root / "logs" / "strategy_sleeves" / _NAMESPACE,
    ):
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            rows[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _fixture_time_checks(fixture: dict[str, Any]) -> dict[str, Any]:
    times = fixture["times"]
    research = validate_point_in_time_snapshot(
        {
            "decision_time": times["overnight"],
            "data_cutoff_time": times["overnight"],
            "market_data": {
                "quote": {"asof": times["overnight"]},
            },
            "available_news": [fixture["event"]],
        }
    )
    entry_market: dict[str, Any] = {
        "quote": {"asof": times["open_execution"]}
    }
    if isinstance(fixture.get("option"), dict):
        entry_market["option_chain"] = [
            {
                **fixture["option"]["contract"],
                **fixture["option"]["entry_quote"],
                "updated_at": times["open_execution"],
            }
        ]
    entry = validate_point_in_time_snapshot(
        {
            "decision_time": times["open_execution"],
            "data_cutoff_time": times["open_execution"],
            "market_data": entry_market,
        }
    )
    exit_market: dict[str, Any] = {"quote": {"asof": times["exit"]}}
    if isinstance(fixture.get("option"), dict):
        exit_market["option_chain"] = [
            {
                **fixture["option"]["contract"],
                **fixture["option"]["exit_quote"],
                "updated_at": times["exit"],
            }
        ]
    exit_check = validate_point_in_time_snapshot(
        {
            "decision_time": times["exit"],
            "data_cutoff_time": times["exit"],
            "market_data": exit_market,
        }
    )
    return {"research": research, "entry": entry, "exit": exit_check}


def _run_scenario(
    project_root: Path,
    fixture_path: Path,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    source_before = _protected_hashes(project_root)
    temporary_path = ""
    result: dict[str, Any]
    with (
        TemporaryDirectory(prefix=f"allocator-golden-{fixture['scenario_id']}-") as raw,
        _deny_live_broker_writes() as live_write_attempts,
    ):
        temporary_root = Path(raw).resolve()
        temporary_path = str(temporary_root)
        shutil.copytree(project_root / "config", temporary_root / "config")
        config = load_runtime_config(temporary_root)
        assert_paper_mode(config)
        tracker = UsageTracker()
        provider = _FixtureProvider(tracker, fixture["probability_buckets"])
        discovery = _FixtureDiscoveryAdapter(fixture)
        news = _FixtureNewsAdapter(fixture)
        option_data = _FixtureOptionDataAdapter(fixture)
        pipeline = AiInstrumentAllocatorPipeline(
            temporary_root,
            config,
            provider,
            tracker,
            discovery_adapter=discovery,
            news_adapter=news,
            option_data=option_data,
        )
        paper_broker_boundary_verified = (
            type(pipeline.broker) is PaperBroker
            and type(pipeline.option_broker) is OptionPaperBroker
        )
        times = fixture["times"]
        time_checks = _fixture_time_checks(fixture)
        if not all(item["valid"] for item in time_checks.values()):
            raise ValueError(
                f"golden fixture violates point-in-time constraints: {time_checks}"
            )

        overnight = pipeline.run_stage("overnight", times["overnight"])
        premarket = pipeline.run_stage(
            "premarket_update",
            times["premarket_update"],
        )
        preopen = pipeline.run_stage(
            "preopen_revalidation",
            times["preopen_revalidation"],
        )
        active = pipeline.plans.active_plans(times["open_execution"])
        opened = pipeline.run_stage("open_execution", times["open_execution"])
        execution = opened["executions"][0] if opened.get("executions") else {}
        selected = (execution.get("allocation") or {}).get("selected_instrument") or {}
        instrument = str(selected.get("instrument_type") or "")
        exposure_id = (
            f"equity:{fixture['ticker']}"
            if instrument == "equity"
            else f"option:{selected.get('option_id')}"
        )
        mandate_before_exit = pipeline.mandates.for_exposure(exposure_id)

        restart_tracker = UsageTracker()
        restarted = AiInstrumentAllocatorPipeline(
            temporary_root,
            config,
            _FixtureProvider(restart_tracker, fixture["probability_buckets"]),
            restart_tracker,
            discovery_adapter=discovery,
            news_adapter=news,
            option_data=option_data,
        )
        discovery.phase = "exit"
        option_data.phase = "exit"
        exited = restarted.monitor_only(times["exit"])
        exit_rows = (
            exited.get("exits", [])
            if instrument == "equity"
            else exited.get("option_exits", [])
        )
        exit_order = exit_rows[0]["order"] if exit_rows else {}
        mandate_after_exit = restarted.mandates.for_exposure(exposure_id)

        state_dir = (
            temporary_root
            / "state"
            / "strategy_sleeves"
            / _NAMESPACE
        )
        wal = _read_json(
            state_dir / "paper_fill_transactions.json",
            {"transactions": {}},
        )
        committed = sum(
            record.get("status") == "committed"
            for record in wal.get("transactions", {}).values()
        )
        wal_transactions = {
            str(transaction_id): record
            for transaction_id, record in wal.get("transactions", {}).items()
            if isinstance(record, dict)
        }
        entry_order_id = str(execution.get("order", {}).get("order_id") or "")
        exit_order_id = str(exit_order.get("order_id") or "")
        expected_order_ids = {entry_order_id, exit_order_id} - {""}
        wal_order_ids = {
            str(record.get("order_id") or "")
            for record in wal_transactions.values()
        } - {""}
        wal_identity_valid = bool(expected_order_ids) and (
            wal_order_ids == expected_order_ids
            and len(wal_transactions) == len(expected_order_ids)
            and all(
                record.get("status") == "committed"
                and record.get("transaction_id") == transaction_id
                for transaction_id, record in wal_transactions.items()
            )
        )
        lifecycle = _read_json(
            state_dir / "trade_lifecycle.json",
            {"open": {}, "closed": []},
        )
        metrics = calculate_metrics(temporary_root, namespace=_NAMESPACE)
        closed = list(lifecycle.get("closed", []))
        allocation = execution.get("allocation", {})
        considered = (
            allocation.get("considered", [])
            if isinstance(allocation.get("considered"), list)
            else []
        )
        selected_candidate = next(
            (
                item
                for item in considered
                if isinstance(item, dict)
                and item.get("instrument_type") == instrument
                and (
                    str(item.get("ticker") or "").upper()
                    == fixture["ticker"]
                    if instrument == "equity"
                    else str(item.get("option_id") or "")
                    == str(selected.get("option_id") or "")
                )
            ),
            None,
        )
        risk_evidence = {
            "allocator_selected": allocation.get("status") == "selected",
            "selected_candidate_eligible": bool(
                selected_candidate and selected_candidate.get("eligible") is True
            ),
            "positive_deterministic_risk": bool(
                selected_candidate
                and float(
                    selected_candidate.get("deterministic_risk_usd")
                    or selected_candidate.get("risk_usd")
                    or 0
                )
                > 0
            ),
            "broker_risk_gate_passed": bool(
                execution.get("status") == "filled"
                and not execution.get("reason")
                and not execution.get("order", {}).get("reject_reason")
            ),
        }
        trace = {
            "proposal": bool(
                overnight.get("plans")
                and overnight["plans"][0].get("signal", {}).get("action")
                == "propose_trade"
            ),
            "plan_persisted": bool(active),
            "preopen_revalidated": bool(
                active
                and active[0].get("preopen_revalidated_at")
                == times["preopen_revalidation"]
            ),
            "allocation_selected": allocation.get("status") == "selected",
            "deterministic_risk_approved": all(risk_evidence.values()),
            "paper_order_created": bool(execution.get("order")),
            "entry_filled": execution.get("order", {}).get("status") == "filled",
            "fill_wal_committed": committed == 2 and wal_identity_valid,
            "mandate_open": bool(
                mandate_before_exit and mandate_before_exit.get("status") == "open"
            ),
            "exit_evaluated": bool(exit_rows),
            "exit_filled": exit_order.get("status") == "filled",
            "mandate_closed": bool(
                mandate_after_exit and mandate_after_exit.get("status") == "closed"
            ),
            "pnl_attributed": bool(
                len(closed) == 1
                and closed[0].get("status") == "closed"
                and closed[0].get("realized_pnl") is not None
            ),
            "paper_broker_boundary": paper_broker_boundary_verified,
            "live_write_guard_clean": not live_write_attempts,
        }
        write_paths = [
            str(path.relative_to(temporary_root))
            for path in temporary_root.rglob("*")
            if path.is_file() and "config" not in path.parts
        ]
        source_unchanged = _protected_hashes(project_root) == source_before
        result = {
            "scenario_id": fixture["scenario_id"],
            "expected_instrument": fixture["expected_instrument"],
            "selected_instrument": instrument,
            "status": (
                "passed"
                if all(trace.values())
                and instrument == fixture["expected_instrument"]
                else "failed"
            ),
            "trace": trace,
            "entry_order_status": execution.get("order", {}).get("status"),
            "exit_order_status": exit_order.get("status"),
            "wal_committed_transactions": committed,
            "wal_order_ids": sorted(wal_order_ids),
            "expected_wal_order_ids": sorted(expected_order_ids),
            "wal_identity_valid": wal_identity_valid,
            "deterministic_risk_evidence": risk_evidence,
            "closed_trade_count": len(closed),
            "realized_pnl_usd": float(metrics["realized_pnl"]),
            "pnl_attribution": closed,
            "execution_cost_decomposition": metrics[
                "execution_cost_decomposition"
            ],
            "model_calls": len(tracker.records),
            "model_id": provider.model,
            "point_in_time": time_checks,
            "manifest": build_validation_manifest(
                project_root,
                data_cutoff=times["exit"],
                model_id=provider.model,
                dataset_paths=[fixture_path],
                source_revision=detect_source_revision(project_root),
            ),
            "live_broker_write_calls": len(live_write_attempts),
            "live_write_guard": {
                "installed": True,
                "blocked_tool_names": sorted(_LIVE_ORDER_TOOL_NAMES),
                "attempts": list(live_write_attempts),
                "paper_broker_boundary_verified": paper_broker_boundary_verified,
            },
            "live_order_tools_called": bool(
                live_write_attempts
                or
                opened.get("live_order_tools_called", False)
                or exited.get("live_order_tools_called", False)
            ),
            "write_scope": "temporary_root_only",
            "temporary_root": temporary_path,
            "temporary_write_file_count": len(write_paths),
            "temporary_write_paths": sorted(write_paths),
            "source_root_unchanged": source_unchanged,
            "research_stage_orders": {
                "overnight": overnight.get("paper_orders_created", 0),
                "premarket_update": premarket.get("paper_orders_created", 0),
                "preopen_revalidation": preopen.get("paper_orders_created", 0),
            },
        }
    result["temporary_root_exists_after"] = Path(temporary_path).exists()
    return result


def run_golden_path_replay(
    project_root: str | Path,
    *,
    scenario_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    selected_ids = list(scenario_ids or _SCENARIO_IDS)
    scenarios = [
        _run_scenario(root, *(_load_fixture(root, scenario_id)))
        for scenario_id in selected_ids
    ]
    passed = sum(item["status"] == "passed" for item in scenarios)
    live_calls = sum(int(item["live_broker_write_calls"]) for item in scenarios)
    return {
        "evidence_type": "functional_liveness",
        "strategy": _NAMESPACE,
        "scenarios": scenarios,
        "summary": {
            "scenario_count": len(scenarios),
            "passed": passed,
            "failed": len(scenarios) - passed,
            "live_broker_write_calls": live_calls,
        },
        "historical_performance_claimed": False,
        "forward_performance_claimed": False,
        "interpretation": (
            "Passing fixtures prove production-path liveness only; they do not "
            "prove historical or forward profitability."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run isolated golden-path allocator functional replay"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=_SCENARIO_IDS,
        dest="scenarios",
    )
    args = parser.parse_args()
    report = run_golden_path_replay(args.root, scenario_ids=args.scenarios)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
