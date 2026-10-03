"""A Claude agent that answers clinical questions using Canon's tools.

    pip install anthropic
    export ANTHROPIC_API_KEY=...
    python examples/claude_agent.py "Is Maria's diabetes getting better, and is it safe to prescribe amoxicillin?"

The agent loads the 7 sample documents (fax, HL7, C-CDA, FHIR, X12, CSV, PDF)
into an in-memory Canon, then reasons over the normalized record with tool
calls. Every claim it makes can be traced back to a source via get_provenance.
"""

from __future__ import annotations

import glob
import json
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from canon.service import Canon  # noqa: E402
from canon.tools import anthropic_tools, call_tool  # noqa: E402

MODEL = "claude-opus-5-5"
SYSTEM = (
    "You are a clinical assistant working over Canon, a service that normalizes a patient's records from many "
    "sources into one coded record. Use the tools to answer. Before stating anything about medications or "
    "allergies, call get_conflicts and mention unresolved conflicts. Cite the source format and date for key "
    "facts. You support clinicians; flag anything that needs human confirmation rather than guessing."
)


def load_samples(canon: Canon) -> str:
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "maria_chen")
    pid = None
    for path in sorted(glob.glob(os.path.join(root, "*"))):
        with open(path, "rb") as fh:
            pid = canon.ingest(fh.read(), filename=os.path.basename(path),
                               patient_id=pid if path.endswith(".csv") else None)["patient_id"]
    return pid


def run(question: str) -> str:
    canon = Canon()
    pid = load_samples(canon)
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": f"Patient id: {pid}\n\n{question}"}]
    while True:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=anthropic_tools(),
            messages=messages,
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return f"[declined: {response.stop_details}]"
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  -> {block.name}({json.dumps(block.input)[:100]})", file=sys.stderr)
                out = call_tool(canon, block.name, block.input, actor="claude-agent")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out), "is_error": "error" in out})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or ("Summarize this patient's diabetes control over time, list current medications "
                                   "with doses, and tell me whether amoxicillin would be safe to prescribe.")
    print(run(q))
