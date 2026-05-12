import logging

from cryptography import fernet
import fastmcp
from fastmcp.server.auth.providers import google as google_auth
from key_value.aio.stores import dynamodb as kv_dynamodb
from key_value.aio.wrappers import encryption as kv_encryption
from starlette import requests as starlette_requests
from starlette import responses as starlette_responses
from starlette import routing as starlette_routing
from starlette.middleware import cors as starlette_cors

from config import settings
from tools import dummy_tool, query_data_catalog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    force=True,
)
_logger = logging.getLogger("data-reports-mcp")
logging.getLogger("data-reports-mcp").setLevel(logging.INFO)

if settings.is_prod:
    _storage_enc_key = settings.storage_enc_key.get_secret_value()
    _client_storage = kv_encryption.FernetEncryptionWrapper(
        key_value=kv_dynamodb.DynamoDBStore(
            table_name=settings.ddb_table_name,
            region_name=settings.aws_region_name,
        ),
        fernet=fernet.Fernet(_storage_enc_key.encode()),
    )
    _primary_domain = next(iter(settings.allowed_domains - {"gmail.com"}), "")
    _extra_authorize_params: dict[str, str] = {"prompt": "select_account"}
    if _primary_domain:
        _extra_authorize_params["hd"] = _primary_domain
    _auth = google_auth.GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        base_url=settings.public_base_url.rstrip("/"),
        required_scopes=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ],
        extra_authorize_params=_extra_authorize_params,
        jwt_signing_key=settings.jwt_signing_key.get_secret_value(),
        client_storage=_client_storage,
    )
    mcp = fastmcp.FastMCP(name="DataReportsMCPServer", auth=_auth)
else:
    mcp = fastmcp.FastMCP(name="DataReportsMCPServer")

dummy_tool.register(mcp)
query_data_catalog.register(mcp)

app = mcp.http_app()

# CORS — MCP Inspector runs in the browser at http://localhost:6274 and makes
# cross-origin fetches to /mcp, /register, /token, /.well-known/*. claude.ai
# is server-to-server and doesn't need this. Bearer tokens in the Authorization
# header aren't CORS "credentials", so allow_credentials stays off and we can
# use the wildcard origin.
app.add_middleware(
    starlette_cors.CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version"],
)


async def _health(
    _request: starlette_requests.Request,
) -> starlette_responses.JSONResponse:
    return starlette_responses.JSONResponse({"status": "ok"})


app.router.routes.insert(0, starlette_routing.Route("/health", _health, methods=["GET"]))
