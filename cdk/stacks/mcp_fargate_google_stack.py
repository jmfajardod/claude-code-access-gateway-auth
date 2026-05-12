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

import time

import aws_cdk as cdk
from aws_cdk import (
    Aws,
    Duration,
    RemovalPolicy,
    Stack,
    aws_athena as athena,
    aws_certificatemanager as acm,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_glue as glue,
    aws_iam as iam,
    aws_lakeformation as lakeformation,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

from assets.seed_sample_data import ensure_sample_csv
from stacks.settings import StackSettings

_SAMPLE_CSV_KEY = "sample_sales.csv"
_CONTAINER_PORT = 8080


# --- Glue / Athena / LakeFormation demo constants -------------------------
# These power the query_data_catalog MCP tool: a Glue database with two
# tables (one tagged LakehouseLayer=Gold, one tagged Bronze) so the tool's
# tag-based access control can be demonstrated end-to-end.
_GLUE_DATABASE_NAME = "mcp_poc_db"
_GLUE_TABLE_GOLD = "sales_gold"
_GLUE_TABLE_BRONZE = "sales_bronze"
_DATA_PREFIX_GOLD = "datasets/gold/sales/"
_DATA_PREFIX_BRONZE = "datasets/bronze/secret/"
_ATHENA_WORKGROUP_NAME = "mcp-poc-wg"
_ATHENA_RESULTS_PREFIX = "athena-results/"
_LF_TAG_KEY = "LakehouseLayer"
_LF_TAG_VALUES = ["Bronze", "Silver", "Gold", "Sandbox"]
_LF_TAG_GOLD = "Gold"
_LF_TAG_BRONZE = "Bronze"

# CSV schema — must match cdk/assets/sample_data/sample_sales.csv
_SALES_TABLE_COLUMNS = [
    glue.CfnTable.ColumnProperty(name="order_id", type="string"),
    glue.CfnTable.ColumnProperty(name="order_date", type="string"),
    glue.CfnTable.ColumnProperty(name="region", type="string"),
    glue.CfnTable.ColumnProperty(name="channel", type="string"),
    glue.CfnTable.ColumnProperty(name="product_category", type="string"),
    glue.CfnTable.ColumnProperty(name="product", type="string"),
    glue.CfnTable.ColumnProperty(name="quantity", type="int"),
    glue.CfnTable.ColumnProperty(name="unit_price", type="double"),
    glue.CfnTable.ColumnProperty(name="total_price", type="double"),
    glue.CfnTable.ColumnProperty(name="customer_id", type="string"),
]

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
        # Re-deploy the same CSV under structured prefixes so Glue/Athena
        # can register them as the Gold and Bronze table backings.
        s3_deployment.BucketDeployment(
            self,
            "GoldTableDataDeployment",
            sources=[s3_deployment.Source.asset(str(sample_data_dir))],
            destination_bucket=results_bucket,
            destination_key_prefix=_DATA_PREFIX_GOLD,
            prune=False,
            retain_on_delete=False,
        )
        s3_deployment.BucketDeployment(
            self,
            "BronzeTableDataDeployment",
            sources=[s3_deployment.Source.asset(str(sample_data_dir))],
            destination_bucket=results_bucket,
            destination_key_prefix=_DATA_PREFIX_BRONZE,
            prune=False,
            retain_on_delete=False,
        )

        # --- Athena results bucket -------------------------------------
        # Separate from results_bucket so Athena query output has its own
        # lifecycle/retention story. Athena writes intermediate CSV +
        # metadata files here on every StartQueryExecution call.
        athena_results_bucket = s3.Bucket(
            self,
            "AthenaResultsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAthenaResults",
                    enabled=True,
                    expiration=Duration.days(7),
                )
            ],
        )

        # --- Glue catalog ---------------------------------------------
        glue_db = glue.CfnDatabase(
            self,
            "GlueDatabase",
            catalog_id=Aws.ACCOUNT_ID,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=_GLUE_DATABASE_NAME,
                description="POC database for the query_data_catalog MCP tool",
            ),
        )

        def _build_table(
            construct_id: str, table_name: str, s3_prefix: str
        ) -> glue.CfnTable:
            table = glue.CfnTable(
                self,
                construct_id,
                catalog_id=Aws.ACCOUNT_ID,
                database_name=_GLUE_DATABASE_NAME,
                table_input=glue.CfnTable.TableInputProperty(
                    name=table_name,
                    table_type="EXTERNAL_TABLE",
                    parameters={
                        "classification": "csv",
                        "skip.header.line.count": "1",
                        "delimiter": ",",
                        "has_encrypted_data": "false",
                    },
                    storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                        columns=_SALES_TABLE_COLUMNS,
                        location=f"s3://{results_bucket.bucket_name}/{s3_prefix}",
                        input_format="org.apache.hadoop.mapred.TextInputFormat",
                        output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                        serde_info=glue.CfnTable.SerdeInfoProperty(
                            serialization_library="org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
                            parameters={
                                "field.delim": ",",
                                "serialization.format": ",",
                            },
                        ),
                    ),
                ),
            )
            table.add_dependency(glue_db)
            return table

        gold_table = _build_table("SalesGoldTable", _GLUE_TABLE_GOLD, _DATA_PREFIX_GOLD)
        bronze_table = _build_table(
            "SalesBronzeTable", _GLUE_TABLE_BRONZE, _DATA_PREFIX_BRONZE
        )

        # --- Athena workgroup ------------------------------------------
        athena_workgroup = athena.CfnWorkGroup(
            self,
            "AthenaWorkGroup",
            name=_ATHENA_WORKGROUP_NAME,
            recursive_delete_option=True,
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=False,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{athena_results_bucket.bucket_name}/{_ATHENA_RESULTS_PREFIX}",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_S3",
                    ),
                ),
            ),
        )

        # --- LakeFormation onboarding ----------------------------------
        # The account has not previously been onboarded to LakeFormation;
        # this block does the one-time setup. CRITICAL: making the CDK
        # CFN-exec role the data lake admin must precede any LF resource
        # creation (tag, registration, permissions). The bootstrap role
        # already has lakeformation:PutDataLakeSettings via its admin
        # policy, so this initial PutDataLakeSettings call succeeds.
        #
        # CreateTableDefaultPermissions / CreateDatabaseDefaultPermissions
        # are set to [] so newly created resources are NOT auto-granted to
        # "IAMAllowedPrincipals" (the legacy fallback). With those empty,
        # LF tag permissions are the sole authorization for our DB/tables.
        cfn_exec_role_arn = (
            f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:role/"
            f"cdk-hnb659fds-cfn-exec-role-{Aws.ACCOUNT_ID}-{Aws.REGION}"
        )

        # Lambda role for the AwsCustomResource that fixes two LF quirks:
        # 1. CfnDataLakeSettings.create_*_default_permissions=[] doesn't
        #    actually clear the defaults (CDK/CFN treats empty list like
        #    "no change" and AWS keeps IAMAllowedPrincipals: ALL as the
        #    default). We use PutDataLakeSettings directly to force it.
        # 2. Existing tables/db that were created before the (broken)
        #    clear took effect already carry IAMAllowedPrincipals grants.
        #    We BatchRevoke those explicitly.
        # Role needs to be a data lake admin to call those APIs, so we
        # include its ARN below in CfnDataLakeSettings.admins.
        lf_fix_role = iam.Role(
            self,
            "LakeFormationFixRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
            description="Used by AwsCustomResource to enforce LF tag-only access",
        )
        lf_fix_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lakeformation:GetDataLakeSettings",
                    "lakeformation:PutDataLakeSettings",
                    "lakeformation:BatchRevokePermissions",
                    "lakeformation:ListPermissions",
                ],
                resources=["*"],
            )
        )

        # Full admin list — both CfnDataLakeSettings and the AwsCustomResource
        # below must declare the SAME set, otherwise the latter strips out
        # whichever admins the former added (PutDataLakeSettings replaces,
        # never merges). Account-ID-bearing ARNs are templated from the
        # CFN token Aws.ACCOUNT_ID so the source file stays account-agnostic.
        _extra_admin_arns: list[str] = []
        if settings.lf_admin_include_root:
            _extra_admin_arns.append(
                f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:root"
            )
        for path in settings.lf_extra_admin_iam_paths.split(","):
            path = path.strip()
            if path:
                _extra_admin_arns.append(
                    f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:{path}"
                )
        _all_admin_arns = [cfn_exec_role_arn, lf_fix_role.role_arn] + _extra_admin_arns
        lf_settings = lakeformation.CfnDataLakeSettings(
            self,
            "LakeFormationSettings",
            admins=[
                lakeformation.CfnDataLakeSettings.DataLakePrincipalProperty(
                    data_lake_principal_identifier=arn,
                )
                for arn in _all_admin_arns
            ],
            create_table_default_permissions=[],
            create_database_default_permissions=[],
        )
        # Ensure LF settings exist BEFORE we try to create the LF-Tag etc.
        glue_db.add_dependency(lf_settings)
        gold_table.add_dependency(lf_settings)
        bronze_table.add_dependency(lf_settings)

        # --- Force-clear IAMAllowedPrincipals defaults ----------------
        # Re-PUT the LF settings via boto3 (not CFN) with explicit empty
        # arrays. The AWS API honors empty arrays; the CFN resource type
        # does not (it treats empty as "unchanged"). Idempotent.
        _force_clear_sdk_call = cr.AwsSdkCall(
            service="LakeFormation",
            action="putDataLakeSettings",
            parameters={
                "DataLakeSettings": {
                    "DataLakeAdmins": [
                        {"DataLakePrincipalIdentifier": arn}
                        for arn in _all_admin_arns
                    ],
                    "CreateTableDefaultPermissions": [],
                    "CreateDatabaseDefaultPermissions": [],
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of("ForceClearLfDefaults"),
        )
        force_clear_defaults = cr.AwsCustomResource(
            self,
            "ForceClearLfDefaults",
            on_create=_force_clear_sdk_call,
            on_update=_force_clear_sdk_call,
            role=lf_fix_role,
            install_latest_aws_sdk=False,
        )
        force_clear_defaults.node.add_dependency(lf_settings)
        # Tables must be created AFTER defaults are cleared, otherwise they
        # inherit IAMAllowedPrincipals at creation time. Use node-level
        # dependency since AwsCustomResource isn't a CfnResource directly.
        glue_db.node.add_dependency(force_clear_defaults)
        gold_table.node.add_dependency(force_clear_defaults)
        bronze_table.node.add_dependency(force_clear_defaults)

        # --- Revoke leftover IAMAllowedPrincipals on existing resources -
        # First-deploy: nothing to revoke, batch-revoke returns "Failures"
        # entries but the call itself succeeds.
        # Subsequent deploys / re-runs: cleans up grants that were applied
        # before ForceClearLfDefaults took effect, OR that get re-added
        # by any out-of-band activity.
        #
        # NB: the physical_resource_id below includes a synth-time nonce so
        # CloudFormation treats the resource as "new" every deploy and
        # actually re-runs the revoke. With a constant ID, CFN sees the
        # parameters as unchanged and skips on_update entirely — which is
        # exactly the bug we hit during the bronze-bypass investigation.
        _lf_revoke_nonce = str(int(time.time()))
        revoke_iam_allowed = cr.AwsCustomResource(
            self,
            "RevokeIAMAllowedPrincipals",
            on_create=cr.AwsSdkCall(
                service="LakeFormation",
                action="batchRevokePermissions",
                parameters={
                    "Entries": [
                        {
                            "Id": "1",
                            "Principal": {
                                "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
                            },
                            "Resource": {
                                "Database": {
                                    "CatalogId": Aws.ACCOUNT_ID,
                                    "Name": _GLUE_DATABASE_NAME,
                                }
                            },
                            "Permissions": ["ALL"],
                        },
                        {
                            "Id": "2",
                            "Principal": {
                                "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
                            },
                            "Resource": {
                                "Table": {
                                    "CatalogId": Aws.ACCOUNT_ID,
                                    "DatabaseName": _GLUE_DATABASE_NAME,
                                    "Name": _GLUE_TABLE_GOLD,
                                }
                            },
                            "Permissions": ["ALL"],
                        },
                        {
                            "Id": "3",
                            "Principal": {
                                "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
                            },
                            "Resource": {
                                "Table": {
                                    "CatalogId": Aws.ACCOUNT_ID,
                                    "DatabaseName": _GLUE_DATABASE_NAME,
                                    "Name": _GLUE_TABLE_BRONZE,
                                }
                            },
                            "Permissions": ["ALL"],
                        },
                    ],
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"RevokeIAMAllowedPrincipals-{_lf_revoke_nonce}"
                ),
                ignore_error_codes_matching="EntityNotFoundException|InvalidInputException",
            ),
            on_update=cr.AwsSdkCall(
                service="LakeFormation",
                action="batchRevokePermissions",
                parameters={
                    "Entries": [
                        {
                            "Id": "1",
                            "Principal": {
                                "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
                            },
                            "Resource": {
                                "Database": {
                                    "CatalogId": Aws.ACCOUNT_ID,
                                    "Name": _GLUE_DATABASE_NAME,
                                }
                            },
                            "Permissions": ["ALL"],
                        },
                        {
                            "Id": "2",
                            "Principal": {
                                "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
                            },
                            "Resource": {
                                "Table": {
                                    "CatalogId": Aws.ACCOUNT_ID,
                                    "DatabaseName": _GLUE_DATABASE_NAME,
                                    "Name": _GLUE_TABLE_GOLD,
                                }
                            },
                            "Permissions": ["ALL"],
                        },
                        {
                            "Id": "3",
                            "Principal": {
                                "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
                            },
                            "Resource": {
                                "Table": {
                                    "CatalogId": Aws.ACCOUNT_ID,
                                    "DatabaseName": _GLUE_DATABASE_NAME,
                                    "Name": _GLUE_TABLE_BRONZE,
                                }
                            },
                            "Permissions": ["ALL"],
                        },
                    ],
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"RevokeIAMAllowedPrincipals-{_lf_revoke_nonce}"
                ),
                ignore_error_codes_matching="EntityNotFoundException|InvalidInputException",
            ),
            role=lf_fix_role,
            install_latest_aws_sdk=False,
        )
        # Must run AFTER tables exist (so the resources are valid revoke
        # targets) and AFTER force_clear_defaults (so new resources from
        # this deploy don't get re-granted).
        # Same node-level dependency reason as above (AwsCustomResource is
        # a high-level construct, so we use .node.add_dependency for both
        # sides of any cross-construct dependency).
        revoke_iam_allowed.node.add_dependency(force_clear_defaults)
        revoke_iam_allowed.node.add_dependency(gold_table)
        revoke_iam_allowed.node.add_dependency(bronze_table)
        revoke_iam_allowed.node.add_dependency(glue_db)

        lf_tag = lakeformation.CfnTag(
            self,
            "LakehouseLayerTag",
            catalog_id=Aws.ACCOUNT_ID,
            tag_key=_LF_TAG_KEY,
            tag_values=_LF_TAG_VALUES,
        )
        lf_tag.add_dependency(lf_settings)

        # Register the data S3 prefix with LakeFormation so it controls
        # data access via GetDataAccess vended credentials. Without this,
        # LF tag permissions exist but Athena still uses raw S3 IAM.
        lf_data_location = lakeformation.CfnResource(
            self,
            "LakeFormationDataLocation",
            resource_arn=f"arn:{Aws.PARTITION}:s3:::{results_bucket.bucket_name}",
            use_service_linked_role=True,
        )
        lf_data_location.add_dependency(lf_settings)

        # --- Tag associations ------------------------------------------
        # Tag the database itself so the task role can DESCRIBE it via the
        # same Gold tag expression. Tables inherit this tag by default, but
        # the explicit per-table associations below override the inherited
        # value for the Bronze table (proving access-control works).
        db_tag_assoc = lakeformation.CfnTagAssociation(
            self,
            "GlueDatabaseTagAssociation",
            lf_tags=[
                lakeformation.CfnTagAssociation.LFTagPairProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    tag_key=_LF_TAG_KEY,
                    tag_values=[_LF_TAG_GOLD],
                )
            ],
            resource=lakeformation.CfnTagAssociation.ResourceProperty(
                database=lakeformation.CfnTagAssociation.DatabaseResourceProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    name=_GLUE_DATABASE_NAME,
                ),
            ),
        )
        db_tag_assoc.add_dependency(lf_tag)
        db_tag_assoc.add_dependency(glue_db)

        gold_tag_assoc = lakeformation.CfnTagAssociation(
            self,
            "SalesGoldTagAssociation",
            lf_tags=[
                lakeformation.CfnTagAssociation.LFTagPairProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    tag_key=_LF_TAG_KEY,
                    tag_values=[_LF_TAG_GOLD],
                )
            ],
            resource=lakeformation.CfnTagAssociation.ResourceProperty(
                table=lakeformation.CfnTagAssociation.TableResourceProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    database_name=_GLUE_DATABASE_NAME,
                    name=_GLUE_TABLE_GOLD,
                ),
            ),
        )
        gold_tag_assoc.add_dependency(lf_tag)
        gold_tag_assoc.add_dependency(gold_table)

        bronze_tag_assoc = lakeformation.CfnTagAssociation(
            self,
            "SalesBronzeTagAssociation",
            lf_tags=[
                lakeformation.CfnTagAssociation.LFTagPairProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    tag_key=_LF_TAG_KEY,
                    tag_values=[_LF_TAG_BRONZE],
                )
            ],
            resource=lakeformation.CfnTagAssociation.ResourceProperty(
                table=lakeformation.CfnTagAssociation.TableResourceProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    database_name=_GLUE_DATABASE_NAME,
                    name=_GLUE_TABLE_BRONZE,
                ),
            ),
        )
        bronze_tag_assoc.add_dependency(lf_tag)
        bronze_tag_assoc.add_dependency(bronze_table)

        # --- Container image -------------------------------------------
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "ServerImage",
            directory="../lambdas/data_reports_mcp_server",
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
                # IS_PROD=true makes the prod-style server (data_reports_mcp_server)
                # wire up GoogleProvider auth instead of the no-auth dev fallback.
                "IS_PROD": "true",
                "PUBLIC_BASE_URL": public_base_url,
                "GOOGLE_CLIENT_ID": settings.google_client_id,
                "ALLOWED_WORKSPACE_DOMAINS": settings.allowed_workspace_domains,
                "DDB_TABLE_NAME": oauth_state_table.table_name,
                "RESULTS_BUCKET": results_bucket.bucket_name,
                "RESULTS_OBJECT_KEY": _SAMPLE_CSV_KEY,
                "PRESIGNED_URL_TTL_SECONDS": "300",
                "AWS_REGION": settings.cdk_default_region,
                # Athena / Glue config consumed by the query_data_catalog tool.
                # No default database — callers fully-qualify `db.table` (or
                # pass `database=` to the tool) because LF-tag grants span
                # every Gold-tagged DB in the catalog.
                "ATHENA_WORKGROUP": athena_workgroup.ref,
                "ATHENA_RESULTS_BUCKET": athena_results_bucket.bucket_name,
                "ATHENA_QUERY_TIMEOUT_SECONDS": "600",
                "ATHENA_INLINE_MAX_BYTES": "102400",
                "ATHENA_INLINE_MAX_ROWS": "1000",
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
        # NOTE: we intentionally do NOT do `results_bucket.grant_read(task_role)`.
        # That would give the task role direct s3:GetObject on the entire data
        # bucket, including the Bronze prefix — and Athena would then read S3
        # directly, bypassing lakeformation:GetDataAccess and thus the LF tag
        # policy. With this grant removed, Athena must call GetDataAccess to
        # obtain vended credentials for each table, which is where the tag
        # policy is enforced.
        #
        # If a future tool needs to read the seed CSV from `results_bucket`
        # directly (e.g., the legacy query_data tool), grant access to that
        # specific OBJECT, not the whole bucket — e.g.:
        #   results_bucket.grant_read(task_def.task_role, _SAMPLE_CSV_KEY)

        # --- Athena / Glue / LakeFormation perms for query_data_catalog tool
        # IAM only opens the door — actual table access is gated by the LF
        # principal-permissions grant below, which restricts to resources
        # tagged LakehouseLayer=Gold.
        athena_results_bucket.grant_read_write(task_def.task_role)
        task_def.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "athena:StartQueryExecution",
                    "athena:GetQueryResults",
                    "athena:GetQueryExecution",
                    "athena:GetWorkGroup",
                    "athena:StopQueryExecution",
                ],
                resources=[
                    f"arn:{Aws.PARTITION}:athena:{Aws.REGION}:{Aws.ACCOUNT_ID}:workgroup/{_ATHENA_WORKGROUP_NAME}",
                ],
            )
        )
        task_def.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetPartition",
                    "glue:GetPartitions",
                ],
                resources=[
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:catalog",
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:database/{_GLUE_DATABASE_NAME}",
                    f"arn:{Aws.PARTITION}:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:table/{_GLUE_DATABASE_NAME}/*",
                ],
            )
        )
        task_def.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lakeformation:GetDataAccess"],
                resources=["*"],
            )
        )

        # --- LakeFormation tag-based permission grant -------------------
        # Grant the task role SELECT+DESCRIBE on TABLE resources tagged
        # LakehouseLayer=Gold, plus DESCRIBE on databases tagged Gold.
        # The Bronze table is intentionally left ungranted — Athena will
        # return AccessDeniedException when the tool queries it, proving
        # tag-based access control works end-to-end.
        gold_expression = [
            lakeformation.CfnPrincipalPermissions.LFTagProperty(
                tag_key=_LF_TAG_KEY,
                tag_values=[_LF_TAG_GOLD],
            ),
        ]
        gold_table_perm = lakeformation.CfnPrincipalPermissions(
            self,
            "GoldTableTagPermission",
            principal=lakeformation.CfnPrincipalPermissions.DataLakePrincipalProperty(
                data_lake_principal_identifier=task_def.task_role.role_arn,
            ),
            resource=lakeformation.CfnPrincipalPermissions.ResourceProperty(
                lf_tag_policy=lakeformation.CfnPrincipalPermissions.LFTagPolicyResourceProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    resource_type="TABLE",
                    expression=gold_expression,
                ),
            ),
            permissions=["SELECT", "DESCRIBE"],
            permissions_with_grant_option=[],
        )
        gold_table_perm.add_dependency(lf_tag)
        gold_table_perm.add_dependency(gold_tag_assoc)

        gold_db_perm = lakeformation.CfnPrincipalPermissions(
            self,
            "GoldDatabaseTagPermission",
            principal=lakeformation.CfnPrincipalPermissions.DataLakePrincipalProperty(
                data_lake_principal_identifier=task_def.task_role.role_arn,
            ),
            resource=lakeformation.CfnPrincipalPermissions.ResourceProperty(
                lf_tag_policy=lakeformation.CfnPrincipalPermissions.LFTagPolicyResourceProperty(
                    catalog_id=Aws.ACCOUNT_ID,
                    resource_type="DATABASE",
                    expression=gold_expression,
                ),
            ),
            permissions=["DESCRIBE"],
            permissions_with_grant_option=[],
        )
        gold_db_perm.add_dependency(lf_tag)
        gold_db_perm.add_dependency(db_tag_assoc)

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
