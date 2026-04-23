# mcp_server/tools/registry.py

from typing import Callable, Dict, Any

class ToolRegistry:
    def __init__(self):
        self.tools: Dict[str, Callable] = {}

    def register(self, name: str):
        def decorator(func: Callable):
            self.tools[name] = func
            return func
        return decorator

    def list_tools(self):
        return list(self.tools.keys())

    def run(self, name: str, *args, **kwargs) -> Any:
        if name not in self.tools:
            raise ValueError(f"Tool not found: {name}")
        return self.tools[name](*args, **kwargs)


registry = ToolRegistry()