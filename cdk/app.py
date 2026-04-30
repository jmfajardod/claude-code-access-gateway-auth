#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.mcp_auth_spike_stack import McpAuthSpikeStack
from stacks.mcp_fargate_google_stack import McpFargateGoogleStack
from stacks.settings import StackSettings

settings = StackSettings()

app = cdk.App()

env = cdk.Environment(
    account=settings.cdk_default_account or None,
    region=settings.cdk_default_region,
)

McpAuthSpikeStack(
    app,
    "McpAuthSpikeStack",
    settings=settings,
    env=env,
)

# McpFargateGoogleStack is opt-in: instantiate only when the hostname/zone
# are configured.  Route53 HostedZone.from_lookup also requires the env
# (account+region) to be concrete, not derived from the CLI at synth time.
if settings.mcp_public_hostname and settings.route53_hosted_zone_name:
    McpFargateGoogleStack(
        app,
        "McpFargateGoogleStack",
        settings=settings,
        env=env,
    )

app.synth()
