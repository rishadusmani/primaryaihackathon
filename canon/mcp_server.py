"""MCP server, dependency-free. `handle` is the JSON-RPC 2.0 core shared by both transports:

* stdio (newline-delimited), this module's `main`:
      claude mcp add canon -- python -m canon mcp --db canon.db
* Streamable HTTP at POST /mcp on the HTTP API (canon/api.py):
      claude mcp add --transport http canon http://127.0.0.1:8080/mcp
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .service import Canon
from .tools import TOOLS_BY_NAME, call_tool, mcp_tools

PROTOCOL_VERSION = "2025-06-18"


def handle(canon: Canon, msg: dict, actor: str = "mcp") -> dict | None:
    """One JSON-RPC message -> its response (None for notifications)."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "canon", "version": __version__},
                  "instructions": "Canon normalizes clinical documents (fax, PDF, HL7, C-CDA, FHIR, claims, CSV) "
                                  "into one coded, cited patient record. Start with get_patient_summary; check "
                                  "get_conflicts before acting on medications or allergies."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": mcp_tools()}
    elif method == "tools/call":
        p = msg.get("params", {})
        name = p.get("name")
        if name not in TOOLS_BY_NAME:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        args = p.get("arguments") or {}
        if not isinstance(args, dict):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "arguments must be an object"}}
        out = call_tool(canon, name, args, actor=actor)
        result = {"content": [{"type": "text", "text": json.dumps(out, indent=1)}], "isError": "error" in out}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("CANON_DB", "canon.db"))
    a = ap.parse_args()
    canon = Canon(a.db)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            resp = handle(canon, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
