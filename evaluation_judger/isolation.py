"""Restrict built-in material reads to one task-participant workspace."""
from pathlib import Path
import json


def enable_image_reads(workspace: Path, model: str) -> None:
    """Declare image input only for an explicitly enabled, tested model."""
    provider, separator, name = model.partition("#")[0].partition("/")
    config_file = workspace / "opencode.jsonc"
    config = json.loads(config_file.read_text(encoding="utf-8-sig"))
    definition = config.get("provider", {}).get(provider, {}).get("models", {}).get(name)
    if not separator or not isinstance(definition, dict):
        raise ValueError("Image input requires the selected provider/model in the workspace configuration")
    modalities = definition.setdefault("modalities", {})
    modalities["input"] = list(dict.fromkeys([*modalities.get("input", ["text"]), "image"]))
    modalities.setdefault("output", ["text"])
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def restrict_reads(workspace: Path) -> None:
    root = workspace.resolve()
    worktree = next((parent for parent in (root, *root.parents) if (parent / ".git").exists()), root)
    relative = root.relative_to(worktree)
    # OpenCode 1.x matches read permissions against paths relative to the
    # Git worktree, which may be wider than its launch directory on Windows.
    paths = {str(relative), relative.as_posix(), str(root), root.as_posix()}
    read = {"*": "deny"}
    if worktree == root:
        # Without an enclosing repository, OpenCode's relative paths are
        # already local to this workspace. Keep parent traversal denied.
        read = {"*": "allow", "../*": "deny", "..\\*": "deny"}
    for path in sorted(paths):
        read[path] = "allow"
        read[path.rstrip("/\\") + "/*"] = "allow"
        read[path.rstrip("/\\") + "\\*"] = "allow"
    config_file = root / "opencode.jsonc"
    config = json.loads(config_file.read_text(encoding="utf-8-sig"))
    agent = config.setdefault("agent", {}).setdefault("judge", {})
    agent.setdefault("permission", {}).update({"read": read, "glob": "deny", "grep": "deny", "lsp": "deny"})
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
