# ABOUTME: Lambda function that monitors user token quotas and sends SNS alerts
# ABOUTME: Reads authoritative per-user cost from TimescaleDB, writes absolute totals to DynamoDB, checks thresholds

import json
import boto3
import os
import ssl
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key, Attr

# Initialize clients
dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")
secrets_client = boto3.client("secretsmanager")

# Configuration from environment
QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
ENABLE_FINEGRAINED_QUOTAS = os.environ.get("ENABLE_FINEGRAINED_QUOTAS", "false").lower() == "true"

# Default limits
MONTHLY_TOKEN_LIMIT = int(os.environ.get("MONTHLY_TOKEN_LIMIT", "300000000"))
WARNING_THRESHOLD_80 = int(os.environ.get("WARNING_THRESHOLD_80", "240000000"))
WARNING_THRESHOLD_90 = int(os.environ.get("WARNING_THRESHOLD_90", "270000000"))
# Cost-based limits ($/user). 0 disables. Cost mode sets the token limits to 0
# (token alerts are skipped at 0 — see check_limits_and_generate_alerts).
MONTHLY_COST_LIMIT_USD = float(os.environ.get("MONTHLY_COST_LIMIT_USD", "0") or 0)
DAILY_COST_LIMIT_USD = float(os.environ.get("DAILY_COST_LIMIT_USD", "0") or 0)

# DynamoDB tables
quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE) if POLICIES_TABLE else None

# --- TimescaleDB (authoritative cost source) ---------------------------------
# Host/port/dbname come from CFN env vars, NOT from the secret. The secret is
# owned by another team's Terraform and its `host` key points at the instance's
# PRIMARY private IP, which is not stable across ASG replacement. We connect to
# a stable SECONDARY private IP on the same ENI instead. The secret's host is
# only a fallback if the env var is empty.
TELEMETRY_DB_HOST = os.environ.get("TELEMETRY_DB_HOST", "")
TELEMETRY_DB_PORT = int(os.environ.get("TELEMETRY_DB_PORT", "5432") or 5432)
TELEMETRY_DB_NAME = os.environ.get("TELEMETRY_DB_NAME", "claude_telemetry")
TELEMETRY_DB_SECRET_ARN = os.environ.get("TELEMETRY_DB_SECRET_ARN", "")
# The server currently reports `SHOW ssl` = off, so `require` would fail the
# connection outright; the default is therefore `disable` (plaintext inside the
# VPC). Flip to `verify-full` once the DB team enables TLS and provides a CA.
TELEMETRY_DB_SSL_MODE = os.environ.get("TELEMETRY_DB_SSL_MODE", "disable").lower()
TELEMETRY_DB_CA_PEM = os.environ.get("TELEMETRY_DB_CA_PEM", "")
DB_CONNECT_TIMEOUT = int(os.environ.get("TELEMETRY_DB_CONNECT_TIMEOUT", "10") or 10)
DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("TELEMETRY_DB_STATEMENT_TIMEOUT_MS", "20000") or 20000)

# shadow = read + compare + refresh liveness only (counters frozen).
# enforce = write absolute totals.
QUOTA_WRITE_MODE = os.environ.get("QUOTA_WRITE_MODE", "shadow").lower()

# Safety floor: refuse to overwrite the table from a suspiciously small result.
# With incremental ADD a bad query wrote nothing; with absolute SET it would
# zero out real month-to-date accounting, so this guard is load-bearing.
DB_MIN_ROW_RATIO = float(os.environ.get("QUOTA_DB_MIN_ROW_RATIO", "0.5") or 0.5)

COST_SOURCE = "telemetry.unified_hourly_cost"

_db_secret_cache = None

# Month-to-date usage per identity, from the deduplicated unified view.
#
# `unified_hourly_cost` is a VIEW over the `unified_hourly` materialized view;
# it adds cost_usd via telemetry.calculate_token_cost() plus the 1.1x
# cross-region multiplier for `us.%` inference profiles. The matview is
# refreshed by TimescaleDB background job `refresh_unified_rollups` on a 15
# minute interval, matching this Lambda's schedule.
#
# Grain is one row per (time, user_email, organization_id, model, type,
# account_source, cost_center_real) -- verified: 50761 rows = 50761 distinct
# tuples MTD. So `type` is a real dimension and sum(total_tokens) is a true
# total across types, not a double count.
#
# `type` carries BOTH spelling families, because the view unions two producers:
# the Bedrock CloudTrail branch emits snake_case (input, output, cache_read,
# cache_creation) and the OTel token_usage branch emits camelCase (cacheRead,
# cacheCreation). Note the cache-write type is `cache_creation`/`cacheCreation`
# -- there is no `cache_write`.
#
# Identity casing: we GROUP BY lower(user_email) so a future casing split
# cannot silently divide one human's spend across two rows, but carry
# min(user_email) as the representative original-case spelling for the
# DynamoDB key. Verified: for all 315 identities present in both stores the
# DB's raw casing matches the existing DynamoDB `pk` exactly, so preserving it
# lands on existing rows and keeps quota_check / alert dedup working unchanged.
#
# Only email identities are counted. `unified_hourly_cost` also carries opaque
# non-email `bedrock_user_id` values -- service principals and CI role sessions
# -- which have no human to notify and should not receive quota rows or alerts.
# `LIKE '%@%'` is the same identity test sidecar_monitor applies. This is a
# deliberate accounting exclusion, not a coverage gap: that spend is still
# visible in the telemetry DB and its dashboards, just not enforced here.
USAGE_SQL = """
SELECT lower(user_email)                                                      AS email_key,
       min(user_email)                                                        AS email_display,
       COALESCE(sum(cost_usd), 0)                                             AS monthly_cost,
       COALESCE(sum(cost_usd) FILTER (
           WHERE time >= date_trunc('day', now())), 0)                        AS daily_cost,
       COALESCE(sum(total_tokens), 0)                                         AS total_tokens,
       COALESCE(sum(total_tokens) FILTER (
           WHERE time >= date_trunc('day', now())), 0)                        AS daily_tokens,
       COALESCE(sum(total_tokens) FILTER (WHERE type = 'input'), 0)            AS input_tokens,
       COALESCE(sum(total_tokens) FILTER (WHERE type = 'output'), 0)           AS output_tokens,
       COALESCE(sum(total_tokens) FILTER (
           WHERE type IN ('cache_read', 'cacheRead')), 0)                      AS cache_tokens
  FROM telemetry.unified_hourly_cost
 WHERE time >= date_trunc('month', now())
   AND user_email IS NOT NULL
   AND btrim(user_email) <> ''
   AND user_email LIKE '%@%'
 GROUP BY 1
"""


def _db_secret():
    """Fetch and cache the DB credentials secret (module-level cache, warm reuse)."""
    global _db_secret_cache
    if _db_secret_cache is None:
        if not TELEMETRY_DB_SECRET_ARN:
            raise RuntimeError("TELEMETRY_DB_SECRET_ARN is not configured")
        resp = secrets_client.get_secret_value(SecretId=TELEMETRY_DB_SECRET_ARN)
        _db_secret_cache = json.loads(resp["SecretString"])
    return _db_secret_cache


def _ssl_context():
    """Build the ssl_context for pg8000 per TELEMETRY_DB_SSL_MODE, or None for plaintext."""
    if TELEMETRY_DB_SSL_MODE in ("disable", "", "off"):
        return None
    if TELEMETRY_DB_SSL_MODE == "verify-full":
        if not TELEMETRY_DB_CA_PEM:
            raise RuntimeError("TELEMETRY_DB_SSL_MODE=verify-full requires TELEMETRY_DB_CA_PEM")
        return ssl.create_default_context(cafile=TELEMETRY_DB_CA_PEM)
    # "require": encrypt the wire, but do NOT authenticate the server. This
    # gives confidentiality only -- it is not MITM-resistant. Use verify-full
    # once a server CA is available.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _db_connect():
    """Open a TimescaleDB connection. Imported lazily so unit tests never need the driver."""
    import pg8000.dbapi  # vendored; see VENDORED.md

    secret = _db_secret()
    host = TELEMETRY_DB_HOST or secret.get("host") or ""
    if not host:
        raise RuntimeError("No telemetry DB host configured (env TELEMETRY_DB_HOST or secret 'host')")

    conn = pg8000.dbapi.connect(
        user=secret.get("username") or secret.get("user") or "postgres",
        password=secret["password"],
        host=host,
        port=TELEMETRY_DB_PORT or int(secret.get("port", 5432)),
        database=TELEMETRY_DB_NAME or secret.get("dbname") or secret.get("database"),
        ssl_context=_ssl_context(),
        timeout=DB_CONNECT_TIMEOUT,
        application_name="claude-code-quota-monitor",
    )
    # pg8000.dbapi.connect() has no `options=` parameter, so the server-side
    # statement timeout must be set as a statement after connecting. This keeps
    # a matview-refresh contention stall from consuming the Lambda budget.
    cur = conn.cursor()
    try:
        cur.execute(f"SET statement_timeout = {int(DB_STATEMENT_TIMEOUT_MS)}")
        conn.commit()
    finally:
        cur.close()
    return conn


def fetch_usage_from_db(conn_factory=None):
    """Return month-to-date usage per identity from TimescaleDB.

    Shape matches what the threshold pass expects:
      {email: {total_tokens, daily_tokens, input_tokens, output_tokens,
               cache_tokens, monthly_cost, daily_cost}}

    Only email identities are returned. USAGE_SQL already filters them
    server-side; the `"@" not in email` skip below is a second, cheap layer so
    an edit to the SQL cannot quietly start writing quota rows for service
    principals. Keep both.

    `conn_factory` is the unit-test seam -- inject a fake connection so tests
    never touch a live database. Raises on any failure; the caller decides
    whether that aborts the write step (it does -- see lambda_handler).
    """
    conn = (conn_factory or _db_connect)()
    users = {}
    skipped_non_email = 0
    try:
        cur = conn.cursor()
        try:
            cur.execute(USAGE_SQL)
            for row in cur.fetchall():
                (email_key, email_display, monthly_cost, daily_cost,
                 total_tokens, daily_tokens, input_tokens, output_tokens, cache_tokens) = row[:9]
                email = (email_display or email_key or "").strip()
                if not email or len(email) > 320:
                    continue
                if "@" not in email:
                    skipped_non_email += 1
                    continue
                users[email] = {
                    "total_tokens": float(total_tokens or 0),
                    "daily_tokens": float(daily_tokens or 0),
                    "input_tokens": float(input_tokens or 0),
                    "output_tokens": float(output_tokens or 0),
                    "cache_tokens": float(cache_tokens or 0),
                    "monthly_cost": float(monthly_cost or 0),
                    "daily_cost": float(daily_cost or 0),
                }
        finally:
            cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # A non-zero skip count means the SQL filter stopped matching -- worth
    # noticing in logs, because the guard is the only thing holding the line.
    print(f"Fetched MTD usage for {len(users)} email identities from {COST_SOURCE} "
          f"({skipped_non_email} non-email identities skipped client-side)")
    return users


def _write_is_safe(row_count, existing_count):
    """Guard absolute overwrites against a truncated or empty query result."""
    if row_count <= 0:
        print("ERROR: telemetry DB returned 0 identities - refusing to overwrite quota counters")
        return False
    if existing_count > 0 and row_count < existing_count * DB_MIN_ROW_RATIO:
        print(f"ERROR: telemetry DB returned {row_count} identities vs {existing_count} "
              f"existing rows (< {DB_MIN_ROW_RATIO:.0%}) - refusing to overwrite quota counters")
        return False
    return True


def _month_end_ttl(now):
    """Expire the item at 00:00 UTC on the first day of the following month.

    Anchoring on day=1 is what makes this correct for every month. The obvious
    day=28 variant skips a month for January in non-leap years -- Jan 28 + 32d
    lands on Mar 1 -- which doubled the intended lifetime of January rows.

    Zeroing the time fields keeps the value stable across invocations. Without
    it the ttl inherited the Lambda's run time-of-day, so a row rewritten every
    15 minutes carried a different expiry on every write.
    """
    import datetime as _dt
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int((first + _dt.timedelta(days=32)).replace(day=1).timestamp())


def write_usage_absolute(usage_data, shadow=False):
    """Write absolute month-to-date totals to DynamoDB (idempotent).

    Replaces the old incremental `ADD` accumulator. Because every value is the
    complete month-to-date total from the source of truth, this is idempotent:
    running it twice produces the same item, and a missed or retried invocation
    self-heals on the next run instead of permanently skewing the counter.

    `daily_*` needs no read-modify-write day-rollover dance either -- the SQL
    FILTER already scopes it to today, and daily_date is always stamped to
    today, so the stale-day guard in _build_usage_entry becomes a no-op for
    freshly written users while still protecting untouched legacy rows.

    In shadow mode only the liveness attributes are written: counters stay
    frozen, but `last_updated` keeps advancing so sidecar_monitor does not
    raise false "sidecar stopped" alerts.
    """
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_date = now.strftime("%Y-%m-%d")
    ttl = _month_end_ttl(now)
    ts = now.isoformat().replace("+00:00", "Z")  # sidecar_monitor parses this format

    written = 0
    errors = 0
    for email, usage in usage_data.items():
        try:
            if shadow:
                update_expr = "SET last_updated = :ts, #ttl = :ttl, email = :email"
                expr_values = {":ts": ts, ":ttl": ttl, ":email": email}
            else:
                update_expr = (
                    "SET total_tokens = :tt, input_tokens = :inp, output_tokens = :out, "
                    "cache_tokens = :cache, estimated_cost = :cost, "
                    "daily_tokens = :dt, daily_cost_usd = :dcost, daily_date = :date, "
                    "last_updated = :ts, #ttl = :ttl, email = :email, cost_source = :src"
                )
                expr_values = {
                    ":tt": Decimal(str(int(usage.get("total_tokens", 0)))),
                    ":inp": Decimal(str(int(usage.get("input_tokens", 0)))),
                    ":out": Decimal(str(int(usage.get("output_tokens", 0)))),
                    ":cache": Decimal(str(int(usage.get("cache_tokens", 0)))),
                    ":cost": Decimal(str(round(float(usage.get("monthly_cost", 0)), 6))),
                    ":dt": Decimal(str(int(usage.get("daily_tokens", 0)))),
                    ":dcost": Decimal(str(round(float(usage.get("daily_cost", 0)), 6))),
                    ":date": current_date,
                    ":ts": ts,
                    ":ttl": ttl,
                    ":email": email,
                    ":src": COST_SOURCE,
                }
            quota_table.update_item(
                Key={"pk": f"USER#{email}", "sk": f"MONTH#{current_month}"},
                UpdateExpression=update_expr,
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues=expr_values,
            )
            written += 1
        except Exception as e:
            errors += 1
            print(f"Error updating quota for {email}: {e}")

    mode = "shadow (counters frozen)" if shadow else "absolute"
    print(f"Wrote {written} UserQuotaMetrics items in {mode} mode ({errors} errors)")
    return written


def _log_shadow_diff(db_usage, existing):
    """Log what the cutover would change, without changing it."""
    db_total = sum(v.get("monthly_cost", 0) for v in db_usage.values())
    ddb_total = sum(v.get("monthly_cost", 0) for v in existing.values())
    matched = [e for e in db_usage if e in existing]
    new_users = [e for e in db_usage if e not in existing]
    missing = [e for e in existing if e not in db_usage]
    print(f"SHADOW: identities db={len(db_usage)} ddb={len(existing)} "
          f"matched={len(matched)} new={len(new_users)} ddb_only={len(missing)}")
    print(f"SHADOW: MTD cost db=${db_total:,.2f} ddb=${ddb_total:,.2f} "
          f"delta=${db_total - ddb_total:,.2f}")
    deltas = sorted(
        ((db_usage[e]["monthly_cost"] - existing[e]["monthly_cost"], e) for e in matched),
        key=lambda t: -abs(t[0]),
    )
    for d, e in deltas[:10]:
        print(f"SHADOW:   {d:+12,.2f}  {existing[e]['monthly_cost']:>10,.2f} -> "
              f"{db_usage[e]['monthly_cost']:>10,.2f}  {e}")



def _build_usage_entry(item, current_date):
    """Build a usage_data entry for threshold checking, applying the stale-day guard.

    Mirrors quota_check.get_user_usage: if the stored daily_date is not today
    (UTC), the daily counter belongs to a prior day and must be treated as 0.
    Otherwise an idle user whose daily_tokens froze above the limit gets a fresh
    "daily exceeded" alert every new UTC day even though they had no activity.
    Monthly (total_tokens / estimated_cost) is unaffected — it accumulates
    across the whole month. The same guard applies to the daily cost counter.
    """
    daily_tokens = float(item.get("daily_tokens", 0))
    daily_cost = float(item.get("daily_cost_usd", 0))
    daily_date = item.get("daily_date")
    if daily_date != current_date:
        daily_tokens = 0
        daily_cost = 0
    return {
        "total_tokens": float(item.get("total_tokens", 0)),
        "daily_tokens": daily_tokens,
        "monthly_cost": float(item.get("estimated_cost", 0)),
        "daily_cost": daily_cost,
    }


def lambda_handler(event, context):
    """Read MTD usage from TimescaleDB, write absolute totals to DynamoDB, check quotas, alert."""
    print(f"Starting quota monitoring at {datetime.now(timezone.utc).isoformat()}")
    shadow = QUOTA_WRITE_MODE != "enforce"
    if shadow:
        print("WARNING: QUOTA_WRITE_MODE=shadow - quota counters are FROZEN "
              "(reading and comparing only). Set QUOTA_WRITE_MODE=enforce to write.")

    now = datetime.now(timezone.utc)
    month_name = now.strftime("%B %Y")
    current_date = now.strftime("%Y-%m-%d")
    days_in_month = (31 if now.month in [1, 3, 5, 7, 8, 10, 12]
                     else (30 if now.month != 2 else (29 if now.year % 4 == 0 else 28)))
    days_remaining = days_in_month - now.day
    db_failed = False

    try:
        # Step 1: Read existing DynamoDB totals FIRST. This serves two purposes:
        # it is the threshold-checking baseline (and keeps users who exist here
        # but not in the DB result being alerted on), and its row count is the
        # denominator for the safety floor that guards absolute overwrites.
        current_month = now.strftime("%Y-%m")
        usage_data = {}
        # NOTE: daily_date MUST be projected so we can apply the same stale-day
        # guard that quota_check uses (quota_check/index.py). Without it, an idle
        # user's frozen daily_tokens is read verbatim and re-alerted every new UTC
        # day even though they had no activity.
        projection = "email, total_tokens, daily_tokens, daily_date, estimated_cost, daily_cost_usd"
        response = quota_table.scan(
            FilterExpression=Attr("sk").eq(f"MONTH#{current_month}") & Attr("pk").begins_with("USER#"),
            ProjectionExpression=projection,
        )
        for item in response.get("Items", []):
            email = item.get("email")
            if email:
                usage_data[email] = _build_usage_entry(item, current_date)
        while "LastEvaluatedKey" in response:
            response = quota_table.scan(
                FilterExpression=Attr("sk").eq(f"MONTH#{current_month}") & Attr("pk").begins_with("USER#"),
                ProjectionExpression=projection,
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            for item in response.get("Items", []):
                email = item.get("email")
                if email:
                    usage_data[email] = _build_usage_entry(item, current_date)
        existing_count = len(usage_data)

        # Step 2: Read authoritative MTD usage from TimescaleDB and write it.
        # Unlike the old optional cost calculation, the DB IS the cost source
        # now, so a failure here is an error - not a non-fatal skip. We still
        # fall through to threshold checking on the DynamoDB values we already
        # have, but we never write a partial or truncated result.
        if not TELEMETRY_DB_SECRET_ARN:
            db_failed = True
            print("ERROR: TELEMETRY_DB_SECRET_ARN is not configured - no cost source available")
        else:
            try:
                db_usage = fetch_usage_from_db()
                if not _write_is_safe(len(db_usage), existing_count):
                    db_failed = True
                    _publish_operational_alert(
                        "Claude Code quota monitor refused to write",
                        f"TimescaleDB returned {len(db_usage)} identities vs {existing_count} "
                        f"existing DynamoDB rows, below the {DB_MIN_ROW_RATIO:.0%} safety floor. "
                        f"Quota counters were left untouched.",
                    )
                else:
                    if shadow:
                        _log_shadow_diff(db_usage, usage_data)
                    write_usage_absolute(db_usage, shadow=shadow)
                    if not shadow:
                        # Alert on the same-run fresh values. This also removes
                        # the read-after-write race the old code had (ADD, then
                        # eventually-consistent Scan).
                        for email, u in db_usage.items():
                            usage_data[email] = {
                                "total_tokens": u.get("total_tokens", 0),
                                "daily_tokens": u.get("daily_tokens", 0),
                                "monthly_cost": u.get("monthly_cost", 0),
                                "daily_cost": u.get("daily_cost", 0),
                            }
            except Exception as e:
                db_failed = True
                print(f"ERROR: telemetry DB read failed, quota counters left untouched: {e}")
                import traceback
                traceback.print_exc()

        if not usage_data:
            print("No usage data in DynamoDB")
            return {"statusCode": 500 if db_failed else 200, "body": "No usage data"}

        # Step 3: Load policies
        policies_cache = {}
        if ENABLE_FINEGRAINED_QUOTAS and policies_table:
            policies_cache = load_all_policies()

        # Step 3: Check sent alerts
        sent_alerts = get_sent_alerts(month_name)

        # Step 4: Check each user against quotas
        alerts_to_send = []
        stats = {"total_users": 0, "over_80": 0, "over_90": 0, "exceeded": 0, "daily_exceeded": 0}
        # Which dimension the counters above were measured against. In cost mode the
        # token limits are 0 (disabled), so counting tokens printed 0/0/0 no matter how
        # far over budget anyone was -- a summary line an operator would reasonably
        # trust. Count whichever limit is actually enforced, and say which one it was.
        bases = set()
        monthly_cost_total = 0.0

        for email, usage in usage_data.items():
            stats["total_users"] += 1
            policy = resolve_user_quota(email, [], policies_cache)
            if policy is None:
                continue

            total_tokens = usage.get("total_tokens", 0)
            daily_tokens = usage.get("daily_tokens", 0)
            monthly_cost = usage.get("monthly_cost", 0)
            daily_cost = usage.get("daily_cost", 0)
            monthly_cost_total += float(monthly_cost or 0)

            alerts = check_limits_and_generate_alerts(
                email=email, total_tokens=total_tokens, daily_tokens=daily_tokens,
                policy=policy, month_name=month_name, current_date=current_date,
                days_remaining=days_remaining, days_in_month=days_in_month, sent_alerts=sent_alerts,
                monthly_cost=monthly_cost, daily_cost=daily_cost,
            )

            # Same precedence as check_limits_and_generate_alerts: a $ budget, if set,
            # is the limit that matters; tokens are the fallback for token-mode stacks.
            monthly_cost_limit = float(policy.get("monthly_cost_limit", 0) or 0)
            monthly_token_limit = policy["monthly_token_limit"]
            if monthly_cost_limit > 0:
                bases.add("cost")
                monthly_pct = (float(monthly_cost or 0) / monthly_cost_limit) * 100
            elif monthly_token_limit > 0:
                bases.add("tokens")
                monthly_pct = (total_tokens / monthly_token_limit) * 100
            else:
                monthly_pct = 0

            if monthly_pct > 100:
                stats["exceeded"] += 1
            elif monthly_pct > 90:
                stats["over_90"] += 1
            elif monthly_pct > 80:
                stats["over_80"] += 1

            daily_cost_limit = float(policy.get("daily_cost_limit", 0) or 0)
            if daily_cost_limit > 0:
                if float(daily_cost or 0) > daily_cost_limit:
                    stats["daily_exceeded"] += 1
            elif policy.get("daily_token_limit") and daily_tokens > policy["daily_token_limit"]:
                stats["daily_exceeded"] += 1

            for alert in alerts:
                # Second gate. check_limits_and_generate_alerts already applied
                # the same test, but it built the key itself; keeping both on the
                # shared builder is what makes this a real guard instead of the
                # no-op it was for daily alerts (the hand-built key here omitted
                # the date, so it could never match a dated daily entry).
                alert_key = alert_dedup_key(
                    email, alert["alert_type"], alert["alert_level"], alert.get("date")
                )
                if alert_key not in sent_alerts:
                    alerts_to_send.append(alert)
                    record_sent_alert(month_name, email, alert["alert_type"], alert["alert_level"], alert)

        if alerts_to_send:
            send_alerts(alerts_to_send)
            print(f"Sent {len(alerts_to_send)} alerts")

        if bases == {"cost"}:
            basis = "cost"
        elif bases == {"tokens"}:
            basis = "tokens"
        elif bases:
            basis = "mixed"
        else:
            basis = "no limits set"
        stats["limit_basis"] = basis
        stats["monthly_cost_total"] = round(monthly_cost_total, 2)

        print(f"Summary ({basis}) - Total: {stats['total_users']}, Over 80%: {stats['over_80']}, "
              f"Over 90%: {stats['over_90']}, Exceeded: {stats['exceeded']}, "
              f"Daily exceeded: {stats['daily_exceeded']}, MTD: ${monthly_cost_total:,.2f}")
        stats["cost_source"] = COST_SOURCE
        stats["write_mode"] = "shadow" if shadow else "enforce"
        stats["db_failed"] = db_failed
        # Surface a cost-source failure as an invocation error so it is visible
        # in the Lambda Errors metric, even though thresholds were still checked.
        return {"statusCode": 500 if db_failed else 200, "body": json.dumps(stats)}

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {"statusCode": 500, "body": json.dumps(f"Error: {e}")}


def _publish_operational_alert(subject, message):
    """Publish an operator-facing alert (distinct from per-user quota alerts)."""
    if not SNS_TOPIC_ARN:
        print(f"SNS_TOPIC_ARN not configured; would have alerted: {subject}")
        return
    try:
        sns_client.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
    except Exception as e:
        print(f"Error publishing operational alert: {e}")


def _policy_from_item(item):
    """Map one QuotaPolicies item to the policy dict resolve_user_quota expects.

    Every scan page must go through this. The pagination loop below used to build
    its own copy of this dict and had silently omitted the two cost limits, so any
    policy that happened to land on page 2+ came back with a $0 budget -- i.e.
    unenforced, for some users and not others depending on scan ordering.

    Cost limits are written by a separate update_item in the CLI
    (`_write_cost_limits`), so they can be absent on an otherwise valid policy;
    `or 0` keeps a null from raising here.
    """
    return {
        "policy_type": item.get("policy_type"), "identifier": item.get("identifier"),
        "monthly_token_limit": int(item.get("monthly_token_limit", 0)),
        "daily_token_limit": int(item.get("daily_token_limit", 0)) if item.get("daily_token_limit") else None,
        "monthly_cost_limit": float(item.get("monthly_cost_limit", 0) or 0),
        "daily_cost_limit": float(item.get("daily_cost_limit", 0) or 0),
        "warning_threshold_80": int(item.get("warning_threshold_80", 0)),
        "warning_threshold_90": int(item.get("warning_threshold_90", 0)),
        "enforcement_mode": item.get("enforcement_mode", "alert"),
        "enabled": item.get("enabled", True),
    }


def load_all_policies():
    """Load all quota policies from QuotaPolicies table."""
    policies = {}
    if not policies_table:
        return policies
    try:
        scan_kwargs = {"FilterExpression": Attr("sk").eq("CURRENT")}
        while True:
            response = policies_table.scan(**scan_kwargs)
            for item in response.get("Items", []):
                pt, ident = item.get("policy_type"), item.get("identifier")
                if pt and ident:
                    policies[f"{pt}:{ident}"] = _policy_from_item(item)
            if "LastEvaluatedKey" not in response:
                break
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    except Exception as e:
        print(f"Error loading policies: {e}")
    return policies


def resolve_user_quota(email, groups, policies_cache):
    """Resolve effective quota policy: user > group > default > env defaults."""
    if not ENABLE_FINEGRAINED_QUOTAS:
        return {
            "policy_type": "default", "identifier": "environment",
            "monthly_token_limit": MONTHLY_TOKEN_LIMIT, "daily_token_limit": None,
            "monthly_cost_limit": MONTHLY_COST_LIMIT_USD, "daily_cost_limit": DAILY_COST_LIMIT_USD,
            "warning_threshold_80": WARNING_THRESHOLD_80, "warning_threshold_90": WARNING_THRESHOLD_90,
            "enforcement_mode": "alert", "enabled": True,
        }
    user_key = f"user:{email}"
    if user_key in policies_cache and policies_cache[user_key].get("enabled"):
        return policies_cache[user_key]
    group_policies = [policies_cache[f"group:{g}"] for g in (groups or [])
                      if f"group:{g}" in policies_cache and policies_cache[f"group:{g}"].get("enabled")]
    if group_policies:
        return min(group_policies, key=lambda p: p["monthly_token_limit"])
    default_key = "default:default"
    if default_key in policies_cache and policies_cache[default_key].get("enabled"):
        return policies_cache[default_key]
    return None


def alert_dedup_key(email, alert_type, alert_level, date=None):
    """THE single builder for sent-alert dedup keys. Never build one by hand.

    Daily alerts are scoped to a DAY, so the date is part of the alert's
    identity; monthly alerts are scoped to the month, which the ALERTS query
    prefix already pins. The two shapes must therefore differ.

    Six call sites used to build this string inline and they disagreed:
    get_sent_alerts' pagination loop tested `atype == "daily"` while its first
    page tested `atype.startswith("daily")`. So once the ALERTS query spilled to
    a second page, a `daily_cost` row was keyed WITHOUT its date, never matched
    the dated key the generator looks for, and the user was re-alerted every 15
    minutes for the rest of the day. Same class of bug as
    .claude/rules/token-endpoint-single-builder.md — one builder, no drift.
    """
    if alert_type.startswith("daily"):
        return f"{email}#{alert_type}#{date}#{alert_level}"
    return f"{email}#{alert_type}#{alert_level}"


def dedup_key_from_sk(sk):
    """Rebuild a dedup key from an ALERTS row's sort key, or None if unusable.

    record_sent_alert writes the date LAST:
        <month>#ALERT#<email>#<type>#<level>[#<date>]
    so level is parts[4] and the optional date is parts[5]. Emails cannot
    contain '#', so positional splitting is safe.
    """
    parts = sk.split("#")
    if len(parts) < 5:
        return None
    email, atype, alevel = parts[2], parts[3], parts[4]
    date = parts[5] if len(parts) >= 6 else None
    if atype.startswith("daily") and date is None:
        # A dated alert type with no date recorded: refuse to guess rather than
        # emit a key that can never match and silently re-alert.
        return None
    return alert_dedup_key(email, atype, alevel, date)


def check_limits_and_generate_alerts(email, total_tokens, daily_tokens, policy,
                                     month_name, current_date, days_remaining, days_in_month, sent_alerts,
                                     monthly_cost=0.0, daily_cost=0.0):
    """Check limits and generate alert dicts (token- and cost-denominated)."""
    alerts = []
    policy_info = f"{policy['policy_type']}:{policy['identifier']}"
    enforcement_mode = policy.get("enforcement_mode", "alert")
    monthly_limit = policy["monthly_token_limit"]
    monthly_pct = (total_tokens / monthly_limit) * 100 if monthly_limit > 0 else 0
    daily_average = total_tokens / max(1, int(current_date.split("-")[2]))
    projected_total = daily_average * days_in_month

    # Token limits. A limit of 0 means token limits are DISABLED (cost mode
    # zeroes them) — without this guard every user with any usage generated a
    # bogus "monthly exceeded" alert on each 15-minute scan.
    level = None
    if monthly_limit > 0:
        if total_tokens > monthly_limit:
            level = "exceeded"
        elif total_tokens > policy["warning_threshold_90"]:
            level = "critical"
        elif total_tokens > policy["warning_threshold_80"]:
            level = "warning"

    if level and alert_dedup_key(email, "monthly", level) not in sent_alerts:
        alerts.append({
            "user": email, "alert_type": "monthly", "alert_level": level,
            "current_usage": int(total_tokens), "limit": monthly_limit,
            "percentage": round(monthly_pct, 1), "month": month_name,
            "days_remaining": days_remaining, "daily_average": int(daily_average),
            "projected_total": int(projected_total), "policy_info": policy_info,
            "enforcement_mode": enforcement_mode,
        })

    daily_limit = policy.get("daily_token_limit")
    if daily_limit:
        daily_pct = (daily_tokens / daily_limit) * 100 if daily_limit > 0 else 0
        dlevel = None
        if daily_tokens > daily_limit:
            dlevel = "exceeded"
        elif daily_tokens > (daily_limit * 0.9):
            dlevel = "critical"
        elif daily_tokens > (daily_limit * 0.8):
            dlevel = "warning"
        if dlevel and alert_dedup_key(email, "daily", dlevel, current_date) not in sent_alerts:
            alerts.append({
                "user": email, "alert_type": "daily", "alert_level": dlevel,
                "current_usage": int(daily_tokens), "limit": daily_limit,
                "percentage": round(daily_pct, 1), "date": current_date,
                "policy_info": policy_info, "enforcement_mode": enforcement_mode,
            })

    # Cost limits ($ budgets, cost mode). Same 80/90/100 ladder as tokens;
    # quota_check does the blocking, these alerts are the early warning.
    monthly_cost_limit = float(policy.get("monthly_cost_limit", 0) or 0)
    if monthly_cost_limit > 0:
        cost_pct = (monthly_cost / monthly_cost_limit) * 100
        clevel = None
        if monthly_cost > monthly_cost_limit:
            clevel = "exceeded"
        elif monthly_cost > monthly_cost_limit * 0.9:
            clevel = "critical"
        elif monthly_cost > monthly_cost_limit * 0.8:
            clevel = "warning"
        if clevel and alert_dedup_key(email, "monthly_cost", clevel) not in sent_alerts:
            alerts.append({
                "user": email, "alert_type": "monthly_cost", "alert_level": clevel,
                "current_usage": round(monthly_cost, 2), "limit": monthly_cost_limit,
                "percentage": round(cost_pct, 1), "month": month_name,
                "days_remaining": days_remaining, "policy_info": policy_info,
                "enforcement_mode": enforcement_mode,
            })

    daily_cost_limit = float(policy.get("daily_cost_limit", 0) or 0)
    if daily_cost_limit > 0:
        dcost_pct = (daily_cost / daily_cost_limit) * 100
        dclevel = None
        if daily_cost > daily_cost_limit:
            dclevel = "exceeded"
        elif daily_cost > daily_cost_limit * 0.9:
            dclevel = "critical"
        elif daily_cost > daily_cost_limit * 0.8:
            dclevel = "warning"
        if dclevel and alert_dedup_key(email, "daily_cost", dclevel, current_date) not in sent_alerts:
            alerts.append({
                "user": email, "alert_type": "daily_cost", "alert_level": dclevel,
                "current_usage": round(daily_cost, 2), "limit": daily_cost_limit,
                "percentage": round(dcost_pct, 1), "date": current_date,
                "policy_info": policy_info, "enforcement_mode": enforcement_mode,
            })
    return alerts


def get_sent_alerts(month_name):
    """Get alerts already sent this month."""
    sent = set()
    try:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        key_condition = Key("pk").eq("ALERTS") & Key("sk").begins_with(f"{month_prefix}#ALERT#")
        kwargs = {}
        # One loop for every page. This used to be the first page plus a copy
        # inside the while, and the copy drifted — see alert_dedup_key.
        while True:
            response = quota_table.query(KeyConditionExpression=key_condition, **kwargs)
            for item in response.get("Items", []):
                key = dedup_key_from_sk(item["sk"])
                if key:
                    sent.add(key)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
    except Exception as e:
        print(f"Error checking sent alerts: {e}")
    return sent


def record_sent_alert(month_name, email, alert_type, alert_level, alert_data):
    """Record sent alert to prevent duplicates."""
    try:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        # dedup_key_from_sk is the inverse of this: it reads level from parts[4]
        # and the optional date from parts[5]. Keep the date LAST, and keep the
        # `startswith("daily")` test in step with alert_dedup_key.
        if alert_type.startswith("daily"):
            date = alert_data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            sk = f"{month_prefix}#ALERT#{email}#{alert_type}#{alert_level}#{date}"
        else:
            sk = f"{month_prefix}#ALERT#{email}#{alert_type}#{alert_level}"
        quota_table.put_item(Item={
            "pk": "ALERTS", "sk": sk,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "email": email, "alert_type": alert_type, "alert_level": alert_level,
            "usage_at_alert": Decimal(str(alert_data.get("current_usage", 0))),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + (60 * 86400),
        })
    except Exception as e:
        print(f"Error recording alert: {e}")


def _alert_message_attributes(alert):
    """SNS message attributes carrying the machine-readable copy of an alert.

    The Message body stays free text for human email subscribers; these
    attributes let subscribers route without parsing prose. The Slack notifier
    subscribes with FilterPolicy {"alert_type": ["monthly_cost", "daily_cost"]},
    so token-quota alerts and operator alerts never invoke it.

    IMPORTANT: an SNS FilterPolicy naming an attribute the message does NOT
    carry is a NON-match. That is exactly how _publish_operational_alert and
    sidecar_monitor are excluded — they publish no attributes at all. It also
    means dropping "alert_type" here silently stops every Slack DM, so keep it.

    alert_payload is Binary, NOT String, and that is load-bearing: SNS treats a
    String attribute whose value is a JSON *object* as invalid while evaluating
    a FilterPolicy and drops the entire message — alert_type included — counting
    it under NumberOfNotificationsFilteredOut-InvalidAttributes. Publish still
    succeeds and the monitor still logs "Sent N alerts", so the loss is visible
    nowhere except that metric. Base64 sidesteps the JSON sniffing. A JSON array
    would too (SNS reads it as String.Array), but only an object holds the
    named fields the DM renders. See .claude/rules/sns-filter-policy-attributes.md.

    default=str on the payload dump is deliberate insurance: usage values are
    floats today, but a DynamoDB Decimal leaking in would otherwise raise
    inside the alert loop and lose the alert entirely.
    """
    return {
        "alert_kind": {"DataType": "String", "StringValue": "user_quota"},
        "alert_type": {"DataType": "String", "StringValue": str(alert["alert_type"])},
        "alert_level": {"DataType": "String", "StringValue": str(alert["alert_level"])},
        "user_email": {"DataType": "String", "StringValue": str(alert["user"])},
        "alert_payload": {
            "DataType": "Binary",
            "BinaryValue": json.dumps(alert, default=str).encode("utf-8"),
        },
    }


def send_alerts(alerts):
    """Send alerts via SNS."""
    if not SNS_TOPIC_ARN:
        print("SNS_TOPIC_ARN not configured")
        return
    for alert in alerts:
        try:
            level_prefix = {"warning": "WARNING", "critical": "CRITICAL", "exceeded": "EXCEEDED"}.get(alert["alert_level"], "ALERT")
            type_label = {
                "monthly": "Monthly Token Quota",
                "daily": "Daily Token Quota",
                "monthly_cost": "Monthly Spend Budget",
                "daily_cost": "Daily Spend Budget",
            }.get(alert["alert_type"], "Quota")
            if "cost" in alert["alert_type"]:
                usage_str = f"${alert['current_usage']:.2f} / ${alert['limit']:.2f}"
            else:
                usage_str = f"{alert['current_usage']:,} / {alert['limit']:,}"
            subject = f"Claude Code {level_prefix} - {type_label} - {alert['percentage']:.0f}%"
            message = (f"USER: {alert['user']}\nALERT: {type_label} - {alert['alert_level'].upper()}\n"
                       f"Usage: {usage_str} ({alert['percentage']:.1f}%)\n"
                       f"Policy: {alert.get('policy_info', 'default')}\n"
                       f"Enforcement: {alert.get('enforcement_mode', 'alert')}")
            sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=subject,
                Message=message,
                MessageAttributes=_alert_message_attributes(alert),
            )
        except Exception as e:
            print(f"Error sending alert for {alert['user']}: {e}")
