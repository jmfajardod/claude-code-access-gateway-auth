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
    oauth_lambda_url: str   # e.g. https://<id>.lambda-url.<region>.on.aws/
    agentcore_gateway_url: str  # e.g. https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp

    # CDK environment (optional — falls back to CDK defaults if not set)
    cdk_default_account: str = ""
    cdk_default_region: str = "us-east-1"
