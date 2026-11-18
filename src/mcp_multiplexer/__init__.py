"""Strict MCP multiplexer.

One MCP endpoint over many equivalent MCP servers. The reference server defines
the tool contract; members with a different tool set, schema or version are
hidden. Tool calls carry a selector parameter (``base`` by default).
"""

__version__ = "0.1.0"
