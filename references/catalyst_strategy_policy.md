# Exa + DeepSeek Catalyst Strategy Policy

`exa_deepseek_catalyst_v1` is independent of `relative_strength_v1` and
`long_directional_options_v1`. It does not require either deterministic
baseline to emit a candidate.

## Candidate discovery

Each regular-session discovery cycle may combine:

- the configured core watchlist;
- a bounded high-market-cap earnings calendar window;
- results from existing saved Robinhood scanners, when present;
- Exa market-event searches;
- price, volume, relative-strength, spread, liquidity, and earnings-surprise
  features calculated from read-only structured market data.

The project does not create or modify scanners. In the currently audited
account `get_scans` returned an empty list, so scanner discovery remains an
optional source until the user creates a saved scan outside this service.

## Model call budget

Candidate extraction and ranking use non-thinking structured calls. At most
three ranked candidates per cycle receive ticker-specific Exa search and the
thinking-enabled Bull/News, Challenge, and Decision stages. All limits are
configuration driven.

## Evidence rules

- Default Exa lookback is 48 hours.
- Published time, event time, first-seen time, and retrieval time are separate.
- Missing event time remains null unless the model explicitly labels it as an
  inference.
- Canonical URL, event fingerprint, and content hash are all used for
  deduplication.
- Raw normalized evidence is stored in immutable files under
  `logs/catalyst_snapshots/`.
- A ticker is not deeply researched again within two hours without a new event.
- The same event fingerprint is not sent to the same model stage again within
  the 24-hour event cooldown.

## Proposal and risk boundary

The Decision Manager may propose only long equity, a fully paid long call, a
fully paid long put, or no trade. The proposal is not an order. Current quotes
are refreshed and the existing deterministic equity, option, and shared-account
risk checks run afterward. Any missing or stale quote, ineligible instrument,
liquidity failure, contract-selection failure, duplicate position, capacity
limit, or Challenge veto produces `no_trade`.

The strategy is `shadow_only`. It writes catalyst decisions and evidence but
does not write equity or option order state and cannot replace an active
strategy without a separate evaluated promotion.
