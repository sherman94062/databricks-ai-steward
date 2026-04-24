from mcp_server.app import mcp


@mcp.tool()
def list_catalogs() -> dict:
    """List available Databricks catalogs. (Stub — returns hardcoded values.)"""
    return {"catalogs": ["main", "analytics", "system"]}
