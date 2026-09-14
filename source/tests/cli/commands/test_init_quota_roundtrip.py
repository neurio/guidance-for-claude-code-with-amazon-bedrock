# ABOUTME: Regression test ensuring re-running init preserves all saved quota fields
# ABOUTME: Guards _check_existing_deployment against round-trip drift with _save_configuration

"""Regression test: re-running `ccwb init` must preserve saved quota settings.

`_check_existing_deployment` rebuilds the in-memory config dict from a saved
Profile when init is re-run. It must mirror every quota field that
`_save_configuration` persists — otherwise omitted fields (e.g.
quota_check_interval) silently reset to their prompt defaults on a re-run.
"""

import sys
from pathlib import Path
from unittest.mock import patch

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from claude_code_with_bedrock.cli.commands.init import InitCommand
from claude_code_with_bedrock.config import Config, Profile


def _make_profile() -> Profile:
    """A profile with non-default quota settings, as if previously saved."""
    return Profile(
        name="test",
        provider_domain="example.okta.com",
        client_id="0oa1234567890",
        identity_pool_name="claude-code-auth",
        credential_storage="keyring",
        aws_region="us-east-1",
        quota_monitoring_enabled=True,
        monthly_token_limit=500_000_000,
        warning_threshold_80=400_000_000,
        warning_threshold_90=450_000_000,
        daily_token_limit=20_000_000,
        burst_buffer_percent=15,
        daily_enforcement_mode="block",
        monthly_enforcement_mode="alert",
        quota_check_interval=5,
        enable_bypass_detection=True,
        telemetry_db_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:telemetry-AbCdEf",
        telemetry_db_host="10.0.0.1",
        telemetry_db_port=15432,
        telemetry_db_name="claude_telemetry_test",
        telemetry_db_ssl_mode="verify-full",
        telemetry_db_ca_pem="-----BEGIN CERTIFICATE-----\nxyz\n-----END CERTIFICATE-----",
        telemetry_db_vpc_id="vpc-0abc",
        telemetry_db_subnet_ids=["subnet-0aaa", "subnet-0bbb"],
        telemetry_db_egress_cidr="10.0.0.0/20",
        quota_write_mode="enforce",
        quota_db_min_row_ratio=0.75,
        # Slack DM notifier: deploy-time only, deliberately not prompted for by
        # the wizard. Values differ from the dataclass defaults so a reset shows.
        slack_bot_token_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-token-roFJNj",
        # Second workspace. A bot cannot see users outside its own workspace,
        # so a domain living elsewhere needs its own token; both fields must
        # survive together or routing silently reverts to the primary bot.
        slack_secondary_bot_token_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-ecobee-XyZ789",
        slack_secondary_domains="ecobee.com",
    )


def _rebuild_config(profile: Profile) -> dict:
    """Run _check_existing_deployment with AWS interaction stubbed out."""
    command = InitCommand()
    fake_config = Config()
    with (
        patch.object(Config, "load", return_value=fake_config),
        patch.object(fake_config, "get_profile", return_value=profile),
        # Avoid any AWS calls; pretend the stack check could not run.
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        return command._check_existing_deployment("test")


def test_rerun_preserves_quota_check_interval():
    """quota_check_interval must survive the profile -> config rebuild."""
    rebuilt = _rebuild_config(_make_profile())

    quota = rebuilt["quota"]
    # The dict uses "check_interval"; the Profile attribute is "quota_check_interval".
    assert quota["check_interval"] == 5


def test_rerun_preserves_all_quota_fields():
    """Every quota field _save_configuration writes must be rebuilt."""
    rebuilt = _rebuild_config(_make_profile())

    quota = rebuilt["quota"]
    assert quota["enabled"] is True
    assert quota["monthly_limit"] == 500_000_000
    assert quota["warning_threshold_80"] == 400_000_000
    assert quota["warning_threshold_90"] == 450_000_000
    assert quota["daily_limit"] == 20_000_000
    assert quota["burst_buffer_percent"] == 15
    assert quota["daily_enforcement_mode"] == "block"
    assert quota["monthly_enforcement_mode"] == "alert"
    assert quota["check_interval"] == 5
    assert quota["enable_bypass_detection"] is True


def test_rerun_preserves_telemetry_db_settings():
    """The telemetry DB is the usage source; losing it silently disables counting.

    A dropped field here is worse than a cosmetic reset: without
    telemetry_db_secret_arn the monitor has no usage source at all, and a reset
    write_mode would flip a deliberate 'enforce' cutover back to 'shadow'.
    """
    rebuilt = _rebuild_config(_make_profile())

    quota = rebuilt["quota"]
    assert quota["telemetry_db_secret_arn"].endswith(":secret:telemetry-AbCdEf")
    assert quota["telemetry_db_host"] == "10.0.0.1"
    assert quota["telemetry_db_port"] == 15432
    assert quota["telemetry_db_name"] == "claude_telemetry_test"
    assert quota["telemetry_db_ssl_mode"] == "verify-full"
    assert "BEGIN CERTIFICATE" in quota["telemetry_db_ca_pem"]
    assert quota["telemetry_db_vpc_id"] == "vpc-0abc"
    assert quota["telemetry_db_subnet_ids"] == ["subnet-0aaa", "subnet-0bbb"]
    assert quota["telemetry_db_egress_cidr"] == "10.0.0.0/20"
    # The Profile attribute is quota_write_mode; the config dict key is write_mode.
    assert quota["write_mode"] == "enforce"
    assert quota["db_min_row_ratio"] == 0.75


TELEMETRY_DB_ATTRS = (
    "telemetry_db_secret_arn",
    "telemetry_db_host",
    "telemetry_db_port",
    "telemetry_db_name",
    "telemetry_db_ssl_mode",
    "telemetry_db_ca_pem",
    "telemetry_db_vpc_id",
    "telemetry_db_subnet_ids",
    "telemetry_db_egress_cidr",
    "quota_write_mode",
    "quota_db_min_row_ratio",
)


def test_full_save_then_rebuild_round_trip():
    """Pin _save_configuration and _check_existing_deployment against each other.

    A field added to one map but not the other passes the single-direction tests
    above and still loses data on a re-run, so drive the real save path here.
    """
    original = _make_profile()
    rebuilt = _rebuild_config(original)

    # Feed the rebuilt dict back through the save path and inspect the Profile it
    # produces, without touching the user's real config file.
    saved: dict = {}
    fake_config = Config()

    def _capture(profile):
        saved["profile"] = profile

    with (
        patch.object(Config, "load", return_value=fake_config),
        patch.object(fake_config, "get_profile", return_value=None),
        patch.object(fake_config, "add_profile", side_effect=_capture),
        patch.object(fake_config, "set_active_profile"),
        patch.object(fake_config, "save"),
    ):
        InitCommand()._save_configuration(
            {
                "provider_domain": original.provider_domain,
                "client_id": original.client_id,
                "credential_storage": original.credential_storage,
                "aws": {
                    "region": original.aws_region,
                    "identity_pool_name": original.identity_pool_name,
                    "stacks": {},
                    "allowed_bedrock_regions": ["us-east-1"],
                },
                "monitoring": {"enabled": True},
                "quota": rebuilt["quota"],
            },
            original.name,
        )

    result = saved["profile"]
    for attr in TELEMETRY_DB_ATTRS:
        assert getattr(result, attr) == getattr(original, attr), f"{attr} was lost in the save -> rebuild round trip"


SLACK_ATTRS = (
    "slack_bot_token_secret_arn",
    "slack_secondary_bot_token_secret_arn",
    "slack_secondary_domains",
)


def test_rerun_preserves_slack_notifier_fields():
    """The wizard never prompts for the Slack fields, so it must not reset them.

    `deploy.py` passes both to CloudFormation on every `ccwb deploy quota` and
    `deploy_stack` does not use UsePreviousValue — so an empty
    slack_bot_token_secret_arn deletes the notifier Lambda and its SNS
    subscription. Adding these to `wizard_fields` without also restoring them in
    `_check_existing_deployment` would silently turn Slack DMs off on the next
    `ccwb init` re-run, which is how PRs #436 / #619 / #624 lost fields.
    """
    original = _make_profile()
    saved: dict = {}
    fake_config = Config()

    with (
        patch.object(Config, "load", return_value=fake_config),
        # The real re-run path: an existing profile is loaded and mutated.
        patch.object(fake_config, "get_profile", return_value=original),
        patch.object(fake_config, "add_profile", side_effect=lambda p: saved.update(profile=p)),
        patch.object(fake_config, "set_active_profile"),
        patch.object(fake_config, "save"),
    ):
        InitCommand()._save_configuration(
            {
                "provider_domain": original.provider_domain,
                "client_id": original.client_id,
                "credential_storage": original.credential_storage,
                "aws": {
                    "region": original.aws_region,
                    "identity_pool_name": original.identity_pool_name,
                    "stacks": {},
                    "allowed_bedrock_regions": ["us-east-1"],
                },
                "monitoring": {"enabled": True},
                "quota": {},
            },
            original.name,
        )

    result = saved["profile"]
    for attr in SLACK_ATTRS:
        assert getattr(result, attr) == getattr(original, attr), f"{attr} was reset by an init re-run"


def test_slack_fields_survive_a_profile_dict_round_trip():
    """Profile.from_dict drops keys that are not declared dataclass fields, so a
    to_dict -> from_dict cycle is what proves these are real fields."""
    original = _make_profile()
    reloaded = Profile.from_dict(original.to_dict())
    for attr in SLACK_ATTRS:
        assert getattr(reloaded, attr) == getattr(original, attr)


def test_old_profiles_without_slack_fields_still_load():
    """Backward compat: profile.json written before this feature must load."""
    data = _make_profile().to_dict()
    for attr in SLACK_ATTRS:
        data.pop(attr, None)
    reloaded = Profile.from_dict(data)
    assert reloaded.slack_bot_token_secret_arn in (None, "")
    # Either secondary half empty means single-workspace routing, which is
    # exactly what a pre-feature profile should get.
    assert reloaded.slack_secondary_bot_token_secret_arn in (None, "")
    assert reloaded.slack_secondary_domains == ""


def test_profiles_still_carrying_the_removed_allowlist_load():
    """The recipient allowlist was removed from Profile, but every profile.json
    already on disk still has the key. from_dict must drop it, not raise —
    otherwise every existing deployment breaks on the next ccwb command.
    """
    data = _make_profile().to_dict()
    data["slack_dm_allowlist"] = "someone@example.com,other@example.com"
    reloaded = Profile.from_dict(data)
    assert not hasattr(reloaded, "slack_dm_allowlist")
    # The surviving Slack config must be untouched by the stale key.
    assert reloaded.slack_secondary_domains == "ecobee.com"
