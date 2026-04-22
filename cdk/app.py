#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.mcp_auth_spike_stack import McpAuthSpikeStack
from stacks.settings import StackSettings

settings = StackSettings()

app = cdk.App()

McpAuthSpikeStack(
    app,
    "McpAuthSpikeStack",
    settings=settings,
    env=cdk.Environment(
        account=settings.cdk_default_account or None,
        region=settings.cdk_default_region,
    ),
)

app.synth()
