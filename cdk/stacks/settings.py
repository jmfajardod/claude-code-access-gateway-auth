from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root — two levels above this file (cdk/stacks/settings.py)
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class StackSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Stytch B2B project
    stytch_project_id: str
    stytch_project_domain: str  # e.g. https://<slug>.customers.stytch.dev  (no trailing slash)
    stytch_org_id: str
    stytch_public_token: str
    connected_app_client_id: str

    # AWS resource identifiers (populated after first CDK deploy)
    oauth_lambda_url: str   # e.g. https://<id>.execute-api.<region>.amazonaws.com/
    agentcore_gateway_url: str  # e.g. https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp

    # CDK environment (optional — falls back to CDK defaults if not set)
    cdk_default_account: str = ""
    cdk_default_region: str = "us-east-1"

    # ------------------------------------------------------------------
    # McpFargateGoogleStack — FastMCP on ECS Fargate behind a public ALB
    # with Google OAuth (FastMCP GoogleProvider OAuth Proxy).
    # All fields below are optional: stack instantiation is skipped when
    # mcp_public_hostname is empty.
    # ------------------------------------------------------------------

    # Public hostname the ALB will serve.  Must be a valid DNS hostname
    # (no underscores) — ACM rejects underscored names during cert validation.
    mcp_public_hostname: str = ""  

    # Route53 hosted zone that owns the apex of the hostname above.
    # Must already exist in this AWS account; CDK will look it up at synth time.
    route53_hosted_zone_name: str = "" 

    # Google OAuth 2.0 Web client_id (created manually in GCP Console).
    # The client_secret is stored in Secrets Manager, NOT in .env.
    google_client_id: str = ""

    # Comma-separated list of Google Workspace domains allowed by the `hd`
    # claim check.  For consumer Gmail the claim is absent — include "gmail.com"
    # to allow it via fallback to the email domain.
    allowed_workspace_domains: str = "gmail.com,boldcf.co"

    # ------------------------------------------------------------------
    # LakeFormation admin allow-list. Populated from .env so the stack file
    # doesn't have to hard-code your AWS account ID / IAM user names (which
    # would be sensitive when committed to a public repo).
    # ------------------------------------------------------------------

    # Include `arn:aws:iam::<account>:root` in the LF admins list. Useful
    # so logging in as root via the AWS console can see LF tags / grants.
    lf_admin_include_root: bool = False

    # Extra LF admin principals to include. Comma-separated IAM-ARN suffixes
    # (the portion AFTER `arn:aws:iam::<account>:`).
    # Account ID is templated in at deploy time via Aws.ACCOUNT_ID.
    lf_extra_admin_iam_paths: str = ""
