"""An MCP server with which the agent books transactions in Hamurai. Not a library.

Its one door is `main`, the `hamurai` command that `.mcp.json` runs.
"""

from hamurai.server import main

__all__ = ["main"]
