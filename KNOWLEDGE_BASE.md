# Knowledge Base

A reference companion to `README.md`. The README is narrative + setup walkthrough; this file is a structured digest you can navigate by section.

---

## 1. Project at a glance

| Field | Value |
|---|---|
| Name | `claude-code-access-gateway-auth` |
| Goal | Front MCP servers with OAuth 2.0 + PKCE via Stytch B2B (Google login). |
| Stacks | **Two coexisting stacks**: `McpAuthSpikeStack` (AgentCore-based) and `McpLambdaSpikeStack` (FastMCP-on-Lambda). |
| Auth model | Stytch B2B Discovery → Stytch Connected Apps → JWT validated server-side. |
| Tools | Same three across both stacks: `get_weather`, `get_time`, `query_data`. |
| Access control | Scope-based RBAC, **default-deny on both stacks**. AgentCore stack: REQUEST/RESPONSE interceptor Lambdas. FastMCP stack: in-process `_ensure_scope` + `tools/list` middleware. |
| Out of scope (intentional) | Cognito, CloudFront, HTML pages, custom session store, Mangum, custom Lambda authorizer, DynamoDB. |
| Runtime | Python 3.13 (Lambdas), CDK v2.248.0, Node 18+ for the CDK CLI. |
| Package manager | `uv`, dep groups `[cdk]` and `[lambdas]` in `pyproject.toml`. |
| Region | Deploys to `us-east-1` by default (`StackSettings.cdk_default_region`). |

### Stack comparison

| Aspect | `McpAuthSpikeStack` | `McpLambdaSpikeStack` |
|---|---|---|
| MCP runtime | Bedrock AgentCore Gateway (managed) | Lambda + FastMCP (Docker image + LWA) |
| Tool schema | Inline `CfnGatewayTarget` payload | Python `@mcp.tool()` decorators |
| JWT validation | AgentCore `CUSTOM_JWT` authorizer | FastMCP `JWTVerifier` (Stytch JWKS, in-process) |
| `tools/call` scope check | REQUEST interceptor Lambda | `_ensure_scope()` inside the tool body |
| `tools/list` filter | RESPONSE interceptor Lambda | `ScopeFilterMiddleware` in FastMCP |
| HTTP-proxy unwrap | RESPONSE interceptor Lambda | n/a — FastMCP returns native shapes |
| `query_data` resource_link | RESPONSE interceptor Lambda | Returned directly by the tool |
| Default scope policy | Default-deny | Default-deny |
| Tool name visible to client | `DummyToolsTarget___<tool>` (triple underscore) | `<tool>` (no prefix) |
| RFC 9728 PRM | Hosted by OAuth Lambda | Auto-hosted by FastMCP `RemoteAuthProvider` |
| MCP endpoint URL | `<gateway>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp` | `<api>.execute-api.<region>.amazonaws.com/mcp` |
| Stytch app | Same Connected App reused | Same Connected App reused |
| AWS resources | Independent | Independent (own SSM, bucket, OAuth Lambda) |

---

## 2. Repository layout

```
.
├── cdk/
│   ├── app.py                            # Instantiates BOTH stacks
│   ├── cdk.json                          # `app: python3 app.py`
│   ├── requirements.txt                  # CDK deps (aws-cdk-lib==2.248.0, pydantic-settings)
│   ├── assets/                           # Used by McpAuthSpikeStack
│   │   ├── seed_sample_data.py
│   │   └── sample_data/
│   ├── assets_mcp_lambda_spike/          # Used by McpLambdaSpikeStack (independent)
│   │   ├── seed_sample_data.py
│   │   └── sample_data/
│   └── stacks/
│       ├── mcp_auth_spike_stack.py       # AgentCore stack
│       ├── mcp_lambda_spike_stack.py     # FastMCP-on-Lambda stack
│       └── settings.py                   # pydantic-settings reader (fields for both stacks)
├── lambdas/
│   ├── oauth_server.py                   # OAuth Lambda — McpAuthSpikeStack
│   ├── request_interceptor.py            # AgentCore REQUEST hook — McpAuthSpikeStack only
│   ├── response_interceptor.py           # AgentCore RESPONSE hook — McpAuthSpikeStack only
│   ├── target.py                         # AgentCore target — McpAuthSpikeStack only
│   ├── requirements.txt
│   └── mcp_lambda_spike/                 # Independent code tree for McpLambdaSpikeStack
│       ├── oauth/
│       │   ├── oauth_server.py           # Independent OAuth Lambda copy
│       │   └── requirements.txt
│       └── mcp/
│           ├── mcp_server.py             # FastMCP app + JWTVerifier + tools
│           ├── Dockerfile                # Lambda Web Adapter container
│           ├── run.sh                    # uvicorn entrypoint
│           └── requirements.txt          # fastmcp, boto3, uvicorn
├── .env                                  # Secrets (gitignored)
├── .env.example                          # Template
├── pyproject.toml                        # uv project, deps grouped [cdk] + [lambdas]
├── uv.lock
└── README.md                             # Long-form spec with mermaid diagrams
```

---

## 3. Component map

### 3.1 AWS resources — `McpAuthSpikeStack` (`cdk/stacks/mcp_auth_spike_stack.py`)

| Resource | CDK construct | Purpose |
|---|---|---|
| SSM SecureString param | `ssm.StringParameter.from_secure_string_parameter_attributes` | Holds the Stytch project secret at `/mcp-spike/stytch/project_secret`. Created out-of-band. |
| IAM `GatewayRole` | `iam.Role` | Trust principal `bedrock-agentcore.amazonaws.com`. Allows `lambda:InvokeFunction`, `lambda:GetFunction`, `iam:PassRole` on `*`. |
| OAuth Server Lambda | `lambda_.Function` (`oauth_server.handler`) | All OAuth/OIDC endpoints. py3.13, 1 min timeout, 256 MB. |
| API Gateway v2 HTTP API | `apigwv2.HttpApi` | Public HTTPS front for OAuth Lambda. CORS=`*`. Default integration → OAuth Lambda. |
| Request Interceptor Lambda | `lambda_.Function` (`request_interceptor.handler`) | AgentCore REQUEST hook. |
| Response Interceptor Lambda | `lambda_.Function` (`response_interceptor.handler`) | AgentCore RESPONSE hook. |
| S3 `QueryDataResultsBucket` | `s3.Bucket` | Encrypted, SSL-only, `auto_delete_objects=True`, `RemovalPolicy.DESTROY`. |
| Bucket seed | `s3_deployment.BucketDeployment` | Uploads `cdk/assets/sample_data/sample_sales.csv`. |
| Target Lambda | `lambda_.Function` (`target.handler`) | The MCP backend. py3.13, 5 min, 256 MB. Has `s3:GetObject` on the bucket. |
| AgentCore Gateway | `agentcore.CfnGateway` | `protocol_type=MCP`, `supported_versions=["2025-11-25"]`, `authorizer_type=CUSTOM_JWT`. |
| AgentCore Gateway Target | `agentcore.CfnGatewayTarget` (`DummyToolsTarget`) | `credential_provider_type=GATEWAY_IAM_ROLE`, inline tool schema for the 3 tools. |

CFN outputs: `OAuthServerUrl`, `GatewayArn`, `QueryDataResultsBucketName`, `RequestInterceptorArn`, `ResponseInterceptorArn`.

### 3.1b AWS resources — `McpLambdaSpikeStack` (`cdk/stacks/mcp_lambda_spike_stack.py`)

| Resource | CDK construct | Purpose |
|---|---|---|
| SSM SecureString param | `ssm.StringParameter.from_secure_string_parameter_attributes` | Stytch secret at `/mcp-lambda-spike/stytch/project_secret`. Independent from the legacy stack. |
| OAuth Server Lambda | `lambda_.Function` (`oauth_server.handler`) | Independent zip-bundled copy of the OAuth Lambda. py3.13, 1 min, 256 MB. |
| MCP Server Lambda | `lambda_.DockerImageFunction` | FastMCP via AWS Lambda Web Adapter. py3.13-slim base + LWA layer + uvicorn. 1024 MB, 30 s. |
| S3 `QueryDataResultsBucket` | `s3.Bucket` | Independent bucket. Encrypted, SSL-only, auto-delete. |
| Bucket seed | `s3_deployment.BucketDeployment` | Uploads `cdk/assets_mcp_lambda_spike/sample_data/sample_sales.csv`. |
| `apigwv2.HttpApi` | Single HTTP API | CORS=`*`. Default → OAuth Lambda; explicit overrides → MCP Lambda. |
| Route → OAuth Lambda | `$default` | Catches `/.well-known/openid-configuration`, `/.well-known/oauth-authorization-server`, `/register`, `/oauth/authorize`, `/login`, `/login/callback`. |
| Route → MCP Lambda | `ANY /mcp`, `ANY /mcp/{proxy+}` | MCP transport endpoints. |
| Route → MCP Lambda | `GET /.well-known/oauth-protected-resource`, `GET /.well-known/oauth-protected-resource/{proxy+}` | RFC 9728 PRM (FastMCP `RemoteAuthProvider` mounts the route at `/.well-known/oauth-protected-resource/mcp`). |

CFN outputs: `McpOAuthServerUrl`, `McpServerUrl`, `QueryDataResultsBucketName`, `OAuthLambdaArn`, `McpLambdaArn`.

### 3.2 Lambda code

**McpAuthSpikeStack (legacy/AgentCore):**

| File | Handler | Purpose |
|---|---|---|
| `lambdas/oauth_server.py` | `handler` | OIDC + AS metadata, DCR, login orchestration, authorization-code issuance. |
| `lambdas/request_interceptor.py` | `handler` | Decode JWT, gate `tools/call` by `scope` claim. |
| `lambdas/response_interceptor.py` | `handler` | Filter `tools/list`, unwrap HTTP-proxy, inject `resource_link` for `query_data`. |
| `lambdas/target.py` | `handler` | Implement `get_weather`, `get_time`, `query_data`. |

**McpLambdaSpikeStack (FastMCP-on-Lambda):**

| File | Entrypoint | Purpose |
|---|---|---|
| `lambdas/mcp_lambda_spike/oauth/oauth_server.py` | `handler` | Independent copy of the OAuth Lambda — same code, different SSM/URL env values. |
| `lambdas/mcp_lambda_spike/mcp/mcp_server.py` | `app` (ASGI) | FastMCP server with `JWTVerifier`, `RemoteAuthProvider`, three tools, `ScopeFilterMiddleware`. |
| `lambdas/mcp_lambda_spike/mcp/Dockerfile` | — | py3.13-slim + AWS Lambda Web Adapter (`/opt/extensions/lambda-adapter`). |
| `lambdas/mcp_lambda_spike/mcp/run.sh` | — | uvicorn launcher (`uvicorn mcp_server:app --host 0.0.0.0 --port 8080 --workers 1`). |

### 3.3 Configuration plane

| File | Role |
|---|---|
| `.env` | Stytch IDs, post-deploy URLs (one set per stack), AWS creds. Not committed. |
| `.env.example` | Template — see §6. |
| `cdk/stacks/settings.py` | `pydantic-settings` reader. Fields for both stacks. |
| SSM `/mcp-spike/stytch/project_secret` | Stytch secret for `McpAuthSpikeStack`. |
| SSM `/mcp-lambda-spike/stytch/project_secret` | Stytch secret for `McpLambdaSpikeStack`. |
| Stytch Dashboard | Same Connected App + RBAC policy, but the Authorization URL and top-level Redirect URLs change depending on which stack you're testing against. |

---

## 4. End-to-end OAuth flow

Authoritative diagram: `README.md` lines 57–115. Plain-prose recap:

1. Client opens MCP at the AgentCore Gateway URL with no token.
2. AgentCore returns `401` with `WWW-Authenticate: Bearer resource_metadata=<URL>`.
3. Client fetches `<gateway>/.well-known/oauth-protected-resource` → it advertises Stytch (`STYTCH_PROJECT_DOMAIN`) as the authorization server.
4. Client fetches Stytch's own `/.well-known/openid-configuration` → discovers `authorization_endpoint = <OAUTH_LAMBDA_URL>/oauth/authorize`, `token_endpoint = <STYTCH>/v1/oauth2/token`.
5. Client `POST /register` (DCR) at the OAuth Lambda → gets the static `CONNECTED_APP_CLIENT_ID`.
6. Client opens browser at `/oauth/authorize?…&code_challenge=…&state=…`.
7. OAuth Lambda has no `stytch_session_jwt` cookie → `302 → /login?returnTo=<current_url>`.
8. `/login` builds the public Stytch B2B Google Discovery start URL → `302 → Google`.
9. User authenticates with Google → Stytch callback → `302 → /login/callback?token=<discovery_token>`.
10. `/login/callback` runs `oauth.discovery.authenticate()` then `discovery.intermediate_sessions.exchange()` (with JIT fallback to first discovered org) → sets `stytch_session_jwt` cookie → `302 → returnTo`.
11. Client lands back at `/oauth/authorize` (now has cookie). OAuth Lambda **augments scopes** (appends `_CUSTOM_SCOPES` to the requested set), calls `idp.oauth.authorize_start()` then `idp.oauth.authorize()` with PKCE `code_challenge` and `state` → `302 → <client redirect_uri>?code=…&state=…`.
12. Client exchanges code at Stytch `/v1/oauth2/token` (PKCE verifier in body) → receives Connected App access token (JWT).
13. Client retries MCP with `Authorization: Bearer <jwt>`. AgentCore validates against Stytch JWKS.
14. REQUEST interceptor checks `scope` claim → either short-circuits with JSON-RPC error or forwards.
15. Target Lambda runs the tool, returns `{statusCode, body}` HTTP-proxy shape.
16. RESPONSE interceptor unwraps the shape and (for `query_data`) appends a `resource_link` content block.

---

## 5. Scope-based RBAC

### 5.1 Tool ↔ scope mapping (both stacks)

| Tool | Scope to call/show |
|---|---|
| `get_weather` | `tool:get_weather` |
| `get_time` | `tool:get_time` |
| `query_data` | `tool:query_data` |
| (any) | `tool:*` |

### 5.1a Default policy — default-deny on both stacks

| Stack | Token has no `tool:*` scopes | Token has matching `tool:<name>` | Token has `tool:*` | Tool not in `_TOOL_SCOPES` |
|---|---|---|---|---|
| `McpAuthSpikeStack` | All denied | That tool allowed | All allowed | Denied (must add to map) |
| `McpLambdaSpikeStack` | All denied | That tool allowed | All allowed | Denied (only registered tools exist) |

Implementation:
- AgentCore stack: `request_interceptor.py:_has_tool_scope` and `response_interceptor.py:_is_tool_visible` both check `tool:*` or `tool:<name>` and return `False` for unknown tools.
- FastMCP stack: `mcp_server.py:_ensure_scope` raises `ToolError` unless the matching scope is present.

**Setup gotcha.** Members without a Stytch RBAC role assigned will see an empty `tools/list` and get JSON-RPC errors on every `tools/call`. Configure the RBAC policy (resources → permissions → scopes → roles → assign to members) before users hit the endpoint, or set up a baseline role granting the scopes you want everyone to have. The OAuth Lambda's `_oauth_authorize` augments scopes server-side, but Stytch still intersects with the member's actual roles — augmentation alone doesn't grant access.

### 5.2 Where the rules live

**McpAuthSpikeStack:**

| File | What to update when adding/removing a tool |
|---|---|
| `lambdas/request_interceptor.py:31` (`_TOOL_SCOPES`) | Add the unprefixed name → required scope. |
| `lambdas/response_interceptor.py:55` (`_TOOL_SCOPES`) | Same map, kept in sync. |
| `lambdas/oauth_server.py:82` (`_DEFAULT_CUSTOM_SCOPES`) | Add to advertised scopes. |
| `cdk/stacks/mcp_auth_spike_stack.py` (`CfnGatewayTarget` inline schema) | Add the tool definition. |
| `lambdas/target.py` (`handler`) | Implement the branch. |
| Stytch Dashboard → RBAC | Add resource action, scope, role, member assignment. |

**McpLambdaSpikeStack:**

| File | What to update when adding/removing a tool |
|---|---|
| `lambdas/mcp_lambda_spike/mcp/mcp_server.py` (`_TOOL_SCOPES`) | Add the tool name → required scope. |
| `lambdas/mcp_lambda_spike/mcp/mcp_server.py` (`_ADVERTISED_SCOPES`) | Add to scopes advertised in PRM. |
| `lambdas/mcp_lambda_spike/mcp/mcp_server.py` (`@mcp.tool()` block) | Add the tool function (call `_ensure_scope` first). |
| `lambdas/mcp_lambda_spike/oauth/oauth_server.py:82` (`_DEFAULT_CUSTOM_SCOPES`) | Add to advertised AS metadata. |
| Stytch Dashboard → RBAC | Same as the legacy stack — both stacks share the Connected App. |

### 5.3 Stytch RBAC plumbing (Stytch Dashboard)

```
Resource (e.g. "tools") + Action (e.g. "get_weather")
  └─► Permission (tools.get_weather)
        ├─► Scope (e.g. "tool:get_weather")  ── included in JWT iff member's roles cover all permissions
        └─► Role  (e.g. "weather-user")      ── assigned to a Member of the Org
```

A token's `scope` claim is the intersection of (scopes the client/app sent) ∩ (scopes whose permissions are covered by the member's roles).

### 5.4 Server-side scope augmentation

Some clients (Claude Desktop's Custom Connectors UI, in particular) cannot configure custom scopes and ignore `scopes_supported` in DCR metadata, so they always request just `openid`. The OAuth Lambda compensates: `_oauth_authorize` (`oauth_server.py:491`) unconditionally appends every entry from `_CUSTOM_SCOPES` before forwarding to Stytch. Stytch RBAC still acts as the authoritative filter — users only get scopes their roles actually grant.

Tradeoff: a client cannot deliberately request *fewer* scopes than its member is entitled to (no client-driven least-privilege).

Override the augmentation set without redeploying via env var on the OAuth Lambda:
```
CUSTOM_SCOPES=tool:read,tool:write,tool:*
```

---

## 6. `.env` reference

Live values are in `.env` (gitignored). The template `.env.example` documents every key. Summary:

| Key | Source | Notes |
|---|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | AWS account | Choose ONE auth strategy. |
| `AWS_PROFILE` | Local AWS CLI | Alternative to access keys. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | — | Default `us-east-1`. |
| `CDK_DEFAULT_ACCOUNT` | AWS | Optional, falls back to caller identity. |
| `CDK_DEFAULT_REGION` | AWS | Defaults to `us-east-1`. |
| `STYTCH_PROJECT_ID` | Stytch Dashboard → API Keys | `project-test-…` or `project-live-…`. |
| `STYTCH_PROJECT_DOMAIN` | Stytch Dashboard → API Keys | `https://<slug>.customers.stytch.dev`, no trailing slash. |
| `STYTCH_ORG_ID` | Stytch Dashboard → Organizations | `organization-test-…`. |
| `STYTCH_PUBLIC_TOKEN` | Stytch Dashboard → API Keys | `public-token-test-…`. |
| `CONNECTED_APP_CLIENT_ID` | Stytch Dashboard → Connected Apps | `connected-app-test-…`. |
| `OAUTH_LAMBDA_URL` | CFN output `OAuthServerUrl` after first deploy of McpAuthSpikeStack | Filled in second pass. |
| `AGENTCORE_GATEWAY_URL` | Bedrock console → Gateway URL after first deploy of McpAuthSpikeStack | `https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`. Used as `allowed_audience`. |
| `MCP_OAUTH_LAMBDA_URL` | CFN output `McpOAuthServerUrl` after first deploy of McpLambdaSpikeStack | API Gateway base URL for the new stack's OAuth Lambda. |
| `MCP_SERVER_URL` | CFN output `McpServerUrl` after first deploy of McpLambdaSpikeStack | `<MCP_OAUTH_LAMBDA_URL>mcp`. JWT `aud` value the FastMCP server accepts. |

The Stytch project secret is **not** in `.env`. It lives in SSM:
- `/mcp-spike/stytch/project_secret` — McpAuthSpikeStack
- `/mcp-lambda-spike/stytch/project_secret` — McpLambdaSpikeStack

Both are SecureString. Independent (per Q11 — fully isolated stacks).

---

## 7. OAuth Lambda endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/.well-known/openid-configuration` | OIDC discovery doc with `issuer = STYTCH_PROJECT_DOMAIN`, `jwks_uri = <stytch>/.well-known/jwks.json`, `authorization_endpoint = <oauth-lambda>/oauth/authorize`. Read by AgentCore `CUSTOM_JWT` authorizer (note: the actual `discovery_url` configured on the Gateway points to **Stytch's own** OIDC doc, not this one — kept here for tooling that expects it). |
| GET | `/.well-known/oauth-authorization-server` | RFC 8414 AS metadata for MCP clients. |
| GET | `/.well-known/oauth-protected-resource` | RFC 9728 PRM. Declares Stytch as AS. |
| POST | `/register` | RFC 7591 DCR. Returns the static `CONNECTED_APP_CLIENT_ID` and echoes the requested `redirect_uris`. |
| GET | `/oauth/authorize` | Stytch Connected Apps Authorization URL. Gates on `stytch_session_jwt` cookie; on success calls `idp.oauth.authorize_start()` + `idp.oauth.authorize()`. |
| GET | `/login` | 302 → Stytch Google Discovery start. |
| GET | `/login/callback` | Exchanges Stytch discovery token → org session JWT, sets `stytch_session_jwt` cookie. |

Cookies: `stytch_session_jwt` (HttpOnly, SameSite=Lax, Secure, 1 h TTL); `mcp_return_to` (10 min TTL, cleared on callback).

Error classification (Stytch SDK exception → response):

| Class | Trigger | Behaviour |
|---|---|---|
| Session expired/invalid | `session_not_found`, `session_expired`, `invalid_session_jwt`, `invalid_session_token`, `session_jwt_expired`, `session_too_old_to_auth_factor`, `unauthorized_credentials` | Clear `stytch_session_jwt` cookie + 302 to `/login?returnTo=…`. |
| `redirect_uri` or `client_id` itself bad | `connected_app_supplied_redirect_url_not_found_in_client`, `connected_app_not_found`, `client_not_found`, `oauth_invalid_client`, missing `redirect_uri` | 400 JSON error. (RFC 6749 forbids redirecting to an untrusted URL.) |
| Client-input error with safe `redirect_uri` | `oauth_invalid_scope_requested`, `oauth_connected_app_scopes_required_but_not_granted`, `oauth_invalid_request`, `oauth_unauthorized_client`, `oauth_unsupported_response_type`, `oauth_access_denied`, `oauth_consent_required` | RFC 6749 §4.1.2.1 error redirect to `redirect_uri?error=<code>&error_description=…&state=…`. |
| Anything else | — | Error redirect with `error=server_error`. |

---

## 8. Tools

### 8.1 `get_weather`
- Input: `{ "location": "<string>" }`.
- Output: `"Weather in {location}: 72F, sunny"`. Canned response.

### 8.2 `get_time`
- Input: `{ "timezone": "<IANA name>" }`.
- Output: `"Current time in {tz}: YYYY-MM-DD HH:MM:SS TZ"`. Falls back to UTC on invalid zone.

### 8.3 `query_data` — hybrid envelope

No input args. Reads `sample_sales.csv` from `RESULTS_BUCKET`, computes summary stats, samples 50 rows, generates a 5-min presigned URL.

Envelope shape:

```json
{
  "status": "success",
  "row_count": 2000,
  "summary_stats": { "<col>": { /* numeric|date|string */ } },
  "sample_rows": [ { /* first 50 rows */ } ],
  "full_results": {
    "format": "csv",
    "size_bytes": 204512,
    "presigned_url": "https://...",
    "presigned_url_expires_at": "ISO-8601 Z",
    "presigned_url_ttl_seconds": 300,
    "inline": false
  },
  "next_steps": "<human prose telling Claude to suggest download+drag>",
  "_meta": { "tool_version": "1.0.0-phase1" }
}
```

`summary_stats` rules (per `_compute_summary_stats` and `_infer_column_type`):

| Column type | Fields produced |
|---|---|
| Integer / number | `min, max, mean, stddev, nulls` |
| Date (ISO) | `min, max, distinct, nulls` |
| String, distinct ≤ 30 | `distinct, nulls, top: [{ value, count }]` (top 5) |
| String, distinct > 30 | `distinct, nulls` (no `top`) |

Constants: `_SAMPLE_ROW_COUNT=50`, `_TOP_VALUES_MAX=5`, `_LOW_CARDINALITY_THRESHOLD=30`, `_TOOL_VERSION="1.0.0-phase1"`.

---

## 8.4 FastMCP differences (McpLambdaSpikeStack)

The new stack returns native MCP content blocks directly from each tool:

- `get_weather`, `get_time` → return a plain `str` (FastMCP wraps it as `TextContent`).
- `query_data` → returns `[TextContent(type="text", text=<json envelope>), ResourceLink(type="resource_link", uri=<presigned>, name="sample_sales.csv", mimeType="text/csv")]`. No HTTP-proxy wrapping, no response interceptor needed.

Tool name namespacing: **none** in this stack. Tools appear to clients as `get_weather`, `get_time`, `query_data` (no `<TargetName>___` prefix — that was an AgentCore convention).

Errors: `ToolError("…")` raised inside a tool body produces a JSON-RPC error result for that tool call. JWT validation failures (401) are emitted by FastMCP's `JWTVerifier` middleware before the tool body runs and include the `WWW-Authenticate: Bearer error="…", resource_metadata="<MCP_SERVER_URL>/.well-known/oauth-protected-resource"` header per RFC 9728.

## 9. Interceptor contract (McpAuthSpikeStack only)

### 9.1 Event shape (RESPONSE interceptor with `pass_request_headers=True`)

```
event["mcp"]["gatewayRequest"]["headers"]["authorization"]   <- JWT
event["mcp"]["gatewayRequest"]["body"]["method"]             <- e.g. "tools/list"
event["mcp"]["gatewayResponse"]["statusCode"]                <- upstream status
event["mcp"]["gatewayResponse"]["body"]                      <- JSON-RPC body (may be null)
```

### 9.2 Output envelope

REQUEST pass-through:
```json
{"interceptorOutputVersion":"1.0",
 "mcp":{"transformedGatewayRequest":{"headers":{...}, "body":{...}}}}
```

REQUEST short-circuit (e.g. unauthorized `tools/call`):
```json
{"interceptorOutputVersion":"1.0",
 "mcp":{"transformedGatewayResponse":{"statusCode":200,
   "body":{"jsonrpc":"2.0","id":<req_id>,"error":{"code":-32600,"message":"Insufficient scope for tool: ..."}}}}}
```

RESPONSE pass-through:
```json
{"interceptorOutputVersion":"1.0",
 "mcp":{"transformedGatewayResponse":{"statusCode":<status>, "body":<body>}}}
```

### 9.3 Tool name namespacing

AgentCore prefixes tool names with `<TargetName>___` (triple underscore) when emitting `tools/list` and `tools/call`. Example: `DummyToolsTarget___get_weather`. Both interceptors and the target Lambda strip this with `_unprefix_tool()` (`name.partition("___")`).

### 9.4 HTTP-proxy unwrap

The target Lambda returns `{"statusCode":200,"body":"<json-string>"}` (AWS HTTP-proxy shape). AgentCore does not unwrap it, so without intervention the client sees `result.content[0].text == '{"statusCode":200,"body":"<inner json>"}'`. `_unwrap_http_proxy_shape()` (`response_interceptor.py:109`) flattens it to just the inner `body` string when the shape exactly matches; otherwise pass-through.

### 9.5 `query_data` resource_link

`_append_query_data_resource_link()` (`response_interceptor.py:140`) extracts `full_results.presigned_url` from the unwrapped envelope and appends an MCP `resource_link` content block alongside the existing text block:

```json
{ "type": "resource_link",
  "uri": "<presigned URL>",
  "name": "sample_sales.csv",
  "description": "Full dataset (2,000 rows, ~200 KB)",
  "mimeType": "text/csv" }
```

**Status:** End-to-end accepted by AgentCore and Inspector. **Refused by Claude.ai Projects** ("Resource links are not currently supported", verified 2026-04-23). Left in place for future client support; remove with one line at `response_interceptor.py:262` if needed.

---

## 10. Sample seed data

Generated by `cdk/assets/seed_sample_data.py` (`random.Random(42)`, deterministic).

| Field | Domain |
|---|---|
| `order_id` | `ORD-00001` … `ORD-02000` |
| `order_date` | 2025-01-01 … 2025-12-31 (ISO date) |
| `region` | NA, EU, APAC, LATAM |
| `channel` | Online, In-Store, Mobile |
| `product_category` | Electronics, Clothing, Home, Sports, Books |
| `product` | 5 per category — see file for the list |
| `quantity` | 1–10 |
| `unit_price` | base × Uniform(0.85, 1.15), rounded to 2 dp |
| `total_price` | `quantity * unit_price`, rounded to 2 dp |
| `customer_id` | `CUST-0001` … `CUST-0250` (250 distinct customers) |

The fixed seed keeps the BucketDeployment asset hash stable across synths.

---

## 11. Setup checklist (canonical)

Pre-deploy in Stytch Dashboard:

1. Create B2B project (test env is fine). Note Project ID, Project Domain, Public Token, Secret.
2. Enable Google OAuth (provide Google Cloud OAuth client ID + secret).
3. Create an Organization. Note the Organization ID.
4. Add the Google account(s) you'll log in with as Members of that Org.
5. Create a **Public** Connected App (PKCE). Note the Client ID. Authorization URL can be a placeholder for now.
6. Register MCP client redirect URIs on the Connected App: Inspector `http://localhost:6274/oauth/callback`, Claude Desktop `https://claude.ai/api/mcp/auth_callback`.

Pre-deploy locally:

7. Put the Stytch secret into SSM:
   ```bash
   aws ssm put-parameter --name "/mcp-spike/stytch/project_secret" --type SecureString --value "<secret>"
   ```
8. `cp .env.example .env` and fill in the Stytch values (leave the URLs as placeholders).
9. First deploy:
   ```bash
   uv sync --all-groups && source .venv/bin/activate
   cd cdk && cdk bootstrap && cdk deploy
   ```

Post-deploy in Stytch Dashboard:

10. Set the Connected App **Authorization URL** to `<OAuthServerUrl>oauth/authorize` (value comes from the CFN output).
11. Add a Stytch top-level Redirect URL `<OAuthServerUrl>login/callback` (type Login, status Enabled). **This is a different page from step 6** — it lives at Dashboard → Redirect URLs, not inside the Connected App.

Post-deploy locally:

12. Fill in `OAUTH_LAMBDA_URL` and `AGENTCORE_GATEWAY_URL` in `.env`, then `cdk deploy` again.

Test:

13. MCP Inspector: `npx @modelcontextprotocol/inspector` → Streamable HTTP → `<gateway-url>/mcp` → Connect.
14. Claude Desktop: Settings → Connectors → Add custom connector → URL = `<gateway-url>/mcp`.

If the API Gateway URL changes (`cdk destroy`+redeploy, renaming the construct, etc.), redo steps 10 and 11 — Stytch's OIDC discovery doc echoes the Authorization URL into `authorization_endpoint`, and a stale value strands clients.

---

## 12. Known gotchas

| Symptom | Root cause / fix |
|---|---|
| `Got message null {"Message":null}` or `403 AccessDeniedException` after teardown/redeploy | Stytch Connected App Authorization URL or Redirect URLs in the Stytch Dashboard still point to the old API Gateway URL. Update both. |
| `invalid_intermediate_session_token_for_organization` on login | The Google account isn't a Member of the configured Stytch Org. Add the member, or rely on JIT (the Lambda already falls back to `discovered_organizations[0]` if `STYTCH_ORG_ID` isn't in the discovery list). |
| `connected_app_supplied_redirect_url_not_found_in_client` | The client's `redirect_uri` isn't in the Connected App's Redirect URIs list. Add it (Inspector and Claude Desktop have different URIs). |
| User stuck at "Choose an account to continue to stytch.com" loop | (Historical, fixed.) Was caused by the Lambda clearing the cookie on a non-session error. Now classified by Stytch `error_type` so only true session failures clear the cookie. Don't regress the classification logic. |
| Claude Desktop only requests `openid` and gets no tool scopes | Expected; the Custom Connectors UI has no scope field. Server-side scope augmentation in `_oauth_authorize` solves this. |
| Resource_link block rejected by Claude.ai with "Resource links are not currently supported" | Client-side limitation as of 2026-04-23. Leave the inject in place; it works in Inspector. |
| Tool result is a JSON-string-in-a-JSON-string | AgentCore doesn't unwrap the target Lambda's HTTP-proxy shape; the response interceptor does. If it ever stops working, check that AgentCore's response payload still matches the shape `_unwrap_http_proxy_shape` expects. |
| `TypeError` calling `idp.oauth.authorize_start()` | The Stytch SDK only accepts `client_id, redirect_uri, response_type, scopes, session_jwt` for `authorize_start()`. `state`, `code_challenge`, `resources` belong to `authorize()` only. |
| AgentCore validating against stale JWKS | AgentCore caches OIDC discovery. The stack already points `discovery_url` directly at Stytch's own discovery doc (authoritative); don't redirect it through the OAuth Lambda. |
| `aws ssm put-parameter` succeeds but Lambda can't read | Ensure parameter name is exactly `/mcp-spike/stytch/project_secret` and type is `SecureString`. The Lambda is granted `grant_read` on this exact name. |

---

## 12.1 McpLambdaSpikeStack — additional notes

| Concern | Detail |
|---|---|
| Lambda Web Adapter | Copied from `public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1` to `/opt/extensions/lambda-adapter` in the Dockerfile. The Lambda runtime auto-detects the extension and uses LWA as its runtime, proxying API Gateway events to uvicorn on `PORT=8080`. |
| Streaming | API Gateway buffers Lambda responses; LWA's `response_stream` mode is configured but only activates if you switch to a Lambda Function URL later. MCP streamable-HTTP works in buffered mode for short responses (initialize, tools/list, tools/call returning quickly). |
| Stateless | `mcp.http_app(path="/mcp", transport="http", stateless_http=True)`. No in-memory session state — each Lambda invocation handles one request independently. |
| Audience accepted | List `[MCP_SERVER_URL, STYTCH_PROJECT_DOMAIN]`. Clients sending `resource=<MCP_SERVER_URL>` (RFC 8707) get tokens whose `aud` matches the first; tokens that fall back to Stytch's default audience (the project domain) match the second. |
| RFC 9728 PRM URL | `<api-gw-base>/.well-known/oauth-protected-resource/mcp` (FastMCP's path-aware mount). Clients discover it via the `resource_metadata=` parameter in the 401's `WWW-Authenticate` header. |
| Two-pass deploy | `cdk deploy McpLambdaSpikeStack` → fill `MCP_OAUTH_LAMBDA_URL` and `MCP_SERVER_URL` in `.env` → `cdk deploy` again. Same shape as the legacy stack's two-pass pattern. |
| Stytch Dashboard for this stack | Connected App **Authorization URL** = `<MCP_OAUTH_LAMBDA_URL>oauth/authorize`; **Redirect URLs** entry `<MCP_OAUTH_LAMBDA_URL>login/callback` (type Login). When switching between stacks for testing, update these to the active stack's URLs. |

## 13. Roadmap & active work

**Recent commits (newest first):**
- `93a387b` — Response interceptor: HTTP-proxy unwrap + improved resource_link injection.
- `610e2b9` — Response interceptor: scope-based `tools/list` filtering + `resource_link`.
- `e7b68ee` — `query_data` tool with presigned S3 URL.
- `1482d7c` — OAuth error handling + custom-scope support in Stytch integration.
- `2253e6e` — Scope-based access control on `tools/call` (REQUEST interceptor).
- `03db393` — Migrate OAuth from Lambda Function URL to API Gateway v2.
- `4fe96a7` — Initial AgentCore Gateway integration.

**Phase 2 (`query_data` → `athena_query`):**
- Glue database + table over the same bucket.
- Athena perms on the target Lambda (`StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`) + S3 perms for the query-output prefix.
- Tool input becomes `{ "sql": "<SELECT-only>" }`. Whitelist `SELECT`, reject DDL/DML, cap rows/bytes.
- Implementation: `start_query_execution` → poll `get_query_execution` → `get_query_results` for first N rows → presign `ResultConfiguration.OutputLocation`.
- Envelope shape **stays identical**; adds top-level `query_execution_id`, `state`, `statistics: { data_scanned_bytes, engine_execution_time_ms, total_execution_time_ms }`.

**Target UX:** Claude.ai Projects + drag-drop + in-sandbox analysis tool. Claude Desktop is *not* the target — it has no analysis sandbox and can't auto-fetch URLs.

**Recent direction (FastMCP-on-Lambda):**
- Built `McpLambdaSpikeStack` as a parallel implementation that swaps AgentCore Gateway for FastMCP running on Lambda via AWS Lambda Web Adapter. Same OAuth flow, same Stytch Connected App, same scope-based RBAC contract — but the MCP server is a Python ASGI app rather than a managed AgentCore service.
- Default-deny enforced via `_ensure_scope` in each tool body (per Q10). The AgentCore stack's interceptors were subsequently flipped to the same posture, so both stacks now share an identical default-deny scope contract.
- Resource_link block on `query_data` returned natively from the tool function (no response interceptor unwrapping).

---

## 14. Quick reference commands

```bash
# Deploy
uv sync --all-groups && source .venv/bin/activate
cd cdk && cdk bootstrap && cdk deploy

# Decode a JWT
python3 -c "
import base64, json, sys
payload = sys.argv[1].split('.')[1] + '=='
print(json.loads(base64.urlsafe_b64decode(payload)))
" <access_token>

# Verify the seed CSV is present in the results bucket
aws s3 ls s3://$(aws cloudformation describe-stacks --stack-name McpAuthSpikeStack \
  --query "Stacks[0].Outputs[?OutputKey=='QueryDataResultsBucketName'].OutputValue" --output text)/

# Tail OAuth Lambda logs
aws logs tail /aws/lambda/$(aws cloudformation describe-stack-resources --stack-name McpAuthSpikeStack \
  --query "StackResources[?LogicalResourceId=='OAuthServerLambda'].PhysicalResourceId" --output text) --follow

# MCP Inspector
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP, URL: <gateway-url>/mcp
```

---

## 15. AWS console quick links

| Resource | Path |
|---|---|
| Gateway | Bedrock → AgentCore → Gateways → `McpAuthSpikeGateway` |
| Lambda functions | Lambda → Functions → `McpAuthSpikeStack-*` |
| CloudFormation outputs | CloudFormation → `McpAuthSpikeStack` → Outputs |
| OAuth Lambda logs | CloudWatch → Log groups → `/aws/lambda/McpAuthSpikeStack-OAuthServerLambda*` |
| SSM parameters | Systems Manager → Parameter Store → `/mcp-spike/*` |
| Results bucket | S3 → Buckets → `<QueryDataResultsBucketName>` |
