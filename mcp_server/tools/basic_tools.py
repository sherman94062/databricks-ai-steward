from mcp_server.tools.registry import registry


@registry.register("list_catalogs")
def list_catalogs():
    # stub for now
    return {
        "catalogs": ["main", "analytics", "system"]
    }