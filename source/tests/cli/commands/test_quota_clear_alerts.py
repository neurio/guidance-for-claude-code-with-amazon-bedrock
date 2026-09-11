# ABOUTME: Tests for clearing a user's triggered-alert dedup rows when their quota policy changes
# ABOUTME: Covers ALERTS sort-key parsing, scoping (user/month), pagination, and CLI wiring

"""Tests for alert-history reset on `ccwb quota set-user`.

quota_monitor records every alert it sends as an ``ALERTS`` row in
``UserQuotaMetrics`` and will not re-send an alert whose row already exists:

    pk = "ALERTS"
    sk = "<YYYY-MM>#ALERT#<email>#<type>#<level>[#<date>]"

That dedup row means "this level was crossed under the budget in force at the
time". So raising a user's budget without clearing their rows leaves the *new*
budget unable to alert: the admin grants $2500, the user spends past it, and the
`monthly_cost/exceeded` row from the old $500 budget silently suppresses both the
SNS alert and the Slack DM.

Pinned here, because each one is a way to get this subtly wrong:

* **Scope.** Only the named user's rows, and only the current month's — the
  ALERTS partition holds every user's alerts, and a too-broad delete would
  un-suppress everyone else's alerts, spamming users who never changed budget.
* **Casing.** The sort key carries the OIDC ``email`` claim's casing, which need
  not match what the admin typed. A case-sensitive match finds nothing and
  reports success.
* **Pagination.** The user's rows can land on any page of the month's query.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from cleo.testers.application_tester import ApplicationTester

from claude_code_with_bedrock.cli import create_application
from claude_code_with_bedrock.cli.commands.quota import _clear_sent_alerts, _sent_alert_email

SEPTEMBER = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)


def _alert_row(sk):
    return {"pk": "ALERTS", "sk": sk}


def _mock_boto3(mock_boto3, pages):
    """Wire the module's boto3 to a table whose query() yields `pages`."""
    table = MagicMock()
    table.query.side_effect = pages
    mock_boto3.resource.return_value.Table.return_value = table
    return table


def _deleted_keys(table):
    batch = table.batch_writer.return_value.__enter__.return_value
    return [call.kwargs["Key"] for call in batch.delete_item.call_args_list]


def _query_sk_prefix(table):
    """The sort-key prefix the last query() filtered on."""
    condition = table.query.call_args.kwargs["KeyConditionExpression"]
    for sub in condition.get_expression()["values"]:
        expression = sub.get_expression()
        if expression["operator"] == "begins_with":
            return expression["values"][1]
    raise AssertionError("key condition has no begins_with on sk")


def _profile(table_name="UserQuotaMetrics"):
    profile = MagicMock()
    profile.aws_region = "us-east-1"
    profile.user_quota_metrics_table = table_name
    profile.quota_policies_table = "QuotaPolicies"
    return profile


class TestSentAlertEmail:
    """Sort-key parsing must stay the exact inverse of record_sent_alert."""

    @pytest.mark.parametrize(
        "sk,expected",
        [
            # Monthly alert: no date segment.
            ("2026-09#ALERT#bill.li@generac.com#monthly_cost#exceeded", "bill.li@generac.com"),
            # Daily alert: the date is written LAST, after the level.
            ("2026-09#ALERT#bill.li@generac.com#daily_cost#warning#2026-09-11", "bill.li@generac.com"),
            ("2026-09#ALERT#a@b.com#monthly#critical", "a@b.com"),
        ],
    )
    def test_extracts_email(self, sk, expected):
        assert _sent_alert_email(sk) == expected

    @pytest.mark.parametrize(
        "sk",
        [
            "2026-09#ALERT#a@b.com#monthly",  # truncated: no level
            "2026-09#USAGE#a@b.com#monthly#exceeded",  # not an alert row
            "USER#a@b.com",  # a usage row that shares the table
            "",
        ],
    )
    def test_rejects_non_alert_rows(self, sk):
        """A row we cannot parse must not be deleted on a guess."""
        assert _sent_alert_email(sk) is None


class TestClearSentAlerts:
    """Delete this user's alert rows for this month, and nothing else."""

    @patch("claude_code_with_bedrock.cli.commands.quota.boto3")
    def test_deletes_only_the_named_users_rows(self, mock_boto3):
        table = _mock_boto3(
            mock_boto3,
            [
                {
                    "Items": [
                        _alert_row("2026-09#ALERT#bill.li@generac.com#monthly_cost#exceeded"),
                        _alert_row("2026-09#ALERT#other@generac.com#monthly_cost#exceeded"),
                        _alert_row("2026-09#ALERT#bill.li@generac.com#daily_cost#warning#2026-09-11"),
                    ]
                }
            ],
        )

        cleared = _clear_sent_alerts(_profile(), "bill.li@generac.com", now=SEPTEMBER)

        assert cleared == 2
        assert _deleted_keys(table) == [
            {"pk": "ALERTS", "sk": "2026-09#ALERT#bill.li@generac.com#monthly_cost#exceeded"},
            {"pk": "ALERTS", "sk": "2026-09#ALERT#bill.li@generac.com#daily_cost#warning#2026-09-11"},
        ]

    @patch("claude_code_with_bedrock.cli.commands.quota.boto3")
    def test_matches_email_case_insensitively(self, mock_boto3):
        """The stored casing comes from the OIDC claim, not from the command line."""
        table = _mock_boto3(
            mock_boto3,
            [{"Items": [_alert_row("2026-09#ALERT#Sergey.Vyacheslavovich@generac.com#monthly_cost#exceeded")]}],
        )

        cleared = _clear_sent_alerts(_profile(), "sergey.vyacheslavovich@generac.com", now=SEPTEMBER)

        assert cleared == 1
        assert len(_deleted_keys(table)) == 1

    @patch("claude_code_with_bedrock.cli.commands.quota.boto3")
    def test_scopes_query_to_current_month(self, mock_boto3):
        """Older months can no longer suppress anything, and carry their own TTL."""
        table = _mock_boto3(mock_boto3, [{"Items": []}])

        _clear_sent_alerts(_profile(), "a@b.com", now=SEPTEMBER)

        assert _query_sk_prefix(table) == "2026-09#ALERT#"

    @patch("claude_code_with_bedrock.cli.commands.quota.boto3")
    def test_reads_every_page(self, mock_boto3):
        """The ALERTS partition spans all users; this user's rows can be on page 2."""
        table = _mock_boto3(
            mock_boto3,
            [
                {
                    "Items": [_alert_row("2026-09#ALERT#other@generac.com#monthly_cost#exceeded")],
                    "LastEvaluatedKey": {"pk": "ALERTS", "sk": "2026-09#ALERT#other@generac.com"},
                },
                {"Items": [_alert_row("2026-09#ALERT#bill.li@generac.com#monthly_cost#exceeded")]},
            ],
        )

        cleared = _clear_sent_alerts(_profile(), "bill.li@generac.com", now=SEPTEMBER)

        assert cleared == 1
        assert table.query.call_count == 2
        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
            "pk": "ALERTS",
            "sk": "2026-09#ALERT#other@generac.com",
        }

    @patch("claude_code_with_bedrock.cli.commands.quota.boto3")
    def test_no_rows_writes_nothing(self, mock_boto3):
        table = _mock_boto3(mock_boto3, [{"Items": []}])

        assert _clear_sent_alerts(_profile(), "a@b.com", now=SEPTEMBER) == 0
        table.batch_writer.assert_not_called()

    @patch("claude_code_with_bedrock.cli.commands.quota.boto3")
    def test_naive_now_is_utc(self, mock_boto3):
        """CI runs in many zones; a naive value must not be read as local time."""
        table = _mock_boto3(mock_boto3, [{"Items": []}, {"Items": []}])

        _clear_sent_alerts(_profile(), "a@b.com", now=datetime(2026, 9, 11, 14, 30))
        naive_prefix = _query_sk_prefix(table)
        _clear_sent_alerts(_profile(), "a@b.com", now=SEPTEMBER)

        assert naive_prefix == _query_sk_prefix(table) == "2026-09#ALERT#"

    @patch("claude_code_with_bedrock.cli.commands.quota.boto3")
    def test_uses_configured_metrics_table(self, mock_boto3):
        _mock_boto3(mock_boto3, [{"Items": []}])

        _clear_sent_alerts(_profile("prod-UserQuotaMetrics"), "a@b.com", now=SEPTEMBER)

        mock_boto3.resource.return_value.Table.assert_called_once_with("prod-UserQuotaMetrics")


def _mock_profile_config(mock_config_cls):
    """Wire Config.load() to a profile pointing at both quota tables."""
    mock_config = MagicMock()
    mock_config.active_profile = "prod"
    mock_config.get_profile.return_value = _profile()
    mock_config_cls.load.return_value = mock_config


def _mock_manager(mock_get_manager, *, exists: bool = False):
    """Wire _get_quota_manager() to a manager whose policy write succeeds."""
    from claude_code_with_bedrock.quota_policies import PolicyAlreadyExistsError

    manager = MagicMock()
    policy = MagicMock()
    policy.monthly_token_limit = 0
    policy.daily_token_limit = None
    policy.enforcement_mode = MagicMock(value="alert")
    policy.daily_enforcement_mode = MagicMock(value="alert")

    if exists:
        manager.create_policy.side_effect = PolicyAlreadyExistsError("exists")
        manager.update_policy.return_value = policy
    else:
        manager.create_policy.return_value = policy

    manager._make_pk.return_value = "POLICY#user#bill.li@generac.com"
    mock_get_manager.return_value = manager
    return manager


SET_USER = "quota set-user bill.li@generac.com --monthly-limit 0 --budget 2500 --profile prod"


class TestSetUserClearsAlerts:
    """The budget write and the alert reset ship together, on create and update."""

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_update_clears_alerts(self, mock_config_cls, mock_get_manager, mock_clear, capsys):
        """The reported case: raising a budget must let the new budget alert."""
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager, exists=True)
        mock_clear.return_value = 3

        tester = ApplicationTester(create_application())
        tester.execute(SET_USER)

        assert tester.status_code == 0
        mock_clear.assert_called_once()
        assert mock_clear.call_args.args[1] == "bill.li@generac.com"
        assert "Cleared 3 triggered alerts" in capsys.readouterr().out

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_create_clears_alerts(self, mock_config_cls, mock_get_manager, mock_clear):
        """A first user policy still inherits alerts sent against the default budget."""
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager)
        mock_clear.return_value = 1

        tester = ApplicationTester(create_application())
        tester.execute(SET_USER)

        assert tester.status_code == 0
        mock_clear.assert_called_once()

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_keep_alerts_skips_clearing(self, mock_config_cls, mock_get_manager, mock_clear, capsys):
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager, exists=True)

        tester = ApplicationTester(create_application())
        tester.execute(f"{SET_USER} --keep-alerts")

        assert tester.status_code == 0
        mock_clear.assert_not_called()
        assert "Kept alert history" in capsys.readouterr().out

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_reports_nothing_to_clear(self, mock_config_cls, mock_get_manager, mock_clear, capsys):
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager, exists=True)
        mock_clear.return_value = 0

        tester = ApplicationTester(create_application())
        tester.execute(SET_USER)

        assert tester.status_code == 0
        assert "No triggered alerts to clear" in capsys.readouterr().out

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_clear_failure_is_not_swallowed(self, mock_config_cls, mock_get_manager, mock_clear, capsys):
        """A suppressed alert is invisible, so a failed clear must not exit 0."""
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager, exists=True)
        mock_clear.side_effect = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Query")

        tester = ApplicationTester(create_application())
        tester.execute(SET_USER)

        assert tester.status_code == 1
        out = capsys.readouterr().out
        assert "Failed to clear triggered alerts" in out
        # The policy write already happened -- say so, or the admin re-does it blind.
        assert "policy was saved" in out

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_missing_metrics_table_is_not_a_failure(self, mock_config_cls, mock_get_manager, mock_clear):
        """Fine-grained policies without the metrics table have no alert history."""
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager, exists=True)
        mock_clear.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "Query"
        )

        tester = ApplicationTester(create_application())
        tester.execute(SET_USER)

        assert tester.status_code == 0

    @pytest.mark.parametrize(
        "command",
        [
            "quota set-group engineering --monthly-limit 1B --profile prod",
            "quota set-default --monthly-limit 1B --profile prod",
        ],
    )
    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_shared_policies_never_clear_alerts(self, mock_config_cls, mock_get_manager, mock_clear, command):
        """A group/default write covers many users; clearing all their alerts would spam."""
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager)

        tester = ApplicationTester(create_application())
        tester.execute(command)

        mock_clear.assert_not_called()


class TestQuotaSetAliasKeepAlerts:
    """`quota set` must not diverge from `quota set-user`."""

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_alias_clears_by_default(self, mock_config_cls, mock_get_manager, mock_clear):
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager, exists=True)
        mock_clear.return_value = 2

        tester = ApplicationTester(create_application())
        tester.execute("quota set bill.li@generac.com --monthly-limit 0 --budget 2500")

        assert tester.status_code == 0
        mock_clear.assert_called_once()

    @patch("claude_code_with_bedrock.cli.commands.quota._clear_sent_alerts")
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_alias_forwards_keep_alerts(self, mock_config_cls, mock_get_manager, mock_clear):
        _mock_profile_config(mock_config_cls)
        _mock_manager(mock_get_manager, exists=True)

        tester = ApplicationTester(create_application())
        tester.execute("quota set bill.li@generac.com --monthly-limit 0 --budget 2500 --keep-alerts")

        assert tester.status_code == 0
        mock_clear.assert_not_called()

    @pytest.mark.parametrize("scope", ["--group engineering", "--default"])
    @patch("claude_code_with_bedrock.cli.commands.quota._get_quota_manager")
    @patch("claude_code_with_bedrock.cli.commands.quota.Config")
    def test_alias_rejects_keep_alerts_for_shared_scopes(self, mock_config_cls, mock_get_manager, scope):
        """Accepting it as a no-op would imply shared writes clear alerts by default."""
        _mock_profile_config(mock_config_cls)
        manager = _mock_manager(mock_get_manager)

        tester = ApplicationTester(create_application())
        tester.execute(f"quota set {scope} --monthly-limit 1B --keep-alerts")

        assert tester.status_code == 1
        assert "only valid for user policies" in tester.io.fetch_output()
        manager.create_policy.assert_not_called()
