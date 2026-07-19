from __future__ import annotations

from pathlib import Path

from scripts.evaluation.calculate_metrics import calculate_metrics


def generate_report(root: str | Path) -> Path:
    metrics = calculate_metrics(root)
    path = Path(root) / "logs" / "performance_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Paper Trading Performance Report", ""]
    for key, value in metrics.items():
        lines.append(f"- {key}: {value}")
    lines.extend([
        "",
        "## Decision",
        f"Promotion eligible: **{metrics['promotion_eligible']}**",
        f"Profitability label: **{metrics['profitability']}**",
        "",
        "Forward paper trading is the primary evidence. Historical replay and Vibe backtests are debugging and screening tools only.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    print(generate_report(args.root))
