from dotenv import load_dotenv

from mcp_server.app import mcp
from mcp_server.tools import basic_tools  # noqa: F401 — imported for side-effect of registering tools

load_dotenv()


if __name__ == "__main__":
    mcp.run()
