import fastmcp

from security import auth


def get_status() -> str:
    """Returns the operational status of the MCP server."""
    auth.enforce_domain_or_raise()
    return "Data Reports MCP Server is operational"


def register(mcp: fastmcp.FastMCP) -> None:
    mcp.tool()(get_status)
