"""Local stdio MCP transport for the existing bounded material inspector."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

DESCRIPTION = (
    "Inspect only manifest-authorized files in this task-participant workspace. "
    "request is JSON with file (original manifest path) and operation: summary, package, xml, cells, rows, "
    "stats, correlation, group, json, text, pptx_visual or browser. Optional sheet/header_row, range, columns "
    "(header name, Excel letter or 1-based integer), x/y, group_by, filters [{column,op,value}], offset/limit. "
    "For JSON use path as an array; for text offsets are characters. package lists OOXML chart definitions; "
    "xml reads a selected part. pptx_visual uses installed PowerPoint to export PDF and measure actual text "
    "bounds; optional slides is a list of 1-based indexes. browser uses installed Chrome/Edge with widths "
    "(1-3 viewports, 320-1920) and finite actions [{type:click/fill/select,selector,value}], at most 12 total. "
    "It allows local webpage JavaScript, blocks external requests, measures DOM/text/interaction and archives "
    "screenshots. Never execute delivered shell/Python/native code. Geometry candidates require interpretation; "
    "chart XML is not proof of appearance. Cite original material locations, not tool output as a primary source."
)


def handle(message: dict, workspace: Path) -> dict | None:
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message.get("method"), message.get("params", {})
    try:
        if method == "initialize":
            versions = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
            requested = params.get("protocolVersion")
            result = {"protocolVersion": requested if requested in versions else "2024-11-05",
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "evaluation-materials", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": "inspector", "description": DESCRIPTION,
                      "inputSchema": {"type": "object", "properties": {"request": {"type": "string"}},
                                      "required": ["request"], "additionalProperties": False}}]}
        elif method == "tools/call":
            arguments = params.get("arguments", {})
            if params.get("name") != "inspector" or set(arguments) != {"request"} or not isinstance(arguments["request"], str):
                raise ValueError("Expected inspector with one JSON string request")
            command = [sys.executable, "-X", "utf8", str(Path(__file__).with_name("inspection.py")),
                       "--workspace", str(workspace.resolve()), "--request", arguments["request"]]
            try:
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                           timeout=120, check=False,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                text = completed.stdout.strip() or completed.stderr[-4000:]
                result = {"content": [{"type": "text", "text": text}], "isError": completed.returncode != 0}
            except subprocess.TimeoutExpired:
                result = {"content": [{"type": "text", "text": "Material inspection exceeded its 120-second limit."}], "isError": True}
        else:
            response["error"] = {"code": -32601, "message": "Method not supported"}
            return response
        response["result"] = result
    except (ValueError, KeyError, TypeError) as exc:
        response["error"] = {"code": -32602, "message": str(exc)}
    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    workspace = parser.parse_args().workspace.resolve()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Expected a JSON-RPC object")
            response = handle(message, workspace)
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC request"}}
        if response is not None:
            try:
                print(json.dumps(response, ensure_ascii=False), flush=True)
            except BrokenPipeError:
                break


if __name__ == "__main__":
    main()
