# ABOUTME: Lambda that receives quota COST alerts from SNS and DMs the affected user on Slack
# ABOUTME: The subscription FilterPolicy limits invocations to monthly_cost/daily_cost alerts

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

# Initialize clients
secrets_client = boto3.client("secretsmanager")

# Configuration from environment
SLACK_BOT_TOKEN_SECRET_ARN = os.environ.get("SLACK_BOT_TOKEN_SECRET_ARN", "")

# Optional SECOND Slack workspace, selected by the email's domain.
#
# A bot can only see and DM users in the workspace it is installed in. There is
# no cross-workspace lookup: users.lookupByEmail returns `users_not_found` for
# an address in another workspace, which is indistinguishable from "this person
# has no Slack account at all". Cross-org DMs additionally require an accepted
# Slack Connect invite per user, so they cannot be automated.
#
# So a domain whose people live in a different workspace needs its own Slack app
# and its own token. Empty ARN or empty domain list = single-workspace behaviour,
# byte-identical to before.
SLACK_SECONDARY_BOT_TOKEN_SECRET_ARN = os.environ.get("SLACK_SECONDARY_BOT_TOKEN_SECRET_ARN", "")
SLACK_SECONDARY_DOMAINS = frozenset(
    d.strip().lower().lstrip("@")
    for d in os.environ.get("SLACK_SECONDARY_DOMAINS", "").split(",")
    if d.strip()
)

# Recipient allowlist. Compared case-insensitively: quota alerts carry the
# original OIDC email casing on purpose (see .claude/rules/quota-usage-source.md),
# so "Cameron.Johnson@..." and "cameron.johnson@..." are one person.
# An EMPTY allowlist denies everyone — this fails closed by design.
ALLOWLIST = frozenset(
    e.strip().lower() for e in os.environ.get("SLACK_DM_ALLOWLIST", "").split(",") if e.strip()
)

SLACK_API_TIMEOUT = int(os.environ.get("SLACK_API_TIMEOUT_SECONDS", "5") or 5)
SLACK_DM_HELP_URL = os.environ.get("SLACK_DM_HELP_URL", "")

# Bound the 429 backoff so one rate-limited call cannot consume the whole timeout
SLACK_MAX_RETRY_SLEEP = 20

_slack_token_cache = {}  # secret ARN -> bot token; NEVER logged
# (secret ARN, lowercased email) -> Slack user ID; warm-container only.
# The ARN is part of the key because a Slack user ID is scoped to its workspace:
# the same address in two workspaces is two different IDs.
_user_id_cache = {}


def lambda_handler(event, context):
    """SNS -> Slack DM. Each record is handled independently.

    This handler deliberately does NOT re-raise. Lambda retries async
    invocations twice, and a retry after a *successful* chat.postMessage would
    double-DM the user — quota_monitor's dedup row cannot help because it is
    written before the publish. Failures are logged with an ERROR: prefix
    instead; alert on that with a CloudWatch Logs metric filter.
    """
    records = event.get("Records") or []
    stats = {"received": len(records), "sent": 0, "skipped": 0, "failed": 0}
    for record in records:
        sns = record.get("Sns") or {}
        try:
            alert = _alert_from_record(sns)
            email = (alert.get("user") or "").strip()
            if not email:
                print("WARNING: alert carried no user email; skipping")
                stats["skipped"] += 1
                continue
            if not _is_allowed(email):
                # Dev allowlist. Checked before any Slack call or secret read, so
                # a non-allowlisted address never leaves this account.
                print(f"INFO: {email} not in SLACK_DM_ALLOWLIST; skipping Slack DM")
                stats["skipped"] += 1
                continue
            if _notify(alert, email):
                stats["sent"] += 1
            else:
                stats["failed"] += 1
        except Exception as e:
            # One bad record must not drop the rest of the batch.
            print(f"ERROR: failed to process SNS record: {e}")
            stats["failed"] += 1
    print(f"INFO: slack notifier {stats}")
    return stats


def _is_allowed(email):
    """Fail CLOSED: an empty allowlist denies everyone, it does not allow all."""
    return email.strip().lower() in ALLOWLIST


def _attr(attrs, name):
    """Read one SNS message attribute out of the Lambda event envelope.

    The Lambda envelope uses {"Type": ..., "Value": ...} — NOT the SQS shape
    ("stringValue") and NOT the publish API shape ("DataType"/"StringValue").
    Getting this wrong yields a silent no-op.
    """
    return ((attrs.get(name) or {}).get("Value") or "").strip()


def _payload_attr(attrs):
    """Recover the alert_payload JSON text.

    quota_monitor publishes it as a Binary attribute because a String attribute
    holding a JSON object makes SNS discard the whole message during FilterPolicy
    evaluation (see that function's docstring). String is still accepted so a
    message published by an older quota_monitor, or already in flight across a
    stack update, still renders full figures instead of degrading to text.
    """
    spec = attrs.get("alert_payload") or {}
    raw = (spec.get("Value") or "").strip()
    if not raw or spec.get("Type") != "Binary":
        return raw
    try:
        # binascii.Error and UnicodeDecodeError are both ValueError subclasses.
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except ValueError:
        print("WARNING: alert_payload was not decodable base64; falling back")
        return ""


def _alert_from_record(sns):
    """Recover the alert dict, most structured source first."""
    attrs = sns.get("MessageAttributes") or {}
    payload = _payload_attr(attrs)
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict) and parsed.get("user"):
                return parsed
        except ValueError:
            print("WARNING: alert_payload was not valid JSON; falling back")
    alert = {
        "user": _attr(attrs, "user_email"),
        "alert_type": _attr(attrs, "alert_type"),
        "alert_level": _attr(attrs, "alert_level"),
    }
    if alert["user"]:
        return alert
    return _alert_from_text(sns.get("Subject", ""), sns.get("Message", ""))


def _alert_from_text(subject, message):
    """Parse the free-text body quota_monitor.send_alerts has always published.

    Only reached when the deployed quota_monitor predates the message
    attributes. Kept small on purpose: it recovers who and how bad, not the
    numbers, and marks itself so the message builder degrades gracefully.
    """
    alert = {"user": "", "alert_type": "", "alert_level": "", "text_fallback": True}
    for line in (message or "").splitlines():
        if line.startswith("USER:"):
            alert["user"] = line.split(":", 1)[1].strip()
    lowered = (subject or "").lower()
    alert["alert_type"] = "daily_cost" if "daily spend" in lowered else "monthly_cost"
    for level in ("exceeded", "critical", "warning"):
        if level in lowered:
            alert["alert_level"] = level
            break
    alert["raw_message"] = message
    return alert


# Key names accepted when the secret holds JSON rather than a bare token.
_TOKEN_KEYS = (
    "bot_token",
    "token",
    "slack_bot_token",
    "SLACK_BOT_TOKEN",
    "bot_user_oauth_token",
)


def _secret_arn_for_email(email):
    """Which workspace's bot token can see this address.

    The domain decides, because workspace membership does. Anything not listed in
    SLACK_SECONDARY_DOMAINS falls through to the primary token, so adding a
    second workspace cannot change delivery for the existing one.
    """
    domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
    if domain and domain in SLACK_SECONDARY_DOMAINS and SLACK_SECONDARY_BOT_TOKEN_SECRET_ARN:
        return SLACK_SECONDARY_BOT_TOKEN_SECRET_ARN
    return SLACK_BOT_TOKEN_SECRET_ARN


def _workspace_label(secret_arn):
    """A log-safe name for a workspace. Never include the ARN or the token."""
    if secret_arn and secret_arn == SLACK_SECONDARY_BOT_TOKEN_SECRET_ARN:
        return "secondary"
    return "primary"


def _slack_token(secret_arn):
    """Fetch and cache one workspace's bot token. NEVER logged."""
    if not secret_arn:
        raise RuntimeError("SLACK_BOT_TOKEN_SECRET_ARN is not configured")
    if secret_arn not in _slack_token_cache:
        raw = secrets_client.get_secret_value(SecretId=secret_arn)["SecretString"]
        _slack_token_cache[secret_arn] = _extract_token(raw)
    return _slack_token_cache[secret_arn]


def _extract_token(raw):
    """Accept EITHER a bare token string OR a JSON object holding the token.

    The secret is owned outside this repo and its shape is not guaranteed, so
    both are supported rather than pinning one and failing at 3am. Error
    messages carry KEY NAMES only — never a value.
    """
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("Slack bot token secret is empty")
    if not text.startswith("{"):
        # Anything that is not a JSON object is the token itself.
        return text
    try:
        data = json.loads(text)
    except ValueError:
        # A literal token that happens to start with '{' — take it verbatim.
        return text
    for key in _TOKEN_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    candidates = [
        v.strip() for v in data.values() if isinstance(v, str) and v.strip().startswith("xox")
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        f"Slack bot token secret JSON has no recognised token key (keys present: {sorted(data)})"
    )


def _slack_call(method, params, form, secret_arn=None):
    """POST to slack.com/api/<method> using one workspace's token.

    form=True (urlencoded) for users.lookupByEmail — that method does not accept
    a JSON body. form=False (JSON) for chat.postMessage, which needs it for
    Block Kit "blocks".

    Returns (body_dict, retry_after_seconds_or_None). Slack reports most
    failures as HTTP 200 with {"ok": false, "error": ...} and rate limits as
    HTTP 429 with a Retry-After header, so both paths must be read.
    """
    url = f"https://slack.com/api/{method}"
    if form:
        body = urllib.parse.urlencode(params).encode("utf-8")
        content_type = "application/x-www-form-urlencoded; charset=utf-8"
    else:
        body = json.dumps(params).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_slack_token(secret_arn or SLACK_BOT_TOKEN_SECRET_ARN)}",
            "Content-Type": content_type,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SLACK_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        retry_after = None
        if e.headers is not None:
            try:
                retry_after = int(e.headers.get("Retry-After") or 0) or None
            except (TypeError, ValueError):
                retry_after = None
        try:
            parsed = json.loads(e.read().decode("utf-8"))
        except Exception:
            parsed = {"ok": False, "error": f"http_{e.code}"}
        return parsed, retry_after


def _slack_call_with_retry(method, params, form, secret_arn=None):
    """One bounded retry on `ratelimited`. Any other error is returned as-is."""
    body, retry_after = _slack_call(method, params, form, secret_arn)
    if body.get("ok") or body.get("error") != "ratelimited":
        return body
    delay = min(retry_after or 1, SLACK_MAX_RETRY_SLEEP)
    print(f"WARNING: {method} ratelimited; retrying in {delay}s")
    time.sleep(delay)
    body, _ = _slack_call(method, params, form, secret_arn)
    return body


def _lookup_user_id(email, secret_arn):
    """Resolve an email to a Slack user ID. Needs users:read.email + users:read.

    Cached per warm container: users.lookupByEmail is a Tier 3 method
    (~50 req/min) and the same handful of identities re-alert across a month.
    """
    key = (secret_arn, email.strip().lower())
    if key in _user_id_cache:
        return _user_id_cache[key]
    body = _slack_call_with_retry("users.lookupByEmail", {"email": email}, True, secret_arn)
    if not body.get("ok"):
        error = body.get("error", "unknown")
        if error == "users_not_found":
            # Expected, not a fault: an AWS/OIDC identity with no Slack account,
            # or a Slack profile using a different primary email. It is ALSO what
            # an address in a different workspace looks like, so name the
            # workspace we searched — otherwise the two are indistinguishable and
            # a missing SLACK_SECONDARY_DOMAINS entry reads as "no Slack account".
            print(
                f"WARNING: no Slack user for {email} in the "
                f"{_workspace_label(secret_arn)} workspace (users_not_found)"
            )
        elif error in (
            "missing_scope",
            "invalid_auth",
            "account_inactive",
            "token_revoked",
            "not_authed",
        ):
            print(
                f"ERROR: Slack auth/scope problem on users.lookupByEmail: {error} "
                f"(needed={body.get('needed')})"
            )
        else:
            print(f"ERROR: users.lookupByEmail failed for {email}: {error}")
        return None
    user_id = (body.get("user") or {}).get("id")
    if user_id:
        _user_id_cache[key] = user_id
    return user_id


def _notify(alert, email):
    """Resolve the user and DM them. Returns True only on a delivered message.

    Both calls must use the SAME workspace token: the user ID returned by one
    workspace is meaningless to another, and posting to it would fail with
    channel_not_found at best.
    """
    secret_arn = _secret_arn_for_email(email)
    user_id = _lookup_user_id(email, secret_arn)
    if not user_id:
        return False
    text, blocks = _build_message(alert)
    body = _slack_call_with_retry(
        "chat.postMessage",
        {
            "channel": user_id,
            "text": text,
            "blocks": blocks,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        False,
        secret_arn,
    )
    if body.get("ok"):
        print(
            f"INFO: sent {alert.get('alert_type')}/{alert.get('alert_level')} "
            f"DM to {email} ({user_id})"
        )
        return True
    error = body.get("error", "unknown")
    if error == "invalid_blocks":
        # Drop back to plain text rather than losing the alert entirely.
        print(
            f"WARNING: invalid_blocks for {email}; retrying as plain text: "
            f"{body.get('response_metadata')}"
        )
        body = _slack_call_with_retry(
            "chat.postMessage", {"channel": user_id, "text": text}, False, secret_arn
        )
        if body.get("ok"):
            return True
        error = body.get("error", "unknown")
    print(f"ERROR: chat.postMessage failed for {email} ({user_id}): {error}")
    return False


_LEVEL = {
    "warning": (":large_yellow_circle:", "Heads up"),
    "critical": (":large_orange_circle:", "Action needed"),
    "exceeded": (":red_circle:", "Budget exceeded"),
}
_TYPE = {
    "monthly_cost": "monthly Bedrock spend budget",
    "daily_cost": "daily Bedrock spend budget",
}


def _esc(value):
    """Slack mrkdwn escaping — & < > only, per Slack's formatting rules."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _money(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _build_message(alert):
    """Return (text, blocks).

    `text` is always set: Slack uses it for the push/desktop notification and
    for screen readers, so omitting it produces a silent-looking DM.
    """
    emoji, lead = _LEVEL.get(alert.get("alert_level"), (":information_source:", "Notice"))
    label = _TYPE.get(alert.get("alert_type"), "Bedrock spend budget")
    used = _money(alert.get("current_usage"))
    limit = _money(alert.get("limit"))
    try:
        pct = f"{float(alert.get('percentage', 0)):.0f}%"
    except (TypeError, ValueError):
        pct = "n/a"

    if alert.get("date"):
        period = f"Day of {alert['date']}"
    else:
        period = str(alert.get("month", "this month"))
        if alert.get("days_remaining") is not None:
            period += f" - {alert['days_remaining']} day(s) left"

    enforcement = (alert.get("enforcement_mode") or "alert").lower()
    consequence = (
        "Claude Code access is blocked once you pass 100% of the budget."
        if enforcement == "block"
        else "This is a notification only - your Claude Code access is not blocked."
    )

    text = f"{lead}: you are at {pct} of your {label} ({used} of {limit})."

    blocks = [
        # header must be plain_text and <=150 chars
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{lead} - Claude Code spend"[:150]},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} You are at *{pct}* of your {_esc(label)}.",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Spend so far*\n{used}"},
                {"type": "mrkdwn", "text": f"*Budget*\n{limit}"},
                {"type": "mrkdwn", "text": f"*Period*\n{_esc(period)}"},
                {"type": "mrkdwn", "text": f"*Enforcement*\n{_esc(enforcement)}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": consequence}},
    ]
    if SLACK_DM_HELP_URL:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Need a higher budget? <{SLACK_DM_HELP_URL}|Request a change>",
                },
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Policy `{_esc(alert.get('policy_info', 'default'))}` - "
                    "figures are month-to-date from the telemetry database, "
                    "refreshed every 15 minutes.",
                }
            ],
        }
    )

    if alert.get("text_fallback"):
        # Degraded path: the numbers were never available, so forward the body
        # we did get rather than rendering a grid of "n/a".
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _esc(alert.get("raw_message", ""))[:2900]},
            }
        ]
        text = "Claude Code spend alert"

    return text, blocks
