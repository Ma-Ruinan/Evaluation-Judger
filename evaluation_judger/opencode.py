"""A small OpenCode CLI adapter; each call is a fresh isolated session."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from json_repair import repair_json


class JudgeError(RuntimeError):
    pass


def environment(project: Path) -> dict[str, str]:
    env = dict(os.environ)
    env_file = project / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if not env.get("AIAAA_API_KEY"):
        raise JudgeError("AIAAA_API_KEY is missing; set it in the environment or local .env")
    return env


def _executable(project: Path, major: int) -> str:
    configured = os.environ.get("OPENCODE_BIN")
    if configured:
        return configured
    if major == 1:
        local = project / ".evaluation-judger" / "opencode-v1" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if local.is_file():
            return str(local)
        raise JudgeError("OpenCode 1.x is required for this gateway. Install locally: npm install --prefix .evaluation-judger/opencode-v1 opencode-ai@1.18.23 --no-save")
    native = shutil.which("opencode.exe")
    return native or shutil.which("opencode") or "opencode"


def _clean(value: str, env: dict[str, str]) -> str:
    key = env.get("AIAAA_API_KEY", "")
    return value.replace(key, "[REDACTED]") if key else value


def parse_events(raw: str) -> tuple[str, str | None, bool]:
    texts, error, used_read = [], None, False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        if event.get("type") == "error":
            error = str(event.get("error", {}))[:1000]
        if part.get("tool") == "read" and isinstance(part.get("state"), dict) and part["state"].get("status") == "completed":
            used_read = True
    return "\n".join(texts).strip(), error, used_read


def extract_json(text: str) -> dict:
    block = re.search(r"BEGIN_JUDGMENT\s*(.*?)\s*END_JUDGMENT", text, re.S)
    payload = block.group(1) if block else text
    payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload.strip(), flags=re.I)
    try:
        result = json.loads(payload, strict=False)
    except json.JSONDecodeError as exc:
        try:
            result = repair_json(payload, return_objects=True)
        except Exception as repair_error:
            raise JudgeError(f"Invalid JSON: {exc}; repair failed: {repair_error}") from exc
    if not isinstance(result, dict):
        raise JudgeError("Expected JSON object")
    return result


def event_session(path: Path) -> str | None:
    session = None
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
            session = event.get("sessionID") or event.get("part", {}).get("sessionID") or session
        except (ValueError, TypeError, AttributeError):
            continue
    return session


class TaskSession:
    """Reuse evidence context only inside one fingerprinted task-participant workspace."""
    def __init__(self, workspace: Path, project: Path, model: str, major: int, fingerprint: str):
        self.workspace, self.project, self.model, self.major = workspace, project, model, major
        self.fingerprint = fingerprint
        self.checkpoint = workspace / "checkpoints" / "judge-session.json"
        self.session_id = None
        if major != 1:
            return
        recover_events = not self.checkpoint.exists()
        if self.checkpoint.exists():
            try:
                saved = json.loads(self.checkpoint.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("Invalid session checkpoint")
                if saved.get("fingerprint") == fingerprint and isinstance(saved.get("session_id"), str):
                    self.session_id = saved["session_id"]
            except (OSError, ValueError, TypeError):
                recover_events = True
        if recover_events and workspace.name == fingerprint[:12]:
            for event in sorted((workspace / "events").glob("atoms-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
                self.session_id = event_session(event)
                if self.session_id:
                    break

    def ask(self, prompt: str, title: str, output: Path) -> tuple[str, bool]:
        try:
            return run(self.workspace, prompt, title, output, self.project, self.model,
                       major=self.major, session_id=self.session_id)
        finally:
            if self.major == 1:
                session = event_session(output)
                if session:
                    self.session_id = session
                    self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.checkpoint.with_suffix(".json.tmp")
                    temporary.write_text(json.dumps({"fingerprint": self.fingerprint, "session_id": session}), encoding="utf-8")
                    temporary.replace(self.checkpoint)


def run(workspace: Path, prompt: str, title: str, output: Path, project: Path, model: str, timeout: int = 900, major: int = 1, *, session_id: str | None = None, _continue_length: bool = True) -> tuple[str, bool]:
    env = environment(project)
    if timeout == 900:
        timeout = int(env.get("OPENCODE_REQUEST_TIMEOUT_SECONDS", "900"))
    if timeout < 1:
        raise JudgeError("OPENCODE_REQUEST_TIMEOUT_SECONDS must be positive")
    executable = _executable(project, major)
    if major == 1:
        # V1 and V2 use incompatible global SQLite schemas.
        isolated = project / ".evaluation-judger" / "opencode-v1" / "state"
        env["XDG_DATA_HOME"] = str(isolated / "data")
        env["XDG_CONFIG_HOME"] = str(isolated / "config")
        env["XDG_CACHE_HOME"] = str(isolated / "cache")
    model_name, _, variant = model.partition("#")
    command = [executable, "run", "--format", "json", "--agent", "judge", "--model", model_name, "--title", title]
    if major == 2:
        command.insert(2, "--standalone")
    elif variant:
        command += ["--variant", variant]
    if session_id:
        command += ["--session", session_id]
    if len(prompt) > 16_000:
        prompt_file = workspace / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        # yargs treats arguments after --file as additional file names.
        command += ["Read the attached UTF-8 prompt and follow it. Return only the requested JSON markers.", "--file", "prompt.txt"]
    else:
        command.append(prompt)
    try:
        completed = subprocess.run(command, cwd=workspace, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        # subprocess still exposes output captured before the timeout. Preserve it
        # so a later run can salvage completed atom decisions.
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_clean(partial, env), encoding="utf-8")
        raise JudgeError(f"OpenCode timed out after {timeout}s") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_clean(completed.stdout, env), encoding="utf-8")
    answer, terminal_error, used_read = parse_events(completed.stdout)
    finishes, response_session = [], None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
            response_session = event.get("sessionID") or response_session
            if event.get("type") == "step_finish":
                finishes.append(event.get("part", {}))
        except (ValueError, TypeError):
            continue
    complete_json = False
    try:
        payload = re.sub(r"^BEGIN_JUDGMENT\s*", "", answer.strip())
        payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload, flags=re.I)
        complete_json = isinstance(json.loads(payload), dict)
    except (ValueError, TypeError):
        pass
    length_limited = bool(finishes and finishes[-1].get("reason") == "length" and "END_JUDGMENT" not in answer and not complete_json)
    if length_limited and major == 1 and response_session and _continue_length:
        continuation = output.with_name(output.stem + "-continuation.jsonl")
        final_answer, more_read = run(
            workspace,
            "The previous response reached its length limit before completing the judgment JSON. "
            "Use the evidence and findings already read in THIS task session. Finish the requested judgment now: "
            "return the complete JSON between BEGIN_JUDGMENT and END_JUDGMENT, not just the missing suffix. "
            "Keep explanations specific and concise. Do not repeat source exploration already completed.",
            title + " finish judgment", continuation, project, model, timeout, major,
            session_id=response_session, _continue_length=False)
        return final_answer, used_read or more_read
    if length_limited:
        raise JudgeError("Response length limit reached before complete judgment text; retry a smaller atom group")
    if not answer:
        details = _clean(completed.stderr[-1500:] or terminal_error or "No response text", env)
        raise JudgeError(f"OpenCode returned no answer (exit {completed.returncode}): {details}")
    if completed.returncode != 0 and not re.search(r"END_JUDGMENT", answer):
        raise JudgeError(f"OpenCode exited {completed.returncode}: {_clean(completed.stderr[-1000:], env)}")
    return answer, used_read


def probe(workspace: Path, project: Path, model: str, major: int = 1) -> None:
    nonce = f"probe-{time.time_ns()}"
    (workspace / "probe.txt").write_text(nonce, encoding="utf-8")
    prompt = 'Use the read tool to read probe.txt. Then return BEGIN_JUDGMENT\n{"nonce":"the exact contents of probe.txt"}\nEND_JUDGMENT. Do not guess.'
    response, used_read = run(workspace, prompt, "judger read probe", workspace / "probe-events.jsonl", project, model, 180, major)
    if extract_json(response).get("nonce") != nonce or not used_read:
        raise JudgeError("OpenCode did not complete a read-tool roundtrip")
