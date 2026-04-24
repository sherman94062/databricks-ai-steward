from mcp_server.app import safe_tool


@safe_tool()
def list_catalogs() -> dict:
    """List available Databricks catalogs. (Stub — returns hardcoded values.)"""
    return {"catalogs": ["main", "analytics", "system"]}
