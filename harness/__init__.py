"""modeldock — a web-GUI harness in front of local LLM servers.

Everything runs locally:
- servers.py + adapters/ : talk to OpenAI-compatible model servers
- chat.py               : per-chat sessions bound to workspace folders
- sandbox.py / approvals : dir confinement + human approval gate
- decisions.py          : global + per-chat decision ledgers
- mcp.py                : MCP server management
- compaction.py         : context compaction (manual + auto at 85%)
"""

__version__ = "0.1.0"
