# LLM News Drift Shadow Policy

## Purpose and boundary

`llm_news_drift_v1` tests a narrow question: can a low-cost, headline-only LLM
classification identify a tradable post-news drift after realistic spread and
slippage? It is independent of technical candidate screening and remains
`shadow_only`. It has no broker, account, position, or live-order dependency.

The first phase permits only positive-news long-equity shadow proposals. A
negative classification is retained as research evidence, but it cannot become
an equity short, option order, or main-account action.

## Forward cycle

1. APScheduler starts one supervised worker every 60 seconds so due labels can
   be observed accurately. Market discovery is independently rate-limited to
   one search every 15 minutes during regular trading and the configured
   premarket/after-hours windows. It does not search throughout the night. The
   worker has a 120-second hard deadline and owns only `news_event_store`.
2. One rotating, market-wide Exa query retrieves recent headlines, publication
   timestamps, source URLs, and bounded inline highlights. No fixed ticker
   watchlist is required at discovery time.
3. Raw evidence is written to an immutable timestamped snapshot. URL, content,
   and event fingerprints prevent the same item from being sent to the model
   repeatedly during the 24-hour event cooldown.
4. `NewsDriftHeadlineAgent` receives headlines, source metadata, optional ticker
   hints, and prior event headlines. It receives no quote, price, volume,
   technical signal, position, or account value.
5. One strict structured-output call maps the ticker and classifies direction,
   event category, materiality, novelty, ambiguity, confidence, and relation:
   `new_event`, `duplicate`, `clarification`, `material_update`,
   `contradiction`, or `follow_up`.
6. Only after classification does Python validate the exact US-listed
   instrument and retrieve Robinhood fundamentals, history, and bid/ask.
7. Deterministic checks reject future or stale quotes, weak liquidity, small
   market cap, wide spread, excessive signal latency, event age over two hours,
   missing pre-event price, and excessive initial reaction. A date-only
   publication timestamp uses `first_seen_at` as the conservative actionable
   event time; the original publication value remains unchanged. A premarket
   signal blocked only by stale market data is rechecked after the open without
   another LLM call. These checks do not alter the LLM signal.
8. A passing positive event creates a local shadow proposal sized to at most
   25% of the independent `$2,000` reference budget. Entry is ask plus adverse
   configured slippage. It never creates a paper or live order.
9. Forward labels are scheduled separately for +1m, +5m, +15m, same-day close,
   next close, and second close. Exit value uses bid minus adverse slippage.

## Event ledger and audit

`state/news_events.sqlite` contains six project-owned tables:

- `news_events`;
- `event_relations`;
- `llm_signals`;
- `tradability_observations`;
- `shadow_proposals`;
- `outcome_labels`.

Every record retains the relevant `published_at`, `first_seen_at`,
`signal_time`, `decision_time`, `outcome_time`, and `label_time`. Exact replay is
idempotent. Duplicate evidence is auditable but does not trigger a new signal;
material updates and contradictions can trigger a new signal.

Independent append-only logs are `news_drift_cycles.jsonl`,
`news_drift_signals.jsonl`, and `news_drift_proposals.jsonl`. Raw snapshots live
under `logs/news_drift_snapshots/`. All runtime files are ignored by Git.

## Evaluation

`scripts/evaluation/evaluate_news_drift.py` reports event-level, firm-day, and
portfolio-day results independently by horizon. It keeps midpoint gross return,
observed spread/slippage-adjusted net return, break-even total cost, and 0/5/10/
25/50 bps sensitivity separate. It also groups by event type, direction, source
tier, and market-cap bucket.

At least 100 valid labels and 20 portfolio days are required before the report
can move beyond `insufficient_forward_evidence`. Even then the lane remains
shadow-only and `promotion_eligible=false`. Exa calls are counted; until a
contracted per-search price is configured, their cost is marked unpriced instead
of silently assumed to be zero.

## Exa feature choice

The current adapter uses Exa Search with inline `contents.highlights`. It does
not use Exa Deep Search, Exa Agent, Monitors, or a separate Contents request.
Deep Search and Agent would duplicate the bounded DeepSeek classification and
would add latency to a time-sensitive path. Monitors may later replace polling
only after delivery timestamps, missed-event recovery, deduplication semantics,
and cost are measured against this SQLite ledger. The separate Contents endpoint
is unnecessary while headline classification is intentionally price- and
body-blind.

## P1 replication boundary

The relevant official replication package is Lopez-Lira and Tang (2026),
Mendeley Data version 2, DOI `10.17632/f39x226htv.2`, released under CC BY 4.0.
It contains two approximately 502 MB ZIP files and instructs users to follow its
README. The associated paper is `arXiv:2304.07619`.

Current forward metrics deliberately mirror firm-day, portfolio-day, horizon,
and cost-sensitivity concepts, but this is not a completed paper replication.
The package must be downloaded into an ignored research directory, its README
and variable definitions audited, and its outputs reproduced independently
before any exact-replication claim.

Official sources:

- https://data.mendeley.com/datasets/f39x226htv/2
- https://arxiv.org/abs/2304.07619

## P2 isolated experiments

The following are future, separately identified experiments and must never be
merged into base-lane results:

- `synthetic_short_equity`: negative-news synthetic stock short with no access
  to an executable account;
- `negative_news_long_put`: fully paid long-put shadow simulation using the
  existing option quote, Greeks, liquidity, expiry, and premium-risk checks;
- `adaptive_event_type_calibration`: walk-forward calibration by event type,
  direction, source tier, and market-cap bucket only after a configured minimum
  sample count.

Long-put returns are not a proxy for stock-short returns. Calibration may adjust
an experimental entry threshold only from matured past labels; it cannot change
hard risk configuration, use future labels, or promote itself into an active
strategy.
