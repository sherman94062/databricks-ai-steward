from fastapi import FastAPI
from mcp_server.tools.registry import registry
from mcp_server.tools import basic_tools

print("TOOLS REGISTERED:", registry.list_tools())  # <-- DEBUG LINE HERE

app = FastAPI(title="Databricks AI Steward MCP Server")


@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/tools")
def list_tools():
    return registry.list_tools()


@app.post("/run/{tool_name}")
def run_tool(tool_name: str, payload: dict):
    return registry.run(tool_name, **payload)