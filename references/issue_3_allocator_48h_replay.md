# Issue #3 Allocator 48-Hour Replay

## Evidence boundary

- Frozen as-of: `2026-08-26T08:16:12+00:00`
- Window start: `2026-08-24T08:16:12+00:00`
- Strategy: `ai_instrument_allocator_v1`
- Immutable snapshots checked: `58`; SHA-256 valid: `58`; invalid: `0`
- Replay model calls: `0`
- Historical orders created: `0`
- Live order tools called: `false`

Hash integrity and point-in-time semantics are different checks. All 58 files
match their immutable hash, but the legacy writer froze the envelope decision
time before network collection. The audit therefore found 229 deduplicated
observation-after-cutoff occurrences: 55 quote timestamps, 58 news
`first_seen_at` timestamps, 58 news retrieval timestamps, and 58 source
retrieval timestamps. Historical files and logs were not rewritten. The new
live path records its cutoff after collection;
explicit replay cutoffs remain fixed and reject later observations.

Because of this legacy timing defect, all 58 snapshots, all 22 linked decisions,
and the one linked allocation are excluded from the strict replayable subset.
The observed audit funnel is still useful for locating the runtime bottleneck,
but it is not presented as lookahead-safe strategy performance. Replay never
invents a missing model response or treats temporally invalid evidence as
permission to trade.

## Observed audit funnel

| Stage | Recorded count |
|---|---:|
| Candidate snapshots | 56 |
| Ranking input | 40 |
| Deep research | 20 |
| Structured decisions, including 2 premarket updates | 22 |
| Watch | 0 |
| Proposal | 2 |
| Allocation | 1 |
| Selected instrument | 0 |
| Paper order | 0 |
| Paper fill | 0 |

## Strict replayable subset

| Stage | Old semantics | New semantics |
|---|---:|---:|
| Candidate snapshots | 0 | 0 |
| Ranking input | 0 | 0 |
| Deep research | 0 | 0 |
| Structured decisions | 0 | 0 |
| Watch | 0 | 0 |
| Proposal | 0 | 0 |
| Allocation/order/fill | 0 | 0 |

The legacy logs suggest an estimated 11 cooldown rejections may have resulted
from rank-only cooldown consumption. That estimate cannot be promoted to an
exact `40 → 51` replay because the old cycle records do not link each skipped
candidate to its prior ranking snapshot or cooldown transition. New cycles now
persist candidate snapshot reference, `ranking_entered`, `deep_research`,
decision outcome, the prior cooldown trigger transition ID, and the new outcome
transition ID, so future windows can calculate the relationship exactly. Strict
proposal delta is zero because there is no
time-valid historical subset and replay does not rerun DeepSeek or promote 17
legacy untyped Challenge vetoes.

The two recorded proposals were XPEV during the regular session and INTU during
overnight research. XPEV reached allocation but failed the signed-direction
gate: bullish mass `0.45`, bearish `0.30`, neutral `0.25`. Its option diagnostic
also considered 20 contracts: 14 exceeded the hard spread limit and 6 had stale
quotes. INTU was a valid conditional overnight plan with `entry_now=false`; the
window ended before its next premarket/open execution stages, so it correctly
had no allocation or order in this replay.

## Blockers

| Blocker | Count | Meaning |
|---|---:|---|
| Spread/liquidity | 20 | 14 wide-spread option quotes plus 6 stale quotes |
| Challenge hard veto | 17 | Legacy untyped vetoes remain fail-closed |
| Cooldown | 16 | 11 are estimated rank-only false suppressions; legacy linkage is incomplete |
| Soft concern | 3 | Diagnostic concern, not an automatic veto under the new contract |
| Model no-trade | 3 | Decision did not form an actionable thesis |
| Direction gate | 1 | XPEV signed mass/margin did not meet deterministic thresholds |

The actual bottleneck is candidate-to-deep-research/proposal conversion, not
the paper broker. No allocator cycle in the window called a live order tool.
The option spread ceiling, account limits, position limits, and all other
deterministic risk rules remain unchanged.
