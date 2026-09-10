# ABOUTME: Tests for 'ccwb quota set-user --expires-month-end' single-month policy overrides
# ABOUTME: Covers month-boundary arithmetic, epoch-seconds units, and which policy types may be stamped

"""Tests for temporary (single-month) user quota policy overrides.

A budget raise granted for one month should not survive into the next. The
override is expressed as a DynamoDB TTL attribute on the ``POLICY#user#<email>``
item in ``QuotaPolicies``, which is opt-in per item: DynamoDB only deletes items
that actually carry a numeric ``ttl``, so default and group policies are never
at risk.

Two things here are easy to get wrong and are therefore pinned:

* **Units.** DynamoDB TTL expects epoch *seconds*. A millisecond value is not
  rejected, it just parks the expiry ~50,000 years out, so the item never dies
  and the bug is invisible until someone audits the table.
* **Month arithmetic.** The obvious ``now.replace(day=28) + timedelta(days=32)``
  idiom skips a month for January in non-leap years (Jan 28 + 32d = Mar 1), which
  would double the intended lifetime. Anchoring on ``day=1`` is what makes it
  correct for all twelve months.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from cleo.testers.application_tester import ApplicationTester

from claude_code_with_bedrock.cli import create_application
from claude_code_with_bedrock.cli.commands.quota import _next_month_start_ttl, _write_policy_ttl
from claude_code_with_bedrock.models import PolicyType
from tests.cfn_yaml import INFRA_DIR, load_resolved

QUOTA_TEMPLATE = INFRA_DIR / "quota-monitoring.yaml"


def _ttl_update_calls(mock_manager):
    """The update_item calls that touched the ttl attribute, as kwargs dicts."""
    return [
        call.kwargs
        for call in mock_manager.table.update_item.call_args_list
        if "#ttl" in str(call.kwargs.get("UpdateExpression", ""))
    ]


def _mock_profile_config(mock_config_cls):
    """Wire Config.load() to hand back a profile pointing at QuotaPolicies."""
    mock_config = MagicMock()
    mock_profile = MagicMock()
    mock_profile.aws_region = "us-east-1"
    mock_profile.quota_policies_table = "QuotaPolicies"
    mock_config.active_profile = "prod"
    mock_config.get_profile.return_value = mock_profile
    mock_config_cls.load.return_value = mock_config
    return mock_profile


def _mock_manager(mock_get_manager, *, exists: bool = False):
    """Wire _get_quota_manager() to a manager whose policy write succeeds."""
    from claude_code_with_bedrock.quota_policies import PolicyAlreadyExistsError

    mock_manager = MagicMock()
    mock_policy = MagicMock()
    mock_policy.monthly_token_limit = 0
    mock_policy.daily_token_limit = None
    mock_policy.enforcement_mode = MagicMock(value="alert")
    mock_policy.daily_enforcement_mode = MagicMock(value="alert")

    if exists:
        mock_manager.create_policy.side_effect = PolicyAlreadyExistsError("exists")
        mock_manager.update_policy.return_value = mock_policy
    else:
        mock_manager.create_policy.return_value = mock_policy

    mock_manager._make_pk.return_value = "POLICY#user#bill.li@generac.com"
    mock_get_manager.return_value = mock_manager
    return mock_manager


class TestNextMonthStartTtl:
    """The expiry must land on the first instant of the following month, in seconds."""

    @pytest.mark.parametrize(
        "now,expected",
        [
            # The reported case: a September override expires Oct 1.
            (datetime(2026, 9, 10, 14, 30, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)),
            # January in a NON-leap year. The day=28 idiom yields Mar 1 here.
            (datetime(2027, 1, 15, tzinfo=timezone.utc), datetime(2027, 2, 1, tzinfo=timezone.utc)),
            # January in a leap year.
            (datetime(2028, 1, 15, tzinfo=timezone.utc), datetime(2028, 2, 1, tzinfo=timezone.utc)),
            # February, both leap and non-leap.
            (datetime(2027, 2, 28, tzinfo=timezone.utc), datetime(2027, 3, 1, tzinfo=timezone.utc)),
            (datetime(2028, 2, 29, tzinfo=timezone.utc), datetime(2028, 3, 1, tzinfo=timezone.utc)),
            # Year rollover from the last instant of December.
            (datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)),
            # First instant of a month still points at the NEXT month, not this one.
            (datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)),
            # 30-day month.
            (datetime(2026, 4, 30, tzinfo=timezone.utc), datetime(2026, 5, 1, tzinfo=timezone.utc)),
        ],
    )
    def test_lands_on_first_of_next_month(self, now, expected):
        assert _next_month_start_ttl(now) == int(expected.timestamp())

    @pytest.mark.parametrize("year", [2027, 2028])  # non-leap, leap
    @pytest.mark.parametrize("month", range(1, 13))
    def test_never_skips_a_month(self, year, month):
        """Every month must advance by exactly one, in every year shape."""
        ttl = _next_month_start_ttl(datetime(year, month, 15, tzinfo=timezone.utc))
        landed = datetime.fromtimestamp(ttl, tz=timezone.utc)

        expected_month = 1 if month == 12 else month + 1
        expected_year = year + 1 if month == 12 else year
        assert (landed.year, landed.month, landed.day) == (expected_year, expected_month, 1)
        assert (landed.hour, landed.minute, landed.second) == (0, 0, 0)

    def test_is_epoch_seconds_not_milliseconds(self):
        """Milliseconds would push the expiry ~50,000 years out and never fire."""
        ttl = _next_month_start_ttl(datetime(2026, 9, 10, tzinfo=timezone.utc))

        assert isinstance(ttl, int)
        assert not isinstance(ttl, bool)
        # Seconds-since-epoch for any near-future date sits in 10 digits.
        assert 1_000_000_000 < ttl < 4_000_000_000

    def test_naive_input_is_treated_as_utc(self):
        """A naive datetime must not be read as local time (CI runs in many zones)."""
        naive = datetime(2026, 9, 10, 14, 30)
        aware = datetime(2026, 9, 10, 14, 30, tzinfo=timezone.utc)

        assert _next_month_start_ttl(naive) == _next_month_start_ttl(aware)

    def test_non_utc_input_normalized(self):
        """A late-month timestamp in a positive offset must not roll into the wrong month."""
        from datetime import timedelta as _td

        tokyo = timezone(_td(hours=9))
        # 2026-10-01 08:00 +09:00 is 2026-09-30 23:00 UTC -- still September.
        ttl = _next_month_start_ttl(datetime(2026, 10, 1, 8, 0, tzinfo=tokyo))

        assert ttl == int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())


class TestWritePolicyTtl:
    """Only user policies may be stamped; the flag is authoritative each run."""

    def test_stamps_user_policy(self):
        manager = MagicMock()
        manager._make_pk.return_value = "POLICY#user#a@b.com"
        now = datetime(2026, 9, 10, tzinfo=timezone.utc)

        ttl = _write_policy_ttl(manager, PolicyType.USER, "a@b.com", True, now=now)

        assert ttl == int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
        kwargs = manager.table.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == "SET #ttl = :ttl"
        assert kwargs["ExpressionAttributeNames"] == {"#ttl": "ttl"}
        assert kwargs["ExpressionAttributeValues"] == {":ttl": ttl}

    def test_clears_when_flag_absent(self):
        """Absence must REMOVE a prior expiry, not silently inherit it."""
        manager = MagicMock()
        manager._make_pk.return_value = "POLICY#user#a@b.com"

        ttl = _write_policy_ttl(manager, PolicyType.USER, "a@b.com", False)

        assert ttl is None
        kwargs = manager.table.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == "REMOVE #ttl"
        assert "ExpressionAttributeValues" not in kwargs

    @pytest.mark.parametrize("policy_type", [PolicyType.GROUP, PolicyType.DEFAULT])
    def test_refuses_to_stamp_shared_policies(self, policy_type):
        """A ttl on a group/default policy would delete enforcement for everyone under it."""
        manager = MagicMock()

        with pytest.raises(ValueError, match="only valid for user policies"):
            _write_policy_ttl(manager, policy_type, "engineering", True)

        manager.table.update_item.assert_not_called()


class TestSetUserExpiresMonthEnd:
    """End-to-end through the CLI: the flag reaches DynamoDB, absence clears."""

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_flag_stamps_ttl_on_create(self, mock_config_cls, mock_get_manager, capsys):
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager)

        tester = ApplicationTester(create_application())
        tester.execute(
            "quota set-user bill.li@generac.com --monthly-limit 0 --budget 2500 --expires-month-end --profile prod"
        )

        assert tester.status_code == 0
        calls = _ttl_update_calls(manager)
        assert len(calls) == 1
        assert calls[0]["UpdateExpression"] == "SET #ttl = :ttl"
        ttl = calls[0]["ExpressionAttributeValues"][":ttl"]
        assert isinstance(ttl, int) and 1_000_000_000 < ttl < 4_000_000_000
        # Rich writes to stdout, not cleo's buffered IO.
        assert "Expires:" in capsys.readouterr().out

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_no_flag_writes_no_ttl_on_create(self, mock_config_cls, mock_get_manager, capsys):
        """A permanent policy must never carry the attribute at all."""
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager)

        tester = ApplicationTester(create_application())
        tester.execute("quota set-user bill.li@generac.com --monthly-limit 0 --budget 2500 --profile prod")

        assert tester.status_code == 0
        assert _ttl_update_calls(manager) == []
        assert "Expires:" not in capsys.readouterr().out

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_rerun_without_flag_clears_existing_ttl(self, mock_config_cls, mock_get_manager):
        """Re-running to make a temporary override permanent must drop the expiry.

        update_policy issues a targeted SET, so an inherited ttl would survive and
        delete the policy the admin just made permanent.
        """
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager, exists=True)

        tester = ApplicationTester(create_application())
        tester.execute("quota set-user bill.li@generac.com --monthly-limit 0 --budget 2500 --profile prod")

        assert tester.status_code == 0
        calls = _ttl_update_calls(manager)
        assert len(calls) == 1
        assert calls[0]["UpdateExpression"] == "REMOVE #ttl"

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_flag_restamps_ttl_on_update(self, mock_config_cls, mock_get_manager):
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager, exists=True)

        tester = ApplicationTester(create_application())
        tester.execute(
            "quota set-user bill.li@generac.com --monthly-limit 0 --budget 2500 --expires-month-end --profile prod"
        )

        assert tester.status_code == 0
        calls = _ttl_update_calls(manager)
        assert len(calls) == 1
        assert calls[0]["UpdateExpression"] == "SET #ttl = :ttl"

    @pytest.mark.parametrize(
        "command",
        [
            "quota set-group engineering --monthly-limit 1B --profile prod",
            "quota set-default --monthly-limit 1B --profile prod",
        ],
    )
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_shared_policies_never_get_a_ttl(self, mock_config_cls, mock_get_manager, command):
        """set-group and set-default must not expose or write an expiry."""
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager)

        tester = ApplicationTester(create_application())
        tester.execute(command)

        assert _ttl_update_calls(manager) == []


class TestQuotaSetAliasForwarding:
    """`quota set` must not diverge from `quota set-user`."""

    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_alias_forwards_flag_to_set_user(self, mock_config_cls, mock_get_manager):
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager)

        tester = ApplicationTester(create_application())
        tester.execute("quota set bill.li@generac.com --monthly-limit 0 --budget 2500 --expires-month-end")

        assert tester.status_code == 0
        calls = _ttl_update_calls(manager)
        assert len(calls) == 1
        assert calls[0]["UpdateExpression"] == "SET #ttl = :ttl"

    @pytest.mark.parametrize("scope", ["--group engineering", "--default"])
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_alias_rejects_flag_for_shared_scopes(self, mock_config_cls, mock_get_manager, scope):
        """Refusing beats silently dropping: the admin asked for a temporary policy."""
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager)

        tester = ApplicationTester(create_application())
        tester.execute(f"quota set {scope} --monthly-limit 1B --expires-month-end")

        assert tester.status_code == 1
        assert "only valid for user policies" in tester.io.fetch_output()
        manager.create_policy.assert_not_called()


class TestQuotaPoliciesTableTtl:
    """The attribute is inert unless the table opts in."""

    def test_policies_table_enables_ttl_on_ttl_attribute(self):
        template = load_resolved(QUOTA_TEMPLATE)
        table = template["Resources"]["QuotaPolicies"]["Properties"]

        assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}

    def test_metrics_table_ttl_unchanged(self):
        """Regression: the usage table's own month-end TTL must survive this change."""
        template = load_resolved(QUOTA_TEMPLATE)
        table = template["Resources"]["UserQuotaMetrics"]["Properties"]

        assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}

    def test_ttl_is_not_a_key_or_index_attribute(self):
        """A TTL attribute must stay off the key schema -- expiry would break lookups."""
        template = load_resolved(QUOTA_TEMPLATE)
        table = template["Resources"]["QuotaPolicies"]["Properties"]

        declared = {a["AttributeName"] for a in table["AttributeDefinitions"]}
        assert "ttl" not in declared
