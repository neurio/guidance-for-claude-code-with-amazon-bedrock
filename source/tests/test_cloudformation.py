# ABOUTME: Tests for CloudFormation template cross-region configuration
# ABOUTME: Validates IAM policies support cross-region inference properly

"""Tests for CloudFormation template configuration."""

import re

from tests.cfn_yaml import INFRA_DIR, load_intrinsics


class TestCloudFormationCrossRegion:
    """Tests for CloudFormation template cross-region support."""

    def get_template(self):
        """Load the CloudFormation template."""
        # Assertions here read intrinsics in long form ({"Ref": "Foo"}), so this
        # must be load_intrinsics rather than load_resolved.
        return load_intrinsics(INFRA_DIR / "cognito-identity-pool.yaml")

    def test_allowed_bedrock_regions_default(self):
        """Test that default AllowedBedrockRegions includes all US cross-region regions."""
        template = self.get_template()

        # Check parameters
        params = template.get("Parameters", {})
        assert "AllowedBedrockRegions" in params

        bedrock_regions_param = params["AllowedBedrockRegions"]
        assert bedrock_regions_param["Type"] == "CommaDelimitedList"

        # Check default value includes all US regions for cross-region
        default_regions = bedrock_regions_param.get("Default", "")
        assert "us-east-1" in default_regions
        assert "us-east-2" in default_regions
        assert "us-west-2" in default_regions

    def test_iam_policy_allows_cross_region_resources(self):
        """Test that IAM policy allows cross-region inference resources."""
        template = self.get_template()

        # Find the BedrockAccessPolicy
        resources = template.get("Resources", {})
        assert "BedrockAccessPolicy" in resources

        policy = resources["BedrockAccessPolicy"]
        assert policy["Type"] == "AWS::IAM::ManagedPolicy"

        # Check policy document
        policy_doc = policy["Properties"]["PolicyDocument"]
        statements = policy_doc["Statement"]

        # Find the AllowBedrockInvoke statement
        invoke_statement = None
        for stmt in statements:
            if stmt.get("Sid") == "AllowBedrockInvoke":
                invoke_statement = stmt
                break

        assert invoke_statement is not None

        # Check resources include cross-region patterns
        resources_allowed = invoke_statement["Resource"]
        assert isinstance(resources_allowed, list)

        # Extract actual resource strings from Fn::Sub or plain strings
        resource_strings = []
        for r in resources_allowed:
            if isinstance(r, dict) and "Fn::Sub" in r:
                resource_strings.append(r["Fn::Sub"])
            elif isinstance(r, str):
                resource_strings.append(r)

        # Should allow foundation models (cross-region)
        assert any("foundation-model" in r for r in resource_strings)

        # Should allow inference profiles
        assert any("inference-profile" in r for r in resource_strings)

        # Check ARN patterns for cross-region (double colon between region and account)
        assert any("*::foundation-model" in r for r in resource_strings)

    def test_iam_policy_has_region_condition(self):
        """Test that IAM policy has region condition for security."""
        template = self.get_template()

        resources = template.get("Resources", {})
        policy = resources["BedrockAccessPolicy"]
        policy_doc = policy["Properties"]["PolicyDocument"]
        statements = policy_doc["Statement"]

        # Find the AllowBedrockInvoke statement
        for stmt in statements:
            if stmt.get("Sid") == "AllowBedrockInvoke":
                # Should have a condition
                assert "Condition" in stmt

                condition = stmt["Condition"]
                assert "StringEquals" in condition

                # Should check aws:RequestedRegion
                string_equals = condition["StringEquals"]
                assert "aws:RequestedRegion" in string_equals

                # The value should reference the AllowedBedrockRegions parameter
                region_ref = string_equals["aws:RequestedRegion"]
                # Check if it's a Ref to AllowedBedrockRegions
                assert isinstance(region_ref, dict)
                assert "Ref" in region_ref
                assert region_ref["Ref"] == "AllowedBedrockRegions"
                break

    def test_bedrock_access_role_configuration(self):
        """Test that the BedrockAccessRole is properly configured."""
        template = self.get_template()

        resources = template.get("Resources", {})
        assert "BedrockAccessRole" in resources

        role = resources["BedrockAccessRole"]
        assert role["Type"] == "AWS::IAM::Role"

        # Check it references the BedrockAccessPolicy
        policy_arns = role["Properties"]["ManagedPolicyArns"]
        # Look for the reference to BedrockAccessPolicy
        found_policy_ref = False
        for arn in policy_arns:
            if isinstance(arn, dict) and "Ref" in arn and arn["Ref"] == "BedrockAccessPolicy":
                found_policy_ref = True
                break
        assert found_policy_ref, "BedrockAccessPolicy not referenced in ManagedPolicyArns"

        # Check assume role policy for Cognito
        assume_policy = role["Properties"]["AssumeRolePolicyDocument"]
        statements = assume_policy["Statement"]

        assert len(statements) > 0
        assume_stmt = statements[0]

        # Should allow Cognito Identity to assume
        # The federated principal may be a string or a conditional (Fn::If) for GovCloud
        federated = assume_stmt["Principal"]["Federated"]
        if isinstance(federated, dict) and "Fn::If" in federated:
            # It's a conditional - verify it includes cognito-identity endpoints
            assert "cognito-identity" in str(federated)
        else:
            # It's a plain string
            assert federated == "cognito-identity.amazonaws.com"

        assert "sts:AssumeRoleWithWebIdentity" in assume_stmt["Action"]

    def test_template_description_mentions_cross_region(self):
        """Test that template description or comments mention cross-region inference."""
        template = self.get_template()

        # Check if Parameters description mentions cross-region
        params = template.get("Parameters", {})
        bedrock_param = params.get("AllowedBedrockRegions", {})
        description = bedrock_param.get("Description", "")

        # Should mention cross-region or multiple regions
        assert "cross-region" in description.lower() or "regions" in description.lower()

    def test_outputs_include_identity_pool(self):
        """Test that outputs include the Identity Pool ID."""
        template = self.get_template()

        outputs = template.get("Outputs", {})
        assert "IdentityPoolId" in outputs

        pool_output = outputs["IdentityPoolId"]
        # Check if Value is a Ref to BedrockIdentityPool
        value = pool_output["Value"]
        assert isinstance(value, dict)
        assert "Ref" in value
        assert value["Ref"] == "BedrockIdentityPool"


# Common Okta thumbprint hardcoded in bedrock-auth-okta.yaml — must NOT appear in the generic template
OKTA_HARDCODED_THUMBPRINT = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"


class TestBedrockAuthGenericTemplate:
    """Tests for bedrock-auth-generic.yaml — covers PingFederate/Keycloak/ForgeRock/etc.

    The template was added to fix a bug where choosing 'Okta (or generic OIDC)' for a
    non-Okta IdP silently applied the Okta template. The generic template must:
      - take the OIDC issuer URL, client ID, and JWKS thumbprint as parameters
      - NOT hardcode the Okta thumbprint
      - NOT contain Okta-specific strings in tags/descriptions
      - emit the same set of outputs as the Okta template (downstream stacks rely on these)
    """

    def get_template(self):
        return load_intrinsics(INFRA_DIR / "bedrock-auth-generic.yaml")

    def test_template_loads(self):
        """Template must parse as valid CloudFormation YAML."""
        template = self.get_template()
        assert template["AWSTemplateFormatVersion"] == "2010-09-09"
        assert "Parameters" in template
        assert "Resources" in template
        assert "Outputs" in template

    def test_required_oidc_parameters(self):
        """Must accept issuer URL, client ID, and thumbprint list as parameters."""
        params = self.get_template()["Parameters"]

        assert "OidcIssuerUrl" in params
        assert "OidcClientId" in params
        assert "OidcThumbprintList" in params
        # ThumbprintList must be a CommaDelimitedList — IAM OIDC supports rotation
        assert params["OidcThumbprintList"]["Type"] == "CommaDelimitedList"
        # Issuer URL pattern must require https://
        assert params["OidcIssuerUrl"]["AllowedPattern"].startswith("^https://")

    def test_no_okta_specific_parameters(self):
        """Must not carry over OktaDomain/OktaClientId from the okta template."""
        params = self.get_template()["Parameters"]
        assert "OktaDomain" not in params
        assert "OktaClientId" not in params

    def test_oidc_provider_resource_uses_parameter_thumbprint(self):
        """OIDC provider must reference the parameter, not hardcode a thumbprint."""
        resources = self.get_template()["Resources"]
        assert "OidcProvider" in resources
        oidc_provider = resources["OidcProvider"]
        assert oidc_provider["Type"] == "AWS::IAM::OIDCProvider"

        thumbprint_list = oidc_provider["Properties"]["ThumbprintList"]
        # Must be a !Ref to OidcThumbprintList, not a literal list of hex strings
        assert isinstance(thumbprint_list, dict), f"ThumbprintList must be a !Ref, got literal: {thumbprint_list}"
        assert thumbprint_list.get("Ref") == "OidcThumbprintList"

    def test_no_hardcoded_okta_thumbprint_anywhere(self):
        """The Okta-specific thumbprint constant must not appear anywhere in the template."""
        template = self.get_template()
        # Stringify the entire template to catch the thumbprint regardless of where it sits
        import json

        serialized = json.dumps(template, default=str)
        assert OKTA_HARDCODED_THUMBPRINT not in serialized, (
            f"Okta-specific thumbprint {OKTA_HARDCODED_THUMBPRINT} leaked into generic template"
        )

    def test_no_okta_substring_in_tags_or_descriptions(self):
        """Tags, descriptions, and resource names must not advertise Okta."""
        import json

        template = self.get_template()
        serialized = json.dumps(template, default=str).lower()
        # 'okta' should not appear anywhere — this template is provider-agnostic
        assert "okta" not in serialized, "Generic template still contains 'okta' references"

    def test_outputs_match_okta_template_contract(self):
        """Downstream stacks (monitoring, packaging) consume these outputs by name."""
        outputs = self.get_template()["Outputs"]
        for required_output in (
            "FederationType",
            "OIDCProviderArn",
            "FederatedRoleArn",
            "DirectSTSRoleArn",
            "BedrockRoleArn",
            "IdentityPoolId",
            "BedrockPolicyArn",
            "ConfigurationJson",
        ):
            assert required_output in outputs, f"Missing output: {required_output}"

    def test_configuration_json_marks_provider_type_as_generic(self):
        """The ConfigurationJson output must declare provider_type=generic so downstream
        consumers don't misclassify the deployment."""
        outputs = self.get_template()["Outputs"]
        config_json = outputs["ConfigurationJson"]["Value"]
        # Value is a !If [cond, direct-config-string, cognito-config-string].
        # Both branches are Fn::Sub strings — verify both contain provider_type=generic.
        if_branches = config_json["Fn::If"]
        assert len(if_branches) == 3, "Expected !If [condition, direct, cognito]"
        for branch in if_branches[1:]:
            assert "Fn::Sub" in branch
            sub_string = branch["Fn::Sub"]
            assert '"provider_type": "generic"' in sub_string, f"Expected provider_type=generic in: {sub_string!r}"

    def test_supports_both_federation_modes(self):
        """Template must support both direct STS and Cognito Identity Pool federation."""
        template = self.get_template()

        params = template["Parameters"]
        assert params["FederationType"]["AllowedValues"] == ["direct", "cognito"]

        # Both conditions must exist
        conditions = template["Conditions"]
        assert "UseDirectIAM" in conditions
        assert "UseCognitoIdentity" in conditions

        # Both role variants must exist
        resources = template["Resources"]
        assert "DirectIAMRole" in resources
        assert "CognitoAuthenticatedRole" in resources

    def test_govcloud_partition_aware(self):
        """Cognito service principals must select the GovCloud variant when deployed there."""
        template = self.get_template()
        conditions = template["Conditions"]
        assert "IsGovCloudWest" in conditions
        assert "IsGovCloudEast" in conditions

        # The Cognito role's principal should reference these (verified by string search —
        # the nested !If chain is awkward to traverse but the string presence is sufficient)
        import json

        cognito_role = template["Resources"]["CognitoAuthenticatedRole"]
        serialized = json.dumps(cognito_role, default=str)
        assert "cognito-identity-us-gov.amazonaws.com" in serialized
        assert "cognito-identity.us-gov-east-1.amazonaws.com" in serialized

    def test_bedrock_policy_uses_partition_pseudoparameter(self):
        """ARN construction must use ${AWS::Partition} for multi-partition support."""
        template = self.get_template()
        policy = template["Resources"]["BedrockAccessPolicy"]
        policy_doc = policy["Properties"]["PolicyDocument"]

        # Find any Resource entries — they should contain ${AWS::Partition}, not literal "aws"
        partition_found = False
        for stmt in policy_doc["Statement"]:
            if "Resource" in stmt:
                resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
                for r in resources:
                    if isinstance(r, dict) and "Fn::Sub" in r and "${AWS::Partition}" in r["Fn::Sub"]:
                        partition_found = True
                        break
        assert partition_found, "Bedrock ARNs must use ${AWS::Partition} for GovCloud support"


class TestFableDenyStatement:
    """The Azure stack denies Claude Fable 5 outright; pin the exact ARN patterns.

    These patterns are load-bearing and easy to break by "tidying": widening the
    suffix to a trailing wildcard silently denies future Fable releases, and
    narrowing the action list lets a new invoke action leak around the Deny.
    """

    DENIED_SUFFIX = "*anthropic.claude-fable-5"

    def get_statement(self):
        template = load_intrinsics(INFRA_DIR / "bedrock-auth-azure.yaml")
        policy_doc = template["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"]
        for stmt in policy_doc["Statement"]:
            if isinstance(stmt, dict) and stmt.get("Sid") == "DenyClaudeFableModels":
                return stmt
        raise AssertionError("DenyClaudeFableModels statement missing from bedrock-auth-azure.yaml")

    def test_denies_all_bedrock_actions(self):
        """'bedrock:*' — not an action list — so a future invoke action cannot leak around it."""
        stmt = self.get_statement()
        assert stmt["Effect"] == "Deny"
        assert stmt["Action"] == "bedrock:*"

    def test_denies_exact_fable_5_suffix(self):
        """Foundation-model and inference-profile ARNs, scoped to the exact model suffix."""
        stmt = self.get_statement()
        resources = [r["Fn::Sub"] for r in stmt["Resource"]]

        assert resources == [
            f"arn:${{AWS::Partition}}:bedrock:*::foundation-model/{self.DENIED_SUFFIX}",
            f"arn:${{AWS::Partition}}:bedrock:::foundation-model/{self.DENIED_SUFFIX}",
            f"arn:${{AWS::Partition}}:bedrock:*:*:inference-profile/{self.DENIED_SUFFIX}",
        ]

    def test_no_trailing_wildcard_after_model_name(self):
        """A trailing '*' would re-widen the Deny beyond what the deployed policy does."""
        stmt = self.get_statement()
        for r in stmt["Resource"]:
            assert r["Fn::Sub"].endswith(self.DENIED_SUFFIX), (
                f"{r['Fn::Sub']!r} must end at the exact model suffix, not a wildcard"
            )

    def test_deny_precedes_allows(self):
        """Order does not affect IAM evaluation, but keep the Deny first for readability."""
        template = load_intrinsics(INFRA_DIR / "bedrock-auth-azure.yaml")
        statements = template["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"]["Statement"]
        assert statements[0].get("Sid") == "DenyClaudeFableModels"


def _iam_wildcard_matches(pattern: str, value: str) -> bool:
    """Match an IAM policy pattern against a value.

    IAM resource/condition wildcards are only '*' (any sequence) and '?' (one
    character) — every other character is literal, unlike fnmatch, which would
    read '[' as a character class.
    """
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, value) is not None


class TestFable51Allowlist:
    """Fable 5.1 is allow-listed: denied for every aws:userid outside the list.

    The list is the access-control decision itself, so pin its shape. The
    failure modes are all silent — a bare email with no '*:' prefix can never
    match a session's aws:userid (locks everyone out), and a trailing wildcard
    on the Fable 5 Deny above would blanket-deny 5.1 regardless of the list.
    """

    ALLOWLISTED_SUFFIX = "*anthropic.claude-fable-5-1"

    def get_statement(self, sid="DenyClaudeFable51ExceptAllowlist"):
        template = load_intrinsics(INFRA_DIR / "bedrock-auth-azure.yaml")
        policy_doc = template["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"]
        for stmt in policy_doc["Statement"]:
            if isinstance(stmt, dict) and stmt.get("Sid") == sid:
                return stmt
        raise AssertionError(f"{sid} statement missing from bedrock-auth-azure.yaml")

    def test_denies_all_bedrock_actions_for_non_allowlisted(self):
        """'bedrock:*' — not an action list — so a future invoke action cannot leak around it."""
        stmt = self.get_statement()
        assert stmt["Effect"] == "Deny"
        assert stmt["Action"] == "bedrock:*"

    def test_covers_foundation_model_and_inference_profile_arns(self):
        """Regional, global (region-less), and inference-profile ARNs all carry the Deny."""
        stmt = self.get_statement()
        resources = [r["Fn::Sub"] for r in stmt["Resource"]]

        assert resources == [
            f"arn:${{AWS::Partition}}:bedrock:*::foundation-model/{self.ALLOWLISTED_SUFFIX}",
            f"arn:${{AWS::Partition}}:bedrock:::foundation-model/{self.ALLOWLISTED_SUFFIX}",
            f"arn:${{AWS::Partition}}:bedrock:*:*:inference-profile/{self.ALLOWLISTED_SUFFIX}",
        ]

    def test_conditions_on_aws_userid_string_not_like(self):
        """StringNotLike + aws:userid is what turns a blanket Deny into an allowlist.

        StringNotEquals would never match, because aws:userid is prefixed with the
        role's unique ID; dropping the Condition entirely would deny everyone.
        """
        stmt = self.get_statement()
        condition = stmt["Condition"]
        assert list(condition) == ["StringNotLike"]
        assert list(condition["StringNotLike"]) == ["aws:userid"]

    def test_allowlist_is_non_empty(self):
        """An empty list denies Fable 5.1 to everyone — safe, but never intentional here."""
        allowed = self.get_statement()["Condition"]["StringNotLike"]["aws:userid"]
        assert isinstance(allowed, list)
        assert allowed, "allowlist is empty — Fable 5.1 would be denied to every user"

    def test_every_entry_wildcards_the_role_unique_id(self):
        """A bare email cannot match '<role-unique-id>:<session-name>' — it locks the user out."""
        allowed = self.get_statement()["Condition"]["StringNotLike"]["aws:userid"]
        for entry in allowed:
            assert entry.startswith("*:"), (
                f"{entry!r} must start with '*:' to match aws:userid's '<role-unique-id>:<session-name>' form"
            )
            assert "@" in entry, f"{entry!r} should be '*:<email>' — RoleSessionName is the raw email claim"

    # Captured from a live session on the deployed role:
    #   aws sts get-caller-identity --profile ClaudeCode --query UserId
    # Azure AD spells the 'email' claim with capitals, which the first version of
    # this allowlist got wrong — it listed a lowercased address, and because
    # StringNotLike is case-sensitive the Deny fired for the very user it was
    # written to exempt.
    REAL_USERID = "AROAZD3IUWBAWNR5LWNXE:Cameron.Johnson@generac.com"

    def test_real_session_userid_is_exempted(self):
        """A known-good aws:userid must match an allowlist entry, casing included."""
        allowed = self.get_statement()["Condition"]["StringNotLike"]["aws:userid"]
        assert any(_iam_wildcard_matches(entry, self.REAL_USERID) for entry in allowed), (
            f"{self.REAL_USERID} matches no entry in {allowed} — that user is denied Fable 5.1"
        )

    def test_entry_casing_is_load_bearing(self):
        """Documents why entries are copied verbatim: a case-folded userid stops matching."""
        allowed = self.get_statement()["Condition"]["StringNotLike"]["aws:userid"]
        role_id, _, session_name = self.REAL_USERID.partition(":")
        folded = f"{role_id}:{session_name.lower()}"
        assert folded != self.REAL_USERID, "pick a REAL_USERID whose session name has capitals"
        assert not any(_iam_wildcard_matches(entry, folded) for entry in allowed), (
            "a lowercased session name still matches — the casing guard above proves nothing"
        )

    def test_allowlisted_userid_is_not_caught_by_the_fable_5_deny(self):
        """The blanket Fable 5 Deny must not extend to 5.1, or the allowlist is dead.

        Guards against re-widening 'claude-fable-5' to 'claude-fable-5*'.
        """
        fable_5_stmt = self.get_statement(sid="DenyClaudeFableModels")
        fable_51_model_arn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-fable-5-1"

        for resource in fable_5_stmt["Resource"]:
            pattern = resource["Fn::Sub"].replace("${AWS::Partition}", "aws")
            assert not _iam_wildcard_matches(pattern, fable_51_model_arn), (
                f"Fable 5 Deny pattern {pattern!r} also matches Fable 5.1 — allowlist can never grant access"
            )

    def test_deny_matches_cris_prefixed_inference_profiles(self):
        """Users invoke us./global./eu. CRIS IDs, not the bare foundation-model ID."""
        stmt = self.get_statement()
        patterns = [r["Fn::Sub"].replace("${AWS::Partition}", "aws") for r in stmt["Resource"]]

        for cris_arn in (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-fable-5-1",
            "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.anthropic.claude-fable-5-1",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-fable-5-1",
        ):
            assert any(_iam_wildcard_matches(p, cris_arn) for p in patterns), (
                f"no Deny resource pattern matches {cris_arn}"
            )
