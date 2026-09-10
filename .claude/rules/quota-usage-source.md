# Quota Usage Source

## Rule
Per-user quota usage comes from the **telemetry database**
(`telemetry.unified_hourly_cost`), written to DynamoDB as **absolute totals** with
`SET`. Never reintroduce an accumulator, and never add a second cost formula.

## Why absolute, not accumulated

The original design queried the CloudWatch Prometheus API for a rolling 15-minute
delta and folded it into DynamoDB with `ADD`. That has no watermark, no dedup key and
no conditional write, so correctness depended entirely on the 900s lookback exactly
tiling the 15-minute schedule. Any retried, duplicated or missed invocation
permanently skewed the counter with no path to self-heal — and it did drift, in both
directions.

Absolute `SET` of a month-to-date total is idempotent: invoke twice, get the same
answer. This is the property to protect.

- ✅ `SET total_tokens = :tt, estimated_cost = :cost, ...`
- ❌ `ADD total_tokens :delta, estimated_cost :cost`

Because writes are absolute, they need a **safety floor**: a query returning too few
rows would zero out real accounting. Compare the returned identity count against the
users already in DynamoDB and refuse the whole batch below `DB_MIN_ROW_RATIO`.

## One cost formula

Cost is computed only in `telemetry.calculate_token_cost()`, with the cross-region
inference surcharge applied by the calling view. Do not add a rate table to this repo
(`lambda-functions/shared/pricing.py` was deleted) and do not multiply token counts by
rates in a consumer — a per-user token mix spans models, so any single-model
arithmetic disagrees with the authoritative total. Show token counts instead.

## If you ever do query OTel token metrics again

`claude_code.token.usage` is a **delta**-temporality counter. Use
`sum_over_time(...[900s])`, not `increase()` or `rate()`: those assume cumulative
counters and misread each delta down-step as a counter reset, silently
under-reporting. This bit us once; keep it written down.

## Consumer contract

The producer must keep writing all of these, or something breaks silently:

| Attribute | Consumer | Breaks if dropped |
|---|---|---|
| `email` (top-level) | quota_monitor's own threshold pass | Alerting sees zero users |
| `last_updated` (`Z`-suffixed) | `sidecar_monitor` | False bypass alerts |
| `estimated_cost`, `daily_cost_usd` (Number) | `quota_check`, `ccwb quota` | Cost enforcement/display |
| `daily_date` | stale-day guard in 3 places | Yesterday's total re-alerts today |

`pk` uses the identity's **original casing**, matching the raw OIDC `email` claim that
`quota_check` looks up. Aggregate with `lower()` in SQL to merge any casing split, but
carry a representative original spelling through to the key — lowercasing the key
orphans existing rows.

**Only email identities are written.** `unified_hourly_cost` also carries opaque
non-email `bedrock_user_id` values — service principals and CI role sessions — and they
are filtered out twice, by `user_email LIKE '%@%'` in the SQL and by an `"@" not in email`
skip in `fetch_usage_from_db`. Keep both layers: the SQL one avoids transferring the rows,
the Python one means a query edit cannot quietly start creating quota rows for identities
that have no human to warn or block. Their spend stays visible in the telemetry DB; it is
simply not enforced here.

## Cutover
`QUOTA_WRITE_MODE` defaults to `shadow` (log per-user diffs, write only liveness
attributes). Switching to `enforce` restates every counter in one step, so users move
in both directions and previously uncounted users appear for the first time. Alert
before blocking.

## Related
`.claude/rules/quota-requires-oidc.md`, `.claude/rules/otel-attribution-chain.md`,
`assets/docs/QUOTA_MONITORING.md`
