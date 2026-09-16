# ABOUTME: Tests that CoWork 3P usage counts toward per-user quota via the telemetry DB
# ABOUTME: Regression test for CoWork tokens not counting toward per-user quota

"""Tests for CoWork 3P quota counting.

quota_monitor used to run a second, CoWork-specific PromQL query against the
`ClaudeCoWork` CloudWatch namespace and merge the result. That is gone: cost and
tokens now come from `telemetry.unified_hourly_cost`, whose Bedrock branch is
built from CloudTrail invocation records. CloudTrail records every Bedrock
invocation with no notion of which client made it (`telemetry.bedrock_invocations`
has no user-agent or client column), so CoWork Desktop traffic is counted by the
same code path as the CLI — with no CoWork-specific query to maintain, and no
dependency on the CoWork MetricFilter pipeline being deployed.

Attribution still depends on the role session name carrying the user's email,
which `credential-process` provides for CoWork exactly as it does for the CLI.
"""

from tests.cfn_yaml import INFRA_DIR, REPO_ROOT, load_resolved


class TestCoWorkQuotaCounting:
    """Verify CoWork usage reaches quota via the shared telemetry path."""

    def test_quota_monitor_has_no_cowork_specific_query(self):
        """CoWork needs no special-casing once cost comes from CloudTrail."""
        lambda_path = INFRA_DIR / "lambda-functions" / "quota_monitor" / "index.py"
        content = lambda_path.read_text(encoding="utf-8")
        assert "ClaudeCoWork" not in content, "the CoWork PromQL branch was removed; CoWork is counted via CloudTrail"

    def test_quota_monitor_counts_all_bedrock_clients(self):
        """The single cost source is the client-agnostic unified view."""
        lambda_path = INFRA_DIR / "lambda-functions" / "quota_monitor" / "index.py"
        content = lambda_path.read_text(encoding="utf-8")
        assert "telemetry.unified_hourly_cost" in content
        assert "user_email" in content, "usage must be attributed per user_email"

    def test_cowork_usage_is_not_gated_on_the_cowork_dashboard_stack(self):
        """Quota counting must not require the optional cowork-dashboard stack."""
        template = load_resolved(INFRA_DIR / "quota-monitoring.yaml")
        rendered = str(template)
        assert "ClaudeCoWork" not in rendered, "quota-monitoring must not depend on the CoWork metric namespace"

    def test_cowork_dashboard_has_user_email_dimension(self):
        """CoWork metric filters must include user_email dimension for per-user attribution."""
        template = load_resolved(INFRA_DIR / "cowork-dashboard.yaml")

        resources = template.get("Resources", {})
        api_request_filters = [
            (name, res)
            for name, res in resources.items()
            if res.get("Type") == "AWS::Logs::MetricFilter"
            and "claude_code.api_request" in res.get("Properties", {}).get("FilterPattern", "")
        ]

        assert len(api_request_filters) >= 6, "Expected at least 6 api_request metric filters"

        for name, res in api_request_filters:
            transforms = res["Properties"]["MetricTransformations"]
            for t in transforms:
                dims = t.get("Dimensions", [])
                dim_keys = [d.get("Key", "") for d in dims]
                assert "user_email" in dim_keys, (
                    f"{name}: MetricFilter must have user_email dimension for quota counting"
                )

    def test_cowork_docs_mention_quota_enforcement(self):
        """COWORK_3P.md must document quota enforcement behavior."""
        docs_path = REPO_ROOT / "assets" / "docs" / "COWORK_3P.md"
        content = docs_path.read_text(encoding="utf-8")
        assert "## Quota Enforcement" in content
        assert "credential-process" in content
        assert "credential refresh" in content.lower() or "refresh cycle" in content.lower()
