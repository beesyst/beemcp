from __future__ import annotations

import base64
import codecs
import fnmatch
import hashlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import yaml
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse, PlainTextResponse
from mcp.server.fastmcp import FastMCP
from mcp_contract import EXPECTED_TOOLS, TOOL_CONTRACT_VERSION, tool_contract_fingerprint


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
                 "max_tree_entries", "max_search_results", "max_review_page_chars"):
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
        "to load the configured project files for the chosen mode.\n\n"
        "Review protocol:\n"
        "1. list_worktrees — discover available worktrees\n"
        "2. get_review_manifest(cursor=\"\") — get paginated Git inventory "
        "(concatenate all content pages, parse as JSON)\n"
        "3. Verify target, branch, base_branch, and snapshot_id\n"
        "4. get_review_bundle_page(cursor=\"\") — get first page of paginated safe diff\n"
        "5. Repeat with next_cursor until has_more=false\n"
        "6. For each existing changed/untracked safe file, use read_project_file "
        "if needed (start_line=1, start_column=0; repeat until next_line is null)\n"
        "7. REVIEW INCOMPLETE is allowed only after exhausting bundle pagination "
        "and per-file read_project_file fallback\n\n"
        "Files with reason 'oversized' or 'context_limit' in omitted_files CAN be "
        "read via read_project_file. Files with reason 'denied', 'binary', 'broken', "
        "or 'escaping' CANNOT be read.\n\n"
        "Available tools:\n"
        "- list_projects: show allowlisted projects and their context modes\n"
        "- list_worktrees: discover Git worktrees for a project\n"
        "- get_project_context: read configured files for a target and mode\n"
        "- get_review_manifest: paginated Git inventory (concatenate content pages, "
        "parse as JSON; cursor protocol)\n"
        "- get_review_bundle_page: get one page of paginated safe diff (cursor protocol)\n"
        "- get_review_bundle: bounded compatibility shim — identity fields + first "
        "safe diff page + pagination metadata; no inventory (use get_review_manifest "
        "for full inventory, get_review_bundle_page to continue)\n"
        "- read_project_file: read a single file by relative path (supports "
        "start_line and start_column for chunked reading; deleted files must be "
        "checked through paginated diff)\n"
        "- search_project: fixed-string search within configured boundaries"
    ),
    host="127.0.0.1",
    port=8000,
)


def _run_git_bytes(argv: list[str], cwd: Path, timeout: int = 30) -> bytes:
    try:
        cp = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
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


def _stream_git_stdout(
    argv: list[str],
    cwd: Path,
    *,
    chunk_size: int = 65536,
    timeout: int = 30,
) -> Generator[bytes, None, None]:
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")

    deadline = time.monotonic() + timeout

    with tempfile.TemporaryFile() as stderr:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Git executable not found: {exc}")

        stdout = proc.stdout
        if stdout is None:
            proc.kill()
            proc.wait()
            raise RuntimeError("Git stdout pipe is unavailable")

        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ)
        timed_out = False

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    proc.kill()
                    break

                if not selector.select(remaining):
                    timed_out = True
                    proc.kill()
                    break

                data = os.read(stdout.fileno(), chunk_size)
                if not data:
                    break

                yield data
        except GeneratorExit:
            if proc.poll() is None:
                proc.kill()
            raise
        finally:
            selector.close()
            stdout.close()

            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()

            stderr.seek(0)
            stderr_data = stderr.read(8192)

        if timed_out:
            raise RuntimeError(
                f"Git command timed out after {timeout}s: {' '.join(argv)}"
            )

        if proc.returncode != 0:
            message = stderr_data.decode(
                "utf-8",
                errors="replace",
            ).strip()
            raise RuntimeError(
                f"Git command failed: {' '.join(argv)}: {message}"
            )


def _is_denied(rel_path: str) -> bool:
    for pattern in DENY_PATTERNS:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def _parse_name_status_z(output: bytes) -> tuple[list[dict[str, str]], list[str]]:
    renamed: list[dict[str, str]] = []
    names: list[str] = []
    if not output:
        return renamed, names

    fields = output.split(b"\0")
    if fields.pop() != b"":
        raise RuntimeError("Malformed Git name-status output")

    index = 0
    while index < len(fields):
        status = fields[index]
        if not status or status[:1] not in b"ACDMRTUXB":
            raise RuntimeError("Malformed Git name-status output")
        index += 1
        if status[:1] in b"RC":
            if index + 1 >= len(fields):
                raise RuntimeError("Malformed Git name-status output")
            from_path = os.fsdecode(fields[index])
            to_path = os.fsdecode(fields[index + 1])
            if not from_path or not to_path:
                raise RuntimeError("Malformed Git name-status output")
            renamed.append({"from": from_path, "to": to_path})
            names.extend((from_path, to_path))
            index += 2
            continue

        if index >= len(fields):
            raise RuntimeError("Malformed Git name-status output")
        path = os.fsdecode(fields[index])
        if not path:
            raise RuntimeError("Malformed Git name-status output")
        names.append(path)
        index += 1
    return renamed, names


def _resolve_target(target: str) -> dict[str, Any]:
    if Path(target).is_absolute():
        raise ValueError("Target must not be an absolute path")
    project_cfg = PROJECTS.get(target)
    if project_cfg is not None:
        source = Path(project_cfg["source"]).expanduser().resolve()
        return {
            "target": target,
            "project": target,
            "root": source,
            "path": str(source),
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
                wt_path = Path(line[len("worktree ") :]).expanduser().resolve()
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
                            "path": str(wt_path),
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
                "path": str(source),
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


def _expand_mode_file_entries(
    target_root: Path,
    entries: list[str],
) -> tuple[list[str], list[str]]:
    def is_denied_directory(path: str) -> bool:
        normalized = path.rstrip("/")
        return _is_denied(normalized) or _is_denied(f"{normalized}/")

    file_entries: list[str] = []
    dir_entries: list[tuple[str, Path]] = []

    for entry in entries:
        try:
            resolved = (target_root / entry).resolve(strict=True)
        except OSError:
            file_entries.append(entry)
            continue

        if not resolved.is_relative_to(target_root) or not resolved.is_dir():
            file_entries.append(entry)
            continue

        relative = str(resolved.relative_to(target_root))
        if is_denied_directory(relative):
            file_entries.append(entry)
            continue

        dir_entries.append((entry, resolved))

    expanded = list(file_entries)
    truncated_roots: list[str] = []
    seen = set(file_entries)
    max_entries = LIMITS["max_tree_entries"]
    recursive_count = 0

    for dir_entry, resolved_dir in dir_entries:
        for dirpath, dirnames, filenames in os.walk(resolved_dir):
            rel_dir = Path(dirpath).relative_to(target_root)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not is_denied_directory(str(rel_dir / name))
            ]
            filenames.sort()

            for filename in filenames:
                rel = str(rel_dir / filename)

                if _is_denied(rel) or rel in seen:
                    continue

                if recursive_count >= max_entries:
                    truncated_roots.append(dir_entry)
                    return expanded, truncated_roots

                seen.add(rel)
                expanded.append(rel)
                recursive_count += 1

    return expanded, truncated_roots


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
    raw_entries = _mode_files(project_cfg, mode)
    files_to_read, truncated_roots = _expand_mode_file_entries(target_root, raw_entries)
    contents: dict[str, str] = {}
    omitted: list[dict[str, str]] = []
    for root_entry in truncated_roots:
        omitted.append({"target": target, "path": root_entry, "reason": "tree_limit"})
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
            wt_path = Path(line[len("worktree ") :]).expanduser().resolve()
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
                    "path": str(wt_path),
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
        "path": resolved["path"],
        "branch": resolved["branch"],
        "head": resolved["head"],
        "is_main": resolved["is_main"],
        "dirty": resolved["dirty"],
        "mode": mode,
        "files": files_content,
        "related_files": related_content,
        "omitted_files": omitted,
        "related_omitted_files": related_omitted,
    }
    if tree:
        result["repository_tree"] = tree
    return result


def _git_inventory(target: str, mode: str) -> dict[str, Any]:
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

    merge_base = _run_git(
        ["git", "merge-base", "HEAD", base_branch], target_root
    ).strip()

    branch_renamed, branch_names = _parse_name_status_z(
        _run_git_bytes(
            [
                "git", "diff", "--find-renames", "--name-status", "-z",
                merge_base, "HEAD", "--",
            ],
            target_root,
        )
    )
    staged_renamed, staged_names = _parse_name_status_z(
        _run_git_bytes(
            [
                "git", "diff", "--find-renames", "--name-status", "-z",
                "--cached", "--",
            ],
            target_root,
        )
    )
    unstaged_renamed, unstaged_names = _parse_name_status_z(
        _run_git_bytes(
            ["git", "diff", "--find-renames", "--name-status", "-z", "--"],
            target_root,
        )
    )
    untracked_names = [
        os.fsdecode(path)
        for path in _run_git_bytes(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            target_root,
        ).split(b"\0")
        if path
    ]

    seen_renames: set[tuple[str, str]] = set()
    renamed_files: list[dict[str, str]] = []
    rename_sources: dict[tuple[str, str], list[str]] = {}
    for renames, source_ref in (
        (branch_renamed, merge_base),
        (staged_renamed, "HEAD"),
        (unstaged_renamed, ""),
    ):
        for rename in renames:
            key = (rename["from"], rename["to"])
            if key not in seen_renames:
                seen_renames.add(key)
                renamed_files.append(rename)
                rename_sources[key] = []
            rename_sources[key].append(source_ref)

    all_changed: set[str] = set()
    all_changed.update(branch_names)
    all_changed.update(staged_names)
    all_changed.update(unstaged_names)
    all_changed.update(untracked_names)

    branch_deleted = [
        os.fsdecode(path)
        for path in _run_git_bytes(
            [
                "git", "diff", "--find-renames", "--diff-filter=D",
                "--name-only", "-z", merge_base, "HEAD", "--",
            ],
            target_root,
        ).split(b"\0")
        if path
    ]
    staged_deleted = [
        os.fsdecode(path)
        for path in _run_git_bytes(
            [
                "git", "diff", "--find-renames", "--diff-filter=D",
                "--name-only", "-z", "--cached", "--",
            ],
            target_root,
        ).split(b"\0")
        if path
    ]
    unstaged_deleted = [
        os.fsdecode(path)
        for path in _run_git_bytes(
            [
                "git", "diff", "--find-renames", "--diff-filter=D",
                "--name-only", "-z", "--",
            ],
            target_root,
        ).split(b"\0")
        if path
    ]
    deleted_files = list(dict.fromkeys(
        branch_deleted + staged_deleted + unstaged_deleted
    ))
    deleted_sources: dict[str, list[str]] = {}
    for deleted, source_ref in (
        (branch_deleted, merge_base),
        (staged_deleted, "HEAD"),
        (unstaged_deleted, ""),
    ):
        for rel in deleted:
            deleted_sources.setdefault(rel, []).append(source_ref)

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

    for rel in sorted(all_changed):
        if rel in deleted_files and not os.path.lexists(target_root / rel):
            continue
        meta = _classify_path_meta(target_root, rel)
        st = meta["status"]
        if st == "safe":
            if meta["size"] > LIMITS["max_file_bytes"]:
                omitted_files.append({"target": target, "path": rel, "reason": "oversized"})
                continue
            safe_paths.add(rel)
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

    for rel in list(safe_paths):
        for src in (
            (merge_base if rel in branch_names else None),
            ("HEAD" if rel in staged_names else None),
            ("" if rel in unstaged_names else None),
        ):
            if src is None:
                continue
            cls = _classify_git_blob(target_root, f"{src}:{rel}")
            if cls["status"] == "oversized" or cls["status"] == "binary":
                safe_paths.discard(rel)
                omitted_files.append({"target": target, "path": rel, "reason": cls["status"]})
                break

    blocked_renames: list[dict[str, str]] = []
    for rename in safe_renames:
        key = (rename["from"], rename["to"])
        for source_ref in rename_sources[key]:
            cls = _classify_git_blob(
                target_root, f"{source_ref}:{rename['from']}"
            )
            if cls["status"] in ("binary", "oversized", "broken"):
                blocked_renames.append(rename)
                safe_paths.discard(rename["from"])
                safe_paths.discard(rename["to"])
                omitted_files.append(
                    {
                        "target": target,
                        "path": rename["from"],
                        "reason": cls["status"],
                    }
                )
                break
    for rename in blocked_renames:
        safe_renames.remove(rename)

    deleted_omitted: list[dict[str, str]] = []
    deleted_filtered: list[str] = []
    for rel in deleted_safe:
        for source_ref in deleted_sources[rel]:
            classification = _classify_deleted_blob(target_root, rel, source_ref)
            if classification["status"] != "safe":
                safe_paths.discard(rel)
                deleted_omitted.append(
                    {
                        "target": target,
                        "path": rel,
                        "reason": classification["status"],
                    }
                )
                break
        else:
            deleted_filtered.append(rel)

    diff_pathspec_set: set[str] = set(safe_paths) | set(deleted_filtered)
    for rename in safe_renames:
        diff_pathspec_set.add(rename["from"])
        diff_pathspec_set.add(rename["to"])
    diff_pathspec = sorted(diff_pathspec_set)

    safe_untracked: list[str] = []
    for f in untracked_names:
        if not f:
            continue
        if f not in safe_paths:
            continue
        safe_untracked.append(f)

    committed = sorted(set(branch_names))
    staged = sorted(set(staged_names))
    unstaged = sorted(set(unstaged_names))
    untracked = sorted(set(untracked_names))
    changed_files = sorted(all_changed)

    omitted_files.extend(deleted_omitted)

    return {
        "resolved": resolved,
        "pcfg": pcfg,
        "base_branch": base_branch,
        "target_root": target_root,
        "merge_base": merge_base,
        "branch_names": branch_names,
        "staged_names": staged_names,
        "unstaged_names": unstaged_names,
        "untracked_names": untracked_names,
        "branch_deleted": branch_deleted,
        "staged_deleted": staged_deleted,
        "unstaged_deleted": unstaged_deleted,
        "renamed_files": renamed_files,
        "safe_renames": safe_renames,
        "all_changed": all_changed,
        "changed_files": changed_files,
        "deleted_files": deleted_files,
        "deleted_safe": deleted_filtered,
        "safe_paths": safe_paths,
        "redacted_paths": redacted_paths,
        "omitted_files": omitted_files,
        "safe_untracked": safe_untracked,
        "diff_pathspec": diff_pathspec,
        "committed": committed,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
    }


def _stream_diff_segments(inv: dict[str, Any]) -> Generator[tuple[str, str], None, None]:
    target_root = inv["target_root"]
    diff_pathspec = inv["diff_pathspec"]
    merge_base = inv["merge_base"]
    safe_untracked = inv["safe_untracked"]

    segment_specs: list[tuple[str, list[str]]] = []
    if diff_pathspec:
        segment_specs.append(
            ("committed", ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", merge_base, "HEAD", "--"] + diff_pathspec)
        )
        segment_specs.append(
            ("staged", ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", "--cached", "--"] + diff_pathspec)
        )
        segment_specs.append(
            ("unstaged", ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", "--"] + diff_pathspec)
        )

    for seg_name, cmd in segment_specs:
        git_stream = _stream_git_stdout(cmd, target_root)
        try:
            for chunk in codecs.iterdecode(git_stream, "utf-8", errors="replace"):
                if chunk:
                    yield seg_name, chunk
        finally:
            git_stream.close()

    for index, f in enumerate(sorted(safe_untracked)):
        cls = _classify_path_meta(target_root, f)
        if cls["status"] != "safe" or cls["size"] > LIMITS["max_file_bytes"]:
            continue
        prefix = f"diff --git a/{f} b/{f}\nnew file mode 100644\n--- /dev/null\n+++ b/{f}\n@@ new file @@\n"
        seg_name = f"untracked:{index}"
        yield seg_name, prefix
        with open(cls["resolved"], "rb") as file_stream:
            for chunk in codecs.iterdecode(
                iter(lambda: file_stream.read(65536), b""),
                "utf-8",
                errors="replace",
            ):
                if chunk:
                    yield seg_name, chunk
        yield seg_name, "\n"


def _build_review_diff_page(
    inventory: dict[str, Any],
    start_segment: str,
    start_offset: int,
    page_size: int,
) -> tuple[str, str, int, bool]:
    if page_size <= 0:
        raise ValueError(f"page_size must be positive, got {page_size}")
    if start_offset < 0:
        raise ValueError(f"Invalid offset: {start_offset}")
    if start_segment == "" and start_offset:
        raise ValueError("Initial diff cursor must have offset 0")

    parts: list[str] = []
    total = 0
    found_start = start_segment == ""
    current_segment = ""
    segment_offset = 0
    stream = _stream_diff_segments(inventory)
    try:
        for segment, chunk in stream:
            if segment != current_segment:
                if current_segment and not found_start and current_segment == start_segment:
                    if start_offset > segment_offset:
                        raise ValueError(f"Offset outside segment: {start_offset}")
                    found_start = True
                current_segment = segment
                segment_offset = 0

            if not found_start:
                if segment != start_segment:
                    continue
                if start_offset > segment_offset + len(chunk):
                    segment_offset += len(chunk)
                    continue
                found_start = True

            consumed = segment_offset
            available = chunk
            if segment == start_segment and start_offset > segment_offset:
                available = chunk[start_offset - segment_offset:]
                consumed = start_offset

            if total == page_size:
                return "".join(parts), segment, consumed, True
            take = available[:page_size - total]
            if take:
                parts.append(take)
                total += len(take)
            if len(take) < len(available):
                return "".join(parts), segment, consumed + len(take), True
            segment_offset += len(chunk)

        if not found_start:
            raise ValueError(f"Unknown segment: {start_segment}")
        return "".join(parts), "", 0, False
    finally:
        stream.close()


def _build_review_diff(inv: dict[str, Any], *, truncate: bool) -> tuple[str, bool]:
    target_root = inv["target_root"]
    diff_pathspec = inv["diff_pathspec"]
    merge_base = inv["merge_base"]
    safe_untracked = inv["safe_untracked"]
    max_diff = LIMITS["max_diff_chars"]
    budget = max_diff if truncate else 10 ** 9
    buf: list[str] = []
    total = 0
    truncated = False

    def emit(text: str) -> None:
        nonlocal total, truncated, budget
        if not text:
            return
        if truncate and len(text) > budget:
            text = text[:budget]
            truncated = True
        buf.append(text)
        total += len(text)
        budget -= len(text)

    def emit_sep() -> None:
        if total > 0 and budget > 0:
            emit("\n")

    if diff_pathspec:
        for cmd_base in (
            ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", merge_base, "HEAD", "--"],
            ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", "--cached", "--"],
            ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", "--"],
        ):
            if truncate and budget <= 0:
                truncated = True
                break
            stream = _stream_git_stdout(
                cmd_base + diff_pathspec,
                target_root,
            )
            wrote_content = False
            try:
                for text in codecs.iterdecode(
                    stream,
                    "utf-8",
                    errors="replace",
                ):
                    if not wrote_content:
                        emit_sep()
                        wrote_content = True
                    emit(text)
                    if truncate and budget <= 0:
                        truncated = True
                        break
            finally:
                stream.close()

    for f in safe_untracked:
        if truncate and budget <= 0:
            truncated = True
            break
        cls = _classify_path(target_root, f)
        if cls["status"] != "safe":
            continue
        content = cls["content"]
        prefix = f"\ndiff --git a/{f} b/{f}\nnew file mode 100644\n--- /dev/null\n+++ b/{f}\n@@ new file @@\n"
        emit(prefix)
        if truncate and len(content) > budget:
            content = content[:budget]
            truncated = True
        emit(content)
        emit("\n")

    result = "".join(buf)
    if truncate and truncated:
        marker = f"\n\n[TRUNCATED at {max_diff} characters]"
        if len(marker) > max_diff:
            marker = "[TRUNCATED]"[:max_diff]
        result = result[:max_diff - len(marker)] + marker
    return result, truncated


def _stream_hash_review_state(inv: dict[str, Any]) -> str:
    target_root = inv["target_root"]
    diff_pathspec = inv["diff_pathspec"]
    merge_base = inv["merge_base"]
    safe_untracked = inv["safe_untracked"]
    chunk = 65536
    h = hashlib.sha256()
    if diff_pathspec:
        for args in (
            ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", merge_base, "HEAD", "--"] + diff_pathspec,
            ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", "--cached", "--"] + diff_pathspec,
            ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--unified=8", "--"] + diff_pathspec,
        ):
            stream = _stream_git_stdout(args, target_root)
            try:
                for block in stream:
                    h.update(block)
            finally:
                stream.close()
    for f in safe_untracked:
        fp = target_root / f
        try:
            h.update(os.fsencode(fp.name))
            size = fp.stat().st_size
            h.update(str(size).encode())
            with open(fp, "rb") as fh:
                while True:
                    data = fh.read(chunk)
                    if not data:
                        break
                    h.update(data)
        except OSError:
            continue
    manifest_state = _build_manifest_dict(inv)
    manifest_state["mode"] = inv.get("_mode", "")
    h.update(
        json.dumps(
            manifest_state,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
    return h.hexdigest()[:16]


def _build_review_snapshot_id(inv: dict[str, Any]) -> str:
    return _stream_hash_review_state(inv)


def _classify_git_blob(target_root: Path, object_spec: str) -> dict[str, Any]:
    try:
        _run_git(
            ["git", "cat-file", "-e", object_spec],
            target_root,
            timeout=10,
        )
    except RuntimeError:
        return {"status": "broken", "reason": object_spec}

    try:
        size_raw = _run_git(
            ["git", "cat-file", "-s", object_spec],
            target_root,
            timeout=10,
        ).strip()
        size = int(size_raw)
    except (RuntimeError, ValueError):
        return {"status": "broken", "reason": object_spec}

    if size > LIMITS["max_file_bytes"]:
        return {"status": "oversized", "reason": object_spec}

    try:
        blob = _run_git_bytes(
            ["git", "cat-file", "blob", object_spec],
            target_root,
            timeout=10,
        )
    except RuntimeError:
        return {"status": "broken", "reason": object_spec}

    if b"\x00" in blob[:8192]:
        return {"status": "binary", "reason": object_spec}

    return {
        "status": "safe",
        "content": blob.decode("utf-8", errors="replace"),
    }


def _classify_deleted_blob(
    target_root: Path,
    rel: str,
    source: str,
) -> dict[str, Any]:
    return _classify_git_blob(target_root, f"{source}:{rel}")


def _build_manifest_dict(inv: dict[str, Any]) -> dict[str, Any]:
    r = inv["resolved"]
    return {
        "target": r["target"],
        "project": r["project"],
        "branch": r["branch"],
        "head": r["head"],
        "base_branch": inv["base_branch"],
        "merge_base": inv["merge_base"],
        "dirty": r["dirty"],
        "committed": inv["committed"],
        "staged": inv["staged"],
        "unstaged": inv["unstaged"],
        "untracked": inv["untracked"],
        "deleted": inv["deleted_files"],
        "renamed": [
            {"from": x["from"], "to": x["to"]}
            for x in inv["renamed_files"]
        ],
        "changed_files": inv["changed_files"],
        "redacted_paths": inv["redacted_paths"],
        "omitted_files": inv["omitted_files"],
    }


def _paginate_content(content: str, page_size: int, page_number: int) -> dict[str, Any]:
    total_pages = max(1, -(-len(content) // page_size))
    if page_number >= total_pages:
        raise ValueError(
            f"Page {page_number} out of range (total_pages={total_pages})"
        )
    start = page_number * page_size
    end = min(start + page_size, len(content))
    page_content = content[start:end]
    has_more = end < len(content)
    return {
        "page_content": page_content,
        "has_more": has_more,
        "total_pages": total_pages,
    }


def _encode_cursor(kind: str, target: str, mode: str, snapshot_id: str, page: int, **extra: Any) -> str:
    data: dict[str, Any] = {
        "kind": kind,
        "target": target,
        "mode": mode,
        "snapshot_id": snapshot_id,
        "page": page,
    }
    data.update(extra)
    return base64.urlsafe_b64encode(
        json.dumps(data, sort_keys=True).encode()
    ).decode()


def _validate_cursor_base(
    decoded: dict[str, Any], kind: str, target: str, mode: str, snapshot_id: str
) -> None:
    if not isinstance(decoded, dict):
        raise ValueError(f"Malformed cursor")
    if decoded.get("kind") != kind:
        raise ValueError(
            f"Cursor kind {decoded.get('kind')!r} does not match expected {kind!r}"
        )
    if decoded.get("target") != target:
        raise ValueError(
            f"Cursor target {decoded.get('target')!r} "
            f"does not match {target!r}"
        )
    if decoded.get("mode") != mode:
        raise ValueError(
            f"Cursor mode {decoded.get('mode')!r} "
            f"does not match {mode!r}"
        )
    if decoded.get("snapshot_id") != snapshot_id:
        raise ValueError(
            "Cursor snapshot is stale: worktree has changed"
        )


def _decode_manifest_cursor(cursor: str, target: str, mode: str, snapshot_id: str) -> int:
    try:
        decoded = json.loads(
            base64.urlsafe_b64decode(cursor.encode()).decode()
        )
    except (ValueError, OSError):
        raise ValueError(f"Malformed cursor: {cursor!r}")
    _validate_cursor_base(decoded, "manifest", target, mode, snapshot_id)
    page = decoded.get("page", 0)
    if not isinstance(page, int) or page < 0:
        raise ValueError(f"Invalid page in cursor: {page!r}")
    return page


def _decode_diff_cursor(
    cursor: str, target: str, mode: str, snapshot_id: str
) -> dict[str, Any]:
    try:
        decoded = json.loads(
            base64.urlsafe_b64decode(cursor.encode()).decode()
        )
    except (ValueError, OSError):
        raise ValueError(f"Malformed cursor: {cursor!r}")
    _validate_cursor_base(decoded, "diff", target, mode, snapshot_id)
    segment = decoded.get("segment", "")
    offset = decoded.get("offset", 0)
    page = decoded.get("page", 0)
    if not isinstance(segment, str):
        raise ValueError(f"Invalid segment in cursor: {segment!r}")
    if not segment:
        raise ValueError("Diff continuation cursor must include a segment")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError(f"Invalid offset in cursor: {offset!r}")
    if not isinstance(page, int) or page < 0:
        raise ValueError(f"Invalid page in cursor: {page!r}")
    return {"segment": segment, "offset": offset, "page": page}


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_review_manifest(
    target: str,
    mode: str,
    cursor: str = "",
) -> dict[str, Any]:
    inv = _git_inventory(target, mode)
    inv["_mode"] = mode
    inv["snapshot_id"] = _build_review_snapshot_id(inv)

    if cursor:
        page_number = _decode_manifest_cursor(
            cursor, target, mode, inv["snapshot_id"]
        )
    else:
        page_number = 0

    manifest_dict = _build_manifest_dict(inv)
    manifest_dict["snapshot_id"] = inv["snapshot_id"]
    serialized = json.dumps(manifest_dict, ensure_ascii=True, sort_keys=True)
    page_size = LIMITS["max_review_page_chars"]
    paginated = _paginate_content(serialized, page_size, page_number)
    next_cursor = (
        _encode_cursor("manifest", target, mode, inv["snapshot_id"], page_number + 1)
        if paginated["has_more"]
        else None
    )

    return {
        "target": target,
        "project": inv["resolved"]["project"],
        "snapshot_id": inv["snapshot_id"],
        "cursor": cursor,
        "next_cursor": next_cursor,
        "has_more": paginated["has_more"],
        "page_number": page_number,
        "total_pages": paginated["total_pages"],
        "content": paginated["page_content"],
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_review_bundle_page(
    target: str,
    mode: str,
    cursor: str = "",
) -> dict[str, Any]:
    inv = _git_inventory(target, mode)
    inv["_mode"] = mode
    snapshot_id = _build_review_snapshot_id(inv)
    r = inv["resolved"]

    if cursor:
        cur = _decode_diff_cursor(cursor, target, mode, snapshot_id)
        page_number = cur.get("page", 0)
        cur_segment = cur.get("segment", "")
        cur_offset = cur.get("offset", 0)
    else:
        page_number = 0
        cur_segment = ""
        cur_offset = 0

    content, next_segment, next_offset, has_more = _build_review_diff_page(
        inv,
        cur_segment,
        cur_offset,
        LIMITS["max_review_page_chars"],
    )

    next_cursor = (
        _encode_cursor(
            "diff", target, mode, snapshot_id, page_number + 1,
            segment=next_segment, offset=next_offset,
        )
        if has_more
        else None
    )

    result: dict[str, Any] = {
        "target": target,
        "project": r["project"],
        "branch": r["branch"],
        "head": r["head"],
        "base_branch": inv["base_branch"],
        "merge_base": inv["merge_base"],
        "snapshot_id": snapshot_id,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "page_number": page_number,
        "content": content,
        "truncated": False,
    }
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_review_bundle(target: str, mode: str) -> dict[str, Any]:
    inv = _git_inventory(target, mode)
    inv["_mode"] = mode
    snapshot_id = _build_review_snapshot_id(inv)
    r = inv["resolved"]
    full_diff, truncated = _build_review_diff(inv, truncate=True)
    return {
        "target": target,
        "project": r["project"],
        "branch": r["branch"],
        "head": r["head"],
        "base_branch": inv["base_branch"],
        "merge_base": inv["merge_base"],
        "dirty": r["dirty"],
        "snapshot_id": snapshot_id,
        "protocol": "bounded",
        "cursor": "",
        "next_cursor": None,
        "has_more": truncated,
        "page_number": 0,
        "total_pages": None,
        "content": full_diff,
        "truncated": truncated,
        "note": "For full inventory, start get_review_manifest(cursor=\"\"). For the full safe diff, start get_review_bundle_page(cursor=\"\"). This bounded compatibility response does not continue through its cursor.",
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


@mcp.custom_route("/versionz", methods=["GET"])
async def _versionz(request: Any) -> JSONResponse:
    try:
        head = _run_git(["git", "rev-parse", "HEAD"], HERE).strip()
    except RuntimeError:
        head = "unknown"
    return JSONResponse({
        "service": "bee-dev-mcp",
        "git_head": head,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "tool_count": len(EXPECTED_TOOLS),
        "tool_contract_sha256": tool_contract_fingerprint(EXPECTED_TOOLS),
    })


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(request: Any) -> PlainTextResponse:
    return PlainTextResponse("live")


@mcp.custom_route("/readyz", methods=["GET"])
async def _readyz(request: Any) -> PlainTextResponse:
    return PlainTextResponse("ready")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
