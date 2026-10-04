"""Bounded rendering probes on local benchmark artifacts, with archived evidence."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import shutil
import threading
from urllib.parse import quote, unquote, urlsplit


def evidence_folder(workspace: Path, path: Path, request: dict) -> Path:
    key = hashlib.sha256(path.read_bytes() + json.dumps(request, sort_keys=True).encode()).hexdigest()[:16]
    folder = workspace / "inspection-evidence" / key
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def powerpoint(workspace: Path, path: Path, request: dict) -> dict:
    if os.name != "nt" or path.suffix.lower() != ".pptx":
        raise ValueError("Native PowerPoint inspection requires Windows, installed PowerPoint and a .pptx material")
    folder = evidence_folder(workspace, path, {"renderer_revision": 1})
    cache = folder / "geometry.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = Path(__file__).parent / "tool_templates/pptx_geometry.ps1"
        result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 "-InputPath", str(path), "-OutputFolder", str(folder)],
                                capture_output=True, text=True, encoding="utf-8", timeout=45)
        if result.returncode:
            raise ValueError("PowerPoint rendering failed: " + result.stderr[-1200:])
        data = json.loads(result.stdout.strip().lstrip("\ufeff"))
        for slide in data["slides"]:
            pairs = []
            shapes = slide["text_shapes"]
            for i, left in enumerate(shapes):
                for right in shapes[i+1:]:
                    a, b = left["bounds"], right["bounds"]
                    width = max(0, min(a["left"]+a["width"], b["left"]+b["width"])-max(a["left"], b["left"]))
                    height = max(0, min(a["top"]+a["height"], b["top"]+b["height"])-max(a["top"], b["top"]))
                    area = min(a["width"]*a["height"], b["width"]*b["height"])
                    if area and width*height/area > .4:
                        pairs.append({"shape_ids": [left["shape_id"], right["shape_id"]], "overlap_fraction": width*height/area})
            slide["overlap_candidates"] = pairs
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    selected = request.get("slides")
    if selected is not None:
        if not isinstance(selected, list) or any(not isinstance(i, int) or not 1 <= i <= data["slide_count"] for i in selected):
            raise ValueError("slides must contain valid 1-based slide numbers")
        data = {**data, "slides": [slide for slide in data["slides"] if slide["slide"] in selected]}
    return {"evidence_file": str(path.relative_to(workspace)), "measurement_record": str(cache.relative_to(workspace)), **data}




def browser(workspace: Path, path: Path, request: dict) -> dict:
    if path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Browser preview requires an authorized local HTML entry")
    runtime = Path(__file__).resolve().parent.parent / ".evaluation-judger/tool-runtime"
    if not (runtime / "node_modules/playwright/package.json").is_file():
        raise ValueError("Browser inspection requires the documented pinned Node Playwright dependency")
    node = shutil.which("node")
    if node is None:
        raise ValueError("Browser inspection requires Node.js on PATH")
    widths = request.get("widths", [1280, 1920])
    actions = request.get("actions", [])
    if not isinstance(widths, list) or not 1 <= len(widths) <= 3 or any(not isinstance(w, int) or not 320 <= w <= 1920 for w in widths):
        raise ValueError("Select 1-3 viewport widths in 320-1920 pixels")
    if not isinstance(actions, list) or len(actions) > 8:
        raise ValueError("At most 8 finite browser actions are allowed")
    if len(widths)*len(actions)>12:
        raise ValueError("At most 12 actions across all viewport widths are allowed")
    if request.get("browser_channel", "chrome") not in {"chrome", "msedge"}:
        raise ValueError("Supported installed browser channels: chrome, msedge")
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    assets = {}
    for entry in manifest["files"]:
        if entry["category"] not in {"sources", "deliveries"}:
            continue
        candidate = (workspace / entry["file"]).resolve()
        if candidate.is_relative_to(path.parent) and candidate.is_relative_to(workspace):
            assets["/" + candidate.relative_to(path.parent).as_posix()] = candidate

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            asset = assets.get(unquote(urlsplit(self.path).path))
            if asset is None:
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
            self.end_headers(); self.wfile.write(asset.read_bytes())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    folder = evidence_folder(workspace, path, request)
    try:
        payload = {"runtime": str(runtime), "origin": origin,
                   "entry": origin+"/"+quote(path.name), "assets": list(assets),
                   "folder": str(folder), "widths": widths, "actions": actions,
                   "channel": request.get("browser_channel", "chrome")}
        process = subprocess.run([node, str(Path(__file__).parent / "tool_templates/browser_render.mjs")],
                                 input=json.dumps(payload), capture_output=True, text=True,
                                 encoding="utf-8", timeout=110)
        if process.returncode:
            raise ValueError("Browser rendering failed: " + process.stderr[-1500:])
        result = json.loads(process.stdout)
        result["evidence_file"] = str(path.relative_to(workspace))
        for snapshot in result["snapshots"]:
            snapshot["screenshot"] = str(Path(snapshot["screenshot"]).relative_to(workspace))
    finally:
        server.shutdown(); server.server_close()
    (folder/"browser-measurements.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
