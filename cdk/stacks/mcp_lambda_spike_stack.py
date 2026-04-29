"""McpLambdaSpikeStack — FastMCP-on-Lambda alternative to the AgentCore stack.

Architecture
------------
- Single API Gateway v2 HTTP API.
- OAuth Lambda (zip-bundled) hosts OIDC/AS metadata, /register, /oauth/authorize,
  /login, /login/callback — same headless OAuth flow as the AgentCore stack.
- MCP Lambda (Docker image) runs FastMCP via AWS Lambda Web Adapter.  Validates
  Stytch JWTs in-process, auto-hosts RFC 9728 protected-resource metadata,
  enforces scope-based RBAC inside each tool body (default-deny).
- S3 results bucket holds the seeded sales CSV that `query_data` returns via a
  presigned URL.

This stack is fully independent from `McpAuthSpikeStack` — no shared SSM
parameter, bucket, Lambda, or assets directory.  The stacks can coexist.

Two-pass deploy
---------------
1. First `cdk deploy`: leave `MCP_OAUTH_LAMBDA_URL` and `MCP_SERVER_URL` as
   placeholders in `.env`.  Read the printed `McpOAuthServerUrl` and
   `McpServerUrl` outputs.
2. Fill those values into `.env`, then `cdk deploy` again so the OAuth Lambda
   learns its own URL and the MCP Lambda learns its `aud`/PRM `resource` URL.
3. In the Stytch Dashboard:
   - Connected App → Authorization URL = `<McpOAuthServerUrl>oauth/authorize`.
   - Redirect URLs (top-level) → add `<McpOAuthServerUrl>login/callback`
     (type Login, status Enabled).
   - Connected App → Redirect URIs → ensure your client's URI is registered.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    BundlingOptions,
    DockerImage,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
)
from constructs import Construct

from assets_mcp_lambda_spike.seed_sample_data import ensure_sample_csv
from stacks.settings import StackSettings

_OAUTH_LAMBDA_DIR = "../lambdas/mcp_lambda_spike/oauth"
_MCP_LAMBDA_DIR = "../lambdas/mcp_lambda_spike/mcp"
_SAMPLE_CSV_KEY = "sample_sales.csv"
_STYTCH_SECRET_SSM_PATH = "/mcp-lambda-spike/stytch/project_secret"
_PYTHON313_BUILD_IMAGE = DockerImage.from_registry(
    "public.ecr.aws/sam/build-python3.13"
)


def _bundled_oauth_code() -> lambda_.Code:
    """Bundle the OAuth Lambda's source + pip deps using the SAM build image."""
    return lambda_.Code.from_asset(
        _OAUTH_LAMBDA_DIR,
        bundling=BundlingOptions(
            image=_PYTHON313_BUILD_IMAGE,
            command=[
                "bash",
                "-c",
                "pip install -r requirements.txt -t /asset-output --no-cache-dir --quiet "
                "&& cp *.py /asset-output",
            ],
        ),
    )


class McpLambdaSpikeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        settings: StackSettings,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -- SSM (own namespace) ----------------------------------------------
        stytch_secret_param = ssm.StringParameter.from_secure_string_parameter_attributes(
            self,
            "StytchSecret",
            parameter_name=_STYTCH_SECRET_SSM_PATH,
        )

        # -- OAuth Lambda (zip) -----------------------------------------------
        oauth_lambda = lambda_.Function(
            self,
            "OAuthServerLambda",
            handler="oauth_server.handler",
            code=_bundled_oauth_code(),
            runtime=lambda_.Runtime.PYTHON_3_13,
            timeout=Duration.minutes(1),
            memory_size=256,
            log_retention=logs.RetentionDays.ONE_MONTH,
            environment={
                "STYTCH_PROJECT_ID": settings.stytch_project_id,
                "STYTCH_PROJECT_DOMAIN": settings.stytch_project_domain,
                "STYTCH_ORG_ID": settings.stytch_org_id,
                "STYTCH_PUBLIC_TOKEN": settings.stytch_public_token,
                "STYTCH_PROJECT_SECRET_SSM_PATH": _STYTCH_SECRET_SSM_PATH,
                "CONNECTED_APP_CLIENT_ID": settings.connected_app_client_id,
                # OAUTH_LAMBDA_URL is added post-creation, after the API Gateway
                # construct is materialized (avoids a circular CDK dep).
            },
        )
        stytch_secret_param.grant_read(oauth_lambda)

        # -- S3 results bucket + seed CSV -------------------------------------
        results_bucket = s3.Bucket(
            self,
            "QueryDataResultsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        sample_data_dir: Path = ensure_sample_csv()
        s3_deployment.BucketDeployment(
            self,
            "QueryDataSeedDeployment",
            sources=[s3_deployment.Source.asset(str(sample_data_dir))],
            destination_bucket=results_bucket,
            prune=False,
            retain_on_delete=False,
        )

        # -- MCP Lambda (Docker image with Lambda Web Adapter) ----------------
        # `mcp_server_url` is a post-deploy value; on first deploy it'll be
        # an empty string or placeholder, the Lambda still boots but the
        # protected-resource metadata won't advertise the right resource URL
        # until the second deploy populates the env var.
        mcp_lambda = lambda_.DockerImageFunction(
            self,
            "McpServerLambda",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=_MCP_LAMBDA_DIR,
            ),
            architecture=lambda_.Architecture.X86_64,
            memory_size=1024,
            timeout=Duration.seconds(30),
            log_retention=logs.RetentionDays.ONE_MONTH,
            environment={
                "STYTCH_PROJECT_DOMAIN": settings.stytch_project_domain,
                "MCP_SERVER_URL": settings.mcp_server_url
                or "https://placeholder.invalid/mcp",
                "RESULTS_BUCKET": results_bucket.bucket_name,
                "RESULTS_OBJECT_KEY": _SAMPLE_CSV_KEY,
                "PRESIGNED_URL_TTL_SECONDS": "300",
            },
        )
        results_bucket.grant_read(mcp_lambda)

        # -- API Gateway v2 (HTTP API) ----------------------------------------
        oauth_integration = apigwv2_integrations.HttpLambdaIntegration(
            "OAuthLambdaIntegration", oauth_lambda
        )
        mcp_integration = apigwv2_integrations.HttpLambdaIntegration(
            "McpLambdaIntegration", mcp_lambda
        )

        http_api = apigwv2.HttpApi(
            self,
            "McpApi",
            api_name="McpLambdaSpikeApi",
            description="API Gateway for the FastMCP-on-Lambda spike",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["*"],
            ),
            # Default integration -> OAuth Lambda.  Explicit /mcp* and
            # /.well-known/oauth-protected-resource* routes override below.
            default_integration=oauth_integration,
        )

        # MCP transport endpoints
        http_api.add_routes(
            path="/mcp",
            methods=[apigwv2.HttpMethod.ANY],
            integration=mcp_integration,
        )
        http_api.add_routes(
            path="/mcp/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=mcp_integration,
        )

        # Per RFC 9728 §3.1, when the resource is at /mcp the protected-resource
        # metadata lives at /.well-known/oauth-protected-resource/mcp.  Cover
        # both the bare path and any subpath FastMCP/MCP clients might probe.
        http_api.add_routes(
            path="/.well-known/oauth-protected-resource",
            methods=[apigwv2.HttpMethod.GET],
            integration=mcp_integration,
        )
        http_api.add_routes(
            path="/.well-known/oauth-protected-resource/{proxy+}",
            methods=[apigwv2.HttpMethod.GET],
            integration=mcp_integration,
        )

        # Inject post-creation URLs.  Same pattern as the legacy stack: this
        # value is read from .env (filled in after the first deploy).
        oauth_lambda.add_environment(
            "OAUTH_LAMBDA_URL", settings.mcp_oauth_lambda_url
        )

        # -- Outputs -----------------------------------------------------------
        api_url = http_api.url or ""
        cdk.CfnOutput(
            self,
            "McpOAuthServerUrl",
            value=api_url,
            description="API Gateway base URL — copy into .env as MCP_OAUTH_LAMBDA_URL",
        )
        cdk.CfnOutput(
            self,
            "McpServerUrl",
            value=(api_url.rstrip("/") + "/mcp") if api_url else "",
            description="MCP streamable-HTTP endpoint — copy into .env as MCP_SERVER_URL",
        )
        cdk.CfnOutput(
            self,
            "QueryDataResultsBucketName",
            value=results_bucket.bucket_name,
        )
        cdk.CfnOutput(self, "OAuthLambdaArn", value=oauth_lambda.function_arn)
        cdk.CfnOutput(self, "McpLambdaArn", value=mcp_lambda.function_arn)
