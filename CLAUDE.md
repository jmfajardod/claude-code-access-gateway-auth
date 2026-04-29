# CLAUDE.md

Guidance for Claude Code when working in this repository. Read first; treat as authoritative when it conflicts with assumptions from training data.

## What this project is

`claude-code-access-gateway-auth` is an **OAuth 2.0 + Stytch B2B authentication spike** for an **MCP server hosted on Amazon Bedrock AgentCore Gateway**. It demonstrates how to put a real Auth Code + PKCE flow (backed by Google login through Stytch B2B Discovery) in front of an AgentCore-hosted MCP server so MCP clients (Claude Desktop / Claude.ai Projects connectors / MCP Inspector) can discover, authenticate, and call tools — with **scope-based RBAC** enforced inside the gateway via interceptor Lambdas.

There is **no Cognito and no CloudFront**. Everything is API Gateway v2 (HTTP API) + Lambda + AgentCore + Stytch.

The active long-form doc is `README.md` (~40 KB, with mermaid diagrams). `KNOWLEDGE_BASE.md` distils it into reference form.

## Two stacks coexist

This repo deploys **two independent stacks** from the same `cdk/app.py`:

| Stack | Active code path | Use when |
|---|---|---|
| `McpAuthSpikeStack` | AgentCore Gateway + REQUEST/RESPONSE interceptors + target Lambda | You want managed MCP via Bedrock AgentCore |
| `McpLambdaSpikeStack` | API Gateway v2 + FastMCP-on-Lambda (Lambda Web Adapter) | You want a pure serverless MCP without AgentCore |

The two stacks share the same Stytch Connected App (per design — Q6) but **no AWS resources**: each has its own SSM parameter, OAuth Lambda, S3 bucket, and seed CSV. Settings flow through the same `.env` file (independent fields).

## Repository map

```
.
├── cdk/
│   ├── app.py                            # Instantiates BOTH stacks
│   ├── cdk.json                          # `app: python3 app.py`
│   ├── requirements.txt                  # CDK deps (aws-cdk-lib 2.248.0, pydantic-settings)
│   ├── assets/
│   │   ├── seed_sample_data.py           # Seed CSV generator (used by McpAuthSpikeStack)
│   │   └── sample_data/                  # Generated at synth time, not in git
│   ├── assets_mcp_lambda_spike/
│   │   ├── seed_sample_data.py           # Independent seed copy (used by McpLambdaSpikeStack)
│   │   └── sample_data/                  # Generated at synth time, not in git
│   └── stacks/
│       ├── mcp_auth_spike_stack.py       # AgentCore stack — REQ/RES interceptors + target
│       ├── mcp_lambda_spike_stack.py     # FastMCP-on-Lambda stack
│       └── settings.py                   # pydantic-settings reader (fields for both stacks)
├── lambdas/
│   ├── oauth_server.py                   # OAuth Lambda for McpAuthSpikeStack
│   ├── request_interceptor.py            # AgentCore REQUEST hook (legacy stack only)
│   ├── response_interceptor.py           # AgentCore RESPONSE hook (legacy stack only)
│   ├── target.py                         # AgentCore target (legacy stack only)
│   ├── requirements.txt                  # Legacy stack runtime deps
│   └── mcp_lambda_spike/
│       ├── oauth/
│       │   ├── oauth_server.py           # Independent OAuth Lambda copy
│       │   └── requirements.txt
│       └── mcp/
│           ├── mcp_server.py             # FastMCP app + JWT verifier + tools
│           ├── Dockerfile                # Lambda Web Adapter container image
│           ├── run.sh                    # uvicorn entrypoint
│           └── requirements.txt          # fastmcp + boto3 + uvicorn
├── .env                                  # Secrets — NEVER commit, NEVER read aloud
├── .env.example                          # Template for .env
├── pyproject.toml                        # uv project, dep groups: [cdk] and [lambdas]
├── uv.lock
└── README.md                             # The long-form spec; KNOWLEDGE_BASE.md is the digest
```

Everything builds via `uv` + Docker. Lambdas are bundled with `public.ecr.aws/sam/build-python3.13` (`_bundled_code()` in `cdk/stacks/mcp_auth_spike_stack.py:30`).

## Architecture in one paragraph (per stack)

**`McpAuthSpikeStack`** (AgentCore-based): A client opens MCP at the AgentCore Gateway URL → AgentCore returns 401 + `WWW-Authenticate` → client follows the OAuth discovery chain → hits this stack's API-Gateway-fronted OAuth Lambda for `/.well-known/*` and `/register` → opens a browser at `/oauth/authorize` → no session → `/login` → Stytch B2B Google Discovery → Google → Stytch callback → `/login/callback` exchanges the discovery token for a Stytch session JWT (cookie) → bounces back to `/oauth/authorize` → server-side scope augmentation + `idp.oauth.authorize()` → authorization code → client exchanges with Stytch's `/v1/oauth2/token` → Stytch issues a Connected App access token (JWT) → client reconnects to MCP with `Authorization: Bearer …` → AgentCore validates against Stytch JWKS → REQUEST interceptor (scope check on `tools/call`) → target Lambda → RESPONSE interceptor (scope-filter `tools/list`, unwrap HTTP-proxy shape, inject `resource_link` for `query_data`).

**`McpLambdaSpikeStack`** (FastMCP-on-Lambda): Identical OAuth flow up to the Stytch token issuance, then the client posts MCP to `<api-gw>/mcp` → API Gateway routes to the MCP Lambda (Docker image with AWS Lambda Web Adapter forwarding to uvicorn-served FastMCP) → FastMCP's `JWTVerifier` validates the JWT against Stytch JWKS in-process (audience accepts both `MCP_SERVER_URL` and `STYTCH_PROJECT_DOMAIN`) → tool body checks the `scope` claim default-deny → tool returns native MCP content blocks (TextContent / ResourceLink) — no HTTP-proxy unwrap, no interceptor chain. RFC 9728 protected-resource metadata is auto-hosted at `/.well-known/oauth-protected-resource/mcp` by FastMCP via `RemoteAuthProvider`.

## What lives where (McpAuthSpikeStack — legacy/AgentCore)

### `cdk/stacks/mcp_auth_spike_stack.py`
Single CDK Stack `McpAuthSpikeStack`. Provisions:

- **SSM SecureString param** `/mcp-spike/stytch/project_secret` (read in via `from_secure_string_parameter_attributes`; created out-of-band — see Setup).
- **IAM `GatewayRole`** trusted by `bedrock-agentcore.amazonaws.com`, with `lambda:InvokeFunction`, `lambda:GetFunction`, `iam:PassRole` on `*`.
- **OAuth Server Lambda** (`oauth_server.handler`, py3.13, 1 min, 256 MB) — env vars from `.env` plus `OAUTH_LAMBDA_URL` injected post-creation to break a CDK circular dep.
- **API Gateway v2 HTTP API** (`OAuthApi`) — CORS=`*`, default integration → OAuth Lambda. URL exported as `OAuthServerUrl`.
- **Request interceptor Lambda**, **Response interceptor Lambda** (no env, scope logic is hard-coded against the three known tools).
- **S3 `QueryDataResultsBucket`** — encrypted, SSL-only, `auto_delete_objects=True`, `RemovalPolicy.DESTROY`. Seeded via `s3_deployment.BucketDeployment` from `cdk/assets/sample_data/` (the path is materialized at synth time by `ensure_sample_csv()`).
- **Target Lambda** (`target.handler`, py3.13, 5 min, 256 MB) — env: `RESULTS_BUCKET`, `RESULTS_OBJECT_KEY`, `PRESIGNED_URL_TTL_SECONDS=300`. Granted `s3:GetObject` on the results bucket.
- **`agentcore.CfnGateway`** (`McpAuthSpikeGateway`):
  - `authorizer_type=CUSTOM_JWT`, `discovery_url={STYTCH_PROJECT_DOMAIN}/.well-known/openid-configuration`, `allowed_audience=[AGENTCORE_GATEWAY_URL, STYTCH_PROJECT_DOMAIN]`.
  - `protocol_type=MCP`, `supported_versions=["2025-11-25"]`.
  - Two interceptors wired with `pass_request_headers=True` (REQUEST and RESPONSE both need to read the `Authorization` header).
- **`agentcore.CfnGatewayTarget`** `DummyToolsTarget` — `credential_provider_type=GATEWAY_IAM_ROLE`, target Lambda ARN, **inline tool schema** for `get_weather`, `get_time`, `query_data`. AgentCore answers `tools/list` from this inline schema, not from the Lambda.
- **CfnOutputs:** `OAuthServerUrl`, `GatewayArn`, `QueryDataResultsBucketName`, `RequestInterceptorArn`, `ResponseInterceptorArn`.

### `cdk/stacks/settings.py`
`pydantic-settings` loader pinned to `../../.env`. Required: `stytch_project_id`, `stytch_project_domain`, `stytch_org_id`, `stytch_public_token`, `connected_app_client_id`, `oauth_lambda_url`, `agentcore_gateway_url`. Optional: `cdk_default_account`, `cdk_default_region` (default `us-east-1`).

### `lambdas/oauth_server.py`
Headless (no HTML) OAuth/OIDC server. Routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/.well-known/openid-configuration` | OIDC discovery — points AgentCore at Stytch JWKS |
| GET | `/.well-known/oauth-authorization-server` | RFC 8414 AS metadata for MCP clients |
| GET | `/.well-known/oauth-protected-resource` | RFC 9728 PRM — declares Stytch as the AS |
| POST | `/register` | RFC 7591 DCR — returns the static pre-registered Connected App `client_id` |
| GET | `/oauth/authorize` | Stytch Connected Apps Authorization URL — gates on session cookie, augments scopes, calls `idp.oauth.authorize()` |
| GET | `/login` | 302 to Stytch Google Discovery start |
| GET | `/login/callback` | Exchanges Stytch discovery token → org session JWT, sets cookie |

Key behaviours to know before editing:

- **`_oauth_authorize` augments client scopes** (`oauth_server.py:491`): always appends `_CUSTOM_SCOPES` (`tool:get_weather, tool:get_time, tool:query_data, tool:*`) to whatever the client requested. Stytch then intersects with the member's RBAC permissions, so the user only ever gets scopes they're entitled to. This is what makes Claude Desktop (which can only request `openid`) get tool scopes at all. Override with `CUSTOM_SCOPES` env var (comma-separated).
- **Error classification** (`_stytch_error_type`, `_STYTCH_TO_OAUTH_ERROR`, `_NO_SAFE_REDIRECT_ERROR_TYPES`, `_SESSION_EXPIRED_ERROR_TYPES`): session-expired → clear cookie + back to `/login`; `redirect_uri`/`client_id` invalid → 400 JSON (must NOT redirect per RFC 6749); other Stytch errors → RFC 6749 §4.1.2.1 error redirect to client's `redirect_uri`. The previous "infinite login loop" symptom (typo'd scope kept bouncing through Google) is fixed by this — preserve the classification when changing things.
- **`authorize_start()` does NOT accept `state`/`code_challenge`/`resources`** — only `authorize()` does. Don't add them to `start_params` (`oauth_server.py:500`).
- **Cookies** are HttpOnly, SameSite=Lax, Secure, with TTLs `_SESSION_TTL=3600s`, `_RETURN_TO_TTL=600s`.
- **JIT org fallback**: if the configured `STYTCH_ORG_ID` isn't in the discovery response, the Lambda falls back to `discovered_organizations[0]` (`oauth_server.py:414-422`).

### `lambdas/request_interceptor.py`
AgentCore REQUEST hook. Decodes the JWT (no signature verify — AgentCore did that), extracts `scope`, and short-circuits `tools/call` with a JSON-RPC error if the caller lacks the required scope. Scope map:

```python
_TOOL_SCOPES = {
    "get_weather": "tool:get_weather",
    "get_time":    "tool:get_time",
    "query_data":  "tool:query_data",
}
```

`tool:*` is wildcard. **Default-DENY** (matches the FastMCP stack): a JWT must contain `tool:*` or the matching `tool:<name>` to call a tool; tokens with only OIDC scopes get nothing. **Unknown tools** (not in `_TOOL_SCOPES`) are also denied — adding a tool to the gateway's inline schema requires updating this map first. Tool names are namespaced `<TargetName>___<tool>` (triple underscore) — strip via `_unprefix_tool()`.

Pass-through output shape:
```python
{"interceptorOutputVersion": "1.0",
 "mcp": {"transformedGatewayRequest": {"headers": {"Authorization": ..., "Content-Type": "application/json"},
                                       "body": body}}}
```

### `lambdas/response_interceptor.py`
AgentCore RESPONSE hook with three jobs (in order, by method):

1. **`tools/list` filter** — **default-deny**: drop tools the JWT lacks scope for, including any unknown tool not in `_TOOL_SCOPES`. Cosmetic; the security boundary is the REQUEST interceptor. Keep `_TOOL_SCOPES` here in lockstep with `request_interceptor.py:_TOOL_SCOPES`.
2. **HTTP-proxy unwrap** — target Lambda returns AWS proxy shape `{"statusCode":200,"body":"<json-string>"}`; AgentCore passes that through unchanged so clients see double-encoded JSON. `_unwrap_http_proxy_shape()` flattens one layer when the shape matches; otherwise pass-through.
3. **`query_data` resource_link injection** — appends an MCP `resource_link` content block (`uri=presigned_url, name="sample_sales.csv", mimeType="text/csv"`). **Currently refused by Claude.ai Projects** with "Resource links are not currently supported" — left in place for future support; Inspector accepts it. The text block is left untouched, so removing the link is non-breaking.

### `lambdas/target.py`
Single MCP Lambda for all three tools. Tool name comes from `context.client_context.custom["bedrockAgentCoreToolName"]` and is namespaced as `<TargetName>___<tool>`. Returns AWS HTTP-proxy shape `{statusCode, body: json.dumps(...)}` — the response interceptor unwraps it.

- `get_weather(location)` → canned `"Weather in {location}: 72F, sunny"`.
- `get_time(timezone)` → `datetime.now(ZoneInfo(tz))` formatted; falls back to UTC if zone is invalid.
- `query_data()` (no args) → reads `sample_sales.csv` from S3, computes type-aware per-column `summary_stats` (numeric: min/max/mean/stddev/nulls; date: min/max/distinct/nulls; string: distinct/nulls + `top` (most-common 5) when distinct ≤ 30), returns first 50 rows as `sample_rows`, generates a 5-min presigned `get_object` URL, and packages everything into the canonical envelope (`status, row_count, summary_stats, sample_rows, full_results, next_steps, _meta`). The envelope shape is the **target UX for Claude.ai Projects** — Phase 2 (Athena) keeps it shape-identical and just adds Athena execution metadata.

### `cdk/assets/seed_sample_data.py`
Deterministic CSV generator with `random.Random(42)`. 2000 rows, columns: `order_id, order_date, region, channel, product_category, product, quantity, unit_price, total_price, customer_id`. Categories: Electronics, Clothing, Home, Sports, Books (5 products each). Date span: 2025-01-01 to 2025-12-31. 250 customers, 4 regions (NA/EU/APAC/LATAM), 3 channels (Online/In-Store/Mobile). The fixed seed keeps the BucketDeployment asset hash stable so synths don't churn.

## What lives where (McpLambdaSpikeStack — FastMCP-on-Lambda)

Independent from the AgentCore stack. Different SSM namespace, different bucket, different OAuth Lambda copy, different seed CSV copy. Reuses only the same Stytch Connected App / RBAC policy.

### `cdk/stacks/mcp_lambda_spike_stack.py`
- **SSM SecureString param** `/mcp-lambda-spike/stytch/project_secret` (own namespace).
- **OAuth Server Lambda** (`oauth_server.handler`, py3.13, zip-bundled from `lambdas/mcp_lambda_spike/oauth/`). Same code as the AgentCore stack's OAuth Lambda; an independent copy by design.
- **MCP Server Lambda** (`DockerImageFunction`, py3.13-slim base + AWS Lambda Web Adapter, image built from `lambdas/mcp_lambda_spike/mcp/Dockerfile`). 1024 MB, 30 s timeout. LWA proxies API Gateway events to uvicorn serving the FastMCP ASGI app.
- **S3 `QueryDataResultsBucket`** — independent copy, encrypted, auto-delete. Seeded by `cdk/assets_mcp_lambda_spike/seed_sample_data.py` via `BucketDeployment`.
- **Single `apigwv2.HttpApi`** (`McpApi`):
  - **default integration** → OAuth Lambda (catches `/.well-known/openid-configuration`, `/.well-known/oauth-authorization-server`, `/register`, `/oauth/authorize`, `/login`, `/login/callback`).
  - **explicit overrides** → MCP Lambda for `ANY /mcp`, `ANY /mcp/{proxy+}`, `GET /.well-known/oauth-protected-resource`, `GET /.well-known/oauth-protected-resource/{proxy+}`.
- **No** AgentCore Gateway, **no** REQUEST/RESPONSE interceptor Lambdas, **no** target Lambda — the MCP Lambda is the entire MCP server.
- **CfnOutputs:** `McpOAuthServerUrl`, `McpServerUrl`, `QueryDataResultsBucketName`, `OAuthLambdaArn`, `McpLambdaArn`.

### `lambdas/mcp_lambda_spike/oauth/oauth_server.py`
Independent copy of the legacy OAuth Lambda. No code changes vs. the original — env-driven via `STYTCH_PROJECT_SECRET_SSM_PATH=/mcp-lambda-spike/stytch/project_secret` and `OAUTH_LAMBDA_URL=<this stack's API GW base>`. Edit this copy when changing OAuth behaviour for the new stack only.

### `lambdas/mcp_lambda_spike/mcp/mcp_server.py`
The FastMCP server. Key elements:

- **Auth**: `JWTVerifier(jwks_uri=<stytch>/.well-known/jwks.json, issuer=<stytch>, audience=[<MCP_SERVER_URL>, <stytch>])` wrapped in `RemoteAuthProvider(authorization_servers=[<stytch>], base_url=<api-gw-base>, scopes_supported=[…])`. The `RemoteAuthProvider` is what auto-hosts the protected-resource metadata.
- **Tools**: `get_weather`, `get_time`, `query_data` registered via `@mcp.tool()`. Each calls `_ensure_scope(<name>)` first — **default-deny**: `ToolError` is raised unless the JWT bears `tool:*` or the matching `tool:<name>` scope. Both stacks now share this posture (the AgentCore stack's interceptors were flipped to match).
- **`tools/list` filter**: `ScopeFilterMiddleware.on_list_tools` filters the returned list using the same scope rule as `_ensure_scope`. Cosmetic; the security boundary is `_ensure_scope` inside each tool body.
- **`query_data`**: returns a list of two MCP content blocks — `TextContent` with the canonical envelope (same shape as the AgentCore stack's `target.py`) and `ResourceLink` pointing at the presigned S3 URL. No HTTP-proxy unwrap needed (FastMCP returns native MCP shapes).
- **ASGI app**: `mcp.http_app(path="/mcp", transport="http", stateless_http=True)`. Stateless — each Lambda invocation is independent.
- **Tool naming**: plain (`get_weather`), no `<TargetName>___` prefix (that was an AgentCore convention).

### `lambdas/mcp_lambda_spike/mcp/Dockerfile` + `run.sh`
- Base: `python:3.13-slim` (NOT a Lambda-prepared base image).
- AWS Lambda Web Adapter copied from `public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1` to `/opt/extensions/lambda-adapter` — the Lambda runtime auto-detects it as an extension and uses it as the runtime, proxying API Gateway events to a local HTTP server on `PORT`.
- `run.sh` execs `uvicorn mcp_server:app --host 0.0.0.0 --port 8080 --workers 1`.
- `AWS_LWA_INVOKE_MODE=response_stream` is set in the Dockerfile but API Gateway buffers responses regardless. If you ever migrate from API Gateway to a Lambda Function URL the streaming mode will activate automatically.

### `cdk/assets_mcp_lambda_spike/seed_sample_data.py`
Independent copy of the AgentCore stack's seed generator. Identical content (same fixed seed 42, same 2000-row sales schema). Writes to `cdk/assets_mcp_lambda_spike/sample_data/`. Both copies must stay in sync schema-wise so `query_data` envelopes match across stacks.

## Conventions to keep

- **Headless OAuth** — do not introduce HTML responses. The spec is "JSON or 302, nothing else."
- **No new auth providers** — Stytch B2B is the AS; Cognito is intentionally absent.
- **Don't bypass the interceptor scope contract** — RBAC is enforced server-side. If you add a tool, update both `_TOOL_SCOPES` dicts (`request_interceptor.py:31`, `response_interceptor.py:55`) AND the `_DEFAULT_CUSTOM_SCOPES` tuple (`oauth_server.py:82`) AND the inline tool schema in the stack.
- **Tool naming uses triple-underscore** (`_TARGET_PREFIX_SEP = "___"`) — that's an AgentCore convention, not a typo.
- **HTTP-proxy shape on target return values** is required — AgentCore unwraps `body` for the MCP `content[0].text` block.
- **The OAuth Lambda's `OAUTH_LAMBDA_URL` env var is set post-creation** to break a CDK circular dep. Don't try to resolve it at synth time from the API Gateway construct.
- **Settings live in `.env`, secrets in SSM** — only the Stytch project secret goes to SSM (`/mcp-spike/stytch/project_secret`); everything else is in `.env`. Don't move other things to SSM "for symmetry."
- **`requires-python = ">=3.12"`** in `pyproject.toml`, but lambdas run on **Python 3.13** (`_PYTHON313_BUILD_IMAGE`, `lambda_.Runtime.PYTHON_3_13`). Don't lower the runtime without re-bundling.

## Working with this repo

### Commands

```bash
# Initial setup
uv sync --all-groups
source .venv/bin/activate

# CDK
cd cdk
cdk bootstrap        # one-time per account/region
cdk synth            # local validation
cdk deploy           # first deploy, then fill .env URLs, deploy again
cdk destroy

# Inspect a JWT (e.g. from Inspector / Claude Desktop logs)
python3 -c "
import base64, json, sys
payload = sys.argv[1].split('.')[1] + '=='
print(json.loads(base64.urlsafe_b64decode(payload)))
" <access_token>

# Verify the seed CSV is in the bucket
aws s3 ls s3://$(aws cloudformation describe-stacks --stack-name McpAuthSpikeStack \
  --query "Stacks[0].Outputs[?OutputKey=='QueryDataResultsBucketName'].OutputValue" --output text)/

# Tail a Lambda's logs (the `aws logs *` permission is pre-granted in .claude/settings.local.json)
aws logs tail /aws/lambda/<function-name> --follow
```

### Two-phase deploy (mandatory, per stack)

Both stacks need their own post-deploy URL filled in `.env` and a redeploy. Each stack has its **own** Stytch Dashboard URLs to update — switching between stacks for testing means updating those URLs in the Dashboard.

**For `McpAuthSpikeStack`**:
1. First `cdk deploy McpAuthSpikeStack` with `OAUTH_LAMBDA_URL` / `AGENTCORE_GATEWAY_URL` as placeholders.
2. Read outputs: `OAuthServerUrl`, `GatewayArn` (Gateway URL via Bedrock console).
3. Fill into `.env`. `cdk deploy McpAuthSpikeStack` again.
4. Stytch Dashboard: Connected App **Authorization URL** = `<OAuthServerUrl>oauth/authorize`; Redirect URLs entry `<OAuthServerUrl>login/callback` (type Login).

**For `McpLambdaSpikeStack`**:
1. First `cdk deploy McpLambdaSpikeStack` with `MCP_OAUTH_LAMBDA_URL` / `MCP_SERVER_URL` as placeholders.
2. Read outputs: `McpOAuthServerUrl`, `McpServerUrl`.
3. Fill into `.env`. `cdk deploy McpLambdaSpikeStack` again.
4. Stytch Dashboard: Connected App **Authorization URL** = `<McpOAuthServerUrl>oauth/authorize`; Redirect URLs entry `<McpOAuthServerUrl>login/callback` (type Login).
5. **Also** put the Stytch project secret into SSM at `/mcp-lambda-spike/stytch/project_secret` (SecureString) before the first deploy — independent from `/mcp-spike/...` used by the legacy stack.

### Don'ts

- **Don't** read or print `.env` — it has live secrets even in test mode.
- **Don't** run `cdk destroy` without confirmation; the bucket auto-deletes objects.
- **Don't** `git add` `cdk/assets/sample_data/` — it's regenerated; left untracked on purpose.
- **Don't** commit `cdk.out/`.
- **Don't** add Cognito or CloudFront — the design is deliberately without them.
- **Don't** "fix" the augmentation of client scopes in `_oauth_authorize` — it's the explicit workaround for clients (Claude Desktop) that can't request custom scopes. Stytch RBAC remains the authoritative boundary. **This is load-bearing under default-deny**: without the augmentation, Claude Desktop tokens would have zero `tool:` scopes and clients would see an empty `tools/list`.
- **Don't** verify JWT signatures inside the interceptors — AgentCore's `CUSTOM_JWT` authorizer already did, and the lambdas only `base64`-decode the payload.

## Active work and known issues

- **Recent direction (last 5 commits):** scope-based access control via interceptors (REQUEST + RESPONSE), `query_data` tool with hybrid envelope (schema + sample + presigned URL), HTTP-proxy unwrapping in the response interceptor, OAuth error classification, server-side scope augmentation. See `git log --oneline -20`.
- **Resource_link blocks**: Inspector accepts; **Claude.ai Projects refuses** with "Resource links are not currently supported." (memory note dated 2026-04-23). Don't waste a round-trip retrying — the client-side limitation is known. Removing the inject is a one-line change in `response_interceptor.py:262`.
- **AgentCore HTTP-proxy passthrough**: AgentCore does not unwrap the target Lambda's `{statusCode, body}` shape. The response interceptor does it. If the unwrap stops working, check whether AgentCore changed behaviour upstream first.
- **Phase 1 vs Phase 2 for `query_data`**: today the tool reads a static seed CSV. Phase 2 swaps in real Athena over Glue, renames the tool to `athena_query`, and adds a `sql` arg. The envelope shape stays identical — preserve it.
- **Target UX is Claude.ai Projects** (with drag-drop + analysis sandbox), not Claude Desktop. Claude Desktop has no analysis tool and can't auto-fetch URLs, so it's only useful for `sample_rows`-sized analysis.

## When in doubt

- The README is the long-form spec. `KNOWLEDGE_BASE.md` is the structured digest.
- For OAuth flow questions, the mermaid sequence diagram in `README.md` (lines 57–115) is authoritative.
- For Stytch dashboard configuration, the step-by-step in `README.md` §1a–1f is the canonical checklist.
- For scope/RBAC plumbing, `README.md` §"Scope-based tool access control" walks through Resources → Permissions → Scopes → Roles → Members.
