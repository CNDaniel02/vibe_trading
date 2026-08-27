from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from scripts.evaluation.calculate_metrics import calculate_metrics
from scripts.replay.allocator_functional_replay import run_golden_path_replay
from scripts.replay.allocator_historical_replay import run_natural_strict_replay
from scripts.replay.allocator_validation_contracts import VALIDATION_SCHEMA_VERSION


_STRATEGY = "ai_instrument_allocator_v1"


def _source_revision(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "not_recorded"
    return completed.stdout.strip() or "not_recorded"


def _forward_evidence(data_root: Path) -> dict[str, Any]:
    try:
        metrics = calculate_metrics(data_root, namespace=_STRATEGY)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return {
            "evidence_type": "forward_paper_evidence",
            "source": "existing isolated paper sleeve",
            "available": False,
            "profitability_claim": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }
    closed = int(metrics.get("closed_trade_count") or 0)
    sufficient = bool(metrics.get("evidence_sufficient", False))
    return {
        "evidence_type": "forward_paper_evidence",
        "source": "existing isolated paper sleeve",
        "available": True,
        "closed_trade_count": closed,
        "open_position_count": int(metrics.get("open_position_count") or 0),
        "realized_pnl_usd": metrics.get("realized_pnl"),
        "ending_equity_usd": metrics.get("ending_equity"),
        "evidence_sufficient": sufficient,
        "promotion_eligible": bool(metrics.get("promotion_eligible", False)),
        "profitability_claim": (
            "forward_evidence_sufficient"
            if sufficient
            else "insufficient_forward_evidence"
        ),
    }


def build_allocator_validation_report(
    project_root: str | Path,
    *,
    data_root: str | Path | None = None,
    asof: str,
    hours: int = 48,
    include_functional: bool = True,
) -> dict[str, Any]:
    source_root = Path(project_root).resolve()
    observed_root = Path(data_root or project_root).resolve()
    revision = _source_revision(source_root)
    natural = run_natural_strict_replay(
        observed_root,
        project_root=source_root,
        hours=hours,
        asof=asof,
        source_revision=revision,
    )
    if include_functional:
        functional = run_golden_path_replay(source_root)
        functional_status = (
            "passed"
            if functional["summary"]["failed"] == 0
            and functional["summary"]["passed"]
            == functional["summary"]["scenario_count"]
            else "failed"
        )
        functional = {"status": functional_status, **functional}
    else:
        functional = {
            "status": "not_run",
            "evidence_type": "functional_liveness",
            "historical_performance_claimed": False,
            "forward_performance_claimed": False,
        }

    report = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "strategy": _STRATEGY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "functional_liveness": functional,
        "historical_performance": natural,
        "forward_evidence": _forward_evidence(observed_root),
        "evidence_boundaries": {
            "functional_liveness": (
                "Fixed fixtures prove production-path liveness only."
            ),
            "historical_performance": (
                "Strict replay is diagnostic until leakage-safe walk-forward "
                "datasets support an executable performance claim."
            ),
            "forward_evidence": (
                "Only the isolated forward paper sleeve is current model evidence."
            ),
        },
        "acceptance": {
            "time_violation_count": natural["time_validation"][
                "time_violation_count"
            ],
            "admitted_time_violation_count": natural["time_validation"][
                "admitted_violation_count"
            ],
            "historical_orders_created": natural[
                "historical_orders_created_by_replay"
            ],
            "live_broker_write_calls": int(
                natural["live_broker_write_calls"]
            )
            + int(functional.get("summary", {}).get("live_broker_write_calls", 0)),
            "functional_source_root_unchanged": all(
                scenario.get("source_root_unchanged", False)
                for scenario in functional.get("scenarios", [])
            )
            if include_functional
            else None,
        },
    }
    schema = json.loads(
        (source_root / "schemas" / "allocator_historical_validation_report.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)
    return report


def write_report(
    report: dict[str, Any],
    output: str | Path,
    *,
    protected_root: str | Path,
) -> Path:
    output_path = Path(output).resolve()
    root = Path(protected_root).resolve()
    protected = ((root / "state").resolve(), (root / "logs").resolve())
    if any(output_path == path or path in output_path.parents for path in protected):
        raise ValueError("validation report cannot be written under state/ or logs/")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the allocator evidence-separated validation report"
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--data-root")
    parser.add_argument("--asof", required=True)
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--skip-functional", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = build_allocator_validation_report(
        args.project_root,
        data_root=args.data_root,
        asof=args.asof,
        hours=args.hours,
        include_functional=not args.skip_functional,
    )
    if args.output:
        write_report(
            report,
            args.output,
            protected_root=args.data_root or args.project_root,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
