"""An MCP server that books Hamurai transactions with Claude Code. Not a library.

Its one door is `main`, the `hamurai` command that `.mcp.json` runs.
"""

from hamurai.server import main

__all__ = ["main"]
