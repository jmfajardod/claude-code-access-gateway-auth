#!/bin/sh
# AWS Lambda Web Adapter entrypoint.  Starts uvicorn on the LWA-expected port,
# proxying API Gateway events to the FastMCP ASGI app at /mcp.  Single worker
# (Lambda already runs one request per execution; multiple workers would only
# increase memory).  No --reload — production lifecycle is per-invocation.
exec uvicorn mcp_server:app \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --workers 1 \
  --log-level info
