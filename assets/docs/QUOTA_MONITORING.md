# Claude Code Quota Monitoring

Quota monitoring tracks user token consumption and sends automated alerts when usage thresholds are exceeded, helping administrators manage costs and prevent unexpected overages.

## Overview

The quota monitoring system is an optional CloudFormation stack that integrates with the dashboard stack to track monthly token consumption per user and send SNS alerts at configurable thresholds.

### Key Features

- **Per-user token tracking**: Monthly and daily consumption monitoring for each authenticated user
- **Fine-grained quota policies**: Set limits at user, group, or default levels with precedence rules
- **Multiple limit types**: Monthly tokens and daily tokens
- **Configurable thresholds**: Alerts at 80%, 90%, and 100% of limits
- **JWT group integration**: Automatically extract group membership from identity provider claims
- **Alert deduplication**: One alert per threshold per limit type per user per period
- **DynamoDB storage**: Efficient tracking with automatic TTL cleanup

### Architecture Components

- **Telemetry Database**: TimescaleDB holding per-invocation Bedrock records, priced per model — the authoritative usage source
- **UserQuotaMetrics Table**: DynamoDB table storing monthly/daily usage totals with token type breakdown
- **QuotaPolicies Table**: DynamoDB table storing fine-grained quota policies (user/group/default)
- **Quota Monitor Lambda**: Scheduled function that reads month-to-date usage from the telemetry database, writes it to DynamoDB, and checks thresholds every 15 minutes
- **SNS Topic**: Alert delivery to administrators
- **EventBridge Rule**: Lambda scheduling

## Configuration

> **Prerequisites**: Monitoring must be enabled and the dashboard stack deployed. See the [CLI Reference](CLI_REFERENCE.md#deploy---deploy-infrastructure) for deployment details.

During `ccwb init`, quota monitoring is **enabled by default** when monitoring is enabled. You'll be prompted to configure:
- Monthly token limit per user (default: 225 million tokens)
- Automatic threshold calculation (80% warning at 180M, 90% critical at 202.5M)
- Daily token limit with burst buffer (auto-calculated from monthly)
- Enforcement modes for daily and monthly limits

Deploy using `poetry run ccwb deploy` (deploys all enabled stacks) or `poetry run ccwb deploy quota` for just the quota stack. The OIDC configuration is automatically passed from your profile settings. For complete deployment instructions, see the [CLI Reference](CLI_REFERENCE.md#deploy---deploy-infrastructure).

## Cost-Based Enforcement (Recommended)

Set dollar limits instead of (or alongside) token limits. Cost is calculated in the telemetry database from Bedrock invocation records, not by the Lambda.

### How it works

1. Every Bedrock invocation is recorded by CloudTrail and loaded into `telemetry.bedrock_invocations`
2. The database prices each invocation with `telemetry.calculate_token_cost()` using the invocation's actual model, applies the cross-region inference surcharge, and rolls the result into the `telemetry.unified_hourly_cost` view
3. Every 15 minutes `quota_monitor` reads month-to-date **and** today's cost and tokens per user from that view in a single query
4. It writes those totals to DynamoDB as absolute values (`SET`), so `estimated_cost`, `daily_cost_usd` and the token counts always equal what the database says
5. `quota_check` compares against `monthly_cost_limit` / `daily_cost_limit` from the policy

Two properties follow from step 4:

- **Idempotent.** A retried, duplicated, or missed invocation cannot skew the counters — the next run restates the correct total. The previous design accumulated a rolling 15-minute PromQL delta with DynamoDB `ADD`, which had no watermark and no way to self-heal.
- **Single source of truth.** Cost is computed once, in the database. There is no rate table in this repository and no per-consumer arithmetic to keep in sync.

### Coverage

Usage is derived from CloudTrail, which records every Bedrock invocation regardless of which client made it:

- Claude Code CLI, CoWork Desktop, and any other Bedrock caller are counted by the same path
- Usage is counted even if a user stops their OTEL sidecar — the gap `EnableBypassDetection` exists to *detect* is now closed for *accounting* purposes
- Attribution requires the role session name to carry the user's email, which `credential-process` provides
- **Only identities that look like email addresses are counted.** The database also attributes spend to opaque non-email identities — service principals and CI role sessions — and those are excluded from quota accounting on purpose: there is no human to warn and blocking one would break a pipeline. Their spend is still visible in the telemetry database and its dashboards, it just does not appear in `ccwb quota` or count against anyone's limit.

From the CloudTrail cutover point (`telemetry.bedrock_source_of_truth_cutover()`) onward, Bedrock records are authoritative; before it, OTEL telemetry fills in. The two are de-duplicated per user-day so nothing is counted twice.

### Freshness

`unified_hourly_cost` is refreshed by a database background job every 15 minutes, and `quota_monitor` runs on the same cadence, so a quota decision can lag real spend by up to roughly 30 minutes in the worst case. Combined with credential-refresh-boundary enforcement, this is not an inline control — see [Enforcement Modes](#enforcement-modes).

### Setting cost limits

```bash
# Set $50/month budget for a user (--budget is shorthand for --monthly-cost-limit)
ccwb quota set-user user@company.com --budget 50

# Set $10/day budget for a team
ccwb quota set-group engineering --daily-budget 10

# Interactive mode (prompts for budget when no flags provided)
ccwb quota set-user user@company.com
```

### Pricing rates

Rates live in the telemetry database — in the body of
`telemetry.calculate_token_cost(model, type, tokens)` — not in this repository. They
are updated by replacing that function, not by redeploying the quota stack. The
Lambda has no rate table and no pricing override environment variable.

To see the rates in force:

```sql
SELECT pg_get_functiondef(p.oid)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'telemetry' AND p.proname = 'calculate_token_cost';
```

Rates are matched per model *tier* (`%opus-5%`, `%sonnet-4-5%`, `%haiku-4-5%`, …) and
per token type (`input`, `output`, `cache_creation`, `cache_creation_1h`,
`cache_read`), with both the camelCase and snake_case type spellings normalized.
Three behaviors are worth knowing:

- **Non-Anthropic models cost $0.** Any model that does not match `%claude%`, `%anthropic%`, or `%instant%` returns zero rather than falling through to a Claude rate. If other teams share the Bedrock account, their spend is deliberately not attributed here.
- **Unrecognized Claude models fall back to Sonnet rates.** A brand-new model name is priced as Sonnet until the function is updated, so watch for new model IDs.
- **The cross-region surcharge is applied by the caller, not the function.** `calculate_token_cost` returns the base cost; the `1.1` multiplier for `us.%`-style cross-region inference profiles is applied in the view that rolls the data up. Calling the function directly gives you the base rate only.

### Handles opusplan correctly

Cost is priced per invocation using that invocation's `model_id`, so `opusplan`
(Opus planning + Sonnet execution) is priced as Opus tokens at Opus rates and
Sonnet tokens at Sonnet rates — no blending assumptions.

### Backward compatible

- Cost limits default to 0 (disabled) — existing token-only deployments unaffected
- Token limits still work independently — both can coexist
- Existing DynamoDB items are updated in place; the attribute names (`estimated_cost`, `daily_cost_usd`, `total_tokens`, …) are unchanged, so `quota_check`, `ccwb quota`, and the dashboards need no changes

> **Cutover warning:** switching from the old accumulator to absolute writes restates every user's counters in one step, and values move in *both* directions (the accumulated figures had drifted). Deploy in `shadow` mode first — see [Cutover](#cutover-shadow-mode) below.

> ⚠️ Cost is derived from Bedrock invocation records, not from your bill. Actual billing may differ with committed throughput or custom agreements. Use AWS Cost Explorer for billing truth.

> **Why not use the client-side `claude_code.cost.usage` metric?** Claude Code emits a cost estimate natively, but it uses generic Anthropic rates (not Bedrock-specific), resets per session (not accumulated monthly), and cannot be trusted for enforcement (client-controlled). Deriving cost from CloudTrail invocation records is tamper-resistant and aggregates across all sessions and clients.

> **CoWork support:** CoWork Desktop cost is counted automatically, because its Bedrock calls appear in CloudTrail like any other. It does **not** require the CoWork dashboard stack or the `model` MetricFilter dimension. See [COWORK_3P.md](COWORK_3P.md#how-cowork-usage-is-counted).

## Telemetry Database Configuration

`quota_monitor` requires a connection to the telemetry database. Without
`TelemetryDbSecretArn` it has no usage source: counters are never updated and every
run reports an error.

| Parameter | Default | Description |
|---|---|---|
| `TelemetryDbSecretArn` | *(none)* | Secrets Manager ARN holding the DB credentials. **Required.** |
| `TelemetryDbHost` | *(from secret)* | Overrides the host in the secret. Prefer a DNS name or a stable secondary IP — an autoscaling-managed instance can change its primary private IP. |
| `TelemetryDbPort` | `5432` | |
| `TelemetryDbName` | `claude_telemetry` | |
| `TelemetryDbSslMode` | `disable` | `disable`, `require`, or `verify-full`. |
| `TelemetryDbCaPem` | *(none)* | CA bundle; required for `verify-full`. |
| `TelemetryDbVpcId` | *(none)* | VPC to attach the Lambda to. |
| `TelemetryDbSubnetIds` | *(none)* | Private subnets that can reach the database. |
| `TelemetryDbEgressCidr` | *(none)* | CIDR the Lambda may open DB connections to. |
| `QuotaWriteMode` | `shadow` | `shadow` (compare only) or `enforce` (write totals). |
| `QuotaDbMinRowRatio` | `0.5` | Refuse to write if the query returns fewer than this fraction of the users already in DynamoDB. |

Notes on the TLS default and networking:

- `TelemetryDbSslMode` defaults to `disable` because a self-hosted TimescaleDB instance may be built with `ssl = off`; in that case any other value fails to connect. Confirm with `SHOW ssl;`. **When SSL is off, database traffic is unencrypted** — acceptable only within a trusted VPC. Prefer `verify-full` with a CA bundle where the server supports TLS. `require` encrypts but does **not** verify the server certificate, so it is not resistant to an active man-in-the-middle.
- Attaching the Lambda to a VPC means its DynamoDB, SNS, and Secrets Manager calls leave through that VPC. If there are no VPC endpoints, they depend on a NAT gateway, which becomes a single point of failure for quota monitoring.
- The connection uses a `statement_timeout` and a connect timeout well below the Lambda timeout, so a hung database surfaces as an error rather than a timeout.

### Safety floor

Because writes are absolute, a query that returns too few rows would zero out real
accounting. Before writing, the Lambda compares the number of identities returned
against the number of current-month users already in DynamoDB and refuses the whole
batch if the ratio is below `QuotaDbMinRowRatio`, publishing an operational alert
instead. A database error does the same. In both cases threshold checking still runs
against the existing DynamoDB values, so alerting does not stop.

### Cutover (shadow mode)

`QuotaWriteMode=shadow` is the default and is where a first deployment should stay.

1. Deploy with `QuotaWriteMode=shadow`. The Lambda queries the database and logs the per-user difference against the current DynamoDB values, but changes no counters. It still stamps `last_updated`, so bypass detection keeps working.
2. Compare a few cycles. Expect movement in **both** directions: the old accumulator drifted, so some users will be higher and some lower. Investigate any user whose value changes by an implausible amount.
3. Expect newly visible users. Anyone who used Bedrock without the OTEL sidecar had no counters at all and will appear for the first time — possibly already above a limit. Set `MonthlyEnforcementMode=alert` for the first enforced cycles so nobody is blocked by a restated total, and warn affected users.
4. Switch to `QuotaWriteMode=enforce` and re-enable blocking.

To verify idempotency after cutover, invoke the Lambda twice and confirm
`estimated_cost` is **unchanged**. Under the old `ADD` behavior it would have doubled.


## Token-Based Limits (Legacy)

| Parameter               | Default     | Description                                    |
| ----------------------- | ----------- | ---------------------------------------------- |
| MonthlyTokenLimit       | 225M tokens | Default maximum per user per month             |
| DailyTokenLimit         | ~8.25M tokens| Daily limit (auto-calculated with burst buffer)|
| BurstBufferPercent      | 10%         | Daily buffer for usage variation (5-25%)       |
| MonthlyEnforcementMode  | block       | Block access when monthly limit exceeded       |
| DailyEnforcementMode    | alert       | Alert only when daily limit exceeded           |
| Warning Threshold       | 80% (180M)  | First alert level                              |
| Critical Threshold      | 90% (202.5M)| Second alert level                             |
| Check Frequency         | 15 minutes  | Lambda execution interval                      |
| Alert Retention         | 60 days     | DynamoDB TTL for deduplication                 |
| EnableFinegrainedQuotas | false       | Enable fine-grained policy support             |

To update limits: Re-run `ccwb init` and redeploy with `ccwb deploy quota`.

## Daily Limits and Bill Shock Protection

To prevent unexpected costs from runaway usage, the system auto-calculates a daily limit from your monthly quota with a configurable burst buffer.

### Why Daily Limits?

Without daily limits, a user could consume their entire monthly quota in just 2-3 days of heavy usage, leading to unexpected costs or blocked access mid-month. Daily limits catch runaway usage within 24 hours while still allowing legitimate work patterns.

### Calculation

```
daily_limit = monthly_limit ÷ 30 × (1 + burst_buffer%)
```

Example with 225M monthly limit and 10% burst:
- Base daily: 225,000,000 ÷ 30 = 7,500,000 tokens/day
- With 10% burst: 7,500,000 × 1.10 = **8,250,000 tokens/day**

### Burst Buffer Guidance

The burst buffer allows for legitimate daily variation above the average:

| Buffer | Daily (225M/month) | Use Case |
|--------|-------------------|----------|
| 5% (strict)  | 7,875,000 tokens | Tight cost control, heavy days blocked quickly |
| 10% (default)| 8,250,000 tokens | Balanced protection for typical usage |
| 25% (flexible)| 9,375,000 tokens | Allows 1.25x average days, catches only extreme spikes |

### Enforcement Modes

Each limit type can be configured with different enforcement:

| Mode | Behavior | Use Case |
|------|----------|----------|
| **alert** | Send notifications, allow continued use | Monitoring, soft limits |
| **block** | Deny credential issuance when exceeded | Hard cost control |

**Recommended defaults:**
- **Daily**: `alert` - Warn about unusual patterns, don't interrupt work
- **Monthly**: `block` - Hard stop at budget limit

### Example Configuration

```
Monthly Limit: 225,000,000 tokens (block)
Daily Limit:   8,250,000 tokens (alert)
Burst Buffer:  10%

Behavior:
- Day 1: User consumes 9M tokens → Daily alert sent
- Day 2: User consumes 8.5M tokens → Daily alert sent
- Day 3-5: Normal usage (~7M/day) → No alerts
- Day 15: Monthly usage reaches 180M → 80% warning alert
- Day 20: Monthly usage reaches 225M → Access blocked
```

## Fine-Grained Quota Policies

Fine-grained quotas allow administrators to set different limits for different users and groups, with a clear precedence hierarchy.

### Policy Types

1. **User Policies**: Apply to a specific user by email address
2. **Group Policies**: Apply to all users in a group (from JWT claims)
3. **Default Policy**: Applies to all users without a more specific policy

### Policy Precedence

When determining the effective quota for a user:

1. **User-specific policy** (highest priority): If a policy exists for the user's email, use it
2. **Group policy** (most restrictive): If user belongs to multiple groups with policies, use the **lowest limit** (most restrictive)
3. **Default policy**: If no user or group policy applies, use the default
4. **No policy**: If no policies are defined, usage is **unlimited** (quota monitoring disabled for that user)

### Limit Types

Each policy can configure two types of limits:

| Limit Type           | Description                        | Reset Period     |
| -------------------- | ---------------------------------- | ---------------- |
| Monthly Token Limit  | Maximum tokens per calendar month  | 1st of each month|
| Daily Token Limit    | Maximum tokens per day             | UTC midnight     |

### Managing Policies with CLI

Use the `ccwb quota` commands to manage policies:

```bash
# Set a user-specific policy
ccwb quota set-user john.doe@company.com --monthly-limit 500M --daily-limit 20M

# Set a group policy
ccwb quota set-group engineering --monthly-limit 400M

# Set the default policy for all users
ccwb quota set-default --monthly-limit 225M --daily-limit 8M

# List all policies
ccwb quota list
ccwb quota list --type group

# Show effective policy for a user
ccwb quota show john.doe@company.com --groups "engineering,ml-team"

# View current usage against limits
ccwb quota usage john.doe@company.com

# Delete a policy
ccwb quota delete group engineering

# Temporarily unblock a user who exceeded quota (Phase 2)
ccwb quota unblock john.doe@company.com --duration 24h
```

### Token Value Shortcuts

The CLI supports human-readable token values:

- `225M` = 225,000,000 (225 million) - default limit
- `500K` = 500,000 (500 thousand)
- `1B` = 1,000,000,000 (1 billion)

### Group Membership from JWT Claims

The system automatically extracts group membership from JWT token claims:

- `groups`: Standard groups claim
- `cognito:groups`: Amazon Cognito groups
- `custom:department`: Custom department claim (treated as a group)

Configure your identity provider to include group claims in the JWT tokens issued to users.

## Alert Management

After deployment, subscribe to the SNS topic for notifications:

```bash
# Get topic ARN from stack outputs
aws cloudformation describe-stacks --stack-name <quota-stack-name> \
  --query 'Stacks[0].Outputs[?OutputKey==`QuotaAlertTopicArn`].OutputValue' \
  --output text

# Subscribe (email, SMS, HTTPS webhook, etc.)
aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint admin@company.com
```

### Alert Types

The system sends alerts for two limit types, each with three threshold levels:

#### Monthly Token Alert

Sent when monthly token usage exceeds 80%, 90%, or 100% of the monthly limit.

#### Daily Token Alert

Sent when daily token usage exceeds 80%, 90%, or 100% of the daily limit. Daily alerts can be sent each day (they include the date in the deduplication key).

### Sample Alert Content

```
Subject: Claude Code CRITICAL - Monthly Token Quota - 92%

Claude Code Usage Alert - Monthly Token Quota

User: john.doe@company.com
Alert Level: CRITICAL
Month: November 2025
Policy: group:engineering

Current Usage: 207,000,000 tokens
Monthly Limit: 225,000,000 tokens
Percentage Used: 92.0%

Days Remaining in Month: 8
Daily Average: 9,409,091 tokens
Projected Monthly Total: 282,272,727 tokens

---
This alert is sent once per threshold level per month.
```

Alerts are deduplicated - each threshold triggers only once per user per period, with history stored in DynamoDB (60-day TTL).

## User Notifications

When users approach or exceed their quota limits, they receive visual notifications in both the terminal and browser.

### Browser Notification

The credential provider opens a browser page showing quota status when:

| Condition | Browser Opens? | Access Granted? |
|-----------|----------------|-----------------|
| Within quota (<80%) | No | Yes |
| Warning (80-99%) | Yes (yellow) | Yes |
| Blocked (100%+) | Yes (red) | No |

The browser page displays:
- **Status header**: Warning (⚠️) or Blocked (🚫)
- **Monthly usage**: Progress bar with percentage
- **Daily usage**: Progress bar with percentage (if daily limits configured)
- **Message**: Explanation and guidance

### Terminal Output

In addition to browser notifications, the terminal shows:

**Warning (80%+ usage):**
```
============================================================
QUOTA WARNING
============================================================
  Monthly: 180,000,000 / 225,000,000 tokens (80.0%)
  Daily: 6,600,000 / 8,250,000 tokens (80.0%)
============================================================
```

**Blocked (100%+ usage):**
```
============================================================
ACCESS BLOCKED - QUOTA EXCEEDED
============================================================

Monthly quota exceeded: 225,000,000 / 225,000,000 tokens (100.0%).
Contact your administrator for assistance.

Current Usage:
  Monthly: 225,000,000 / 225,000,000 tokens (100.0%)

Policy: user:john.doe@company.com

To request an unblock, contact your administrator.
============================================================
```

### Periodic Quota Re-Check

By default, quota is re-checked every 30 minutes even when credentials are cached. This closes the enforcement gap where users could continue working for up to 12 hours after being blocked (the credential cache duration).

Configure during `ccwb init`:

| Interval | Check Frequency | Max Enforcement Delay | UX Impact |
|----------|----------------|----------------------|-----------|
| 0 | Every request | Immediate | ~200ms per request |
| 15 | Every 15 min | 15 minutes | Minimal |
| 30 (default) | Every 30 min | 30 minutes | Imperceptible |
| 60 | Every hour | 1 hour | None |

**How it works:**

1. User requests credentials (cached or fresh)
2. If last quota check was more than `interval` minutes ago:
   - Call quota API (~200ms)
   - Update timestamp
3. If blocked: Show browser notification, deny credentials
4. If warning (80%+): Show browser notification, issue credentials
5. If OK: Issue credentials silently

**Trade-offs:**

- **Interval = 0** (strictest): Every request checks quota. Adds ~200ms latency to each credential request. Use for strict cost control where immediate enforcement is critical.
- **Interval = 30** (recommended): Balance between enforcement tightness and user experience. Users are blocked within 30 minutes of exceeding quota.
- **Interval = 60+** (relaxed): Minimal impact but users may work up to an hour after being blocked.

The check happens in the background when returning cached credentials - users only see a browser notification if their quota status changes.

## Bulk Policy Management

For organizations with many users, the CLI provides import/export commands to manage policies in bulk.

### Export Policies

Export existing policies to JSON or CSV for backup, audit, or migration:

```bash
# Export all policies to JSON
ccwb quota export policies.json

# Export to CSV for spreadsheet editing
ccwb quota export policies.csv

# Export only user policies
ccwb quota export users.json --type user
```

### Import Policies

Import policies from a file:

```bash
# Import from CSV, creating new and updating existing
ccwb quota import users.csv --update

# Preview changes without applying
ccwb quota import users.csv --dry-run

# Auto-calculate daily limits (monthly / 30 + burst buffer)
ccwb quota import users.csv --auto-daily --burst 15
```

### CSV Template

Create a CSV file with these columns:

```csv
type,identifier,monthly_token_limit,daily_token_limit,enforcement_mode,enabled
user,alice@example.com,300M,15M,alert,true
user,bob@example.com,200M,,block,true
group,engineering,500M,25M,alert,true
default,default,225M,8M,alert,true
```

**Required columns:** `type`, `identifier`, `monthly_token_limit`

**Token format:** Supports `K` (thousands), `M` (millions), `B` (billions), e.g., `300M` = 300,000,000 tokens

### Typical Workflow

1. **Initial setup from HR system:**
   ```bash
   # Export user list from HR, create CSV
   ccwb quota import users.csv --auto-daily --update
   ```

2. **Backup before changes:**
   ```bash
   ccwb quota export backup-$(date +%Y%m%d).json
   ```

3. **Cross-environment sync:**
   ```bash
   # Export from staging
   ccwb quota export policies.json --profile staging

   # Import to production
   ccwb quota import policies.json --profile production --update
   ```

See [CLI Reference](CLI_REFERENCE.md#quota-export---export-policies) for full documentation.

## Troubleshooting

### Quick Checks

```bash
# View Lambda logs
aws logs tail /aws/lambda/claude-code-quota-monitor --follow

# Query user quotas
aws dynamodb scan --table-name UserQuotaMetrics \
  --projection-expression "email, total_tokens, daily_tokens"

# List quota policies
aws dynamodb scan --table-name QuotaPolicies \
  --filter-expression "sk = :current" \
  --expression-attribute-values '{":current": {"S": "CURRENT"}}'
```

### Common Issues

- **No alerts**: Verify SNS subscriptions are confirmed and EventBridge rule is enabled. A topic with zero subscriptions swallows every alert silently — `publish` succeeds, nobody is notified. Check with `aws sns list-subscriptions-by-topic`.
- **Alerts stop after the first one per user**: expected. Alerts are deduplicated per month by `{email}#{alert_type}#{level}`, recorded under `pk=ALERTS`. A user already alerted at `warning` will not re-alert at `warning` again this month, but will still alert when they cross `critical` and `exceeded`.
- **Missing users**: Check JWT tokens include email claim
- **Wrong policy applied**: Verify group claims are present in JWT tokens
- **Groups not detected**: Check that `ENABLE_FINEGRAINED_QUOTAS` is set to `true`
- **Reading the `Summary` log line**: it is tagged with the dimension it measured — `Summary (cost)` counts users against their `MonthlyCostLimitUsd` budget, `Summary (tokens)` against `MonthlyTokenLimit`. A `$` budget takes precedence when both are set, since that is the limit `quota_check` enforces. `Summary (no limits set)` means both limits are 0 and nothing is being enforced at all.

### Database connectivity

The monitor returns HTTP 500 and logs an error whenever it cannot read usage. Look
for these in the Lambda logs:

- **`TELEMETRY_DB_SECRET_ARN is not configured`** — the stack was deployed without a telemetry database. Set it via `ccwb init` and redeploy.
- **Connection timeouts** — the Lambda cannot reach the database. Check that `TelemetryDbSubnetIds` are in a VPC with a route to it, that the database's security group allows the monitor's security group (or its subnet CIDRs) on the DB port, and that `TelemetryDbHost` still resolves. An autoscaling-managed instance can change its primary private IP; prefer a DNS name or a stable secondary IP.
- **SSL errors** — the server may have `ssl = off`. Confirm with `SHOW ssl;` and set `TelemetryDbSslMode=disable` if so, or supply `TelemetryDbCaPem` for `verify-full`.
- **Timeouts on DynamoDB/SNS/Secrets Manager rather than the database** — once the function is attached to a VPC, those calls need NAT egress or VPC endpoints. A missing route here looks like an unrelated failure.
- **`Refusing to write` with an operational alert** — the safety floor tripped: the query returned far fewer identities than DynamoDB already has. Run the query manually before overriding `QuotaDbMinRowRatio`; a genuinely truncated result written as absolute values would zero out real accounting.

To run the monitor's query by hand, tunnel to the database and query the same view
the Lambda reads:

```bash
aws ssm start-session --region us-east-1 --target <instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<db-host>"],"portNumber":["5432"],"localPortNumber":["15432"]}'
```

```sql
SELECT count(*) AS identities, round(sum(cost_usd)::numeric, 2) AS mtd_cost
  FROM telemetry.unified_hourly_cost
 WHERE time >= date_trunc('month', now());
```

If that returns plausible numbers but DynamoDB does not change, check
`QUOTA_WRITE_MODE` — in `shadow` mode the monitor logs differences without writing
counters.

For detailed monitoring setup, see the [Monitoring Guide](MONITORING.md).

## Cost Considerations

**Estimated monthly costs for <1000 users: $2-10**
- Lambda: ~2,880 invocations x $0.0000002 = $0.58
- DynamoDB: Pay-per-request for user count x 2,880 operations
- SNS: $0.50 per million notifications
- CloudWatch Logs: Standard retention pricing
- QuotaPolicies table: Minimal cost (policies rarely change)
- NAT gateway data processing, if the function runs in a VPC without VPC endpoints (the queries themselves are small, but all DynamoDB/SNS/Secrets Manager traffic now traverses NAT)

The CloudWatch Prometheus-compatible API queries the old design made are no longer
performed, so that cost goes away.

## Data Schema

### UserQuotaMetrics Table

**User Totals**: `PK: USER#{email}`, `SK: MONTH#{YYYY-MM}`
- Attributes: `total_tokens`, `daily_tokens`, `daily_date`, `input_tokens`, `output_tokens`, `cache_tokens`, `estimated_cost`, `daily_cost_usd`, `cost_source`, `groups`, `last_updated`, `email`
- All usage attributes are **absolute month-to-date totals** restated on every run, not running sums. `daily_tokens`/`daily_cost_usd` apply to `daily_date`; consumers treat them as 0 when `daily_date` is not today.
- `cache_tokens` counts cache **reads** only. Cache-creation tokens are included in `total_tokens` and priced, but are not broken out separately.
- `cost_source` records which relation the figures came from (e.g. `telemetry.unified_hourly_cost`), so a stale item written by an older version is identifiable.
- `estimated_cost` and `daily_cost_usd` are DynamoDB **Numbers**, not strings.
- TTL: End of following month

**Alert History**: `PK: ALERTS`, `SK: {YYYY-MM}#ALERT#{email}#{type}#{level}[#{date}]`
- Attributes: `sent_at`, `alert_type`, `alert_level`, `usage_at_alert`, `policy_info`
- TTL: 60 days

### QuotaPolicies Table

**Policy Records**: `PK: POLICY#{type}#{identifier}`, `SK: CURRENT`
- Attributes: `policy_type`, `identifier`, `monthly_token_limit`, `daily_token_limit`, `warning_threshold_80`, `warning_threshold_90`, `enforcement_mode`, `enabled`, `created_at`, `updated_at`, `created_by`

**GSI: PolicyTypeIndex**
- PK: `policy_type` (user, group, default)
- SK: `identifier`
- Enables efficient queries like "list all group policies"

## Migration from Basic Quotas

If you're upgrading from the basic quota system (single global limit):

1. Deploy the updated CloudFormation stack (adds QuotaPolicies table)
2. Existing UserQuotaMetrics data continues working (new fields are nullable)
3. Set `EnableFinegrainedQuotas: true` in stack parameters
4. Optionally create a default policy to maintain previous behavior:
   ```bash
   ccwb quota set-default --monthly-limit 225M
   ```
5. Gradually add group/user policies as needed

**No breaking changes** - this is an enhancement that's opt-in through policy creation.

## Access Blocking (Phase 2)

When `enforcement_mode` is set to `"block"` for a policy, the system will deny credential issuance when a user exceeds their quota limits.

### How Blocking Works

1. **Quota Check API**: A real-time API endpoint checks user quota before credential issuance
2. **Enforcement Point**: The credential provider calls the quota check API after OIDC authentication
3. **Block Triggers**: Access is blocked when:
   - Monthly token usage ≥ monthly_token_limit
   - Daily token usage ≥ daily_token_limit (if configured)

### Configuring Blocking

Enable blocking for a policy:

```bash
# Set user policy with blocking enabled
ccwb quota set-user john.doe@company.com --monthly-limit 10M --enforcement block

# Set group policy with blocking
ccwb quota set-group engineering --monthly-limit 50M --enforcement block

# Set default with blocking
ccwb quota set-default --monthly-limit 225M --enforcement block
```

### Admin Override (Unblock)

Administrators can temporarily unblock users who have exceeded their quota:

```bash
# Unblock for 24 hours (default)
ccwb quota unblock john.doe@company.com

# Unblock for 7 days
ccwb quota unblock john.doe@company.com --duration 7d

# Unblock until end of month (quota reset)
ccwb quota unblock john.doe@company.com --duration until-reset

# With reason
ccwb quota unblock john.doe@company.com --duration 24h --reason "Urgent project deadline"
```

The unblock record expires automatically and is cleaned up by DynamoDB TTL.

### Error Handling: Fail-Open vs Fail-Closed

By default, the system uses **fail-open** behavior - if the quota check API is unavailable, access is allowed. This prevents service disruptions due to network issues.

Configure fail mode in your profile config:

```json
{
  "quota_fail_mode": "open"   // Allow on error (default)
  // OR
  "quota_fail_mode": "closed" // Deny on error (stricter)
}
```

The 15-minute Lambda monitoring job continues to run regardless, so alerts will still be sent even if real-time checks fail.

### Quota Check API

The Quota Check API is a secured HTTP endpoint that validates user quotas before credential issuance.

#### API Security

The API requires JWT authentication using your OIDC provider's tokens:

> **IAM Identity Center users**: Quota enforcement uses IAM SigV4 authentication instead of JWT. The credential-process signs the quota API request with SigV4 (`execute-api` service). API Gateway validates IAM credentials, and the quota Lambda extracts the user email from the caller ARN session name (`arn:aws:sts::ACCOUNT:assumed-role/Role/user@company.com`). Same per-user DynamoDB lookup and enforcement as OIDC. See [IAM Identity Center Setup](providers/iam-identity-center-setup.md) for details.

- **Authentication**: JWT token in `Authorization: Bearer <token>` header (OIDC) or SigV4-signed request (IDC)
- **Validation**: API Gateway JWT Authorizer validates the token against your OIDC provider
- **User Identity**: Email and group membership extracted from validated JWT claims (no query parameters)

This ensures:
- Only authenticated users can check quotas
- User identity cannot be spoofed (claims come from validated JWT)
- No additional credentials needed (uses same OIDC token from auth flow)

#### Deployment Configuration

When using `ccwb deploy quota`, the OIDC configuration is **automatically passed** from your profile settings (configured during `ccwb init`). No manual parameter configuration is required.

For manual CloudFormation deployments, provide your OIDC configuration:

```bash
aws cloudformation deploy \
  --stack-name claude-code-quota \
  --template-file quota-monitoring.yaml \
  --parameter-overrides \
    OidcIssuerUrl="https://company.okta.com" \
    OidcClientId="your-client-id" \
    # ... other parameters
```

The OIDC parameters must match your credential provider configuration:
- `OidcIssuerUrl`: Your identity provider's issuer URL (e.g., `https://company.okta.com` for Okta)
- `OidcClientId`: The client ID configured in your identity provider

After deploying, get the API endpoint from stack outputs:

```bash
# Get quota check API endpoint
aws cloudformation describe-stacks --stack-name <quota-stack-name> \
  --query 'Stacks[0].Outputs[?OutputKey==`QuotaCheckApiEndpoint`].OutputValue' \
  --output text
```

Configure the endpoint in your credential provider config.json:

```json
{
  "profiles": {
    "ClaudeCode": {
      "quota_api_endpoint": "https://xxx.execute-api.us-east-1.amazonaws.com"
    }
  }
}
```

#### API Responses

| Scenario | HTTP Status | Response |
|----------|-------------|----------|
| No/invalid JWT | 401 | Unauthorized (API Gateway rejects) |
| Valid JWT, quota OK | 200 | `{"allowed": true, ...}` |
| Valid JWT, quota exceeded | 200 | `{"allowed": false, "reason": "monthly_exceeded", ...}` |
| Valid JWT, missing email claim | 200 | `{"allowed": true, "reason": "missing_email_claim"}` (fail-open) |

### Enforcement Timing

**Important**: Quota enforcement occurs at credential issuance time — every time the credential-process exchanges tokens for AWS credentials. This includes both browser re-authentication and silent refresh (via stored refresh_token).

With silent refresh enabled (default since June 2026), the credential-process automatically renews credentials without browser interaction. Quota is checked on each renewal, so enforcement gaps are bounded by the STS session duration, not by how often the user sees a browser prompt.

#### Example Timeline (1-hour STS session, silent refresh)

```
09:00 - User authenticates via browser, quota check passes (at 50%)
09:00 - AWS credentials issued, valid for 1 hour
10:00 - Credentials expire, silent refresh triggers
10:00 - Quota check passes (at 80%), new credentials issued
11:00 - Silent refresh triggers again
11:00 - Quota check BLOCKS access (user exceeded limit)
```

The enforcement gap equals the STS session duration (typically 1 hour), regardless of refresh_token lifetime.

#### Recommendation for Tight Enforcement

Reduce `max_session_duration` when blocking is enabled:

| Session Duration | Enforcement Gap | Use Case |
|------------------|-----------------|----------|
| 12h (default) | Up to 12 hours | Alert-only mode |
| 4h | Up to 4 hours | Moderate enforcement |
| 1h (recommended) | Up to 1 hour | Strict cost control |

Configure in your profile:

```json
{
  "profiles": {
    "ClaudeCode": {
      "max_session_duration": 3600,
      "quota_api_endpoint": "https://xxx.execute-api.us-east-1.amazonaws.com"
    }
  }
}
```

**Trade-off**: Shorter sessions mean more frequent re-authentication prompts for users, but provide tighter quota enforcement.

## Sidecar Bypass Detection

In sidecar mode, per-user token usage is measured from telemetry the local OTEL
sidecar sends to CloudWatch. If a developer stops the sidecar on their machine,
their usage stops being counted — so the quota check never sees them exceed a
limit, even though they can still invoke Bedrock. This is an inherent property
of client-side telemetry.

Sidecar bypass detection is an **opt-in detective control** that surfaces this.
It does not block access; it reports which users are invoking Bedrock without
reporting telemetry, so administrators can follow up.

### How It Works

A scheduled Lambda (`claude-code-bypass-detection`) runs every 15 minutes and:

1. Queries **CloudTrail** for Bedrock invocation events (`InvokeModel`,
   `InvokeModelWithResponseStream`, `Converse`, `ConverseStream`) in the last
   window. These are logged as CloudTrail **management events** — captured by
   default, with no trail or data-event charges. The caller's email is read from
   the assumed-role session name (`assumed-role/<role>/<email>`), making this a
   tamper-proof source of truth for who actually used Bedrock.
2. For each of those (typically few) active users, does a single DynamoDB
   `GetItem` point read on their `UserQuotaMetrics` record and checks whether
   `last_updated` falls within the window. This scales with the number of
   *active* users, not the total user count — no full-table scan.
3. A user active in CloudTrail whose record is missing or stale has a
   stopped/bypassed sidecar.
4. Publishes CloudWatch metrics under the `ClaudeCode/SidecarHealth` namespace
   (`SidecarStopped` per user, `SidecarStoppedUserCount` aggregate) and sends an
   SNS alert (via the existing quota alert topic) listing affected users.

### Configuration

Disabled by default (opt-in). Enable during `ccwb init` (sidecar mode only) or
via the `EnableBypassDetection` parameter on the quota stack. In central mode the
collector runs server-side (users cannot stop it), so this control is disabled
automatically.

```bash
# Enable during init
ccwb init  # Select "Yes" for bypass detection when prompted (sidecar mode)

# Or enable on existing deployment
ccwb deploy quota --parameters EnableBypassDetection=true
```

| Parameter | Default | Description |
| --- | --- | --- |
| EnableBypassDetection | false | Enable sidecar bypass detection (sidecar mode) |
| BypassDetectionLookbackMinutes | 15 | Detection window; should match the detection schedule |

### Limitations

- **Detective, not preventive.** It reports bypass; it does not block Bedrock.
  For tamper-proof enforcement, source quota usage from Bedrock model invocation
  logging (server-side) — a larger change tracked as a future enhancement.
- Relies on Bedrock runtime calls being present in CloudTrail management events
  (the default). Detection lag is one schedule interval (15 minutes).
- Alerts fire every schedule interval (15 min) while bypass is active. To reduce
  notification frequency, create a CloudWatch Alarm on `SidecarStoppedUserCount`
  with a longer evaluation period (e.g., 1 hour) instead of relying on raw SNS.

## Current Limitations

- Quotas reset on calendar month/day (UTC timezone)
- Requires email claim in JWT tokens, or email as IAM session name for Identity Center users (see [IAM Identity Center Setup](providers/iam-identity-center-setup.md#quota-enforcement))
- Group membership requires JWT group claims from identity provider (not available for IDC users — user-level policies only)
- Enforcement only at credential issuance (see [Enforcement Timing](#enforcement-timing) for mitigation)

## CoWork 3P Usage Counting

When the CoWork dashboard stack is deployed, CoWork (Claude Desktop) token usage is automatically counted toward the same per-user quota as Claude Code:

| Source | Namespace | Metric | Dimension |
|--------|-----------|--------|-----------|
| Claude Code | `ClaudeCode` | `claude_code.token.usage` | `user.email` |
| CoWork 3P | `ClaudeCoWork` | `token.usage.input` / `token.usage.output` | `user_email` |

The `quota_monitor` Lambda queries both namespaces and merges the results into a single DynamoDB record per user. This means:

- `ccwb quota usage <email>` shows combined Claude Code + CoWork usage
- Quota limits apply to the combined total
- A user hitting their limit on CoWork will be blocked on the next Claude Code credential refresh (and vice versa)

**Requirements:**
- CoWork monitoring stack deployed (`ccwb deploy --stack cowork-dashboard`)
- Attribution headers configured (collector injects `user_email` from `x-user-email` HTTP header)
- Central or sidecar monitoring mode (both support CoWork telemetry counting)

**Without attribution headers:** CoWork usage is aggregate-only and cannot be counted toward individual user quotas. The `quota_monitor` CoWork query gracefully returns empty results.

## Data Latency

Different data paths have different latency characteristics:

| Data path | Latency | Use case |
| --- | --- | --- |
| Quota enforcement (DynamoDB) | ~1-5 seconds | Real-time quota checks |
| CloudWatch metrics/dashboards | ~1-5 minutes | Live operational monitoring |
| Analytics (Firehose → S3 → Athena) | Up to 15 minutes | Historical reporting, cost analysis |

The analytics pipeline uses Kinesis Firehose with a configurable buffer interval
(`FirehoseBufferInterval` parameter, default 900 seconds / 15 minutes). Firehose
accumulates records before flushing to S3 to reduce cost and API calls.

To reduce analytics latency, lower the buffer interval (minimum 60 seconds) at
the cost of more frequent S3 writes:

```bash
ccwb deploy analytics --parameters FirehoseBufferInterval=60
```

## Future Enhancements

- **Tamper-proof enforcement**: Source quota usage from Bedrock model invocation
  logging (server-side) so usage is counted even if the sidecar is stopped,
  upgrading sidecar health monitoring from detective to preventive.
- **Bulk import/export**: Manage policies via JSON files
- **Quota reporting**: Generate usage reports across all users

## Integration Points

- **Dashboard**: Shares DynamoDB metrics table and OTEL pipeline
- **Analytics**: Quota data available in Athena queries (see [Analytics Guide](ANALYTICS.md))
- **External Systems**: SNS topic supports webhooks, Lambda triggers, and third-party integrations
- **Identity Provider**: Group membership extracted from JWT claims

For complete monitoring setup and general telemetry information, see the [Monitoring Guide](MONITORING.md).
