import aws_cdk as cdk
from aws_cdk import (
    BundlingOptions,
    DockerImage,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
)
from constructs import Construct

from assets.seed_sample_data import ensure_sample_csv
from stacks.settings import StackSettings

LAMBDAS_DIR = "../lambdas"
_SAMPLE_CSV_KEY = "sample_sales.csv"
_PYTHON313_BUILD_IMAGE = DockerImage.from_registry(
    "public.ecr.aws/sam/build-python3.13"
)


def _bundled_code() -> lambda_.Code:
    """Bundle lambda source + pip dependencies using the Lambda Docker image."""
    return lambda_.Code.from_asset(
        LAMBDAS_DIR,
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


class McpAuthSpikeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, settings: StackSettings, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        stytch_secret_param = ssm.StringParameter.from_secure_string_parameter_attributes(
            self,
            "StytchSecret",
            parameter_name="/mcp-spike/stytch/project_secret",
        )

        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            inline_policies={
                "GatewayPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["lambda:InvokeFunction", "lambda:GetFunction"],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            actions=["iam:PassRole"],
                            resources=["*"],
                        ),
                    ]
                )
            },
        )

        common_lambda_kwargs = dict(
            runtime=lambda_.Runtime.PYTHON_3_13,
            log_retention=logs.RetentionDays.ONE_MONTH,
            timeout=Duration.minutes(1),
            memory_size=256,
        )

        oauth_lambda = lambda_.Function(
            self,
            "OAuthServerLambda",
            handler="oauth_server.handler",
            code=_bundled_code(),
            environment={
                "STYTCH_PROJECT_ID": settings.stytch_project_id,
                "STYTCH_PROJECT_DOMAIN": settings.stytch_project_domain,
                "STYTCH_ORG_ID": settings.stytch_org_id,
                "STYTCH_PUBLIC_TOKEN": settings.stytch_public_token,
                "STYTCH_PROJECT_SECRET_SSM_PATH": "/mcp-spike/stytch/project_secret",
                # client_id of the pre-registered Public Connected App in Stytch dashboard.
                "CONNECTED_APP_CLIENT_ID": settings.connected_app_client_id,
                # OAUTH_LAMBDA_URL is injected after add_function_url() below.
            },
            **common_lambda_kwargs,
        )

        stytch_secret_param.grant_read(oauth_lambda)

        oauth_api = apigwv2.HttpApi(
            self,
            "OAuthApi",
            api_name="McpAuthSpikeOAuthApi",
            description="OAuth server for MCP Auth Spike",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["*"],
            ),
            default_integration=apigwv2_integrations.HttpLambdaIntegration(
                "OAuthLambdaIntegration",
                oauth_lambda,
            ),
        )
        # Avoids a CDK circular dependency: Lambda env references API GW URL, but
        # API GW integration references Lambda ARN.  Set OAUTH_LAMBDA_URL in .env
        # after the first deploy (see CDK output: OAuthServerUrl).
        oauth_lambda.add_environment("OAUTH_LAMBDA_URL", settings.oauth_lambda_url)

        request_interceptor = lambda_.Function(
            self,
            "RequestInterceptorLambda",
            handler="request_interceptor.handler",
            code=_bundled_code(),
            environment={},
            **common_lambda_kwargs,
        )

        response_interceptor = lambda_.Function(
            self,
            "ResponseInterceptorLambda",
            handler="response_interceptor.handler",
            code=_bundled_code(),
            environment={},
            **common_lambda_kwargs,
        )

        results_bucket = s3.Bucket(
            self,
            "QueryDataResultsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        sample_data_dir = ensure_sample_csv()
        s3_deployment.BucketDeployment(
            self,
            "QueryDataSeedDeployment",
            sources=[s3_deployment.Source.asset(str(sample_data_dir))],
            destination_bucket=results_bucket,
            prune=False,
            retain_on_delete=False,
        )

        target_lambda = lambda_.Function(
            self,
            "TargetLambda",
            handler="target.handler",
            code=_bundled_code(),
            environment={
                "RESULTS_BUCKET": results_bucket.bucket_name,
                "RESULTS_OBJECT_KEY": _SAMPLE_CSV_KEY,
                "PRESIGNED_URL_TTL_SECONDS": "300",
            },
            timeout=Duration.minutes(5),
            memory_size=256,
            runtime=lambda_.Runtime.PYTHON_3_13,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        results_bucket.grant_read(target_lambda)

        gateway = agentcore.CfnGateway(
            self,
            "McpGateway",
            name="McpAuthSpikeGateway",
            role_arn=gateway_role.role_arn,
            description="MCP auth spike - AgentCore Gateway with Stytch B2B JWT auth",
            exception_level="DEBUG",
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    # Point directly to Stytch's own OIDC discovery — authoritative JWKS source.
                    # Stytch's discovery already has our Lambda's authorization_endpoint.
                    discovery_url=f"{settings.stytch_project_domain}/.well-known/openid-configuration",
                    # Stytch Connected Apps tokens contain the 'resource' parameter as the
                    # audience (aud) claim.  MCP clients send resource=<mcp-server-url>.
                    # Verify by decoding a token: python3 -c "
                    #   import base64, json, sys
                    #   payload = sys.argv[1].split('.')[1] + '=='
                    #   print(json.loads(base64.urlsafe_b64decode(payload)))
                    # " <access_token>
                    # and checking the 'aud' claim.
                    allowed_audience=[
                        settings.agentcore_gateway_url,
                        # Fallback: Stytch may also use the project domain as audience
                        settings.stytch_project_domain,
                    ],
                )
            ),
            protocol_type="MCP",
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    instructions="Use this gateway to connect to the MCP auth spike",
                    supported_versions=["2025-11-25"],
                )
            ),
            interceptor_configurations=[
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST"],
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=request_interceptor.function_arn,
                        )
                    ),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True,
                    ),
                ),
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["RESPONSE"],
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=response_interceptor.function_arn,
                        )
                    ),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True,
                    ),
                ),
            ],
        )

        target_lambda.add_permission(
            "AllowAgentCoreGatewayInvoke",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=gateway.attr_gateway_arn,
        )

        agentcore.CfnGatewayTarget(
            self,
            "TargetLambdaRegistration",
            name="DummyToolsTarget",
            gateway_identifier=gateway.attr_gateway_identifier,
            description="Tool target for the MCP auth spike (get_weather, get_time, query_data)",
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=target_lambda.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[
                                agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                    name="get_weather",
                                    description="Get weather for a location",
                                    input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                                        type="object",
                                        properties={
                                            "location": agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                                                type="string",
                                                description="e.g. seattle, wa",
                                            )
                                        },
                                        required=["location"],
                                    ),
                                ),
                                agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                    name="get_time",
                                    description="Get current time for a timezone",
                                    input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                                        type="object",
                                        properties={
                                            "timezone": agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                                                type="string",
                                                description="e.g. America/New_York",
                                            )
                                        },
                                        required=["timezone"],
                                    ),
                                ),
                                agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                    name="query_data",
                                    description=(
                                        "Query the sales sample dataset. Returns a JSON envelope: "
                                        "status, row_count, summary_stats (per-column: min/max/"
                                        "mean/stddev for numeric, min/max/distinct for date, "
                                        "distinct + top values for low-cardinality strings, nulls), "
                                        "sample_rows (first 50 rows), full_results (format, "
                                        "size_bytes, presigned_url, presigned_url_expires_at, "
                                        "presigned_url_ttl_seconds, inline), next_steps. Use "
                                        "summary_stats and sample_rows to answer directly when the "
                                        "question can be answered from the preview. For questions "
                                        "needing the whole dataset, tell the user to click "
                                        "full_results.presigned_url, download the CSV, and drop it "
                                        "into this chat so the analysis tool can process every row."
                                    ),
                                    input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                                        type="object",
                                        properties={},
                                    ),
                                ),
                            ]
                        ),
                    )
                )
            ),
        )

        cdk.CfnOutput(self, "OAuthServerUrl", value=oauth_api.url or "")
        cdk.CfnOutput(self, "GatewayArn", value=gateway.attr_gateway_arn)
        cdk.CfnOutput(self, "QueryDataResultsBucketName", value=results_bucket.bucket_name)
        cdk.CfnOutput(
            self,
            "RequestInterceptorArn",
            value=request_interceptor.function_arn,
        )
        cdk.CfnOutput(
            self,
            "ResponseInterceptorArn",
            value=response_interceptor.function_arn,
        )
