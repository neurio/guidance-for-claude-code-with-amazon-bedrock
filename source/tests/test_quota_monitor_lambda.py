# ABOUTME: Tests for the quota_monitor Lambda's TimescaleDB cost source and absolute writes
# ABOUTME: Covers the stale-day guard, idempotent SET writes, shadow mode and the safety floor

"""Tests for quota_monitor.

Two regressions are pinned here:

1. Stale-day guard: an idle user's daily_tokens froze above the daily limit and a
   fresh "Daily Token Quota EXCEEDED" alert went out every new UTC day, because
   the threshold step read daily_tokens verbatim (without the guard that
   quota_check already applies). This still matters for users present in
   DynamoDB but absent from the telemetry DB result.

2. Absolute writes: cost used to be accumulated with a DynamoDB `ADD` over a
   rolling 15-minute PromQL delta, which had no watermark and no dedup key — a
   retried or missed invocation permanently skewed the counter. The monitor now
   reads month-to-date totals from TimescaleDB and writes them with `SET`, which
   is idempotent and self-healing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "quota_monitor"
    / "index.py"
)

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:telemetry-db-AbCdEf"


def _load_quota_monitor(env: dict) -> object:
    """Load the quota_monitor Lambda module fresh with the given environment."""
    for key, value in env.items():
        os.environ[key] = value

    module_name = f"quota_monitor_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def base_env():
    # QUOTA_WRITE_MODE is intentionally left at its "shadow" default here so the
    # threshold tests exercise the DynamoDB-derived path.
    return {
        "QUOTA_TABLE": "TestQuotaTable",
        "POLICIES_TABLE": "TestPoliciesTable",
        "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-alerts",
        "ENABLE_FINEGRAINED_QUOTAS": "false",
        "MONTHLY_TOKEN_LIMIT": "40000000",
        "TELEMETRY_DB_SECRET_ARN": SECRET_ARN,
        "TELEMETRY_DB_HOST": "10.0.0.1",
        "QUOTA_WRITE_MODE": "shadow",
    }


@pytest.fixture
def enforce_env(base_env):
    env = dict(base_env)
    env["QUOTA_WRITE_MODE"] = "enforce"
    return env


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _scan_response(items: list[dict]) -> dict:
    """A single-page DynamoDB scan response (no LastEvaluatedKey)."""
    return {"Items": items}


def _usage(monthly_cost=0.0, daily_cost=0.0, total=0, daily=0, inp=0, out=0, cache=0):
    return {
        "total_tokens": total,
        "daily_tokens": daily,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_tokens": cache,
        "monthly_cost": monthly_cost,
        "daily_cost": daily_cost,
    }


def _fake_conn(rows):
    """A DB-API stand-in for the telemetry DB, so tests never need a live server."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def _patch_monitor(mod, scan_item: dict, daily_token_limit: int = 2_000_000, db_usage=None):
    """Wire the monitor so the DB read is stubbed and the scan returns one user row.

    The stubbed DB result deliberately describes a *different* identity than the
    scanned row: that is the real-world case the stale-day guard protects (a user
    with a DynamoDB row who no longer appears in the telemetry query), and it
    keeps the safety floor satisfied.

    Returns the MagicMock SNS client for assertions on published alerts.
    """
    if db_usage is None:
        db_usage = {"someone.else@example.com": _usage(monthly_cost=1.0, total=100)}
    mod.fetch_usage_from_db = MagicMock(return_value=db_usage)

    mod.quota_table = MagicMock()
    mod.quota_table.scan.return_value = _scan_response([scan_item])
    # get_sent_alerts issues a query; return no prior alerts.
    mod.quota_table.query.return_value = {"Items": []}

    # Force the env-var policy to carry a daily limit so daily checks run.
    base_policy = {
        "policy_type": "default",
        "identifier": "environment",
        "monthly_token_limit": 40_000_000,
        "daily_token_limit": daily_token_limit,
        "warning_threshold_80": 32_000_000,
        "warning_threshold_90": 36_000_000,
        "enforcement_mode": "alert",
        "enabled": True,
    }
    mod.resolve_user_quota = MagicMock(return_value=base_policy)

    mod.sns_client = MagicMock()
    return mod.sns_client


def _daily_alert_published(sns_client) -> bool:
    """True if any SNS publish call carried a Daily Token Quota alert."""
    for call in sns_client.publish.call_args_list:
        subject = call.kwargs.get("Subject", "")
        if "Daily Token Quota" in subject:
            return True
    return False


def _user_updates(quota_table):
    """The update_item calls that targeted a USER# item, as kwargs dicts."""
    return [
        c.kwargs
        for c in quota_table.update_item.call_args_list
        if str(c.kwargs.get("Key", {}).get("pk", "")).startswith("USER#")
    ]


class TestStaleDailyReset:
    def test_idle_user_stale_day_no_daily_alert(self, base_env):
        """daily_date=yesterday + daily_tokens over limit + no activity -> no alert."""
        mod = _load_quota_monitor(base_env)
        sns = _patch_monitor(
            mod,
            scan_item={
                "email": "idle.user@example.com",
                "total_tokens": 3_355_533,
                "daily_tokens": 3_355_533,  # frozen, above 2,000,000 limit
                "daily_date": _yesterday(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert not _daily_alert_published(sns), "stale daily counter must not re-alert"

    def test_active_user_same_day_over_limit_still_alerts(self, base_env):
        """daily_date=today + over limit -> daily alert IS generated (no over-correction)."""
        mod = _load_quota_monitor(base_env)
        sns = _patch_monitor(
            mod,
            scan_item={
                "email": "active.user@example.com",
                "total_tokens": 3_355_533,
                "daily_tokens": 3_355_533,  # above 2,000,000 limit, today
                "daily_date": _today(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert _daily_alert_published(sns), "genuine same-day over-limit must still alert"

    def test_build_usage_entry_zeros_stale_daily(self, base_env):
        """_build_usage_entry applies the stale-day guard; monthly is untouched."""
        mod = _load_quota_monitor(base_env)
        item = {
            "email": "idle.user@example.com",
            "total_tokens": 3_355_533,
            "daily_tokens": 3_355_533,
            "daily_date": _yesterday(),
        }
        entry = mod._build_usage_entry(item, _today())
        assert entry["daily_tokens"] == 0
        assert entry["total_tokens"] == 3_355_533

    def test_build_usage_entry_keeps_same_day_daily(self, base_env):
        mod = _load_quota_monitor(base_env)
        item = {
            "email": "active.user@example.com",
            "total_tokens": 3_355_533,
            "daily_tokens": 3_355_533,
            "daily_date": _today(),
        }
        entry = mod._build_usage_entry(item, _today())
        assert entry["daily_tokens"] == 3_355_533


class TestPolicyPagination:
    """Every scan page must produce the same policy shape.

    The pagination branch used to rebuild the policy dict itself and had dropped
    `monthly_cost_limit`/`daily_cost_limit`, so a policy landing on page 2+ came
    back with a $0 budget and went unenforced -- for some users and not others,
    depending on scan ordering. Once there are enough policies to paginate, that
    is invisible without a test like this.
    """

    def _policy_item(self, policy_type, identifier, monthly_cost=None):
        item = {
            "pk": f"POLICY#{policy_type}#{identifier}",
            "sk": "CURRENT",
            "policy_type": policy_type,
            "identifier": identifier,
            "monthly_token_limit": 0,
            "daily_token_limit": 0,
            "warning_threshold_80": 0,
            "warning_threshold_90": 0,
            "enforcement_mode": "alert",
            "enabled": True,
        }
        if monthly_cost is not None:
            item["monthly_cost_limit"] = Decimal(str(monthly_cost))
        return item

    def _paged(self, mod, pages):
        mod.policies_table = MagicMock()
        mod.policies_table.scan.side_effect = pages
        return mod.load_all_policies()

    def test_cost_limits_survive_a_paginated_scan(self, base_env):
        mod = _load_quota_monitor(base_env)
        policies = self._paged(
            mod,
            [
                {
                    "Items": [self._policy_item("default", "default", 1000)],
                    "LastEvaluatedKey": {"pk": "POLICY#default#default", "sk": "CURRENT"},
                },
                {"Items": [self._policy_item("user", "big.spender@example.com", 2500)]},
            ],
        )
        assert policies["default:default"]["monthly_cost_limit"] == 1000.0
        # The page-2 policy is the one the old code silently zeroed.
        assert policies["user:big.spender@example.com"]["monthly_cost_limit"] == 2500.0

    def test_every_page_uses_the_same_policy_shape(self, base_env):
        mod = _load_quota_monitor(base_env)
        policies = self._paged(
            mod,
            [
                {
                    "Items": [self._policy_item("default", "default", 1000)],
                    "LastEvaluatedKey": {"pk": "POLICY#default#default", "sk": "CURRENT"},
                },
                {"Items": [self._policy_item("user", "second.page@example.com", 50)]},
            ],
        )
        assert set(policies["default:default"]) == set(policies["user:second.page@example.com"])

    def test_pagination_carries_the_filter_and_the_start_key(self, base_env):
        mod = _load_quota_monitor(base_env)
        self._paged(
            mod,
            [
                {"Items": [], "LastEvaluatedKey": {"pk": "POLICY#a#b", "sk": "CURRENT"}},
                {"Items": []},
            ],
        )
        first, second = mod.policies_table.scan.call_args_list
        assert "FilterExpression" in first.kwargs
        assert "ExclusiveStartKey" not in first.kwargs
        # Dropping the filter on later pages would pull in non-CURRENT revisions.
        assert "FilterExpression" in second.kwargs
        assert second.kwargs["ExclusiveStartKey"] == {"pk": "POLICY#a#b", "sk": "CURRENT"}

    def test_stops_when_no_start_key_is_returned(self, base_env):
        """side_effect raises StopIteration if the loop scans a third time."""
        mod = _load_quota_monitor(base_env)
        policies = self._paged(
            mod,
            [
                {
                    "Items": [self._policy_item("default", "default", 1000)],
                    "LastEvaluatedKey": {"pk": "POLICY#default#default", "sk": "CURRENT"},
                },
                {"Items": [self._policy_item("user", "last@example.com", 10)]},
            ],
        )
        assert mod.policies_table.scan.call_count == 2
        assert len(policies) == 2

    def test_missing_cost_limit_is_zero_not_an_error(self, base_env):
        """Cost limits are written by a separate update_item, so they can be absent."""
        mod = _load_quota_monitor(base_env)
        policies = self._paged(mod, [{"Items": [self._policy_item("default", "default")]}])
        assert policies["default:default"]["monthly_cost_limit"] == 0.0
        assert policies["default:default"]["daily_cost_limit"] == 0.0


class TestSummaryCounters:
    """The Summary log line must measure the limit that is actually enforced.

    In cost mode the token limits are 0 (disabled), so counting token percentages
    printed `Over 80%: 0, Over 90%: 0, Exceeded: 0` even with users thousands of
    dollars over their $1,000 budget. An operator reads that line as "nobody is
    near their limit", so a silently token-denominated summary is a real
    observability bug, not a cosmetic one.
    """

    COST_POLICY = {
        "policy_type": "default",
        "identifier": "environment",
        # Cost mode: token limits and their thresholds are all disabled.
        "monthly_token_limit": 0,
        "daily_token_limit": None,
        "warning_threshold_80": 0,
        "warning_threshold_90": 0,
        "monthly_cost_limit": 1000.0,
        "daily_cost_limit": 0.0,
        "enforcement_mode": "alert",
        "enabled": True,
    }

    def _run(self, mod, items, policy=None):
        mod.fetch_usage_from_db = MagicMock(return_value={})
        mod.quota_table = MagicMock()
        mod.quota_table.scan.return_value = _scan_response(items)
        mod.quota_table.query.return_value = {"Items": []}
        mod.resolve_user_quota = MagicMock(return_value=policy or self.COST_POLICY)
        mod.sns_client = MagicMock()
        result = mod.lambda_handler({}, None)
        return json.loads(result["body"])

    def _item(self, email, cost):
        return {
            "email": email,
            "estimated_cost": cost,
            "total_tokens": 1_000_000,
            "daily_tokens": 0,
            "daily_date": _today(),
        }

    def test_counts_the_cost_ladder_not_tokens(self, base_env):
        mod = _load_quota_monitor(base_env)
        stats = self._run(
            mod,
            [
                self._item("under@example.com", 100.00),  # 10%
                self._item("warn@example.com", 850.00),  # 85%
                self._item("critical@example.com", 950.00),  # 95%
                self._item("over@example.com", 3405.83),  # 340%
                self._item("also.over@example.com", 1449.22),  # 145%
            ],
        )
        assert stats["limit_basis"] == "cost"
        assert stats["total_users"] == 5
        assert stats["over_80"] == 1
        assert stats["over_90"] == 1
        assert stats["exceeded"] == 2

    def test_reports_mtd_spend_total(self, base_env):
        mod = _load_quota_monitor(base_env)
        stats = self._run(
            mod,
            [
                self._item("a@example.com", 100.50),
                self._item("b@example.com", 200.25),
            ],
        )
        assert stats["monthly_cost_total"] == 300.75

    def test_falls_back_to_tokens_when_no_cost_budget(self, base_env):
        """Token-mode stacks must keep their original behaviour."""
        mod = _load_quota_monitor(base_env)
        policy = dict(self.COST_POLICY)
        policy["monthly_cost_limit"] = 0.0
        policy["monthly_token_limit"] = 40_000_000
        item = self._item("heavy@example.com", 0.0)
        item["total_tokens"] = 41_000_000  # over the token limit
        stats = self._run(mod, [item], policy=policy)
        assert stats["limit_basis"] == "tokens"
        assert stats["exceeded"] == 1

    def test_cost_budget_wins_when_both_limits_are_set(self, base_env):
        """A user inside their token limit but over budget must still be counted."""
        mod = _load_quota_monitor(base_env)
        policy = dict(self.COST_POLICY)
        policy["monthly_token_limit"] = 40_000_000
        item = self._item("spendy@example.com", 1200.00)
        item["total_tokens"] = 1_000_000  # well under the token limit
        stats = self._run(mod, [item], policy=policy)
        assert stats["limit_basis"] == "cost"
        assert stats["exceeded"] == 1

    def test_daily_exceeded_uses_the_daily_cost_budget(self, base_env):
        mod = _load_quota_monitor(base_env)
        policy = dict(self.COST_POLICY)
        policy["daily_cost_limit"] = 50.0
        item = self._item("burst@example.com", 200.00)
        item["daily_cost_usd"] = 75.00
        stats = self._run(mod, [item], policy=policy)
        assert stats["daily_exceeded"] == 1


class TestUsageSql:
    """The SQL is the contract with the telemetry database."""

    def test_targets_the_deduplicated_unified_view(self, base_env):
        mod = _load_quota_monitor(base_env)
        assert "telemetry.unified_hourly_cost" in mod.USAGE_SQL
        assert mod.COST_SOURCE == "telemetry.unified_hourly_cost"

    def test_scopes_to_month_and_day(self, base_env):
        mod = _load_quota_monitor(base_env)
        assert "date_trunc('month', now())" in mod.USAGE_SQL
        assert "date_trunc('day', now())" in mod.USAGE_SQL

    def test_groups_on_lowered_identity_but_selects_display_casing(self, base_env):
        """Aggregate case-insensitively; key DynamoDB on the original spelling."""
        mod = _load_quota_monitor(base_env)
        assert "lower(user_email)" in mod.USAGE_SQL
        assert "min(user_email)" in mod.USAGE_SQL

    def test_counts_both_cache_read_spellings(self, base_env):
        """The view unions a snake_case and a camelCase producer."""
        mod = _load_quota_monitor(base_env)
        assert "'cache_read'" in mod.USAGE_SQL
        assert "'cacheRead'" in mod.USAGE_SQL

    def test_filters_to_email_identities(self, base_env):
        """Pin the server-side half of the email-only rule.

        Dropping this from the SQL would still be caught by the client guard,
        but only after transferring every service principal's row.
        """
        mod = _load_quota_monitor(base_env)
        assert "user_email LIKE '%@%'" in mod.USAGE_SQL

    def test_is_a_static_select_with_no_interpolation(self, base_env):
        mod = _load_quota_monitor(base_env)
        assert mod.USAGE_SQL.strip().startswith("SELECT")
        assert "%s" not in mod.USAGE_SQL
        assert "{" not in mod.USAGE_SQL


class TestFetchUsageFromDb:
    def _rows(self):
        # (email_key, email_display, monthly_cost, daily_cost,
        #  total_tokens, daily_tokens, input_tokens, output_tokens, cache_tokens)
        return [
            (
                "alice.smith@example.com",
                "Alice.Smith@example.com",
                Decimal("123.456789"),
                Decimal("12.5"),
                1_000_000,
                50_000,
                400_000,
                100_000,
                500_000,
            ),
            ("cdp-ci-role", "cdp-ci-role", Decimal("23.95"), Decimal("0"), 9_000, 0, 3_000, 1_000, 5_000),
        ]

    def test_preserves_original_case_for_the_dynamodb_key(self, base_env):
        """Verified against production: the DB's casing matches the existing pk."""
        mod = _load_quota_monitor(base_env)
        conn, _ = _fake_conn(self._rows())
        usage = mod.fetch_usage_from_db(conn_factory=lambda: conn)
        assert "Alice.Smith@example.com" in usage
        assert "alice.smith@example.com" not in usage

    def test_maps_all_counters(self, base_env):
        mod = _load_quota_monitor(base_env)
        conn, _ = _fake_conn(self._rows())
        usage = mod.fetch_usage_from_db(conn_factory=lambda: conn)
        u = usage["Alice.Smith@example.com"]
        assert u["monthly_cost"] == pytest.approx(123.456789)
        assert u["daily_cost"] == pytest.approx(12.5)
        assert u["total_tokens"] == 1_000_000
        assert u["daily_tokens"] == 50_000
        assert u["input_tokens"] == 400_000
        assert u["output_tokens"] == 100_000
        assert u["cache_tokens"] == 500_000

    def test_excludes_non_email_service_identities(self, base_env):
        """Only humans get quota rows; service/CI roles have nobody to notify.

        USAGE_SQL filters these server-side, so a fake cursor that *returns* one
        is exactly the case the client-side guard exists for -- this test fails
        if that guard is dropped on the assumption the SQL is enough.
        """
        mod = _load_quota_monitor(base_env)
        conn, _ = _fake_conn(self._rows())
        usage = mod.fetch_usage_from_db(conn_factory=lambda: conn)
        assert "cdp-ci-role" not in usage
        assert list(usage) == ["Alice.Smith@example.com"]

    def test_closes_the_connection(self, base_env):
        mod = _load_quota_monitor(base_env)
        conn, cursor = _fake_conn(self._rows())
        mod.fetch_usage_from_db(conn_factory=lambda: conn)
        conn.close.assert_called_once()
        cursor.close.assert_called_once()

    def test_skips_blank_and_overlong_identities(self, base_env):
        mod = _load_quota_monitor(base_env)
        rows = [
            ("", "   ", Decimal("1"), Decimal("0"), 1, 0, 0, 0, 0),
            ("x" * 400, "x" * 400, Decimal("1"), Decimal("0"), 1, 0, 0, 0, 0),
        ]
        conn, _ = _fake_conn(rows)
        assert mod.fetch_usage_from_db(conn_factory=lambda: conn) == {}


class TestAbsoluteWrites:
    def _run(self, mod, usage=None):
        mod.quota_table = MagicMock()
        usage = usage or {
            "a@b.com": _usage(monthly_cost=42.123456789, daily_cost=1.5, total=999, daily=10, inp=5, out=4, cache=990)
        }
        mod.write_usage_absolute(usage, shadow=False)
        return _user_updates(mod.quota_table)[0]

    def test_uses_set_and_never_add(self, enforce_env):
        """The whole point of the migration: no accumulator."""
        mod = _load_quota_monitor(enforce_env)
        kwargs = self._run(mod)
        expr = kwargs["UpdateExpression"]
        assert expr.startswith("SET ")
        assert "ADD" not in expr

    def test_writes_every_counter_and_the_consumer_contract_fields(self, enforce_env):
        mod = _load_quota_monitor(enforce_env)
        kwargs = self._run(mod)
        expr = kwargs["UpdateExpression"]
        for attr in (
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "cache_tokens",
            "estimated_cost",
            "daily_tokens",
            "daily_cost_usd",
            "daily_date",
            "last_updated",
            "email",
            "cost_source",
        ):
            assert attr in expr, f"{attr} must be written"
        assert kwargs["ExpressionAttributeNames"] == {"#ttl": "ttl"}

    def test_estimated_cost_is_a_number_not_a_string(self, enforce_env):
        """quota_check does float(...) on this; a String would break comparisons."""
        mod = _load_quota_monitor(enforce_env)
        kwargs = self._run(mod)
        assert isinstance(kwargs["ExpressionAttributeValues"][":cost"], Decimal)
        assert kwargs["ExpressionAttributeValues"][":cost"] == Decimal("42.123457")

    def test_last_updated_is_z_suffixed(self, enforce_env):
        """sidecar_monitor parses this with .replace("Z", "+00:00")."""
        mod = _load_quota_monitor(enforce_env)
        kwargs = self._run(mod)
        assert kwargs["ExpressionAttributeValues"][":ts"].endswith("Z")

    def test_email_attribute_is_written(self, enforce_env):
        """The threshold pass joins on the top-level email attribute, not the key."""
        mod = _load_quota_monitor(enforce_env)
        kwargs = self._run(mod)
        assert kwargs["ExpressionAttributeValues"][":email"] == "a@b.com"
        assert kwargs["Key"]["pk"] == "USER#a@b.com"

    def test_daily_date_is_stamped_to_today(self, enforce_env):
        mod = _load_quota_monitor(enforce_env)
        kwargs = self._run(mod)
        assert kwargs["ExpressionAttributeValues"][":date"] == _today()

    def test_no_read_before_write(self, enforce_env):
        """Absolute writes need no prior daily_date, so the per-user GetItem is gone."""
        mod = _load_quota_monitor(enforce_env)
        mod.quota_table = MagicMock()
        mod.write_usage_absolute({"a@b.com": _usage(total=1)}, shadow=False)
        mod.quota_table.get_item.assert_not_called()

    def test_zero_usage_user_is_still_written(self, enforce_env):
        """Zero is a legitimate absolute value; skipping it stranded daily_date."""
        mod = _load_quota_monitor(enforce_env)
        mod.quota_table = MagicMock()
        mod.write_usage_absolute({"a@b.com": _usage()}, shadow=False)
        assert len(_user_updates(mod.quota_table)) == 1


class TestIdempotence:
    def test_two_identical_runs_produce_identical_writes(self, enforce_env):
        """Under the old ADD this doubled the counters; now it is a no-op."""
        mod = _load_quota_monitor(enforce_env)
        usage = {"a@b.com": _usage(monthly_cost=10.0, total=500)}

        mod.quota_table = MagicMock()
        mod.write_usage_absolute(usage, shadow=False)
        first = _user_updates(mod.quota_table)[0]

        mod.quota_table = MagicMock()
        mod.write_usage_absolute(usage, shadow=False)
        second = _user_updates(mod.quota_table)[0]

        assert first["UpdateExpression"] == second["UpdateExpression"]
        assert first["ExpressionAttributeValues"][":tt"] == second["ExpressionAttributeValues"][":tt"]
        assert first["ExpressionAttributeValues"][":cost"] == second["ExpressionAttributeValues"][":cost"]


class TestMonthEndTtl:
    """The usage row must expire one month on, at a stable instant.

    Two defects are pinned here. Both were silent: nothing reads an expired row
    (October writes land on a different sort key), so the only symptom was rows
    lingering in the table longer than intended.

    1. Month skip: `now.replace(day=28) + timedelta(days=32)` overshoots into the
       month *after* next for January in non-leap years (Jan 28 + 32d = Mar 1),
       doubling the lifetime of every January row.
    2. Drifting expiry: the time fields were inherited from the Lambda's run
       time, so a row rewritten every 15 minutes carried a different ttl on each
       write.
    """

    @pytest.mark.parametrize("year", [2027, 2028])  # non-leap, leap
    @pytest.mark.parametrize("month", range(1, 13))
    def test_advances_exactly_one_month(self, base_env, year, month):
        mod = _load_quota_monitor(base_env)
        ttl = mod._month_end_ttl(datetime(year, month, 15, 12, 34, 56, tzinfo=timezone.utc))
        landed = datetime.fromtimestamp(ttl, tz=timezone.utc)

        expected_month = 1 if month == 12 else month + 1
        expected_year = year + 1 if month == 12 else year
        assert (landed.year, landed.month, landed.day) == (expected_year, expected_month, 1)
        assert (landed.hour, landed.minute, landed.second) == (0, 0, 0)

    def test_january_non_leap_does_not_skip_february(self, base_env):
        """Regression: the day=28 idiom returned Mar 1 here, not Feb 1."""
        mod = _load_quota_monitor(base_env)
        ttl = mod._month_end_ttl(datetime(2027, 1, 15, tzinfo=timezone.utc))

        assert ttl == int(datetime(2027, 2, 1, tzinfo=timezone.utc).timestamp())

    def test_expiry_is_stable_across_run_times(self, base_env):
        """Same month, different invocation times -> identical ttl."""
        mod = _load_quota_monitor(base_env)
        ttls = {
            mod._month_end_ttl(datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc))
            for day, hour, minute in ((1, 0, 0), (10, 20, 34), (30, 23, 59))
        }

        assert ttls == {int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())}

    def test_is_epoch_seconds(self, base_env):
        """Milliseconds would park the expiry ~50,000 years out and never fire."""
        mod = _load_quota_monitor(base_env)
        ttl = mod._month_end_ttl(datetime(2026, 9, 10, tzinfo=timezone.utc))

        assert isinstance(ttl, int)
        assert 1_000_000_000 < ttl < 4_000_000_000

    def test_written_row_carries_the_computed_ttl(self, enforce_env):
        """The value reaching DynamoDB is the helper's, not a separate calculation."""
        mod = _load_quota_monitor(enforce_env)
        mod.quota_table = MagicMock()
        mod.write_usage_absolute({"a@b.com": _usage(monthly_cost=10.0, total=500)}, shadow=False)

        kwargs = _user_updates(mod.quota_table)[0]
        written = kwargs["ExpressionAttributeValues"][":ttl"]
        expected = mod._month_end_ttl(datetime.now(timezone.utc))

        assert written == expected
        landed = datetime.fromtimestamp(written, tz=timezone.utc)
        assert (landed.day, landed.hour, landed.minute, landed.second) == (1, 0, 0, 0)


class TestShadowMode:
    def test_shadow_writes_only_liveness_attributes(self, base_env):
        """Counters stay frozen, but last_updated keeps bypass detection working."""
        mod = _load_quota_monitor(base_env)
        mod.quota_table = MagicMock()
        mod.write_usage_absolute({"a@b.com": _usage(monthly_cost=99.0, total=1)}, shadow=True)
        kwargs = _user_updates(mod.quota_table)[0]
        expr = kwargs["UpdateExpression"]
        assert "last_updated" in expr
        assert "email" in expr
        for frozen in ("total_tokens", "estimated_cost", "daily_cost_usd", "cost_source"):
            assert frozen not in expr, f"{frozen} must not be written in shadow mode"

    def test_handler_defaults_to_shadow(self, base_env):
        env = dict(base_env)
        env.pop("QUOTA_WRITE_MODE", None)
        os.environ.pop("QUOTA_WRITE_MODE", None)
        mod = _load_quota_monitor(env)
        assert mod.QUOTA_WRITE_MODE == "shadow"

    def test_shadow_reports_mode_in_response(self, base_env):
        mod = _load_quota_monitor(base_env)
        _patch_monitor(mod, scan_item={"email": "a@b.com", "total_tokens": 1, "daily_date": _today()})
        result = mod.lambda_handler({}, None)
        assert '"write_mode": "shadow"' in result["body"]


class TestSafetyFloor:
    """With absolute SET, a bad query would zero out real accounting."""

    def test_empty_result_is_refused(self, enforce_env):
        mod = _load_quota_monitor(enforce_env)
        assert mod._write_is_safe(0, 316) is False

    def test_truncated_result_is_refused(self, enforce_env):
        mod = _load_quota_monitor(enforce_env)
        assert mod._write_is_safe(10, 316) is False

    def test_plausible_result_is_allowed(self, enforce_env):
        mod = _load_quota_monitor(enforce_env)
        assert mod._write_is_safe(413, 316) is True

    def test_first_ever_run_is_allowed(self, enforce_env):
        """No existing rows means no baseline to compare against."""
        mod = _load_quota_monitor(enforce_env)
        assert mod._write_is_safe(413, 0) is True

    def test_handler_does_not_write_when_floor_trips(self, enforce_env):
        mod = _load_quota_monitor(enforce_env)
        sns = _patch_monitor(
            mod,
            scan_item={"email": "a@b.com", "total_tokens": 5_000_000, "daily_date": _today()},
            db_usage={},  # zero identities -> floor trips
        )
        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 500
        assert _user_updates(mod.quota_table) == []
        assert sns.publish.called, "operators must be told the write was refused"

    def test_db_failure_aborts_writes_but_still_checks_thresholds(self, enforce_env):
        """The cost source is no longer optional, but alerting must not stop."""
        mod = _load_quota_monitor(enforce_env)
        _patch_monitor(
            mod,
            scan_item={
                "email": "a@b.com",
                "total_tokens": 3_355_533,
                "daily_tokens": 3_355_533,
                "daily_date": _today(),
            },
        )
        mod.fetch_usage_from_db = MagicMock(side_effect=RuntimeError("connection refused"))

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 500
        assert _user_updates(mod.quota_table) == []
        assert '"total_users": 1' in result["body"], "thresholds must still be evaluated"

    def test_missing_db_config_is_an_error_not_a_silent_skip(self, base_env):
        env = dict(base_env)
        env["TELEMETRY_DB_SECRET_ARN"] = ""
        mod = _load_quota_monitor(env)
        _patch_monitor(mod, scan_item={"email": "a@b.com", "total_tokens": 1, "daily_date": _today()})
        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 500


class TestEnforceModeMerge:
    def test_fresh_db_values_drive_alerts(self, enforce_env):
        """Alerting on same-run values removes the old read-after-write race."""
        mod = _load_quota_monitor(enforce_env)
        sns = _patch_monitor(
            mod,
            scan_item={
                "email": "a@b.com",
                "total_tokens": 100,  # stale DynamoDB value
                "daily_tokens": 0,
                "daily_date": _today(),
            },
            db_usage={"a@b.com": _usage(total=5_000_000, daily=3_000_000, monthly_cost=50.0)},
        )
        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        # daily limit is 2,000,000 and the DB says 3,000,000 today
        assert _daily_alert_published(sns), "must alert on the DB-derived daily value"


class TestSslMode:
    def test_disable_yields_no_ssl_context(self, base_env):
        """The server currently has ssl=off, so plaintext is the working default."""
        env = dict(base_env)
        env["TELEMETRY_DB_SSL_MODE"] = "disable"
        mod = _load_quota_monitor(env)
        assert mod._ssl_context() is None

    def test_require_builds_an_unverified_context(self, base_env):
        env = dict(base_env)
        env["TELEMETRY_DB_SSL_MODE"] = "require"
        mod = _load_quota_monitor(env)
        ctx = mod._ssl_context()
        assert ctx is not None
        assert ctx.check_hostname is False

    def test_verify_full_requires_a_ca(self, base_env):
        env = dict(base_env)
        env["TELEMETRY_DB_SSL_MODE"] = "verify-full"
        env["TELEMETRY_DB_CA_PEM"] = ""
        mod = _load_quota_monitor(env)
        with pytest.raises(RuntimeError, match="TELEMETRY_DB_CA_PEM"):
            mod._ssl_context()


class TestNoPromqlSurfaceRemains:
    def test_promql_and_pricing_paths_are_gone(self, base_env):
        """Cost is computed in the database now — there is exactly one formula."""
        mod = _load_quota_monitor(base_env)
        for gone in (
            "_promql_query",
            "fetch_usage_from_promql",
            "update_quota_metrics",
            "AGGREGATION_WINDOW",
            "TOKEN_TYPE_TO_RATE_KEY",
            "PROMQL_ENDPOINT",
            "METRICS_REGION",
        ):
            assert not hasattr(mod, gone), f"{gone} should have been removed"

    def test_driver_import_is_lazy(self, base_env):
        """A module-scope pg8000 import would break every test in this file."""
        source = LAMBDA_PATH.read_text()
        module_level = [
            line for line in source.splitlines() if line.startswith("import pg8000") or line.startswith("from pg8000")
        ]
        assert module_level == [], "pg8000 must be imported inside _db_connect"


class TestAlertMessageAttributes:
    """The SNS routing contract consumed by quota_slack_notifier.

    The Message body is free text for human email subscribers, so the machine
    readable copy of each alert rides along as MessageAttributes. Two properties
    matter and neither is visible in a normal deploy:

    - `alert_type` must be present on every per-user alert. The Slack
      subscription's FilterPolicy names it, and an SNS FilterPolicy naming an
      attribute the message does NOT carry is a NON-match — dropping the
      attribute here would silence every DM with zero errors anywhere.
    - Operator alerts must keep publishing NO attributes at all. That missing
      attribute non-match is the only thing stopping end users from being DM'd
      about a TimescaleDB safety-floor trip.
    """

    COST_ALERT = {
        "user": "Cameron.Johnson@generac.com",
        "alert_type": "monthly_cost",
        "alert_level": "warning",
        "current_usage": 85.0,
        "limit": 100.0,
        "percentage": 85.0,
        "policy_info": "default:default",
        "enforcement_mode": "alert",
    }

    def _publish(self, base_env, alert):
        mod = _load_quota_monitor(base_env)
        mod.sns_client = MagicMock()
        mod.send_alerts([alert])
        assert mod.sns_client.publish.call_count == 1, "alert was swallowed"
        return mod, mod.sns_client.publish.call_args.kwargs

    def test_all_five_attributes_present_and_non_empty(self, base_env):
        _, kwargs = self._publish(base_env, dict(self.COST_ALERT))
        attrs = kwargs["MessageAttributes"]
        assert set(attrs) == {
            "alert_kind",
            "alert_type",
            "alert_level",
            "user_email",
            "alert_payload",
        }
        for name, spec in attrs.items():
            # SNS rejects an empty or non-string StringValue outright.
            assert spec["DataType"] == "String", name
            assert isinstance(spec["StringValue"], str) and spec["StringValue"], name

    def test_routing_attributes_carry_the_filterable_values(self, base_env):
        _, kwargs = self._publish(base_env, dict(self.COST_ALERT))
        attrs = kwargs["MessageAttributes"]
        assert attrs["alert_kind"]["StringValue"] == "user_quota"
        assert attrs["alert_type"]["StringValue"] == "monthly_cost"
        assert attrs["alert_level"]["StringValue"] == "warning"
        assert attrs["user_email"]["StringValue"] == "Cameron.Johnson@generac.com"

    def test_user_email_keeps_its_original_casing(self, base_env):
        """pk uses the raw OIDC claim casing; the notifier lowercases on compare."""
        _, kwargs = self._publish(base_env, dict(self.COST_ALERT))
        assert "Cameron.Johnson" in kwargs["MessageAttributes"]["user_email"]["StringValue"]

    def test_payload_round_trips(self, base_env):
        alert = dict(self.COST_ALERT, month="September 2026", days_remaining=20)
        _, kwargs = self._publish(base_env, alert)
        assert json.loads(kwargs["MessageAttributes"]["alert_payload"]["StringValue"]) == alert

    def test_decimal_usage_does_not_lose_the_alert(self, base_env):
        """DynamoDB hands back Decimal; a raw json.dumps would raise in-loop."""
        alert = dict(self.COST_ALERT, current_usage=Decimal("85.5"), limit=Decimal("100"))
        _, kwargs = self._publish(base_env, alert)
        payload = json.loads(kwargs["MessageAttributes"]["alert_payload"]["StringValue"])
        assert payload["current_usage"] == "85.5"

    @pytest.mark.parametrize("alert_type", ["monthly", "daily"])
    def test_token_alerts_stay_outside_the_slack_filter_set(self, base_env, alert_type):
        """Token quota alerts publish attributes too, but a value the
        FilterPolicy does not list — so they never invoke the notifier."""
        alert = dict(self.COST_ALERT, alert_type=alert_type, current_usage=1_000_000, limit=2_000_000)
        _, kwargs = self._publish(base_env, alert)
        value = kwargs["MessageAttributes"]["alert_type"]["StringValue"]
        assert value == alert_type
        assert value not in ("monthly_cost", "daily_cost")

    def test_subject_and_message_are_byte_identical_to_before(self, base_env):
        """Email subscribers predate the attributes; the body is the contract."""
        _, kwargs = self._publish(base_env, dict(self.COST_ALERT))
        assert kwargs["Subject"] == "Claude Code WARNING - Monthly Spend Budget - 85%"
        assert kwargs["Message"] == (
            "USER: Cameron.Johnson@generac.com\n"
            "ALERT: Monthly Spend Budget - WARNING\n"
            "Usage: $85.00 / $100.00 (85.0%)\n"
            "Policy: default:default\n"
            "Enforcement: alert"
        )

    def test_operational_alerts_publish_no_attributes(self, base_env):
        """The missing-attribute non-match is what excludes operator alerts."""
        mod = _load_quota_monitor(base_env)
        mod.sns_client = MagicMock()
        mod._publish_operational_alert("Safety floor tripped", "details")
        kwargs = mod.sns_client.publish.call_args.kwargs
        assert "MessageAttributes" not in kwargs


class TestAlertDedupKey:
    """Pin the dedup key against the pagination drift that re-alerted daily users.

    `get_sent_alerts` built the key inline in two places: the first page tested
    `atype.startswith("daily")` and the pagination loop tested `atype == "daily"`.
    So a `daily_cost` row on the second page was keyed WITHOUT its date, never
    matched the dated key `check_limits_and_generate_alerts` looks for, and the
    user was re-alerted on every 15-minute run for the rest of the day.

    This is not hypothetical at scale: the ALERTS query is a single 1MB page, so
    it spills once daily budgets are enabled broadly (users x 3 levels x days).
    """

    ALERT_TYPES_WITH_DATE = ("daily", "daily_cost")
    ALERT_TYPES_NO_DATE = ("monthly", "monthly_cost")

    def _paged(self, mod, pages):
        """Wire quota_table.query to return the given pages in order."""
        responses = []
        for i, items in enumerate(pages):
            resp = {"Items": items}
            if i < len(pages) - 1:
                resp["LastEvaluatedKey"] = {"pk": "ALERTS", "sk": items[-1]["sk"]}
            responses.append(resp)
        mod.quota_table = MagicMock()
        mod.quota_table.query.side_effect = responses

    @staticmethod
    def _sk(email, atype, level, date=None):
        """Build a sort key exactly as record_sent_alert does: date LAST."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        tail = f"#{date}" if date else ""
        return f"{month}#ALERT#{email}#{atype}#{level}{tail}"

    @pytest.mark.parametrize("alert_type", ALERT_TYPES_WITH_DATE)
    def test_dated_types_keep_the_date_on_every_page(self, base_env, alert_type):
        """THE regression. Page 2 must key daily rows identically to page 1."""
        mod = _load_quota_monitor(base_env)
        day = _today()
        page1 = [self._sk("a@x.com", alert_type, "warning", day)]
        page2 = [self._sk("b@x.com", alert_type, "warning", day)]
        self._paged(mod, [[{"sk": s} for s in page1], [{"sk": s} for s in page2]])

        sent = mod.get_sent_alerts("September 2026")

        # Both users must be deduped, not just the one that landed on page 1.
        assert mod.alert_dedup_key("a@x.com", alert_type, "warning", day) in sent
        assert mod.alert_dedup_key("b@x.com", alert_type, "warning", day) in sent

    @pytest.mark.parametrize("alert_type", ALERT_TYPES_WITH_DATE)
    def test_a_second_page_daily_alert_is_not_regenerated(self, base_env, alert_type):
        """End to end: the generator must not re-emit an already-sent daily alert.

        This is the user-visible symptom — a duplicate DM every 15 minutes — and
        it fails with the old `atype == "daily"` pagination test for daily_cost.
        """
        mod = _load_quota_monitor(base_env)
        day = _today()
        email = "b@x.com"
        # The row sits on page 2, which is where the drift lived.
        self._paged(
            mod,
            [
                [{"sk": self._sk("a@x.com", alert_type, "warning", day)}],
                [{"sk": self._sk(email, alert_type, "warning", day)}],
            ],
        )
        sent = mod.get_sent_alerts("September 2026")

        over_80_not_90 = {"daily": 1_700_000, "daily_cost": 0}
        policy = {
            "policy_type": "default",
            "identifier": "default",
            "enforcement_mode": "alert",
            "monthly_token_limit": 0,
            "warning_threshold_80": 0,
            "warning_threshold_90": 0,
            "daily_token_limit": 2_000_000 if alert_type == "daily" else 0,
            "monthly_cost_limit": 0,
            "daily_cost_limit": 0 if alert_type == "daily" else 100.0,
        }
        alerts = mod.check_limits_and_generate_alerts(
            email=email,
            total_tokens=0,
            daily_tokens=over_80_not_90["daily"] if alert_type == "daily" else 0,
            policy=policy,
            month_name="September 2026",
            current_date=day,
            days_remaining=19,
            days_in_month=30,
            sent_alerts=sent,
            monthly_cost=0.0,
            daily_cost=85.0 if alert_type == "daily_cost" else 0.0,
        )
        assert [a for a in alerts if a["alert_type"] == alert_type] == [], (
            f"{alert_type} was re-alerted despite an existing dedup row on page 2"
        )

    @pytest.mark.parametrize("alert_type", ALERT_TYPES_NO_DATE)
    def test_monthly_types_have_no_date_segment(self, base_env, alert_type):
        """Monthly keys must stay undated — the query prefix already pins the month."""
        mod = _load_quota_monitor(base_env)
        key = mod.alert_dedup_key("a@x.com", alert_type, "exceeded")
        assert key == f"a@x.com#{alert_type}#exceeded"
        assert _today() not in key

    def test_builder_and_parser_round_trip(self, base_env):
        """dedup_key_from_sk must be the exact inverse of record_sent_alert's sk."""
        mod = _load_quota_monitor(base_env)
        day = _today()
        for atype, date in (
            ("monthly", None),
            ("monthly_cost", None),
            ("daily", day),
            ("daily_cost", day),
        ):
            sk = self._sk("a@x.com", atype, "critical", date)
            assert mod.dedup_key_from_sk(sk) == mod.alert_dedup_key("a@x.com", atype, "critical", date), (
                f"{atype} did not round-trip"
            )

    def test_the_two_shapes_are_not_interchangeable(self, base_env):
        """Guard the premise: if these ever collide the whole test class is vacuous."""
        mod = _load_quota_monitor(base_env)
        dated = mod.alert_dedup_key("a@x.com", "daily_cost", "warning", _today())
        undated = mod.alert_dedup_key("a@x.com", "daily_cost", "warning")
        assert dated != undated
        assert "None" in undated  # a dated type built without a date is a bug marker

    def test_undated_daily_row_is_skipped_not_guessed(self, base_env):
        """A daily row missing its date yields no key rather than an unmatchable one."""
        mod = _load_quota_monitor(base_env)
        assert mod.dedup_key_from_sk(self._sk("a@x.com", "daily_cost", "warning")) is None

    def test_malformed_sort_keys_are_ignored(self, base_env):
        """A short sk must not raise inside the monitor's main loop."""
        mod = _load_quota_monitor(base_env)
        for sk in ("", "2026-09", "2026-09#ALERT", "2026-09#ALERT#a@x.com"):
            assert mod.dedup_key_from_sk(sk) is None

    def test_every_page_is_queried_with_the_start_key(self, base_env):
        """Pagination must advance; a dropped ExclusiveStartKey loops forever."""
        mod = _load_quota_monitor(base_env)
        day = _today()
        self._paged(
            mod,
            [
                [{"sk": self._sk("a@x.com", "daily_cost", "warning", day)}],
                [{"sk": self._sk("b@x.com", "daily_cost", "warning", day)}],
                [{"sk": self._sk("c@x.com", "daily_cost", "warning", day)}],
            ],
        )
        sent = mod.get_sent_alerts("September 2026")
        assert len(sent) == 3
        calls = mod.quota_table.query.call_args_list
        assert len(calls) == 3
        assert "ExclusiveStartKey" not in calls[0].kwargs
        assert calls[1].kwargs["ExclusiveStartKey"]["sk"].endswith(day)
