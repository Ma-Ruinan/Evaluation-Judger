"""Optional bounded orchestration of the unchanged native evaluation command."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .judge import write_json


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _binding(config_path, tasks, config):
    files = {Path(config["process_excel"]["path"]).resolve(), config_path.resolve()}
    for task in tasks:
        files.update((task.question, task.rubric.path, *task.sources))
        for deliveries in task.deliveries.values():
            files.update(deliveries)
    payload = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    payload["opencode_override"] = hashlib.sha256(os.environ.get("OPENCODE_CONFIG_CONTENT", "").encode()).hexdigest()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _progress(run_dir):
    files = sorted((run_dir / "workspaces").glob("**/checkpoints/*.json"))
    files += sorted((run_dir / "results").glob("*/*.json"))
    files += [p for name in ("writer-draft.json", "narrative-review.json")
              if (p := run_dir / "reports" / name).exists()]
    return hashlib.sha256(b"".join(str(p.relative_to(run_dir)).encode() + p.read_bytes() for p in files)).hexdigest()


def _report_ready(config, run_dir, tasks):
    if not (run_dir / "reports/evaluation-report.docx").is_file():
        return False
    mode = config.get("report_mode", "auto")
    formal = mode == "formal" or mode == "auto" and (not config.get("tasks") or len(tasks) >= 4)
    if not formal:
        return True
    try:
        narrative = _read(run_dir / "reports/narrative.json")
        review = _read(run_dir / "reports/narrative-review.json")
        return (narrative.get("narrative", {}).get("source") == "opencode"
                and review.get("ok") is True
                and review.get("facts_hash") == narrative.get("facts_hash")
                and bool(review.get("facts_hash")))
    except (OSError, ValueError, AttributeError):
        return False


def _permanent(run_dir):
    markers = ("unauthorized", "invalid api key", "authentication failed", "model not found",
               "endpoint_migrated", "地址已迁移", "api 地址已迁移", "permission requested")
    failures = _read(run_dir / "failures.json")
    return any(any(marker in str(failure.get("message", "")).casefold() for marker in markers)
               for failure in failures) if isinstance(failures, list) else False


def _worker(command, project, archive):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    child = subprocess.Popen(command, cwd=project, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    write_json(archive / "auto-worker.json", {"pid": child.pid, "exited": False})
    def relay():
        with (archive / "auto-native.log").open("a", encoding="utf-8") as log:
            for line in child.stdout:
                log.write(line); log.flush(); print(line, end="", flush=True)
    reader = threading.Thread(target=relay, daemon=True)
    reader.start()
    try:
        while child.poll() is None:
            try:
                child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                print("评测进程仍在运行，正在等待模型或复核；有效检查点会保留。", flush=True)
        reader.join()
        return child.returncode
    finally:
        if child.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
            else:
                child.terminate()
            child.wait()
        reader.join(timeout=5)
        child.stdout.close()
        write_json(archive / "auto-worker.json", {"pid": child.pid, "exited": True, "returncode": child.returncode})


def supervise(config_path, config, project, run_dir, tasks, *, max_attempts=4, retry_delay=30):
    from .cli import _results
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max-auto-attempts must be a positive integer")
    if not math.isfinite(retry_delay) or retry_delay < 0:
        raise ValueError("auto-retry-delay must be finite and nonnegative")
    archive = run_dir / "operation-records"
    archive.mkdir(parents=True, exist_ok=True)
    lock = archive / "auto-resume.lock"
    worker = _read(archive / "auto-worker.json")
    if lock.exists():
        owner = _read(lock)
        if _alive(owner.get("pid")) or (not worker.get("exited", True) and _alive(worker.get("pid"))):
            raise RuntimeError("An evaluator is still active; refusing duplicate automatic run")
        lock.unlink()
    with lock.open("x", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid()}, handle)
    try:
        path = archive / "auto-resume-state.json"
        binding = _binding(config_path, tasks, config)
        state = _read(path)
        if state and state.get("binding") != binding:
            raise ValueError("Evaluation inputs/configuration changed; use a new run directory")
        state = state or {"binding": binding, "attempts": 0, "stalled": 0}
        total = len(tasks) * len(config["subjects"])
        command = [sys.executable, "-X", "utf8", "-u", "-m", "evaluation_judger.cli", "run", "--config", str(config_path.resolve())]
        _, missing = _results(config, run_dir, tasks)
        if not missing and state.get("status") == "completed" and _report_ready(config, run_dir, tasks):
            return 0
        while state["attempts"] < max_attempts:
            valid, missing = _results(config, run_dir, tasks)
            if not missing and state.get("status") == "completed" and _report_ready(config, run_dir, tasks):
                return 0
            before = _progress(run_dir)
            state["attempts"] += 1  # Persist budget before launching, including interrupted rounds.
            state["status"] = "running"
            write_json(path, state)
            print(f"自动评测第 {state['attempts']}/{max_attempts} 轮：已完成 {len(valid)}/{total} 份对象评分。", flush=True)
            code = _worker(command, project, archive)
            if _binding(config_path, tasks, config) != binding:
                raise ValueError("Inputs changed during evaluation; refusing further automatic scoring")
            after, missing = _results(config, run_dir, tasks)
            state["last_exit_code"] = code
            state["completed"] = len(after)
            if code == 0 and not missing and _report_ready(config, run_dir, tasks):
                state["status"] = "completed"
                write_json(path, state)
                print(f"评测及报告生成完成：{len(after)}/{total} 份评分。", flush=True)
                return 0
            state["stalled"] = state["stalled"] + 1 if len(after) <= len(valid) and _progress(run_dir) == before else 0
            state["status"] = "incomplete"
            write_json(path, state)
            if _permanent(run_dir) or state["stalled"] >= 3:
                break
            if state["attempts"] < max_attempts:
                print(f"本轮未完整结束；等待 {retry_delay:g} 秒后复用有效结果及检查点续跑。", flush=True)
                time.sleep(retry_delay)
        print("自动续跑已达到上限或遇到不可恢复问题；未完成评分不进入报告，现场已保留。", flush=True)
        return 1
    finally:
        lock.unlink(missing_ok=True)
