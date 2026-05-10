import logging

from fastmcp.server import dependencies as fastmcp_deps

from config import settings

_logger = logging.getLogger(__name__)


def enforce_domain_or_raise() -> dict:
    if not settings.is_prod:
        return {}

    token = fastmcp_deps.get_access_token()
    if token is None:
        raise PermissionError("No access token in request")

    claims = getattr(token, "claims", {}) or {}
    email = (claims.get("email") or "").lower()
    hd = (claims.get("hd") or "").lower()
    email_domain = email.rpartition("@")[2]
    domain = hd or email_domain

    if not settings.allowed_domains:
        raise PermissionError("Server misconfiguration: no allowed domains")

    if domain not in settings.allowed_domains:
        _logger.warning(
            "Rejecting user email=%s hd=%s domain=%s — not in allowlist", email, hd, domain
        )
        raise PermissionError(f"Domain {domain!r} is not allowed")

    if claims.get("email_verified") is False:
        raise PermissionError("Google email is not verified")

    return {"email": email, "hd": hd, "domain": domain}
