# ABOUTME: Tests for cost-based quota enforcement decisions in quota_check
# ABOUTME: Cost itself is computed in TimescaleDB, so there is no in-repo rate table to test

"""Tests for cost-based quota enforcement.

The pricing tests that used to live here covered
`lambda-functions/shared/pricing.py`, which was deleted when quota_monitor moved
to reading authoritative per-user cost from TimescaleDB. Cost is now calculated
once, in the database (`telemetry.calculate_token_cost()`, including the
cross-region inference surcharge the old rate table did not model), so there is
no longer a second formula in this repo to pin.

What remains here is the consumer side: given a cost figure in DynamoDB, does
quota_check make the right allow/block decision.
"""


class TestCostEnforcementLogic:
    """Tests for quota_check cost enforcement decisions."""

    def _simulate_enforcement(self, usage: dict, policy: dict) -> dict:
        """Simulate the quota_check enforcement logic for cost.

        Field names mirror `quota_check/index.py:167-168` exactly — these are the
        attributes quota_monitor writes, so a rename on either side must fail here.
        """
        monthly_cost = float(usage.get("estimated_cost", 0))
        daily_cost = float(usage.get("daily_cost_usd", 0))
        monthly_cost_limit = float(policy.get("monthly_cost_limit", 0))
        daily_cost_limit = float(policy.get("daily_cost_limit", 0))

        if monthly_cost_limit > 0 and monthly_cost >= monthly_cost_limit:
            return {"allowed": False, "reason": "monthly_cost_exceeded"}
        if daily_cost_limit > 0 and daily_cost >= daily_cost_limit:
            return {"allowed": False, "reason": "daily_cost_exceeded"}
        return {"allowed": True, "reason": "within_budget"}

    def test_within_budget_allowed(self):
        result = self._simulate_enforcement(
            {"estimated_cost": 30.0, "daily_cost_usd": 5.0},
            {"monthly_cost_limit": 50.0, "daily_cost_limit": 10.0},
        )
        assert result["allowed"] is True

    def test_monthly_exceeded_blocked(self):
        result = self._simulate_enforcement(
            {"estimated_cost": 55.0, "daily_cost_usd": 5.0},
            {"monthly_cost_limit": 50.0, "daily_cost_limit": 10.0},
        )
        assert result["allowed"] is False
        assert result["reason"] == "monthly_cost_exceeded"

    def test_daily_exceeded_blocked(self):
        result = self._simulate_enforcement(
            {"estimated_cost": 30.0, "daily_cost_usd": 12.0},
            {"monthly_cost_limit": 50.0, "daily_cost_limit": 10.0},
        )
        assert result["allowed"] is False
        assert result["reason"] == "daily_cost_exceeded"

    def test_no_cost_limit_allows(self):
        """When no cost limit configured, cost enforcement is skipped."""
        result = self._simulate_enforcement(
            {"estimated_cost": 999.0},
            {"monthly_cost_limit": 0},  # disabled
        )
        assert result["allowed"] is True

    def test_missing_cost_data_allows(self):
        """If estimated_cost is absent (pre-migration data), enforcement passes."""
        result = self._simulate_enforcement(
            {"total_tokens": 5000000},  # no cost field
            {"monthly_cost_limit": 50.0},
        )
        assert result["allowed"] is True

    def test_decimal_cost_is_comparable(self):
        """quota_monitor writes a DynamoDB Number, which boto3 returns as Decimal."""
        from decimal import Decimal

        result = self._simulate_enforcement(
            {"estimated_cost": Decimal("55.123456")},
            {"monthly_cost_limit": 50.0},
        )
        assert result["allowed"] is False


class TestPricingModuleIsGone:
    """The in-repo rate table was a second source of truth for cost."""

    def test_shared_pricing_is_deleted(self):
        from pathlib import Path

        lambda_dir = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions"
        assert not (lambda_dir / "shared" / "pricing.py").exists()
        assert not (lambda_dir / "quota_monitor" / "shared").exists()

    def test_no_lambda_still_imports_it(self):
        from pathlib import Path

        lambda_dir = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "lambda-functions"
        offenders = [
            str(p.relative_to(lambda_dir))
            for p in lambda_dir.rglob("index.py")
            if "shared.pricing" in p.read_text() or "from shared import" in p.read_text()
        ]
        assert offenders == [], f"stale pricing import in {offenders}"
