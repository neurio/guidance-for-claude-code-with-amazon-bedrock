# ABOUTME: Tests for the quota_slack_notifier Lambda that DMs users about spend budgets
# ABOUTME: Pins the SNS envelope shape, workspace routing, secret shapes, and template wiring

"""The Slack notifier has four failure modes that are silent by construction,
so each one is pinned here:

- The Lambda SNS envelope uses ``{"Type": ..., "Value": ...}``, unlike the SQS
  shape (``stringValue``) and unlike the publish API shape
  (``DataType``/``StringValue``). Reading the wrong key yields a no-op that
  looks like a healthy invocation.
- An SNS FilterPolicy naming an attribute the message does not carry is a
  NON-match, so the template's FilterPolicy is the only thing keeping operator
  alerts away from end users — and the only thing letting cost alerts through.
- There is deliberately NO recipient gate: every identity that crosses a
  threshold is DM'd. Reintroducing one would silence alerts with nothing louder
  than an INFO log per skipped user, so its absence is pinned.
- The bot token secret is owned outside this repo, so both a bare token string
  and a JSON envelope must work, and no code path may log the value.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.cfn_yaml import INFRA_DIR, load_intrinsics, load_resolved

LAMBDA_DIR = INFRA_DIR / "lambda-functions" / "quota_slack_notifier"
LAMBDA_PATH = LAMBDA_DIR / "index.py"
TEMPLATE = INFRA_DIR / "quota-monitoring.yaml"

FAKE_TOKEN = "xoxb-fake-token-never-log-me"
ALLOWED = "Cameron.Johnson@generac.com"


def _load(env: dict | None = None):
    """Load the Lambda fresh with the given environment.

    Config is read at module scope, so every env permutation needs its own
    module instance. os.environ is restored afterwards so later tests do not
    inherit this configuration.
    """
    base = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "SLACK_BOT_TOKEN_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-AbCdEf",
        "SLACK_API_TIMEOUT_SECONDS": "5",
        "SLACK_DM_HELP_URL": "",
    }
    base.update(env or {})
    prior = {key: os.environ.get(key) for key in base}
    for key, value in base.items():
        os.environ[key] = str(value)
    try:
        module_name = f"slack_notifier_{abs(hash(frozenset((k, str(v)) for k, v in base.items())))}"
        spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module.secrets_client = MagicMock()
        module.secrets_client.get_secret_value.return_value = {"SecretString": FAKE_TOKEN}
        return module
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _cost_alert(**overrides):
    alert = {
        "user": ALLOWED,
        "alert_type": "monthly_cost",
        "alert_level": "warning",
        "current_usage": 85.0,
        "limit": 100.0,
        "percentage": 85.0,
        "month": "September 2026",
        "days_remaining": 20,
        "policy_info": "default:default",
        "enforcement_mode": "alert",
    }
    alert.update(overrides)
    return alert


def _sns_event(alert=None, *, attributes=None, subject="", message=""):
    """Build a Lambda SNS event. Note Type/Value — the Lambda envelope shape.

    alert_payload is Binary/base64 because that is what quota_monitor publishes:
    a String attribute holding a JSON object makes SNS discard the message
    during FilterPolicy evaluation, so it can never reach this Lambda.
    """
    if attributes is None:
        attributes = {}
        if alert is not None:
            attributes = {
                "alert_kind": {"Type": "String", "Value": "user_quota"},
                "alert_type": {"Type": "String", "Value": alert["alert_type"]},
                "alert_level": {"Type": "String", "Value": alert["alert_level"]},
                "user_email": {"Type": "String", "Value": alert["user"]},
                "alert_payload": {
                    "Type": "Binary",
                    "Value": base64.b64encode(json.dumps(alert).encode("utf-8")).decode("ascii"),
                },
            }
    return {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "Type": "Notification",
                    "Subject": subject,
                    "Message": message,
                    "MessageAttributes": attributes,
                },
            }
        ]
    }


def _stub_slack(mod, responses):
    """Replace _slack_call with a scripted stub. responses maps method -> body.

    The 4th element of each recorded call is the secret ARN, i.e. which
    workspace's token the call would have used. Defaulted so the many
    single-workspace tests below read unchanged.
    """
    calls = []

    def fake(method, params, form, secret_arn=None):
        calls.append((method, params, form, secret_arn))
        body = responses.get(method, {"ok": True})
        if callable(body):
            body = body(len([c for c in calls if c[0] == method]))
        return body, body.get("_retry_after")

    mod._slack_call = fake
    return calls


_OK_LOOKUP = {"ok": True, "user": {"id": "U123"}}


class TestExtractToken:
    """The secret may be a bare token or a JSON envelope; both must work."""

    def test_bare_string(self):
        assert _load()._extract_token(FAKE_TOKEN) == FAKE_TOKEN

    def test_bare_string_is_stripped(self):
        assert _load()._extract_token(f"  {FAKE_TOKEN} \n") == FAKE_TOKEN

    @pytest.mark.parametrize(
        "key", ["bot_token", "token", "slack_bot_token", "SLACK_BOT_TOKEN", "bot_user_oauth_token"]
    )
    def test_json_key_variants(self, key):
        raw = json.dumps({key: FAKE_TOKEN, "team": "generac"})
        assert _load()._extract_token(raw) == FAKE_TOKEN

    def test_unknown_key_single_xox_value_heuristic(self):
        raw = json.dumps({"someUnexpectedName": FAKE_TOKEN})
        assert _load()._extract_token(raw) == FAKE_TOKEN

    def test_two_xox_values_is_ambiguous_and_raises(self):
        raw = json.dumps({"a": "xoxb-one", "b": "xoxp-two"})
        with pytest.raises(RuntimeError):
            _load()._extract_token(raw)

    def test_empty_secret_raises(self):
        with pytest.raises(RuntimeError):
            _load()._extract_token("")

    def test_empty_json_object_raises(self):
        with pytest.raises(RuntimeError):
            _load()._extract_token("{}")

    def test_only_a_leading_brace_triggers_json_parsing(self):
        # Everything else is the token itself. A Slack token can never start
        # with '{', so this split is unambiguous.
        assert _load()._extract_token("[1, 2]") == "[1, 2]"

    def test_unparseable_brace_string_is_taken_verbatim(self):
        assert _load()._extract_token("{not json") == "{not json"

    def test_error_names_keys_but_never_the_value(self):
        raw = json.dumps({"weird_key": "xoxb-one", "other_key": "xoxb-two"})
        with pytest.raises(RuntimeError) as exc:
            _load()._extract_token(raw)
        assert "weird_key" in str(exc.value)
        assert "xoxb-one" not in str(exc.value)


class TestNoRecipientGate:
    """Every identity that crosses a threshold gets a DM — there is no allowlist.

    The recipient allowlist was a dev-only gate and was removed deliberately for
    production, where all users must be alerted. These tests pin the *absence* of
    a gate, because reintroducing one would silence alerts with nothing louder
    than an INFO log per skipped user.
    """

    def test_no_allowlist_symbols_remain(self):
        mod = _load()
        assert not hasattr(mod, "_is_allowed"), "recipient gate was reintroduced"
        assert not hasattr(mod, "ALLOWLIST"), "recipient gate was reintroduced"

    def test_module_does_not_read_an_allowlist_env_var(self):
        source = LAMBDA_PATH.read_text(encoding="utf-8")
        assert "SLACK_DM_ALLOWLIST" not in source

    @pytest.mark.parametrize(
        "email",
        [
            ALLOWED,
            "cameron.johnson@generac.com",
            "  CAMERON.JOHNSON@GENERAC.COM  ",
            "someone.else@generac.com",
            "kate.breedon@ecobee.com",
            "brand.new.hire@generac.com",
        ],
    )
    def test_every_recipient_is_delivered_to(self, email):
        """Previously only ALLOWED survived; now all of these must be DM'd."""
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        stats = mod.lambda_handler(_sns_event(_cost_alert(user=email)), None)
        assert stats == {"received": 1, "sent": 1, "skipped": 0, "failed": 0}
        assert [c[0] for c in calls] == ["users.lookupByEmail", "chat.postMessage"]

    def test_alert_without_an_email_is_still_skipped(self):
        """Removing the allowlist must not remove the empty-email guard: an
        alert with no identity has nobody to DM and must not reach Slack."""
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP})
        stats = mod.lambda_handler(_sns_event(_cost_alert(user="")), None)
        assert stats == {"received": 1, "sent": 0, "skipped": 1, "failed": 0}
        assert calls == []


class TestSnsEnvelope:
    def test_payload_attribute_wins(self):
        mod = _load()
        alert = _cost_alert()
        got = mod._alert_from_record(_sns_event(alert)["Records"][0]["Sns"])
        assert got == alert

    def test_legacy_string_payload_still_parses(self):
        """A message published by a quota_monitor from before the Binary switch,
        or one already in flight across a stack update, must not degrade to the
        text fallback and render a grid of "n/a"."""
        mod = _load()
        alert = _cost_alert()
        attrs = {
            "alert_type": {"Type": "String", "Value": alert["alert_type"]},
            "alert_payload": {"Type": "String", "Value": json.dumps(alert)},
        }
        got = mod._alert_from_record(_sns_event(attributes=attrs)["Records"][0]["Sns"])
        assert got == alert

    def test_undecodable_binary_payload_degrades_to_flat_attributes(self):
        mod = _load()
        attrs = {
            "alert_payload": {"Type": "Binary", "Value": "!!!not-base64!!!"},
            "user_email": {"Type": "String", "Value": ALLOWED},
            "alert_type": {"Type": "String", "Value": "monthly_cost"},
            "alert_level": {"Type": "String", "Value": "warning"},
        }
        got = mod._alert_from_record(_sns_event(attributes=attrs)["Records"][0]["Sns"])
        assert got["user"] == ALLOWED
        assert got["alert_type"] == "monthly_cost"

    def test_reads_lambda_type_value_shape_not_publish_shape(self):
        mod = _load()
        # The publish-API shape must NOT be understood — if it were, the real
        # Lambda envelope would be silently unparsed.
        sns = {
            "MessageAttributes": {
                "user_email": {"DataType": "String", "StringValue": ALLOWED},
            },
            "Subject": "",
            "Message": "",
        }
        assert mod._alert_from_record(sns)["user"] == ""

    def test_malformed_payload_degrades_to_flat_attributes(self):
        mod = _load()
        attrs = {
            "alert_payload": {"Type": "String", "Value": "{not json"},
            "user_email": {"Type": "String", "Value": ALLOWED},
            "alert_type": {"Type": "String", "Value": "daily_cost"},
            "alert_level": {"Type": "String", "Value": "critical"},
        }
        got = mod._alert_from_record(_sns_event(attributes=attrs)["Records"][0]["Sns"])
        assert got["user"] == ALLOWED
        assert got["alert_type"] == "daily_cost"

    def test_no_attributes_degrades_to_text_parsing(self):
        mod = _load()
        sns = _sns_event(
            attributes={},
            subject="Claude Code CRITICAL - Monthly Spend Budget - 92%",
            message=f"USER: {ALLOWED}\nALERT: Monthly Spend Budget - CRITICAL",
        )["Records"][0]["Sns"]
        got = mod._alert_from_record(sns)
        assert got["user"] == ALLOWED
        assert got["alert_level"] == "critical"
        assert got["alert_type"] == "monthly_cost"
        assert got["text_fallback"] is True

    def test_text_parsing_detects_daily(self):
        mod = _load()
        got = mod._alert_from_text("Claude Code WARNING - Daily Spend Budget - 81%", f"USER: {ALLOWED}")
        assert got["alert_type"] == "daily_cost"
        assert got["alert_level"] == "warning"


class TestMessageBuilding:
    def test_monthly_warning_shape(self):
        mod = _load()
        text, blocks = mod._build_message(_cost_alert())
        assert text  # Slack uses this for the push notification
        assert "85%" in text
        assert blocks[0]["type"] == "header"
        assert len(blocks[0]["text"]["text"]) <= 150
        fields = blocks[2]["fields"]
        assert len(fields) <= 10
        rendered = json.dumps(blocks)
        assert "$85.00" in rendered and "$100.00" in rendered
        assert "September 2026" in rendered and "20 day(s) left" in rendered

    def test_daily_alert_shows_the_date_not_the_month(self):
        mod = _load()
        _, blocks = mod._build_message(
            _cost_alert(alert_type="daily_cost", date="2026-09-10", month=None, days_remaining=None)
        )
        rendered = json.dumps(blocks)
        assert "Day of 2026-09-10" in rendered
        assert "daily Bedrock spend budget" in rendered

    def test_enforcement_block_warns_about_being_blocked(self):
        mod = _load()
        _, blocks = mod._build_message(_cost_alert(enforcement_mode="block"))
        assert "access is blocked" in json.dumps(blocks)

    def test_enforcement_alert_says_not_blocked(self):
        mod = _load()
        _, blocks = mod._build_message(_cost_alert(enforcement_mode="alert"))
        rendered = json.dumps(blocks)
        assert "not blocked" in rendered
        assert "access is blocked once" not in rendered

    @pytest.mark.parametrize("level", ["warning", "critical", "exceeded"])
    def test_all_levels_render(self, level):
        mod = _load()
        text, blocks = mod._build_message(_cost_alert(alert_level=level))
        assert text and blocks

    def test_mrkdwn_is_escaped(self):
        mod = _load()
        _, blocks = mod._build_message(_cost_alert(policy_info="a<b>&c"))
        rendered = json.dumps(blocks)
        assert "&lt;b&gt;" in rendered and "&amp;c" in rendered

    def test_missing_numbers_do_not_raise(self):
        mod = _load()
        text, blocks = mod._build_message({"user": ALLOWED, "alert_type": "monthly_cost"})
        assert "n/a" in json.dumps(blocks)
        assert text

    def test_help_url_omitted_when_empty(self):
        assert "Request a change" not in json.dumps(_load()._build_message(_cost_alert())[1])

    def test_help_url_included_when_set(self):
        mod = _load({"SLACK_DM_HELP_URL": "https://runbook.example.com/budget"})
        assert "Request a change" in json.dumps(mod._build_message(_cost_alert())[1])

    def test_text_fallback_forwards_the_raw_body(self):
        mod = _load()
        alert = {"user": ALLOWED, "text_fallback": True, "raw_message": "USER: x\nsomething"}
        _, blocks = mod._build_message(alert)
        assert len(blocks) == 1
        assert "something" in blocks[0]["text"]["text"]

    def test_blocks_respect_slack_limits(self):
        mod = _load({"SLACK_DM_HELP_URL": "https://x.example.com"})
        _, blocks = mod._build_message(_cost_alert())
        assert len(blocks) <= 50
        for block in blocks:
            if block.get("text", {}).get("type") == "mrkdwn":
                assert len(block["text"]["text"]) <= 3000
            for field in block.get("fields", []):
                assert len(field["text"]) <= 2000


class TestDelivery:
    def test_happy_path_sends_one_dm(self):
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        stats = mod.lambda_handler(_sns_event(_cost_alert()), None)
        assert stats == {"received": 1, "sent": 1, "skipped": 0, "failed": 0}
        assert [c[0] for c in calls] == ["users.lookupByEmail", "chat.postMessage"]
        assert calls[1][1]["channel"] == "U123"
        # lookupByEmail is form-encoded; postMessage is JSON
        assert calls[0][2] is True and calls[1][2] is False

    def test_email_sent_to_slack_keeps_its_original_casing(self):
        """The cache key lowercases; what goes on the wire must not.

        `_lookup_by_email` is handed the address verbatim from the alert, which
        carries the raw OIDC `email` claim (e.g. "Cameron.Johnson@generac.com").
        Slack does not document whether the `email` argument is normalized, so
        preserving the spelling the identity provider gave us is the only shape
        we can reason about — a `.lower()` slipped in for tidiness here could
        turn every DM into `users_not_found` with no other symptom.
        """
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        mod.lambda_handler(_sns_event(_cost_alert(user="Cameron.Johnson@generac.com")), None)
        lookup = next(c for c in calls if c[0] == "users.lookupByEmail")
        assert lookup[1]["email"] == "Cameron.Johnson@generac.com"

    def test_both_spellings_share_one_cache_entry_and_one_lookup(self):
        """Casing is preserved on the wire but folded in the cache key, so the
        same human alerting under two spellings costs one Tier 3 API call."""
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        event = _sns_event(_cost_alert(user="Cameron.Johnson@generac.com"))
        event["Records"] += _sns_event(_cost_alert(user="cameron.johnson@generac.com"))["Records"]
        stats = mod.lambda_handler(event, None)
        assert stats["sent"] == 2
        assert [c[0] for c in calls].count("users.lookupByEmail") == 1
        # Keyed by (secret ARN, lowercased email): a Slack user ID is scoped to
        # the workspace that issued it, so the ARN has to be part of the key.
        assert {email for _arn, email in mod._user_id_cache} == {"cameron.johnson@generac.com"}
        assert len(mod._user_id_cache) == 1

    def test_missing_email_is_skipped(self):
        mod = _load()
        calls = _stub_slack(mod, {})
        stats = mod.lambda_handler(_sns_event(attributes={}), None)
        assert stats["skipped"] == 1
        assert calls == []

    def test_users_not_found_does_not_post(self, capsys):
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": {"ok": False, "error": "users_not_found"}})
        stats = mod.lambda_handler(_sns_event(_cost_alert()), None)
        assert stats["failed"] == 1
        assert [c[0] for c in calls] == ["users.lookupByEmail"]
        assert "users_not_found" in capsys.readouterr().out

    def test_missing_scope_logs_error(self, capsys):
        mod = _load()
        _stub_slack(
            mod,
            {"users.lookupByEmail": {"ok": False, "error": "missing_scope", "needed": "users:read.email"}},
        )
        mod.lambda_handler(_sns_event(_cost_alert()), None)
        out = capsys.readouterr().out
        assert "ERROR:" in out and "missing_scope" in out

    def test_ratelimited_retries_exactly_once_with_bounded_sleep(self):
        mod = _load()
        slept = []
        mod.time = MagicMock()
        mod.time.sleep = lambda s: slept.append(s)
        attempts = {"n": 0}

        def lookup(_n):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"ok": False, "error": "ratelimited", "_retry_after": 999}
            return _OK_LOOKUP

        calls = _stub_slack(mod, {"users.lookupByEmail": lookup, "chat.postMessage": {"ok": True}})
        stats = mod.lambda_handler(_sns_event(_cost_alert()), None)
        assert stats["sent"] == 1
        assert [c[0] for c in calls].count("users.lookupByEmail") == 2
        # Retry-After of 999 must be clamped so a 429 cannot eat the timeout
        assert slept == [mod.SLACK_MAX_RETRY_SLEEP]

    def test_invalid_blocks_falls_back_to_plain_text(self):
        mod = _load()
        attempts = {"n": 0}

        def post(_n):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"ok": False, "error": "invalid_blocks"}
            return {"ok": True}

        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": post})
        stats = mod.lambda_handler(_sns_event(_cost_alert()), None)
        assert stats["sent"] == 1
        posts = [c for c in calls if c[0] == "chat.postMessage"]
        assert len(posts) == 2
        assert "blocks" not in posts[1][1]
        assert posts[1][1]["text"]

    def test_user_id_is_cached_across_records(self):
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        event = _sns_event(_cost_alert())
        event["Records"].append(dict(event["Records"][0]))
        stats = mod.lambda_handler(event, None)
        assert stats["sent"] == 2
        assert [c[0] for c in calls].count("users.lookupByEmail") == 1

    def test_one_bad_record_does_not_stop_the_others(self):
        mod = _load()
        _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        event = _sns_event(_cost_alert())
        event["Records"].insert(0, {"EventSource": "aws:sns"})  # no Sns key at all
        stats = mod.lambda_handler(event, None)
        assert stats["sent"] == 1
        assert stats["received"] == 2

    def test_handler_never_raises_when_slack_blows_up(self):
        mod = _load()

        def boom(method, params, form):
            raise RuntimeError("network gone")

        mod._slack_call = boom
        stats = mod.lambda_handler(_sns_event(_cost_alert()), None)
        assert stats["failed"] == 1

    def test_empty_event_is_a_noop(self):
        mod = _load()
        assert mod.lambda_handler({}, None)["received"] == 0


class TestNoTokenLogging:
    def test_token_never_reaches_stdout(self, capsys):
        mod = _load()
        _stub_slack(
            mod,
            {
                "users.lookupByEmail": {"ok": False, "error": "invalid_auth"},
            },
        )
        mod.lambda_handler(_sns_event(_cost_alert()), None)
        mod.lambda_handler(_sns_event(_cost_alert(user="nope@generac.com")), None)
        assert FAKE_TOKEN not in capsys.readouterr().out


class TestPackagingConstraints:
    def test_no_shared_imports(self):
        source = LAMBDA_PATH.read_text(encoding="utf-8")
        assert "from shared import" not in source
        assert "import shared" not in source

    def test_uses_stdlib_urllib_not_requests(self):
        source = LAMBDA_PATH.read_text(encoding="utf-8")
        assert "import requests" not in source
        assert "urllib.request" in source

    def test_no_requirements_or_vendored_packages(self):
        assert not (LAMBDA_DIR / "requirements.txt").exists()
        assert [p.name for p in LAMBDA_DIR.iterdir() if p.is_dir() and p.name != "__pycache__"] == []

    def test_every_slack_api_call_has_a_timeout(self):
        source = LAMBDA_PATH.read_text(encoding="utf-8")
        assert source.count("urlopen(") == source.count("timeout=")


@pytest.fixture(scope="module")
def resolved():
    """Template with intrinsic tags collapsed to their payloads."""
    return load_resolved(TEMPLATE)


@pytest.fixture(scope="module")
def intrinsics():
    """Template in canonical long form, so a Ref is distinguishable from a literal."""
    return load_intrinsics(TEMPLATE)


class TestTemplateWiring:
    def test_parameters_exist_with_defaults(self, resolved):
        params = resolved["Parameters"]
        for name in (
            "SlackBotTokenSecretArn",
            "SlackApiTimeoutSeconds",
            "SlackDmHelpUrl",
        ):
            assert name in params, f"{name} missing"
            assert "Default" in params[name], f"{name} needs a Default for existing stacks"
        assert params["SlackBotTokenSecretArn"]["Default"] == ""
        assert "SlackDmAllowlist" not in params, "the recipient allowlist was removed"

    def test_condition_gates_every_resource_and_the_output(self, resolved):
        assert "SlackNotifierEnabled" in resolved["Conditions"]
        for logical_id in (
            "QuotaSlackNotifierRole",
            "QuotaSlackNotifierFunction",
            "QuotaSlackNotifierPermission",
            "QuotaSlackNotifierSubscription",
        ):
            assert resolved["Resources"][logical_id]["Condition"] == "SlackNotifierEnabled"
        assert resolved["Outputs"]["QuotaSlackNotifierFunctionArn"]["Condition"] == "SlackNotifierEnabled"

    def test_code_path_matches_the_real_directory(self, resolved):
        code = resolved["Resources"]["QuotaSlackNotifierFunction"]["Properties"]["Code"]
        assert code == "./lambda-functions/quota_slack_notifier/"
        assert LAMBDA_PATH.exists()

    def test_filter_policy_limits_invocations_to_cost_alerts(self, resolved):
        """The regression that would silence every DM, or DM users about
        operator problems, if it drifted."""
        sub = resolved["Resources"]["QuotaSlackNotifierSubscription"]["Properties"]
        assert sub["Protocol"] == "lambda"
        assert sub["FilterPolicy"] == {"alert_type": ["monthly_cost", "daily_cost"]}

    def test_subscription_waits_for_the_invoke_permission(self, resolved):
        sub = resolved["Resources"]["QuotaSlackNotifierSubscription"]
        assert sub["DependsOn"] == "QuotaSlackNotifierPermission"

    def test_permission_is_scoped_to_the_quota_topic(self, intrinsics):
        props = intrinsics["Resources"]["QuotaSlackNotifierPermission"]["Properties"]
        assert props["Principal"] == "sns.amazonaws.com"
        assert props["SourceArn"] == {"Ref": "QuotaAlertTopic"}

    def test_role_grants_only_the_secret_read(self, intrinsics):
        statements = intrinsics["Resources"]["QuotaSlackNotifierRole"]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        assert len(statements) == 1
        assert statements[0]["Action"] == ["secretsmanager:GetSecretValue"]
        # Must be the parameter(s), never a hand-built ARN (the random suffix is
        # unguessable) — see .claude/rules/aws-identifiers.md. The second
        # workspace's secret is granted ONLY when that feature is on, so a
        # single-workspace stack keeps a one-resource grant.
        assert statements[0]["Resource"] == {
            "Fn::If": [
                "SlackSecondaryWorkspaceEnabled",
                [{"Ref": "SlackBotTokenSecretArn"}, {"Ref": "SlackSecondaryBotTokenSecretArn"}],
                {"Ref": "SlackBotTokenSecretArn"},
            ]
        }

    def test_secret_grant_has_no_wildcard_in_either_branch(self, intrinsics):
        """A wildcard here would hand the function every secret in the account."""
        statement = intrinsics["Resources"]["QuotaSlackNotifierRole"]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ][0]
        assert "*" not in json.dumps(statement["Resource"])

    def test_secondary_workspace_needs_both_halves(self, intrinsics):
        """A token with no domains is dead config; domains with no token silently
        fall back to the primary workspace. Require both, so neither half can be
        set alone and look like it is working."""
        condition = intrinsics["Conditions"]["SlackSecondaryWorkspaceEnabled"]
        rendered = json.dumps(condition)
        assert "Fn::And" in rendered
        assert "SlackSecondaryBotTokenSecretArn" in rendered
        assert "SlackSecondaryDomains" in rendered

    def test_secondary_params_default_to_off(self, resolved):
        """Existing stacks must update without turning anything on."""
        params = resolved["Parameters"]
        assert params["SlackSecondaryBotTokenSecretArn"]["Default"] == ""
        assert params["SlackSecondaryDomains"]["Default"] == ""

    def test_function_is_not_vpc_attached(self, resolved):
        props = resolved["Resources"]["QuotaSlackNotifierFunction"]["Properties"]
        assert "VpcConfig" not in props

    def test_template_supplies_every_env_var_the_module_reads(self, resolved):
        """Guards the classic drift where one side is renamed."""
        source = LAMBDA_PATH.read_text(encoding="utf-8")
        env = resolved["Resources"]["QuotaSlackNotifierFunction"]["Properties"]["Environment"]["Variables"]
        for name in (
            "SLACK_BOT_TOKEN_SECRET_ARN",
            "SLACK_API_TIMEOUT_SECONDS",
            "SLACK_DM_HELP_URL",
        ):
            assert f'"{name}"' in source, f"{name} not read by the Lambda"
            assert name in env, f"{name} not set by the template"
        assert "SLACK_DM_ALLOWLIST" not in env, "the recipient allowlist was removed"


class TestDeployWiring:
    def test_deploy_passes_the_slack_parameters(self):
        """deploy_stack does not use UsePreviousValue, so a parameter absent
        from deploy.py's list is reset to its template default on every deploy —
        silently deleting the notifier and its subscription."""
        source = (
            Path(__file__).resolve().parents[1] / "claude_code_with_bedrock" / "cli" / "commands" / "deploy.py"
        ).read_text(encoding="utf-8")
        assert "SlackBotTokenSecretArn=" in source
        assert "SlackSecondaryBotTokenSecretArn=" in source
        assert "SlackSecondaryDomains=" in source
        # Passing a parameter the template no longer declares fails the deploy.
        assert "SlackDmAllowlist=" not in source


SECONDARY_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-ecobee-XyZ789"
PRIMARY_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-AbCdEf"
EXTERNAL = "kate.breedon@ecobee.com"


def _two_workspace_env(**overrides):
    env = {
        "SLACK_SECONDARY_BOT_TOKEN_SECRET_ARN": SECONDARY_ARN,
        "SLACK_SECONDARY_DOMAINS": "ecobee.com",
    }
    env.update(overrides)
    return env


class TestSecondaryWorkspaceRouting:
    """A Slack bot can only see users in its own workspace.

    users.lookupByEmail returned `users_not_found` for kate.breedon@ecobee.com
    against the generac workspace bot — verified live. That error is
    indistinguishable from "has no Slack account", so a domain living in another
    workspace must be routed to a token installed THERE. 167 of 395 identities in
    the quota table are on that domain, so this is the majority-of-users path,
    not an edge case.
    """

    def test_secondary_domain_uses_the_secondary_token(self):
        mod = _load(_two_workspace_env())
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        mod.lambda_handler(_sns_event(_cost_alert(user=EXTERNAL)), None)
        assert [c[3] for c in calls] == [SECONDARY_ARN, SECONDARY_ARN]

    def test_primary_domain_is_unaffected_by_the_second_workspace(self):
        """Adding a workspace must not change delivery for the existing one."""
        mod = _load(_two_workspace_env())
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        mod.lambda_handler(_sns_event(_cost_alert(user=ALLOWED)), None)
        assert [c[3] for c in calls] == [PRIMARY_ARN, PRIMARY_ARN]

    def test_lookup_and_post_share_one_workspace(self):
        """A user ID from one workspace is meaningless in another."""
        mod = _load(_two_workspace_env())
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        mod.lambda_handler(_sns_event(_cost_alert(user=EXTERNAL)), None)
        assert len({c[3] for c in calls}) == 1

    def test_unconfigured_secondary_falls_back_to_primary(self):
        """Default (both empty) must behave exactly as the single-workspace build."""
        mod = _load()
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        mod.lambda_handler(_sns_event(_cost_alert(user=EXTERNAL)), None)
        assert [c[3] for c in calls] == [PRIMARY_ARN, PRIMARY_ARN]

    def test_domains_without_a_token_do_not_route(self):
        """Half-configured must not silently point at an empty ARN."""
        mod = _load({"SLACK_SECONDARY_DOMAINS": "ecobee.com"})
        assert mod._secret_arn_for_email(EXTERNAL) == PRIMARY_ARN

    def test_token_without_domains_does_not_route(self):
        mod = _load({"SLACK_SECONDARY_BOT_TOKEN_SECRET_ARN": SECONDARY_ARN})
        assert mod._secret_arn_for_email(EXTERNAL) == PRIMARY_ARN

    def test_domain_matching_is_case_insensitive_and_padded_tolerant(self):
        mod = _load(_two_workspace_env(SLACK_SECONDARY_DOMAINS=" ECOBEE.COM , @other.com "))
        assert mod._secret_arn_for_email("Kate.Breedon@EcoBee.CoM") == SECONDARY_ARN
        assert mod._secret_arn_for_email("someone@other.com") == SECONDARY_ARN
        assert mod._secret_arn_for_email(ALLOWED) == PRIMARY_ARN

    def test_subdomain_is_not_treated_as_a_match(self):
        """Exact domain only — evil-ecobee.com must not borrow the token."""
        mod = _load(_two_workspace_env())
        assert mod._secret_arn_for_email("x@evil-ecobee.com") == PRIMARY_ARN
        assert mod._secret_arn_for_email("x@sub.ecobee.com") == PRIMARY_ARN

    def test_malformed_address_falls_back_to_primary(self):
        mod = _load(_two_workspace_env())
        assert mod._secret_arn_for_email("no-at-sign") == PRIMARY_ARN

    def test_each_workspace_token_is_fetched_and_cached_separately(self):
        mod = _load(_two_workspace_env())
        _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        assert mod._slack_token(PRIMARY_ARN) == FAKE_TOKEN
        assert mod._slack_token(SECONDARY_ARN) == FAKE_TOKEN
        assert set(mod._slack_token_cache) == {PRIMARY_ARN, SECONDARY_ARN}
        # Two distinct secrets => two reads, then cached.
        assert mod.secrets_client.get_secret_value.call_count == 2
        mod._slack_token(PRIMARY_ARN)
        assert mod.secrets_client.get_secret_value.call_count == 2

    def test_same_email_in_two_workspaces_is_two_cache_entries(self):
        """The cache key must include the workspace or a stale ID gets reused."""
        mod = _load(_two_workspace_env())
        _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        mod._lookup_user_id(ALLOWED, PRIMARY_ARN)
        mod._lookup_user_id(ALLOWED, SECONDARY_ARN)
        assert len(mod._user_id_cache) == 2
        assert (PRIMARY_ARN, ALLOWED.lower()) in mod._user_id_cache
        assert (SECONDARY_ARN, ALLOWED.lower()) in mod._user_id_cache

    def test_users_not_found_names_the_workspace_searched(self, capsys):
        """Without this, a missing SLACK_SECONDARY_DOMAINS entry is unfixable from
        the logs — it looks identical to the user having no Slack account."""
        mod = _load(_two_workspace_env())
        _stub_slack(mod, {"users.lookupByEmail": {"ok": False, "error": "users_not_found"}})
        mod.lambda_handler(_sns_event(_cost_alert(user=EXTERNAL)), None)
        out = capsys.readouterr().out
        assert "users_not_found" in out
        assert "secondary workspace" in out

    def test_neither_workspace_arn_is_ever_logged_with_a_token(self, capsys):
        mod = _load(_two_workspace_env())
        _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        mod.lambda_handler(_sns_event(_cost_alert(user=EXTERNAL)), None)
        out = capsys.readouterr().out
        assert FAKE_TOKEN not in out
        assert "xox" not in out

    def test_secondary_domain_needs_no_opt_in(self):
        """With the allowlist gone, an ecobee address is DM'd on sight — via the
        secondary workspace's token, not the primary one."""
        mod = _load(_two_workspace_env())
        calls = _stub_slack(mod, {"users.lookupByEmail": _OK_LOOKUP, "chat.postMessage": {"ok": True}})
        stats = mod.lambda_handler(_sns_event(_cost_alert(user=EXTERNAL)), None)
        assert stats == {"received": 1, "sent": 1, "skipped": 0, "failed": 0}
        assert [c[3] for c in calls] == [SECONDARY_ARN, SECONDARY_ARN]
