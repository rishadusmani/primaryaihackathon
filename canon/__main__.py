"""CLI.

    python -m canon normalize samples/maria_chen/*        # print the summary
    python -m canon normalize --view record FILES...      # full canonical record
    python -m canon normalize --view fhir FILES...        # FHIR R4 Bundle
    python -m canon serve --port 8080                     # HTTP API
    python -m canon mcp                                   # MCP server on stdio
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .service import Canon, CanonError


def normalize_cmd(a: argparse.Namespace) -> int:
    canon = Canon(a.db)
    pid = a.patient_id
    for path in a.files:
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            r = canon.ingest(data, filename=os.path.basename(path), patient_id=pid if a.patient_id or
                             (pid and path.lower().endswith(".csv")) else None, actor="cli",
                             use_llm=True if a.llm else None)
        except CanonError as e:
            if e.code == "patient_unidentified" and pid:
                r = canon.ingest(data, filename=os.path.basename(path), patient_id=pid, actor="cli")
            else:
                print(f"! {path}: {e.message}", file=sys.stderr)
                continue
        pid = pid or r["patient_id"]
        d = r["document"]
        print(f"+ {os.path.basename(path):34s} {d['format']:8s} -> {r['patient_id']} ({r['match']['method']}) "
              f"{d['extraction']['counts']}", file=sys.stderr)
        for w in d["extraction"].get("warnings", []):
            print(f"  ! {w}", file=sys.stderr)
    if not pid:
        return 1
    out = {"summary": canon.summary, "record": canon.record, "fhir": canon.fhir}[a.view](pid)
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="canon", description="Universal clinical data normalization for AI agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("normalize", help="Ingest files and print the canonical view")
    n.add_argument("files", nargs="+")
    n.add_argument("--view", choices=["summary", "record", "fhir"], default="summary")
    n.add_argument("--patient-id")
    n.add_argument("--db", default=":memory:")
    n.add_argument("--llm", action="store_true", help="Also run Claude extraction on text/PDF documents")
    s = sub.add_parser("serve", help="Run the HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--db", default="canon.db")
    m = sub.add_parser("mcp", help="Run the MCP server on stdio")
    m.add_argument("--db", default="canon.db")
    a = ap.parse_args()
    if a.cmd == "normalize":
        return normalize_cmd(a)
    if a.cmd == "serve":
        from .api import serve
        httpd = serve(a.host, a.port, a.db)
        print(f"Canon listening on http://{a.host}:{a.port}", file=sys.stderr)
        httpd.serve_forever()
    if a.cmd == "mcp":
        sys.argv = ["canon.mcp_server", "--db", a.db]
        from .mcp_server import main as mcp_main
        mcp_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
