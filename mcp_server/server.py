import asyncio

from dotenv import load_dotenv

from mcp_server.app import mcp
from mcp_server.lifecycle import run_with_lifecycle
from mcp_server.tools import basic_tools, health  # noqa: F401 — imported for side-effect of registering tools

load_dotenv()


if __name__ == "__main__":
    asyncio.run(run_with_lifecycle(mcp))
