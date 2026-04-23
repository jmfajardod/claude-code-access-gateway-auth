"""OAuth Server Lambda - headless, no HTML, no CloudFront.

All routes either return JSON or issue HTTP 302 redirects.  No HTML is served.

Routes
------
GET  /.well-known/openid-configuration      OIDC discovery for AgentCore CUSTOM_JWT authorizer
GET  /.well-known/oauth-protected-resource  RFC 9728 Protected Resource Metadata (points to Stytch AS)
POST /register                              RFC 7591 DCR - returns static pre-registered client_id
GET  /oauth/authorize                       Stytch Connected Apps authorization endpoint
                                            (configured as "Authorization URL" in Stytch dashboard)
GET  /login                                 Start Google OAuth via Stytch B2B backend REST API (302)
GET  /login/callback                        Receive Stytch token, set session cookie, 302 to returnTo

Authorization flow (no HTML at any step)
-----------------------------------------
1. Claude Desktop discovers Stytch as AS via AgentCore Gateway's built-in PRM.
2. With pre-registered client_id (advanced settings), Claude calls Stytch's
   /v1/oauth2/authorize, which redirects to our /oauth/authorize.
3. /oauth/authorize checks for stytch_session_jwt cookie.
   - Not present -> 302 to /login?returnTo=<current_url>
   - Present     -> calls client.idp.oauth.authorize(consent_granted=True, ...)
                    -> 302 to auth_resp.redirect_uri (contains the auth code for Claude)
4. /login calls Stytch B2B Google OAuth start REST API -> gets oauth_url -> 302 to Google.
5. Google -> Stytch -> /login/callback?token=<stytch_token>
6. /login/callback authenticates the token, sets stytch_session_jwt cookie, 302 to returnTo.
7. Flow continues from step 3 with a valid session cookie.
8. Claude exchanges the code at Stytch /v1/oauth2/token -> Connected Apps access token (JWT).
9. AgentCore validates this JWT against Stytch JWKS (discovered via our OIDC doc).

Prerequisites (manual Stytch Dashboard steps)
----------------------------------------------
- Connected Apps -> Enable Connected Apps features
- Connected Apps -> Settings -> Authorization URL: {lambda_url}/oauth/authorize
- Connected Apps -> Settings -> Enable Dynamic Client Registration (optional)
- Connected Apps -> Create a "Public" Connected App -> copy client_id -> CONNECTED_APP_CLIENT_ID
- Frontend SDKs -> Authorized domains -> add {lambda_url}
- API -> Redirect URLs -> add {lambda_url}/login/callback

Required environment variables
-------------------------------
STYTCH_PROJECT_ID              : Stytch project ID
STYTCH_PROJECT_DOMAIN          : https://<project>.customers.stytch.dev
STYTCH_ORG_ID                  : Stytch B2B organisation ID
CONNECTED_APP_CLIENT_ID        : client_id of the pre-registered Public Connected App
STYTCH_PUBLIC_TOKEN            : Stytch public token (public-token-test-... or public-token-live-...)
STYTCH_PROJECT_SECRET_SSM_PATH : SSM path to the Stytch project secret (SecureString)
OAUTH_LAMBDA_URL               : This Lambda's own Function URL (injected by CDK)
"""

import base64
import json
import logging
import os
import typing
import urllib.parse
import urllib.request

import boto3
import stytch

_LOGGER = logging.getLogger()
_LOGGER.setLevel(logging.INFO)

# -- Environment -----------------------------------------------------------------
_STYTCH_PROJECT_ID = os.environ["STYTCH_PROJECT_ID"]
_STYTCH_PROJECT_DOMAIN = os.environ["STYTCH_PROJECT_DOMAIN"].rstrip("/")
_STYTCH_ORG_ID = os.environ["STYTCH_ORG_ID"]
_CONNECTED_APP_CLIENT_ID = os.environ["CONNECTED_APP_CLIENT_ID"]
_STYTCH_PUBLIC_TOKEN = os.environ["STYTCH_PUBLIC_TOKEN"]
_STYTCH_SECRET_SSM_PATH = os.environ["STYTCH_PROJECT_SECRET_SSM_PATH"]
_OAUTH_LAMBDA_URL = os.environ["OAUTH_LAMBDA_URL"].rstrip("/")

# Custom OAuth scopes to advertise in the AS / OIDC / PRM metadata, on top of
# the baseline OIDC scopes.  Clients that honour DCR metadata will then include
# these in their authorize requests.  Mirror whatever you define in
# Stytch Dashboard -> RBAC Policy -> Scopes.
#
# Override via CUSTOM_SCOPES env var (comma-separated) if you change the RBAC
# policy without redeploying the whole stack.
_BASELINE_SCOPES = ("openid", "email", "profile")
_DEFAULT_CUSTOM_SCOPES = (
    "tool:get_weather",
    "tool:get_time",
    "tool:query_data",
    "tool:*",
)
_CUSTOM_SCOPES: tuple[str, ...] = tuple(
    s.strip()
    for s in os.environ.get("CUSTOM_SCOPES", ",".join(_DEFAULT_CUSTOM_SCOPES)).split(",")
    if s.strip()
)
_ADVERTISED_SCOPES: list[str] = [*_BASELINE_SCOPES, *_CUSTOM_SCOPES]

# -- Cookie names ----------------------------------------------------------------
_SESSION_COOKIE = "stytch_session_jwt"
_RETURN_TO_COOKIE = "mcp_return_to"
_SESSION_TTL = 3600   # 1 hour
_RETURN_TO_TTL = 600  # 10 minutes

# -- Module-level caches ---------------------------------------------------------
_SSM_CLIENT = None
_STYTCH_SECRET: str | None = None
_STYTCH_CLIENT = None


# -- Infrastructure helpers ------------------------------------------------------

def _ssm():
    global _SSM_CLIENT
    if _SSM_CLIENT is None:
        _SSM_CLIENT = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _SSM_CLIENT


def _get_stytch_secret() -> str:
    global _STYTCH_SECRET
    if _STYTCH_SECRET is None:
        _STYTCH_SECRET = _ssm().get_parameter(
            Name=_STYTCH_SECRET_SSM_PATH, WithDecryption=True
        )["Parameter"]["Value"]
    return _STYTCH_SECRET


def _get_stytch_client() -> stytch.B2BClient:
    global _STYTCH_CLIENT
    if _STYTCH_CLIENT is None:
        _STYTCH_CLIENT = stytch.B2BClient(
            project_id=_STYTCH_PROJECT_ID,
            secret=_get_stytch_secret(),
        )
    return _STYTCH_CLIENT


# -- HTTP response helpers --------------------------------------------------------

def _json(status: int, body: dict, extra_headers: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": status, "headers": headers, "body": json.dumps(body)}


def _redirect(location: str, cookies: list[str] | None = None) -> dict:
    resp: dict = {
        "statusCode": 302,
        "headers": {"Location": location},
        "body": "",
    }
    if cookies:
        resp["cookies"] = cookies
    return resp


def _error(status: int, error: str, description: str = "") -> dict:
    body: dict = {"error": error}
    if description:
        body["error_description"] = description
    return _json(status, body)


# -- Cookie helpers ---------------------------------------------------------------

def _get_cookie(event: dict, name: str) -> str:
    """Extract a cookie value from a Lambda Function URL event."""
    for c in (event.get("cookies") or []):
        n, _, v = c.strip().partition("=")
        if n.strip() == name:
            return urllib.parse.unquote(v.strip())
    cookie_header = (event.get("headers") or {}).get("cookie", "")
    for part in cookie_header.split(";"):
        n, _, v = part.strip().partition("=")
        if n.strip() == name:
            return urllib.parse.unquote(v.strip())
    return ""


def _cookie(name: str, value: str, path: str = "/", max_age: int = _RETURN_TO_TTL, secure: bool = True) -> str:
    encoded = urllib.parse.quote(value, safe="")
    c = f"{name}={encoded}; HttpOnly; SameSite=Lax; Path={path}; Max-Age={max_age}"
    if secure:
        c += "; Secure"
    return c


def _clear_cookie(name: str) -> str:
    return f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


# -- Stytch B2B Google OAuth start (backend REST, not frontend SDK) ---------------

def _stytch_base_url() -> str:
    """Return the correct Stytch API base URL for this project environment."""
    if _STYTCH_PROJECT_ID.startswith("project-test-"):
        return "https://test.stytch.com"
    return "https://api.stytch.com"


def _google_oauth_start(login_redirect_url: str) -> str:
    """Return the Stytch B2B Google Discovery OAuth start URL.

    This is a public client-side endpoint — no server-side API call needed.
    We construct the redirect URL using the public token and let the browser
    follow it.  Stytch redirects to Google, then back to login_redirect_url.
    """
    params = urllib.parse.urlencode({
        "public_token": _STYTCH_PUBLIC_TOKEN,
        "discovery_redirect_url": login_redirect_url,
    })
    return f"{_stytch_base_url()}/v1/b2b/public/oauth/google/discovery/start?{params}"


# -- Stytch error classification --------------------------------------------------
#
# RFC 6749 §4.1.2.1: on authorization failure, redirect to the client's
# redirect_uri with ?error=...&error_description=...&state=..., EXCEPT when the
# redirect_uri itself (or the client_id) is invalid — in that case we must NOT
# redirect and instead surface the error directly.
#
# We inspect Stytch SDK exceptions (they carry .error_type) to decide:
#   - is this a session-level failure (clear cookie, send user back to /login)?
#   - is this a client-input failure (redirect to client's redirect_uri w/ error)?
#   - is this a redirect_uri/client failure (cannot redirect; return JSON error)?

# Error types that indicate the redirect_uri or client_id themselves are bad,
# so bouncing back to redirect_uri would be unsafe or impossible.
_NO_SAFE_REDIRECT_ERROR_TYPES = frozenset({
    "connected_app_supplied_redirect_url_not_found_in_client",
    "connected_app_not_found",
    "client_not_found",
    "oauth_invalid_client",
})

# Stytch error_type -> (OAuth 2.0 error code, http status to log at)
_STYTCH_TO_OAUTH_ERROR: dict[str, str] = {
    "oauth_invalid_scope_requested": "invalid_scope",
    "oauth_connected_app_scopes_required_but_not_granted": "invalid_scope",
    "oauth_invalid_request": "invalid_request",
    "oauth_unauthorized_client": "unauthorized_client",
    "oauth_unsupported_response_type": "unsupported_response_type",
    "oauth_access_denied": "access_denied",
    "oauth_consent_required": "access_denied",
}

# Anything suggesting the user's session is gone/bad -> clear cookie, relogin.
_SESSION_EXPIRED_ERROR_TYPES = frozenset({
    "session_not_found",
    "session_expired",
    "unauthorized_credentials",
    "invalid_session_jwt",
    "invalid_session_token",
    "session_jwt_expired",
    "session_too_old_to_auth_factor",
})


def _stytch_error_type(exc: BaseException) -> str:
    """Best-effort extraction of Stytch's error_type string from an SDK exception."""
    t = getattr(exc, "error_type", None)
    if isinstance(t, str) and t:
        return t
    details = getattr(exc, "original_json", None)
    if isinstance(details, dict):
        t = details.get("error_type")
        if isinstance(t, str):
            return t
    return ""


def _oauth_error_redirect(redirect_uri: str, state: str, error: str, description: str) -> dict:
    """RFC 6749 §4.1.2.1 error redirect back to the client's redirect_uri."""
    params = {"error": error}
    if description:
        params["error_description"] = description
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return _redirect(f"{redirect_uri}{sep}{urllib.parse.urlencode(params)}")


# -- Route handlers ---------------------------------------------------------------

def _as_metadata() -> dict:
    """RFC 8414 OAuth Authorization Server Metadata.

    MCP clients fetch this after discovering our Lambda URL as the AS via the
    gateway's built-in Protected Resource Metadata.  The authorization_endpoint
    points back to us; token_endpoint and jwks_uri point to Stytch directly.
    """
    return _json(200, {
        "issuer": _OAUTH_LAMBDA_URL,
        "authorization_endpoint": f"{_OAUTH_LAMBDA_URL}/oauth/authorize",
        "token_endpoint": f"{_STYTCH_PROJECT_DOMAIN}/v1/oauth2/token",
        "registration_endpoint": f"{_OAUTH_LAMBDA_URL}/register",
        "jwks_uri": f"{_STYTCH_PROJECT_DOMAIN}/.well-known/jwks.json",
        "scopes_supported": _ADVERTISED_SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    })


def _oidc_discovery() -> dict:
    """OIDC discovery doc consumed by AgentCore Gateway's discoveryUrl config.

    issuer and jwks_uri must match Stytch Connected Apps signing keys so AgentCore
    can cryptographically validate the Connected Apps JWTs.
    """
    return _json(200, {
        "issuer": _STYTCH_PROJECT_DOMAIN,
        "authorization_endpoint": f"{_OAUTH_LAMBDA_URL}/oauth/authorize",
        "token_endpoint": f"{_STYTCH_PROJECT_DOMAIN}/v1/oauth2/token",
        "registration_endpoint": f"{_OAUTH_LAMBDA_URL}/register",
        "jwks_uri": f"{_STYTCH_PROJECT_DOMAIN}/.well-known/jwks.json",
        "scopes_supported": _ADVERTISED_SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    })


def _protected_resource_metadata() -> dict:
    """RFC 9728 Protected Resource Metadata.

    Tells MCP clients which AS to use.  Points to Stytch Connected Apps AS
    so clients can discover Stytch's token/registration endpoints directly.
    """
    return _json(200, {
        "resource": _OAUTH_LAMBDA_URL,
        "authorization_servers": [_STYTCH_PROJECT_DOMAIN],
        "bearer_methods_supported": ["header"],
        "scopes_supported": _ADVERTISED_SCOPES,
    })


def _register(event: dict) -> dict:
    """RFC 7591 Dynamic Client Registration.

    Returns the pre-registered public Connected App client_id so Claude Desktop
    can complete its mandatory DCR handshake without creating a new Stytch client.
    """
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode()
    try:
        req_body = json.loads(raw_body)
    except Exception:
        req_body = {}
    redirect_uris = req_body.get("redirect_uris", [])
    return _json(201, {
        "client_id": _CONNECTED_APP_CLIENT_ID,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "redirect_uris": redirect_uris,
    })


def _login(event: dict) -> dict:
    """Start Google OAuth via Stytch B2B backend REST API.

    No HTML served.  Stores returnTo in a cookie, then 302-redirects the
    browser to Google's consent screen.
    """
    qs = event.get("queryStringParameters") or {}
    return_to = qs.get("returnTo") or _OAUTH_LAMBDA_URL

    callback_url = f"{_OAUTH_LAMBDA_URL}/login/callback"
    try:
        oauth_url = _google_oauth_start(callback_url)
    except Exception as exc:
        _LOGGER.error("Google OAuth start failed: %s", exc)
        return _error(502, "upstream_error", "Failed to start Google OAuth with Stytch")

    _LOGGER.info("Redirecting browser to Google OAuth via Stytch")
    return _redirect(
        oauth_url,
        cookies=[_cookie(_RETURN_TO_COOKIE, return_to, max_age=_RETURN_TO_TTL)],
    )


def _login_callback(event: dict) -> dict:
    """Receive Stytch OAuth callback, authenticate token, set session cookie.

    No HTML served.  Sets stytch_session_jwt cookie, clears the return_to cookie,
    then 302-redirects to the URL stored in the mcp_return_to cookie.
    """
    qs = event.get("queryStringParameters") or {}
    oauth_token = qs.get("token", "")

    if not oauth_token:
        return _error(400, "invalid_request", "Missing token parameter")

    try:
        stytch_client = _get_stytch_client()
        # Step 1: authenticate the discovery OAuth token -> intermediate session
        disc_resp = stytch_client.oauth.discovery.authenticate(
            discovery_oauth_token=oauth_token,
        )
        discovered_org_ids = [
            o.organization.organization_id
            for o in disc_resp.discovered_organizations
        ]
        _LOGGER.info(
            "Discovery OAuth authenticated; discovered org(s): %s", discovered_org_ids
        )
        # Step 2: exchange intermediate session into the target organisation.
        # Prefer the configured org; fall back to the first discovered org so
        # that JIT-provisioned users (whose email domain matches the org) also work.
        if _STYTCH_ORG_ID in discovered_org_ids:
            target_org_id = _STYTCH_ORG_ID
        elif discovered_org_ids:
            target_org_id = discovered_org_ids[0]
            _LOGGER.warning(
                "Configured org %s not in discovered list; using %s instead",
                _STYTCH_ORG_ID,
                target_org_id,
            )
        else:
            _LOGGER.error(
                "No discovered organisations for this user. "
                "Add the user as a member or enable JIT provisioning in Stytch Dashboard."
            )
            return _error(403, "access_denied", "Your account is not a member of any organisation")
        exchange_resp = stytch_client.discovery.intermediate_sessions.exchange(
            organization_id=target_org_id,
            intermediate_session_token=disc_resp.intermediate_session_token,
        )
        session_jwt = exchange_resp.session_jwt
    except Exception as exc:
        _LOGGER.error("Stytch discovery OAuth authenticate/exchange failed: %s", exc)
        return _error(502, "upstream_error", "Stytch token exchange failed")

    return_to = _get_cookie(event, _RETURN_TO_COOKIE) or _OAUTH_LAMBDA_URL
    _LOGGER.info("Login callback: authenticated, redirecting to returnTo")

    return _redirect(
        return_to,
        cookies=[
            _cookie(_SESSION_COOKIE, session_jwt, max_age=_SESSION_TTL),
            _clear_cookie(_RETURN_TO_COOKIE),
        ],
    )


def _oauth_authorize(event: dict) -> dict:
    """Stytch Connected Apps Authorization URL endpoint.

    Configure this URL in Stytch Dashboard -> Connected Apps -> Settings ->
    "Authorization URL".  Stytch redirects users here from /v1/oauth2/authorize.

    No HTML served:
    - No session  -> 302 to /login?returnTo=<this_url>
    - Has session -> client.idp.oauth.authorize() -> 302 to auth_resp.redirect_uri
    """
    qs = event.get("queryStringParameters") or {}

    current_url = f"{_OAUTH_LAMBDA_URL}/oauth/authorize"
    if qs:
        current_url += "?" + urllib.parse.urlencode(qs)

    session_jwt = _get_cookie(event, _SESSION_COOKIE)
    if not session_jwt:
        _LOGGER.info("No session cookie; redirecting to /login")
        login_url = f"{_OAUTH_LAMBDA_URL}/login?returnTo={urllib.parse.quote(current_url)}"
        return _redirect(login_url)

    client_id = qs.get("client_id", "")
    redirect_uri = qs.get("redirect_uri", "")
    response_type = qs.get("response_type", "code")
    scope_str = qs.get("scope", "openid").replace("+", " ")  # + is URL-encoded space
    state = qs.get("state", "")
    code_challenge = qs.get("code_challenge", "")
    resource = qs.get("resource", "")

    requested_scopes = [s for s in scope_str.split() if s]

    # Some MCP clients (notably Claude Desktop remote connectors) don't let you
    # configure custom scopes and ignore `scopes_supported` from AS metadata, so
    # they end up requesting only `openid` — resulting in tokens without any
    # `tool:*` scope, which then bypasses the interceptors' scope filtering.
    #
    # To make filtering work uniformly across clients, we append every
    # advertised custom scope here.  Stytch still filters against the member's
    # RBAC roles, so a user only ever gets the scopes they're entitled to; this
    # just removes the need for the client to know what to ask for.
    scopes = list(dict.fromkeys([*requested_scopes, *_CUSTOM_SCOPES]))
    if scopes != requested_scopes:
        _LOGGER.info(
            "Augmented client scopes: requested=%s -> sent-to-stytch=%s",
            requested_scopes,
            scopes,
        )

    # authorize_start() does NOT accept state/code_challenge/code_challenge_method
    start_params: dict = dict(
        client_id=client_id,
        redirect_uri=redirect_uri,
        response_type=response_type,
        scopes=scopes,
        session_jwt=session_jwt,
    )

    # authorize() accepts state, code_challenge, resources (but NOT code_challenge_method)
    authorize_params: dict = dict(
        consent_granted=True,
        client_id=client_id,
        redirect_uri=redirect_uri,
        response_type=response_type,
        scopes=scopes,
        session_jwt=session_jwt,
    )
    if state:
        authorize_params["state"] = state
    if code_challenge:
        authorize_params["code_challenge"] = code_challenge
    if resource:
        authorize_params["resources"] = [resource]

    try:
        stytch_client = _get_stytch_client()
        start_resp = stytch_client.idp.oauth.authorize_start(**start_params)
        if start_resp.consent_required:
            _LOGGER.info("Consent required — auto-granting for pre-configured client")

        auth_resp = stytch_client.idp.oauth.authorize(**authorize_params)
        _LOGGER.info("Authorization complete; redirecting to client redirect_uri")
        return _redirect(auth_resp.redirect_uri)

    except Exception as exc:
        stytch_err = _stytch_error_type(exc)
        err_message = getattr(exc, "error_message", None) or str(exc)
        _LOGGER.error(
            "idp.oauth.authorize failed: stytch_error_type=%r message=%s",
            stytch_err,
            err_message,
        )

        # 1) Session-level failure -> clear cookie, bounce to /login.
        if stytch_err in _SESSION_EXPIRED_ERROR_TYPES:
            login_url = f"{_OAUTH_LAMBDA_URL}/login?returnTo={urllib.parse.quote(current_url)}"
            return _redirect(login_url, cookies=[_clear_cookie(_SESSION_COOKIE)])

        # 2) redirect_uri or client_id is itself bad -> cannot safely redirect.
        if stytch_err in _NO_SAFE_REDIRECT_ERROR_TYPES or not redirect_uri:
            return _error(400, "invalid_request", err_message or stytch_err or "Authorization failed")

        # 3) Client-input failure we can surface via the standard OAuth error redirect.
        oauth_err = _STYTCH_TO_OAUTH_ERROR.get(stytch_err, "server_error")
        return _oauth_error_redirect(redirect_uri, state, oauth_err, err_message)


# -- Lambda entry point ----------------------------------------------------------

def handler(event: dict, context: typing.Any) -> dict:
    safe = {k: v for k, v in event.items() if k != "body"}
    _LOGGER.info("OAuth server event: %s", json.dumps(safe))

    http = (event.get("requestContext") or {}).get("http", {})
    method = http.get("method", "").upper()
    path = event.get("rawPath", "/")

    if method == "GET" and path == "/.well-known/openid-configuration":
        return _oidc_discovery()
    if method == "GET" and path == "/.well-known/oauth-authorization-server":
        return _as_metadata()
    if method == "GET" and path == "/.well-known/oauth-protected-resource":
        return _protected_resource_metadata()
    if method == "POST" and path == "/register":
        return _register(event)
    if method == "GET" and path == "/oauth/authorize":
        return _oauth_authorize(event)
    if method == "GET" and path == "/login":
        return _login(event)
    if method == "GET" and path == "/login/callback":
        return _login_callback(event)

    return _error(404, "not_found", f"No route: {method} {path}")
