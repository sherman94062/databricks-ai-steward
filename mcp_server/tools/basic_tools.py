from mcp_server.app import safe_tool
from mcp_server.databricks.client import get_workspace, run_in_thread


def _catalog_to_dict(c) -> dict:
    return {
        "name": c.name,
        "type": c.catalog_type.value if c.catalog_type else None,
        "comment": c.comment,
    }


@safe_tool()
async def list_catalogs() -> dict:
    """List Unity Catalog catalogs visible to the configured workspace.

    Returns each catalog's name, type (e.g. SYSTEM_CATALOG, MANAGED_CATALOG,
    DELTASHARING_CATALOG), and comment. Auth comes from DATABRICKS_HOST
    and DATABRICKS_TOKEN.
    """
    catalogs = await run_in_thread(lambda: list(get_workspace().catalogs.list()))
    return {"catalogs": [_catalog_to_dict(c) for c in catalogs]}
