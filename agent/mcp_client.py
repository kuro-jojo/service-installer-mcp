"""
Reusable MCP stdio client wrapper.

Connects to the service-installer MCP server, discovers its tools and
invokes them on behalf of the agent.
"""

import os
from typing import Any, Dict, List

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_BIN = os.path.join(PROJECT_ROOT, "venv", "bin", "python")


class MCPConnection:
    """Async context manager holding an open session to the MCP server."""

    def __init__(self, project_root: str = PROJECT_ROOT) -> None:
        self._params = StdioServerParameters(
            command=PYTHON_BIN,
            args=["-m", "server.mcp_server"],
            cwd=project_root,
        )
        self._stdio_cm = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "MCPConnection":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        self._stdio_cm = stdio_client(self._params)
        read, write = await self._stdio_cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._stdio_cm is not None:
            await self._stdio_cm.__aexit__(None, None, None)
            self._stdio_cm = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("Not connected. Use 'async with MCPConnection()' first.")
        return self._session

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Return tool definitions in OpenAI-compatible JSON-schema format."""
        result = await self.session.list_tools()
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema or {"type": "object"},
                },
            }
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Invoke a tool and return its text output."""
        result = await self.session.call_tool(name, arguments)
        parts = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        return "\n".join(parts) if parts else "(no output)"
