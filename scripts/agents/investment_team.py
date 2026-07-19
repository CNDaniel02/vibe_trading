from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from scripts.core.models import Quote, utc_now

EvidenceGrade = Literal["A", "B", "C", "D"]
Recommendation = Literal["candidate", "watch", "reject"]


@dataclass(frozen=True)
class EvidenceItem:
    claim: str
    source: str
    observed_at: str
    grade: EvidenceGrade
    cross_checked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentTask:
    role: str
    subject: str
    description: str
    active_form: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentReport:
    role: str
    stance: Recommendation
    confidence: float
    evidence: list[EvidenceItem]
    source_gaps: list[str]
    bull_case: list[str] = field(default_factory=list)
    bear_case: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    red_lines: list[str] = field(default_factory=list)
    invalidation_triggers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [item.to_dict() for item in self.evidence]
        return data


@dataclass(frozen=True)
class TeamDecision:
    decision_id: str
    symbol: str
    recommendation: Recommendation
    confidence: float
    tasks: list[AgentTask]
    reports: list[AgentReport]
    reviewer_status: Literal["approved", "challenged", "rejected"]
    availability_rating: EvidenceGrade
    source_gaps: list[str]
    thesis: str
    assumptions: list[str]
    red_lines: list[str]
    invalidation_triggers: list[str]
    generated_at: str
    quote_seen_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tasks"] = [task.to_dict() for task in self.tasks]
        data["reports"] = [report.to_dict() for report in self.reports]
        return data


def build_default_tasks(symbol: str) -> list[AgentTask]:
    return [
        AgentTask(
            "market-structure-analyst",
            f"{symbol} quote and liquidity",
            "Check price, spread, quote freshness, and whether the setup is tradable for paper execution.",
            "Assessing bid/ask, spread, liquidity, stale quote risk, and execution feasibility.",
        ),
        AgentTask(
            "news-pulse-analyst",
            f"{symbol} news and catalyst gaps",
            "Separate verified events from missing news context; require source gaps when no feed is connected.",
            "Looking for catalyst evidence, source quality, and unsupported event assumptions.",
        ),
        AgentTask(
            "technical-momentum-analyst",
            f"{symbol} short-horizon momentum",
            "Score only data visible at the quote timestamp; do not use future bars or midpoint fills.",
            "Checking price action using available historical context without lookahead.",
        ),
        AgentTask(
            "bear-challenge-reviewer",
            f"{symbol} challenge review",
            "State the strongest reason to reject or delay, including stale data, wide spreads, and thesis gaps.",
            "Challenging the candidate before it reaches risk and paper execution.",
        ),
    ]


def availability_rating(reports: list[AgentReport]) -> EvidenceGrade:
    grades = [item.grade for report in reports for item in report.evidence]
    if grades and all(grade in ("A", "B") for grade in grades) and not any(report.source_gaps for report in reports):
        return "A"
    if grades and any(grade in ("A", "B") for grade in grades):
        return "B"
    if grades:
        return "C"
    return "D"


def run_investment_team(symbol: str, quote: Quote, context: dict[str, Any] | None = None) -> TeamDecision:
    context = context or {}
    now = str(context.get("now") or utc_now())
    tasks = build_default_tasks(symbol)
    spread_bps = quote.spread_bps()
    evidence = EvidenceItem(
        claim=f"{symbol} quote visible with bid {quote.bid:.2f}, ask {quote.ask:.2f}, spread {spread_bps:.1f} bps.",
        source=quote.source,
        observed_at=quote.asof,
        grade="B" if quote.source not in ("fixture", "replay") else "C",
        cross_checked=False,
    )

    reports = [
        AgentReport(
            role="market-structure-analyst",
            stance="candidate" if spread_bps <= float(context.get("max_spread_bps", 25)) else "reject",
            confidence=0.62,
            evidence=[evidence],
            source_gaps=["No independent second quote source connected for cross-check."],
            bull_case=["Spread and displayed quote are inside configured tradability constraints."],
            bear_case=["Single-source quote can hide data outage, bad print, or venue-specific spread."],
            assumptions=["Displayed bid/ask remains valid until paper submission."],
            red_lines=["Quote becomes stale before order submission.", "Bid/ask inverts or spread exceeds limit."],
            invalidation_triggers=["fresh_quote_missing", "spread_too_wide", "halted_symbol"],
        ),
        AgentReport(
            role="news-pulse-analyst",
            stance="watch",
            confidence=0.35,
            evidence=[],
            source_gaps=["No news feed adapter is wired into v1 paper cycle."],
            bull_case=["No adverse event is known to this deterministic v1 pipeline."],
            bear_case=["Absence of news data is not evidence that no catalyst or risk exists."],
            assumptions=["Trading decision must remain small and paper-only while catalyst data is absent."],
            red_lines=["Unverified takeover, halt, delisting, bankruptcy, or regulatory event appears."],
            invalidation_triggers=["material_news_unverified", "news_feed_unavailable_for_live"],
        ),
        AgentReport(
            role="technical-momentum-analyst",
            stance="watch",
            confidence=0.4,
            evidence=[evidence],
            source_gaps=["No historical bar window supplied to compute momentum score."],
            bull_case=["Last price is inside the quoted market."],
            bear_case=["No 20/60-bar context, relative strength, or volume confirmation is available."],
            assumptions=["Replay and forward strategy must use the same visible-at-time data only."],
            red_lines=["Strategy asks for future bars or same-bar close after the decision timestamp."],
            invalidation_triggers=["lookahead_detected", "insufficient_history"],
        ),
        AgentReport(
            role="bear-challenge-reviewer",
            stance="candidate" if quote.bid > 0 and quote.ask >= quote.bid else "reject",
            confidence=0.7,
            evidence=[evidence],
            source_gaps=["Reviewer has no account-level or macro feed beyond the risk gate inputs."],
            bull_case=["Candidate may proceed only to existing risk gate; no direct execution approval is granted here."],
            bear_case=["Low evidence depth should reduce conviction and block any live-trading interpretation."],
            assumptions=["Risk gate remains final authority on size, duplicate orders, and stale quotes."],
            red_lines=["Any live trading flag is enabled.", "Paper broker is bypassed."],
            invalidation_triggers=["live_trading_enabled", "paper_broker_bypassed"],
        ),
    ]

    gaps = sorted({gap for report in reports for gap in report.source_gaps})
    rating = availability_rating(reports)
    rejection_votes = sum(1 for report in reports if report.stance == "reject")
    candidate_votes = sum(1 for report in reports if report.stance == "candidate")
    recommendation: Recommendation = "reject" if rejection_votes else ("candidate" if candidate_votes >= 2 else "watch")
    reviewer_status = "rejected" if recommendation == "reject" else ("challenged" if gaps else "approved")
    confidence = round(sum(report.confidence for report in reports) / len(reports), 2)
    thesis = (
        f"{symbol} can be considered for equity paper trading only if the existing risk gate approves "
        "fresh quote quality, liquidity, size, and duplicate-order checks."
    )

    return TeamDecision(
        decision_id=f"team_{symbol}_{now.replace(':', '').replace('+', 'Z')}",
        symbol=symbol,
        recommendation=recommendation,
        confidence=confidence,
        tasks=tasks,
        reports=reports,
        reviewer_status=reviewer_status,
        availability_rating=rating,
        source_gaps=gaps,
        thesis=thesis,
        assumptions=sorted({item for report in reports for item in report.assumptions}),
        red_lines=sorted({item for report in reports for item in report.red_lines}),
        invalidation_triggers=sorted({item for report in reports for item in report.invalidation_triggers}),
        generated_at=now,
        quote_seen_at=quote.asof,
    )
