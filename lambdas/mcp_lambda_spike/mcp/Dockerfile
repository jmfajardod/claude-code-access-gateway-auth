# Container image for the MCP Lambda using AWS Lambda Web Adapter (LWA).
#
# Why LWA: FastMCP exposes a Starlette ASGI app for streamable-HTTP transport.
# LWA lets us run any HTTP server inside Lambda by translating API Gateway
# events to local HTTP requests — no Mangum, no code changes to the ASGI app.
# It also supports Lambda response streaming, which the MCP streamable-HTTP
# transport relies on for long-lived responses.
#
# Layers:
#   1. python:3.13-slim                  : minimal base
#   2. /opt/extensions/lambda-adapter    : LWA binary copied in from upstream
#   3. /var/task                         : our app code + pip-installed deps
#
# Env knobs LWA reads:
#   PORT                       : where uvicorn listens (default 8080)
#   AWS_LWA_INVOKE_MODE        : "response_stream" enables streaming responses

FROM public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 AS lwa

FROM public.ecr.aws/docker/library/python:3.13-slim

COPY --from=lwa /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /var/task

# Install dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_server.py run.sh ./
RUN chmod +x ./run.sh

ENV PORT=8080 \
    AWS_LWA_INVOKE_MODE=response_stream \
    PYTHONUNBUFFERED=1

# LWA discovers and runs the entrypoint via this command.
CMD ["./run.sh"]
