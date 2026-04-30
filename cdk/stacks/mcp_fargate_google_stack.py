"""ECS Fargate + ALB stack running FastMCP with Google OAuth.

Architecture (no ALB OIDC — see README rationale):

    Claude Desktop  ──HTTPS──►  ALB (HTTPS:443, ACM cert)
                                  │
                                  ▼
                                ECS Fargate Service
                                  └── FastMCP container :8080
                                        ├─ /health             (ALB health)
                                        ├─ /.well-known/...    (RFC 9728 / 8414)
                                        ├─ /register           (DCR shim)
                                        ├─ /authorize, /token, /auth/callback
                                        └─ /mcp                (Streamable HTTP)

OAuth-proxy state lives in DynamoDB (encrypted at rest with a Fernet key
stored in Secrets Manager).  Google OAuth client_secret is also kept in
Secrets Manager — populated out-of-band after first deploy.

Domain restriction is enforced by the FastMCP container (see server.py),
not by AWS — the stack just plumbs ALLOWED_WORKSPACE_DOMAINS into the env.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
)
from constructs import Construct

from assets.seed_sample_data import ensure_sample_csv
from stacks.settings import StackSettings

_SAMPLE_CSV_KEY = "sample_sales.csv"
_CONTAINER_PORT = 8080

# SSM SecureString parameter names — must be pre-created with
# `aws ssm put-parameter --type SecureString` BEFORE first cdk deploy.
# CDK can only reference existing SecureStrings, not create them.
_SSM_GOOGLE_CLIENT_SECRET = "/mcp-fargate-google/google-client-secret"
_SSM_JWT_SIGNING_KEY = "/mcp-fargate-google/jwt-signing-key"
_SSM_STORAGE_ENC_KEY = "/mcp-fargate-google/storage-enc-key"


class McpFargateGoogleStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        settings: StackSettings,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not settings.mcp_public_hostname or not settings.route53_hosted_zone_name:
            raise ValueError(
                "McpFargateGoogleStack requires MCP_PUBLIC_HOSTNAME and "
                "ROUTE53_HOSTED_ZONE_NAME to be set in .env"
            )

        public_base_url = f"https://{settings.mcp_public_hostname}"

        # --- VPC --------------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # --- DNS + TLS --------------------------------------------------
        hosted_zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=settings.route53_hosted_zone_name,
        )
        certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=settings.mcp_public_hostname,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        # --- DynamoDB (OAuth-proxy state) -------------------------------
        # Schema matches py-key-value-aio's DynamoDBStore (built-in):
        #   partition: "collection" (S)   sort: "key" (S)   ttl attr: "ttl"
        # The store will auto-create the table+TTL itself if missing, but
        # we provision it via CDK so IAM, encryption, and lifecycle live in
        # one place and the container starts up faster (no first-call create).
        oauth_state_table = dynamodb.Table(
            self,
            "OAuthStateTable",
            partition_key=dynamodb.Attribute(
                name="collection",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="key",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Secrets (SSM Parameter Store SecureStrings) ---------------
        # All three must be created out-of-band BEFORE first cdk deploy:
        #
        #   aws ssm put-parameter --type SecureString --region us-east-1 \
        #     --name /mcp-fargate-google/google-client-secret \
        #     --value '<paste-from-GCP-Console>'
        #
        #   aws ssm put-parameter --type SecureString --region us-east-1 \
        #     --name /mcp-fargate-google/jwt-signing-key \
        #     --value "$(openssl rand -base64 48)"
        #
        #   FERNET=$(uv run python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
        #   aws ssm put-parameter --type SecureString --region us-east-1 \
        #     --name /mcp-fargate-google/storage-enc-key --value "$FERNET"
        #
        # Rotating any value: `aws ssm put-parameter --overwrite ...` then
        # `aws ecs update-service --force-new-deployment` to pick it up.
        google_client_secret = ssm.StringParameter.from_secure_string_parameter_attributes(
            self,
            "GoogleClientSecretParam",
            parameter_name=_SSM_GOOGLE_CLIENT_SECRET,
        )
        jwt_signing_key_param = ssm.StringParameter.from_secure_string_parameter_attributes(
            self,
            "JwtSigningKeyParam",
            parameter_name=_SSM_JWT_SIGNING_KEY,
        )
        storage_enc_key_param = ssm.StringParameter.from_secure_string_parameter_attributes(
            self,
            "StorageEncKeyParam",
            parameter_name=_SSM_STORAGE_ENC_KEY,
        )

        # --- Results bucket (independent from McpAuthSpikeStack) --------
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

        # --- Container image -------------------------------------------
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "ServerImage",
            directory="../lambdas/mcp_fastmcp_server",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # --- ECS cluster + task -----------------------------------------
        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        log_group = logs.LogGroup(
            self,
            "ServiceLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            cpu=256,
            memory_limit_mib=512,
        )

        container = task_def.add_container(
            "Server",
            image=ecs.ContainerImage.from_docker_image_asset(image_asset),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="mcp", log_group=log_group),
            environment={
                "PUBLIC_BASE_URL": public_base_url,
                "GOOGLE_CLIENT_ID": settings.google_client_id,
                "ALLOWED_WORKSPACE_DOMAINS": settings.allowed_workspace_domains,
                "DDB_TABLE_NAME": oauth_state_table.table_name,
                "RESULTS_BUCKET": results_bucket.bucket_name,
                "RESULTS_OBJECT_KEY": _SAMPLE_CSV_KEY,
                "PRESIGNED_URL_TTL_SECONDS": "300",
                "AWS_REGION": settings.cdk_default_region,
            },
            secrets={
                "GOOGLE_CLIENT_SECRET": ecs.Secret.from_ssm_parameter(google_client_secret),
                "JWT_SIGNING_KEY": ecs.Secret.from_ssm_parameter(jwt_signing_key_param),
                "STORAGE_ENC_KEY": ecs.Secret.from_ssm_parameter(storage_enc_key_param),
            },
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    "python -c 'import urllib.request,sys; "
                    "sys.exit(0 if urllib.request.urlopen(\"http://127.0.0.1:8080/health\", timeout=2).status==200 else 1)'",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(30),
            ),
        )
        container.add_port_mappings(
            ecs.PortMapping(container_port=_CONTAINER_PORT, protocol=ecs.Protocol.TCP)
        )

        oauth_state_table.grant_read_write_data(task_def.task_role)
        # py-key-value-aio's DynamoDBStore probes the table on first use.
        # grant_read_write_data covers PutItem/GetItem/etc but not these:
        #   - DescribeTable           (one-shot existence check)
        #   - DescribeTimeToLive      (verify TTL is configured)
        # UpdateTimeToLive is intentionally NOT granted: CDK already enables
        # TTL on the table, so the store's "if DISABLED, enable" branch
        # never fires.  If TTL ever needs reconfiguring, do it via CDK,
        # not by re-granting Update perms here.
        task_def.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:DescribeTable",
                    "dynamodb:DescribeTimeToLive",
                ],
                resources=[oauth_state_table.table_arn],
            )
        )
        results_bucket.grant_read(task_def.task_role)

        # --- Security groups -------------------------------------------
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=vpc,
            description="ALB ingress for MCP Fargate",
            allow_all_outbound=True,
        )
        # TODO: tighten ALB ingress to Anthropic published IP ranges.
        # For now, leaving 0.0.0.0/0 open — FastMCP OAuth still gates
        # everything behind Bearer tokens, so this is a "TLS terminator
        # exposed to internet" not an authenticated endpoint.
        alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS from internet (TODO: restrict to Anthropic CIDRs)",
        )
        alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(80),
            "HTTP for redirect to 443",
        )

        service_sg = ec2.SecurityGroup(
            self,
            "ServiceSg",
            vpc=vpc,
            description="Fargate service ingress (ALB only)",
            allow_all_outbound=True,
        )
        service_sg.add_ingress_rule(
            alb_sg,
            ec2.Port.tcp(_CONTAINER_PORT),
            "ALB to container 8080",
        )

        # --- Fargate service -------------------------------------------
        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            assign_public_ip=False,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[service_sg],
            min_healthy_percent=100,
            max_healthy_percent=200,
            health_check_grace_period=Duration.seconds(60),
        )

        # --- ALB --------------------------------------------------------
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
        )

        # 80 → 443 redirect
        alb.add_redirect(
            source_port=80,
            source_protocol=elbv2.ApplicationProtocol.HTTP,
            target_port=443,
            target_protocol=elbv2.ApplicationProtocol.HTTPS,
        )

        https_listener = alb.add_listener(
            "HttpsListener",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
        )

        target_group = https_listener.add_targets(
            "FargateTarget",
            port=_CONTAINER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service],
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
            deregistration_delay=Duration.seconds(15),
        )
        # Sticky-ish: long MCP sessions benefit from landing on the same
        # task.  Currently desired_count=1 makes this moot, but keep the
        # cookie-based stickiness on so scaling out later doesn't break
        # in-flight OAuth flows that span multiple HTTP requests.
        target_group.enable_cookie_stickiness(Duration.hours(1))

        # --- Route53 alias ---------------------------------------------
        route53.ARecord(
            self,
            "AliasRecord",
            zone=hosted_zone,
            record_name=settings.mcp_public_hostname,
            target=route53.RecordTarget.from_alias(
                route53_targets.LoadBalancerTarget(alb)
            ),
        )

        # --- Outputs ---------------------------------------------------
        cdk.CfnOutput(self, "PublicUrl", value=public_base_url)
        cdk.CfnOutput(self, "McpEndpoint", value=f"{public_base_url}/mcp")
        cdk.CfnOutput(
            self,
            "OAuthCallbackToRegisterInGoogle",
            value=f"{public_base_url}/auth/callback",
            description="Add this exactly to GCP OAuth client -> Authorized redirect URIs",
        )
        cdk.CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=service.service_name)
        cdk.CfnOutput(
            self,
            "GoogleClientSecretParamName",
            value=_SSM_GOOGLE_CLIENT_SECRET,
            description="SSM SecureString — must be pre-populated before deploy",
        )
        cdk.CfnOutput(
            self,
            "JwtSigningKeyParamName",
            value=_SSM_JWT_SIGNING_KEY,
            description="SSM SecureString — must be pre-populated before deploy",
        )
        cdk.CfnOutput(
            self,
            "StorageEncKeyParamName",
            value=_SSM_STORAGE_ENC_KEY,
            description="SSM SecureString — must hold a Fernet.generate_key() value",
        )
        cdk.CfnOutput(
            self,
            "OAuthStateTableName",
            value=oauth_state_table.table_name,
        )
        cdk.CfnOutput(
            self,
            "ResultsBucketName",
            value=results_bucket.bucket_name,
        )
