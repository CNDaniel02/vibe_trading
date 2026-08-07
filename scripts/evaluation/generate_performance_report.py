from __future__ import annotations

from pathlib import Path

from scripts.evaluation.calculate_metrics import calculate_metrics


def generate_report(root: str | Path) -> Path:
    metrics = calculate_metrics(root)
    ai_namespace = "ai_gated_technical_v1"
    ai_state = Path(root) / "state" / "strategy_sleeves" / ai_namespace / "paper_account.json"
    ai_metrics = calculate_metrics(root, namespace=ai_namespace) if ai_state.exists() else None
    path = Path(root) / "logs" / "performance_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Paper Trading Performance Report",
        "",
        "## Shared Account",
        f"- Initial cash: ${metrics['initial_cash']:.2f}",
        f"- Ending equity: ${metrics['ending_equity']:.2f}",
        f"- Valuation status: {metrics['valuation_status']}",
        f"- Valuation as of: {metrics['valuation_asof']}",
        f"- Net return: {metrics['net_return_pct']:.4f}%",
        f"- Maximum drawdown: {metrics['max_drawdown_pct']:.4f}%",
        f"- Rule violations: {metrics['rule_violations']}",
    ]
    for line_name, line_metrics in metrics["lines"].items():
        lines.extend(
            [
                "",
                f"## {line_name.title()} Line",
                f"- Net PnL: ${line_metrics['net_pnl']:.2f}",
                f"- Closed trades: {line_metrics['closed_trade_count']}",
                f"- Win rate: {line_metrics['win_rate']:.4f}",
                f"- Profit factor: {line_metrics['profit_factor']}",
                f"- Fill rate: {line_metrics['fill_rate']:.4f}",
                f"- Unfilled rate: {line_metrics['unfilled_rate']:.4f}",
                f"- Evidence sufficient: {line_metrics['evidence_sufficient']}",
                f"- Promotion eligible: {line_metrics['promotion_eligible']}",
                f"- Profitability label: {line_metrics['profitability']}",
            ]
        )
    if ai_metrics is not None:
        lines.extend(
            [
                "",
                "## AI Gated Isolated Paper Sleeve",
                f"- Ending equity: ${ai_metrics['ending_equity']:.2f}",
                f"- Valuation status: {ai_metrics['valuation_status']}",
                f"- Net return: {ai_metrics['net_return_pct']:.4f}%",
                f"- Closed trades: {ai_metrics['closed_trade_count']}",
                f"- Win rate: {ai_metrics['win_rate']:.4f}",
                f"- Profit factor: {ai_metrics['profit_factor']}",
                f"- Maximum drawdown: {ai_metrics['max_drawdown_pct']:.4f}%",
                f"- Promotion eligible: {ai_metrics['promotion_eligible']}",
                "- Account and order state are isolated from the deterministic baseline.",
            ]
        )
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
