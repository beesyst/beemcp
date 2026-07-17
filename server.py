from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from mcp.types import ToolAnnotations
from starlette.responses import PlainTextResponse
from mcp.server.fastmcp import FastMCP


HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BEE_MCP_CONFIG", HERE / "projects.yml")).expanduser()
READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _check_relative_path(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} must be a string, got {type(value).__name__}")
    if not value:
        raise RuntimeError(f"{label} must not be empty")
    p = Path(value)
    if p.is_absolute():
        raise RuntimeError(f"{label} must be relative, got absolute: {value}")
    if ".." in p.parts:
        raise RuntimeError(f"{label} must not contain '..': {value}")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"Config not found: {CONFIG_PATH}")
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError("projects.yml must be a mapping")

    limits = raw.get("limits")
    if not isinstance(limits, dict):
        raise RuntimeError("projects.yml must contain limits mapping")
    for key in ("max_file_bytes", "max_diff_chars", "max_context_chars",
                 "max_tree_entries", "max_search_results"):
        val = limits.get(key)
        if not isinstance(val, int) or val <= 0:
            raise RuntimeError(f"limits.{key} must be a positive integer, got {val!r}")

    deny = raw.get("deny_paths")
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise RuntimeError("deny_paths must be a list of strings")

    projects = raw.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise RuntimeError("projects must be a non-empty mapping")

    for name, cfg in projects.items():
        if not isinstance(cfg, dict):
            raise RuntimeError(f"project {name!r}: must be a mapping")

        source_raw = cfg.get("source")
        if not isinstance(source_raw, str):
            raise RuntimeError(f"project {name!r}: source must be a string")
        source = Path(source_raw).expanduser().resolve()
        if not source.is_dir() or not (source / ".git").is_dir():
            raise RuntimeError(f"project {name!r}: source is not a Git repository: {source}")

        base_branch = cfg.get("base_branch")
        if not isinstance(base_branch, str) or not base_branch:
            raise RuntimeError(f"project {name!r}: base_branch must be a non-empty string")
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", "refs/heads/" + base_branch],
                cwd=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"project {name!r}: base_branch {base_branch!r} does not exist in {source}"
            )

        modes = cfg.get("context_modes")
        if not isinstance(modes, dict):
            raise RuntimeError(f"project {name!r}: context_modes must be a mapping")

        mode_names = set()
        for mname, mcfg in modes.items():
            if not isinstance(mcfg, dict):
                raise RuntimeError(f"project {name!r}: mode {mname!r} must be a mapping")
            if mname in mode_names:
                raise RuntimeError(f"project {name!r}: duplicate mode name {mname!r}")
            mode_names.add(mname)

            for key in ("files", "tree_roots"):
                items = mcfg.get(key)
                if items is not None:
                    if not isinstance(items, list) or not all(
                        isinstance(i, str) for i in items
                    ):
                        raise RuntimeError(
                            f"project {name!r}: mode {mname!r}.{key} must be a list of strings"
                        )
                    for item in items:
                        _check_relative_path(item, f"project {name!r}: mode {mname!r}.{key} item")

        for mname, mcfg in modes.items():
            ext = mcfg.get("extends")
            if ext is not None:
                if not isinstance(ext, str) or ext not in modes:
                    raise RuntimeError(
                        f"project {name!r}: mode {mname!r} extends non-existent mode {ext!r}"
                    )

        for mname in modes:
            seen = set()
            current = mname
            while current in modes:
                if current in seen:
                    raise RuntimeError(
                        f"project {name!r}: cycle in extends for mode {mname!r}"
                    )
                seen.add(current)
                current = modes[current].get("extends")

        related_list = cfg.get("related")
        if related_list is not None:
            if not isinstance(related_list, list):
                raise RuntimeError(f"project {name!r}: related must be a list")
            for ritem in related_list:
                if not isinstance(ritem, dict):
                    raise RuntimeError(f"project {name!r}: related item must be a mapping")
                rproj = ritem.get("project")
                if not isinstance(rproj, str) or rproj not in projects:
                    raise RuntimeError(
                        f"project {name!r}: related project {rproj!r} not in projects"
                    )
                rfiles = ritem.get("files")
                if not isinstance(rfiles, list):
                    raise RuntimeError(f"project {name!r}: related files must be a list")
                for rf in rfiles:
                    _check_relative_path(rf, f"project {name!r}: related file")

    return raw


CONFIG = load_config()
LIMITS: dict[str, int] = CONFIG["limits"]
DENY_PATTERNS: list[str] = CONFIG["deny_paths"]
PROJECTS: dict[str, Any] = CONFIG["projects"]

mcp = FastMCP(
    name="Bee Dev Read-Only Review",
    instructions=(
        "This server provides read-only access to allowlisted Git repositories. "
        "All tools are read-only: no files are modified, no agents or checks are "
        "started, and no Git write operations are performed.\n\n"
        "Before providing an architecture or review answer, call get_project_context "
        "to load the configured project files for the chosen mode. "
        "Before final review, call get_review_bundle to obtain the current diff and "
        "changed file contents.\n\n"
        "omitted_files and related_omitted_files contain objects with target, path, "
        "and reason. Files with reason 'oversized' or 'context_limit' CAN be read: "
        "use read_project_file with the object's target and path, and with "
        "start_line=1 (and start_column=0). The response includes next_line and "
        "next_column — pass them as start_line and start_column in the next call. "
        "Repeat until next_line is null. For a single line longer than "
        "max_context_chars, next_column advances across chunks. Files with reason "
        "'denied', 'binary', 'broken', or 'escaping' CANNOT be read and should be "
        "skipped.\n\n"
        "Available tools:\n"
        "- list_projects: show allowlisted projects and their context modes\n"
        "- list_worktrees: discover Git worktrees for a project\n"
        "- get_project_context: read configured files for a target and mode\n"
        "- get_review_bundle: get diff, changed files, and context for review\n"
        "- read_project_file: read a single file by relative path (supports "
        "start_line and start_column for chunked reading)\n"
        "- search_project: fixed-string search within configured boundaries"
    ),
    host="127.0.0.1",
    port=8000,
)


def _run_git(argv: list[str], cwd: Path, timeout: int = 30) -> str:
    try:
        cp = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Git executable not found: {exc}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Git command timed out after {timeout}s: {' '.join(argv)}")
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"Git command failed: {' '.join(argv)}: {msg}")
    return cp.stdout


def _is_denied(rel_path: str) -> bool:
    for pattern in DENY_PATTERNS:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def _parse_name_status(output: str) -> tuple[list[dict[str, str]], list[str]]:
    renamed: list[dict[str, str]] = []
    names: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            if len(parts) >= 3:
                renamed.append({"from": parts[1], "to": parts[2]})
                names.append(parts[1])
                names.append(parts[2])
        else:
            names.append(parts[-1])
    return renamed, names


def _resolve_target(target: str) -> dict[str, Any]:
    project_cfg = PROJECTS.get(target)
    if project_cfg is not None:
        source = Path(project_cfg["source"]).expanduser().resolve()
        return {
            "target": target,
            "project": target,
            "root": source,
            "branch": _run_git(["git", "branch", "--show-current"], source).strip(),
            "head": _run_git(["git", "rev-parse", "HEAD"], source).strip(),
            "is_main": True,
            "dirty": bool(_run_git(["git", "status", "--porcelain"], source).strip()),
        }

    candidates: list[dict[str, Any]] = []
    for pname, pcfg in PROJECTS.items():
        source = Path(pcfg["source"]).expanduser().resolve()
        common_dir = _run_git(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            source,
        ).strip()
        worktree_out = _run_git(
            ["git", "worktree", "list", "--porcelain", "-z"],
            source,
        )
        entries = worktree_out.split("\0")
        i = 0
        while i < len(entries):
            line = entries[i].strip()
            if not line:
                i += 1
                continue
            if line.startswith("worktree "):
                wt_path = Path(line[len("worktree ") :])
                wt_branch = ""
                wt_head = ""
                i += 1
                while i < len(entries):
                    sub = entries[i].strip()
                    if not sub:
                        break
                    if sub.startswith("HEAD "):
                        wt_head = sub[len("HEAD ") :]
                    elif sub.startswith("branch "):
                        ref = sub[len("branch ") :]
                        if ref.startswith("refs/heads/"):
                            wt_branch = ref[len("refs/heads/") :]
                    i += 1
                try:
                    wt_common = _run_git(
                        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                        wt_path,
                    ).strip()
                except RuntimeError:
                    continue
                if wt_common != common_dir:
                    continue
                if wt_path.name == target:
                    is_dirty = bool(
                        _run_git(["git", "status", "--porcelain"], wt_path).strip()
                    )
                    if candidates:
                        raise RuntimeError(
                            f"Ambiguous target {target!r}: found in multiple worktrees"
                        )
                    candidates.append(
                        {
                            "target": target,
                            "project": pname,
                            "root": wt_path,
                            "branch": wt_branch,
                            "head": wt_head,
                            "is_main": False,
                            "dirty": is_dirty,
                        }
                    )
            else:
                i += 1

        if source.name == target:
            is_dirty = bool(
                _run_git(["git", "status", "--porcelain"], source).strip()
            )
            candidate = {
                "target": target,
                "project": pname,
                "root": source,
                "branch": _run_git(["git", "branch", "--show-current"], source).strip(),
                "head": _run_git(["git", "rev-parse", "HEAD"], source).strip(),
                "is_main": True,
                "dirty": is_dirty,
            }
            if candidates:
                raise RuntimeError(
                    f"Ambiguous target {target!r}: found in multiple worktrees"
                )
            candidates.append(candidate)

    if not candidates:
        raise ValueError(
            f"Unknown target: {target!r}. Use list_projects or list_worktrees."
        )
    return candidates[0]


def _classify_path_meta(target_root: Path, rel: str) -> dict[str, Any]:
    if _is_denied(rel):
        return {"status": "denied", "reason": rel}

    try:
        resolved = (target_root / rel).resolve(strict=True)
    except OSError:
        return {"status": "broken", "reason": rel}

    if not resolved.is_relative_to(target_root):
        return {"status": "escaping", "reason": f"{rel} resolves outside target root"}

    resolved_rel = str(resolved.relative_to(target_root))
    if _is_denied(resolved_rel):
        return {"status": "denied", "reason": resolved_rel}

    if resolved.is_dir():
        return {"status": "broken", "reason": f"{rel} is a directory"}

    try:
        with open(resolved, "rb") as f:
            header = f.read(8192)
    except OSError:
        return {"status": "broken", "reason": rel}

    if b"\x00" in header:
        return {"status": "binary", "reason": rel}

    return {"status": "safe", "resolved": resolved, "size": resolved.stat().st_size}


def _classify_path(target_root: Path, rel: str) -> dict[str, Any]:
    meta = _classify_path_meta(target_root, rel)
    if meta["status"] != "safe":
        return meta

    if meta["size"] > LIMITS["max_file_bytes"]:
        return {"status": "oversized", "reason": rel}

    try:
        data = meta["resolved"].read_bytes()
    except OSError:
        return {"status": "broken", "reason": rel}

    return {"status": "safe", "content": data.decode("utf-8", errors="replace")}


def _safe_read_file_chunked(
    target_root: Path,
    raw_path: str,
    start_line: int = 1,
    start_column: int = 0,
) -> dict[str, Any]:
    if start_line < 1:
        raise ValueError(f"start_line must be >= 1, got {start_line}")
    if start_column < 0:
        raise ValueError(f"start_column must be >= 0, got {start_column}")

    meta = _classify_path_meta(target_root, raw_path)
    if meta["status"] != "safe":
        raise ValueError(f"Cannot read {raw_path}: {meta['status']}")

    resolved = meta["resolved"]
    max_chars = LIMITS["max_context_chars"]

    parts: list[str] = []
    total = 0
    cur_line = 1
    cur_col = 0
    end_line: int | None = None

    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
        while cur_line < start_line:
            chunk = f.readline(max_chars)
            if not chunk:
                return {
                    "path": raw_path, "start_line": start_line,
                    "start_column": start_column, "end_line": None,
                    "next_line": None, "next_column": None, "content": "",
                }
            if chunk.endswith("\n"):
                cur_line += 1

        remaining = start_column
        while remaining > 0:
            chunk = f.readline(min(max_chars, remaining))
            if not chunk:
                raise ValueError(
                    f"start_column exceeds line {start_line}: {start_column}"
                )
            remaining -= len(chunk)
            if chunk.endswith("\n"):
                raise ValueError(
                    f"start_column exceeds line {start_line}: {start_column}"
                )

        cur_col = start_column

        while total < max_chars:
            chunk = f.readline(max_chars - total)
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
            end_line = cur_line
            if chunk.endswith("\n"):
                cur_line += 1
                cur_col = 0
            else:
                cur_col += len(chunk)

        has_more = bool(f.read(1))

    return {
        "path": raw_path,
        "start_line": start_line,
        "start_column": start_column,
        "end_line": end_line,
        "next_line": cur_line if has_more else None,
        "next_column": cur_col if has_more else None,
        "content": "".join(parts),
    }


def _mode_files(project_cfg: dict[str, Any], mode: str) -> list[str]:
    modes = project_cfg.get("context_modes", {})
    if mode not in modes:
        raise ValueError(f"Unknown mode: {mode}")
    collected: list[str] = []
    seen: set[str] = set()
    stack = [mode]
    while stack:
        current = stack.pop()
        mcfg = modes[current]
        ext = mcfg.get("extends")
        if ext:
            stack.append(ext)
        for f in mcfg.get("files", []):
            if f not in seen:
                seen.add(f)
                collected.append(f)
    return collected


def _mode_tree_roots(project_cfg: dict[str, Any], mode: str) -> list[str]:
    modes = project_cfg.get("context_modes", {})
    if mode not in modes:
        raise ValueError(f"Unknown mode: {mode}")
    collected: list[str] = []
    seen: set[str] = set()
    stack = [mode]
    while stack:
        current = stack.pop()
        mcfg = modes[current]
        ext = mcfg.get("extends")
        if ext:
            stack.append(ext)
        for r in mcfg.get("tree_roots", []):
            if r not in seen:
                seen.add(r)
                collected.append(r)
    return collected


def _read_mode_files(
    target_root: Path, project_cfg: dict[str, Any], mode: str, target: str = ""
) -> tuple[dict[str, str], list[dict[str, str]]]:
    files_to_read = _mode_files(project_cfg, mode)
    contents: dict[str, str] = {}
    omitted: list[dict[str, str]] = []
    remaining = LIMITS["max_context_chars"]
    for rel in files_to_read:
        if remaining <= 0:
            omitted.append({"target": target, "path": rel, "reason": "context_limit"})
            continue
        meta = _classify_path_meta(target_root, rel)
        if meta["status"] != "safe":
            omitted.append(
                {"target": target, "path": rel, "reason": meta.get("status", "unknown")}
            )
            continue
        if meta["size"] > LIMITS["max_file_bytes"]:
            omitted.append({"target": target, "path": rel, "reason": "oversized"})
            continue
        if meta["size"] > remaining:
            omitted.append({"target": target, "path": rel, "reason": "context_limit"})
            continue
        result = _classify_path(target_root, rel)
        contents[rel] = result["content"]
        remaining -= len(result["content"])
    return contents, omitted


def _read_related(
    mode: str, project_cfg: dict[str, Any], max_chars: int | None = None
) -> tuple[dict[str, str], list[dict[str, str]]]:
    if max_chars is None:
        max_chars = LIMITS["max_context_chars"]
    related = project_cfg.get("related", [])
    result: dict[str, str] = {}
    omitted: list[dict[str, str]] = []
    remaining = max_chars
    for ritem in related:
        rproj = ritem["project"]
        rpcfg = PROJECTS[rproj]
        rsource = Path(rpcfg["source"]).expanduser().resolve()
        for rf in ritem["files"]:
            if remaining <= 0:
                omitted.append({"target": rproj, "path": rf, "reason": "context_limit"})
                continue
            meta = _classify_path_meta(rsource, rf)
            if meta["status"] != "safe":
                omitted.append(
                    {"target": rproj, "path": rf, "reason": meta.get("status", "unknown")}
                )
                continue
            if meta["size"] > LIMITS["max_file_bytes"]:
                omitted.append({"target": rproj, "path": rf, "reason": "oversized"})
                continue
            if meta["size"] > remaining:
                omitted.append({"target": rproj, "path": rf, "reason": "context_limit"})
                continue
            key = f"{rproj}/{rf}"
            classification = _classify_path(rsource, rf)
            result[key] = classification["content"]
            remaining -= len(classification["content"])
    return result, omitted


def _build_repository_tree(
    target_root: Path, tree_roots: list[str], max_entries: int
) -> list[str]:
    entries: list[str] = []
    for root in tree_roots:
        root_path = target_root / root
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames.sort()
            filenames.sort()
            rel_dir = Path(dirpath).relative_to(target_root)
            for f in filenames:
                rel = str(rel_dir / f)
                if _is_denied(rel):
                    continue
                entries.append(rel)
                if len(entries) >= max_entries:
                    return entries
    return entries


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_projects() -> dict[str, Any]:
    items = []
    for name in sorted(PROJECTS):
        pcfg = PROJECTS[name]
        modes = {}
        for mname, mcfg in (pcfg.get("context_modes") or {}).items():
            modes[mname] = {
                k: v for k, v in mcfg.items() if k in ("files", "tree_roots", "extends")
            }
        related_list = pcfg.get("related", [])
        related_projects = [r["project"] for r in related_list] if related_list else []
        items.append(
            {
                "project": name,
                "base_branch": pcfg.get("base_branch", "main"),
                "context_modes": modes,
                "related_projects": related_projects,
            }
        )
    return {"projects": items}


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_worktrees(project: str) -> dict[str, Any]:
    pcfg = PROJECTS.get(project)
    if pcfg is None:
        raise ValueError(f"Unknown project: {project!r}")
    source = Path(pcfg["source"]).expanduser().resolve()
    common_dir = _run_git(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        source,
    ).strip()

    out = _run_git(["git", "worktree", "list", "--porcelain", "-z"], source)
    worktrees: list[dict[str, Any]] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        line = entries[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("worktree "):
            wt_path = Path(line[len("worktree ") :])
            wt_branch = ""
            wt_head = ""
            i += 1
            while i < len(entries):
                sub = entries[i].strip()
                if not sub:
                    break
                if sub.startswith("HEAD "):
                    wt_head = sub[len("HEAD ") :]
                elif sub.startswith("branch "):
                    ref = sub[len("branch ") :]
                    if ref.startswith("refs/heads/"):
                        wt_branch = ref[len("refs/heads/") :]
                i += 1

            if not wt_path.is_dir():
                continue
            try:
                wt_common = _run_git(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    wt_path,
                ).strip()
            except RuntimeError:
                continue
            if wt_common != common_dir:
                continue

            is_main = wt_path == source
            is_dirty = bool(
                _run_git(["git", "status", "--porcelain"], wt_path).strip()
            )
            worktrees.append(
                {
                    "target": project if is_main else wt_path.name,
                    "project": project,
                    "branch": wt_branch,
                    "head": wt_head,
                    "is_main": is_main,
                    "dirty": is_dirty,
                }
            )
        else:
            i += 1

    return {"project": project, "worktrees": worktrees}


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_project_context(target: str, mode: str) -> dict[str, Any]:
    resolved = _resolve_target(target)
    pcfg = PROJECTS[resolved["project"]]
    modes = pcfg.get("context_modes", {})
    if mode not in modes:
        raise ValueError(
            f"Unknown mode {mode!r} for project {resolved['project']!r}. "
            f"Available modes: {', '.join(sorted(modes))}"
        )

    files_content, omitted = _read_mode_files(
        resolved["root"], pcfg, mode, target
    )
    used = sum(len(v) for v in files_content.values())
    related_content, related_omitted = _read_related(
        mode, pcfg, max(0, LIMITS["max_context_chars"] - used)
    )
    tree_roots = _mode_tree_roots(pcfg, mode)
    tree: list[str] = []
    if tree_roots:
        tree = _build_repository_tree(resolved["root"], tree_roots, LIMITS["max_tree_entries"])

    result: dict[str, Any] = {
        "target": target,
        "project": resolved["project"],
        "branch": resolved["branch"],
        "mode": mode,
        "files": files_content,
        "related_files": related_content,
        "omitted_files": omitted,
        "related_omitted_files": related_omitted,
    }
    if tree:
        result["repository_tree"] = tree
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_review_bundle(target: str, mode: str) -> dict[str, Any]:
    resolved = _resolve_target(target)
    pcfg = PROJECTS[resolved["project"]]
    base_branch = pcfg["base_branch"]
    target_root = resolved["root"]

    modes = pcfg.get("context_modes", {})
    if mode not in modes:
        raise ValueError(
            f"Unknown mode {mode!r} for project {resolved['project']!r}. "
            f"Available modes: {', '.join(sorted(modes))}"
        )

    status_out = _run_git(["git", "status", "--porcelain"], target_root)

    # Merge-base: required; failure stops the tool
    merge_base = _run_git(
        ["git", "merge-base", "HEAD", base_branch], target_root
    ).strip()

    # Collect changed file names and renames from all sources
    branch_renamed, branch_names = _parse_name_status(
        _run_git(
            ["git", "diff", "--name-status", merge_base, "HEAD", "--"],
            target_root,
        )
    )
    staged_renamed, staged_names = _parse_name_status(
        _run_git(
            ["git", "diff", "--name-status", "--cached", "--"], target_root
        )
    )
    unstaged_renamed, unstaged_names = _parse_name_status(
        _run_git(["git", "diff", "--name-status", "--"], target_root)
    )
    untracked_names = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard"], target_root
    ).splitlines()

    seen_renames: set[tuple[str, str]] = set()
    renamed_files: list[dict[str, str]] = []
    for r in branch_renamed + staged_renamed + unstaged_renamed:
        key = (r["from"], r["to"])
        if key not in seen_renames:
            seen_renames.add(key)
            renamed_files.append(r)

    all_changed: set[str] = set()
    all_changed.update(branch_names)
    all_changed.update(staged_names)
    all_changed.update(unstaged_names)
    all_changed.update(untracked_names)

    # Deleted files from all sources
    deleted_out = _run_git(
        ["git", "diff", "--diff-filter=D", "--name-only", merge_base, "HEAD", "--"],
        target_root,
    ).splitlines()
    staged_del = _run_git(
        ["git", "diff", "--diff-filter=D", "--name-only", "--cached", "--"],
        target_root,
    ).splitlines()
    unstaged_del = _run_git(
        ["git", "diff", "--diff-filter=D", "--name-only", "--"], target_root
    ).splitlines()
    deleted_files = list(dict.fromkeys(
        f for f in deleted_out + staged_del + unstaged_del if f
    ))

    deleted_denied: list[str] = []
    deleted_safe: list[str] = []
    for rel in deleted_files:
        if _is_denied(rel):
            deleted_denied.append(rel)
        else:
            deleted_safe.append(rel)

    safe_paths: set[str] = set()
    redacted_paths: list[str] = list(deleted_denied)
    omitted_files: list[dict[str, str]] = []
    file_contents: dict[str, str] = {}

    for rel in sorted(all_changed):
        if rel in deleted_files:
            continue
        meta = _classify_path_meta(target_root, rel)
        st = meta["status"]
        if st == "safe":
            if meta["size"] > LIMITS["max_file_bytes"]:
                omitted_files.append({"target": target, "path": rel, "reason": "oversized"})
                continue
            classification = _classify_path(target_root, rel)
            safe_paths.add(rel)
            file_contents[rel] = classification["content"]
        elif st == "denied":
            redacted_paths.append(rel)
        elif st == "escaping":
            redacted_paths.append(f"{rel} [escaping symlink]")
        elif st in ("binary",):
            omitted_files.append({"target": target, "path": rel, "reason": st})
        else:
            omitted_files.append({"target": target, "path": rel, "reason": st})

    safe_renames: list[dict[str, str]] = []

    for rename in renamed_files:
        paths = (rename["from"], rename["to"])
        denied = any(
            _is_denied(path) or path in redacted_paths
            for path in paths
        )

        if denied:
            for path in paths:
                safe_paths.discard(path)
                file_contents.pop(path, None)
                if path not in redacted_paths:
                    redacted_paths.append(path)
            continue

        target_meta = _classify_path_meta(target_root, rename["to"])
        if (
            target_meta["status"] != "safe"
            or target_meta["size"] > LIMITS["max_file_bytes"]
        ):
            for path in paths:
                if path in deleted_safe:
                    deleted_safe.remove(path)
            continue

        safe_renames.append(rename)

    diff_pathspec_set: set[str] = set(safe_paths) | set(deleted_safe)

    for rename in safe_renames:
        diff_pathspec_set.add(rename["from"])
        diff_pathspec_set.add(rename["to"])
    diff_pathspec = sorted(diff_pathspec_set)

    diff_parts: list[str] = []

    if diff_pathspec:
        branch_diff = _run_git(
            ["git", "diff", "--no-ext-diff", "--unified=8", merge_base, "HEAD", "--"]
            + diff_pathspec,
            target_root,
        )
        if branch_diff.strip():
            diff_parts.append(branch_diff)

        staged_diff = _run_git(
            ["git", "diff", "--no-ext-diff", "--unified=8", "--cached", "--"]
            + diff_pathspec,
            target_root,
        )
        if staged_diff.strip():
            diff_parts.append(staged_diff)

        unstaged_diff = _run_git(
            ["git", "diff", "--no-ext-diff", "--unified=8", "--"]
            + diff_pathspec,
            target_root,
        )
        if unstaged_diff.strip():
            diff_parts.append(unstaged_diff)

    safe_untracked: list[str] = []
    for f in untracked_names:
        if not f:
            continue
        if f not in safe_paths:
            continue
        classification = _classify_path(target_root, f)
        if classification["status"] != "safe":
            continue
        safe_untracked.append(f)
        diff_parts.append(
            f"\ndiff --git a/{f} b/{f}\n"
            f"new file mode 100644\n--- /dev/null\n+++ b/{f}\n"
            f"@@ new file @@\n{classification['content']}\n"
        )

    full_diff = "\n".join(d for d in diff_parts if d.strip())
    if len(full_diff) > LIMITS["max_diff_chars"]:
        full_diff = (
            full_diff[: LIMITS["max_diff_chars"]]
            + f"\n\n[TRUNCATED at {LIMITS['max_diff_chars']} characters]"
        )

    context, context_omitted = _read_mode_files(target_root, pcfg, mode, target)
    used_ctx = sum(len(v) for v in context.values())
    related_context, related_omitted = _read_related(
        mode, pcfg, max(0, LIMITS["max_context_chars"] - used_ctx)
    )

    seen_omit: set[tuple[str, str, str]] = set()
    for o in omitted_files:
        seen_omit.add((o["target"], o["path"], o["reason"]))
    for o in context_omitted:
        key = (o["target"], o["path"], o["reason"])
        if key not in seen_omit:
            seen_omit.add(key)
            omitted_files.append(o)

    return {
        "target": target,
        "project": resolved["project"],
        "branch": resolved["branch"],
        "head": resolved["head"],
        "base_branch": base_branch,
        "git_status": status_out.strip(),
        "changed_files": sorted(all_changed),
        "deleted_files": deleted_files,
        "renamed_files": renamed_files,
        "untracked_files": safe_untracked,
        "diff": full_diff,
        "file_contents": file_contents,
        "redacted_paths": redacted_paths,
        "omitted_files": omitted_files,
        "context": context,
        "related_context": related_context,
        "related_omitted_files": related_omitted,
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def read_project_file(
    target: str, path: str, start_line: int = 1, start_column: int = 0
) -> dict[str, Any]:
    _check_relative_path(path, "path")
    resolved = _resolve_target(target)
    chunk = _safe_read_file_chunked(resolved["root"], path, start_line, start_column)
    return {"target": target, **chunk}


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def search_project(target: str, query: str, mode: str) -> dict[str, Any]:
    if not query:
        raise ValueError("query must not be empty")
    if len(query) > LIMITS["max_context_chars"]:
        raise ValueError(
            f"query length {len(query)} exceeds max_context_chars"
            f" ({LIMITS['max_context_chars']})"
        )
    resolved = _resolve_target(target)
    pcfg = PROJECTS[resolved["project"]]
    target_root = resolved["root"]

    modes = pcfg.get("context_modes", {})
    if mode not in modes:
        raise ValueError(
            f"Unknown mode {mode!r} for project {resolved['project']!r}. "
            f"Available modes: {', '.join(sorted(modes))}"
        )

    search_paths = _mode_files(pcfg, mode)
    tree_roots = _mode_tree_roots(pcfg, mode)
    for root in tree_roots:
        root_path = target_root / root
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames.sort()
            filenames.sort()
            rel_dir = Path(dirpath).relative_to(target_root)
            for f in filenames:
                rel = str(rel_dir / f)
                if _is_denied(rel):
                    continue
                if rel not in search_paths:
                    search_paths.append(rel)

    results: list[dict[str, Any]] = []
    truncated = False

    for rel in search_paths:
        if len(results) >= LIMITS["max_search_results"]:
            truncated = True
            break
        meta = _classify_path_meta(target_root, rel)
        if meta["status"] != "safe":
            continue
        resolved_path = meta["resolved"]
        segment_chars = LIMITS["max_context_chars"]
        preview_chars = max(
            1,
            segment_chars // LIMITS["max_search_results"],
        )

        with open(
            resolved_path,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as stream:
            lineno = 1
            prefix_parts: list[str] = []
            prefix_length = 0
            line_length = 0
            overlap = ""
            matched = False

            while True:
                segment = stream.readline(segment_chars)
                eof = segment == ""
                line_complete = eof or segment.endswith("\n")

                if not eof:
                    line_length += len(segment)

                    if prefix_length < preview_chars:
                        take = min(
                            preview_chars - prefix_length,
                            len(segment),
                        )
                        prefix_parts.append(segment[:take])
                        prefix_length += take

                    searchable = overlap + segment
                    if query in searchable:
                        matched = True

                    if len(query) > 1:
                        overlap = searchable[-(len(query) - 1):]
                    else:
                        overlap = ""

                if line_complete:
                    if matched:
                        text = "".join(prefix_parts).rstrip()
                        if line_length > preview_chars:
                            text += "..."

                        candidate = {
                            "path": rel,
                            "line": lineno,
                            "text": text,
                        }
                        trial = {
                            "results": results + [candidate],
                            "truncated": False,
                        }

                        if (
                            len(json.dumps(trial, ensure_ascii=False))
                            > LIMITS["max_context_chars"]
                        ):
                            truncated = True
                            break

                        results.append(candidate)

                        if len(results) >= LIMITS["max_search_results"]:
                            truncated = True
                            break

                    if eof:
                        break

                    lineno += 1
                    prefix_parts = []
                    prefix_length = 0
                    line_length = 0
                    overlap = ""
                    matched = False

        if truncated:
            break

    return {"results": results, "truncated": truncated}


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(request: Any) -> PlainTextResponse:
    return PlainTextResponse("live")


@mcp.custom_route("/readyz", methods=["GET"])
async def _readyz(request: Any) -> PlainTextResponse:
    return PlainTextResponse("ready")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
