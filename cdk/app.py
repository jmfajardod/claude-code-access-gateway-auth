#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.mcp_auth_spike_stack import McpAuthSpikeStack
from stacks.mcp_lambda_spike_stack import McpLambdaSpikeStack
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

McpLambdaSpikeStack(
    app,
    "McpLambdaSpikeStack",
    settings=settings,
    env=env,
)

app.synth()
