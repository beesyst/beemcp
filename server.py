from __future__ import annotations

import fnmatch
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP


HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BEE_MCP_CONFIG", HERE / "projects.yml")).expanduser()
ACCESS_TOKEN = os.environ.get("BEE_MCP_TOKEN", "")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,120}$")
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"Config not found: {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("projects"), dict):
        raise RuntimeError("projects.yml must contain a projects mapping")
    return data


CONFIG = load_config()
RUN_ROOT = Path(CONFIG.get("run_root", "~/agent-runs")).expanduser().resolve()
STATE_ROOT = RUN_ROOT / ".bee-dev-mcp"
RUN_META_ROOT = STATE_ROOT / "runs"
JOB_ROOT = STATE_ROOT / "jobs"
RUN_META_ROOT.mkdir(parents=True, exist_ok=True)
JOB_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    name="Bee Dev Orchestrator",
    instructions=(
        "Use one allowlisted project per run. The first implementation must use "
        "start_copilot_initial. Later corrections must use start_codex_fix. "
        "Never claim approval without calling get_diff, read_changed_file as needed, "
        "and run_checks. This server cannot commit, push, merge, deploy, or access GitHub."
    ),
    host="127.0.0.1",
    port=8000,
)


def require_token(token: str) -> None:
    if not ACCESS_TOKEN:
        raise RuntimeError("BEE_MCP_TOKEN is not configured on the server")
    if not hmac.compare_digest(token, ACCESS_TOKEN):
        raise PermissionError("Invalid access token")


def clean_env() -> dict[str, str]:
    """Return a child environment with credentials removed."""
    env = dict(os.environ)
    sensitive_markers = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")
    for key in list(env):
        if key == "BEE_MCP_TOKEN" or any(marker in key.upper() for marker in sensitive_markers):
            env.pop(key, None)
    env["NO_COLOR"] = "1"
    env["CLICOLOR"] = "0"
    return env


def copilot_env() -> dict[str, str]:
    """
    Pass only the explicitly allowlisted BYOK variables to Copilot.
    The DeepSeek key is never passed to Codex, checks, Git, or MCP tools.
    """
    env = clean_env()
    allowed = (
        "COPILOT_PROVIDER_TYPE",
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_MODEL",
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS",
        "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS",
        "COPILOT_OFFLINE",
    )
    for key in allowed:
        value = os.environ.get(key)
        if value:
            env[key] = value

    required = (
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_MODEL",
    )
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError(
            "Missing Copilot BYOK environment variables: " + ", ".join(missing)
        )
    return env


def run_cmd(
    argv: list[str],
    cwd: Path,
    timeout: int = 120,
    *,
    input_text: str | None = None,
) -> dict[str, Any]:
    try:
        cp = subprocess.run(
            argv,
            cwd=cwd,
            env=clean_env(),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": str(exc)}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": f"Timed out after {timeout}s\n{exc.stderr or ''}",
        }
    return {
        "ok": cp.returncode == 0,
        "returncode": cp.returncode,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
    }


def project_config(project: str) -> dict[str, Any]:
    raw = CONFIG["projects"].get(project)
    if not isinstance(raw, dict):
        raise ValueError(f"Unknown project: {project}")
    result = dict(CONFIG.get("defaults") or {})
    result.update(raw)
    source = Path(result["source"]).expanduser().resolve()
    if not (source / ".git").exists():
        raise ValueError(f"Not a Git repository: {source}")
    result["source"] = source
    return result


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "task")[:48]


def meta_path(run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("Invalid run_id")
    return RUN_META_ROOT / f"{run_id}.json"


def save_meta(meta: dict[str, Any]) -> None:
    meta_path(meta["run_id"]).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_meta(run_id: str) -> dict[str, Any]:
    path = meta_path(run_id)
    if not path.is_file():
        raise ValueError(f"Unknown run_id: {run_id}")
    meta = json.loads(path.read_text(encoding="utf-8"))
    run_dir = Path(meta["run_dir"]).resolve()
    if not run_dir.is_relative_to(RUN_ROOT):
        raise RuntimeError("Run directory escaped RUN_ROOT")
    if not (run_dir / ".git").exists():
        raise RuntimeError("Run repository no longer exists")
    return meta


def changed_files(meta: dict[str, Any]) -> list[str]:
    run_dir = Path(meta["run_dir"])
    base = meta["base_commit"]
    tracked = run_cmd(["git", "diff", "--name-only", base, "--"], run_dir)
    untracked = run_cmd(["git", "ls-files", "--others", "--exclude-standard"], run_dir)
    names = set(tracked["stdout"].splitlines()) | set(untracked["stdout"].splitlines())
    return sorted(name for name in names if name)


def current_diff(meta: dict[str, Any]) -> str:
    run_dir = Path(meta["run_dir"])
    base = meta["base_commit"]
    cfg = project_config(meta["project"])
    limit = int(cfg.get("max_diff_chars", 80000))

    tracked = run_cmd(
        ["git", "diff", "--no-ext-diff", "--unified=40", base, "--"],
        run_dir,
        timeout=120,
    )["stdout"]

    chunks = [tracked]
    tracked_names = set(
        run_cmd(["git", "diff", "--name-only", base, "--"], run_dir)["stdout"].splitlines()
    )
    for rel in changed_files(meta):
        if rel in tracked_names:
            continue
        path = (run_dir / rel).resolve()
        if not path.is_relative_to(run_dir) or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            chunks.append(f"\n[untracked binary file: {rel}]\n")
            continue
        text = data.decode("utf-8", errors="replace")
        chunks.append(
            f"\ndiff --git a/{rel} b/{rel}\n"
            f"new file mode 100644\n--- /dev/null\n+++ b/{rel}\n"
            f"@@ new file @@\n{text}\n"
        )

    diff = "".join(chunks)
    if len(diff) > limit:
        return diff[:limit] + f"\n\n[TRUNCATED at {limit} characters]"
    return diff


def policy_violations(meta: dict[str, Any]) -> list[str]:
    cfg = project_config(meta["project"])
    patterns = list(cfg.get("deny_paths") or [])
    run_dir = Path(meta["run_dir"]).resolve()
    violations: list[str] = []

    for rel in changed_files(meta):
        if any(fnmatch.fnmatch(rel, pattern) for pattern in patterns):
            violations.append(f"forbidden path changed: {rel}")
        path = run_dir / rel
        if path.is_symlink():
            try:
                target = path.resolve()
            except OSError:
                violations.append(f"broken symlink: {rel}")
                continue
            if not target.is_relative_to(run_dir):
                violations.append(f"symlink escapes run directory: {rel} -> {target}")

    head = run_cmd(["git", "rev-parse", "HEAD"], run_dir)["stdout"].strip()
    branch = run_cmd(["git", "branch", "--show-current"], run_dir)["stdout"].strip()
    if head != meta["base_commit"]:
        violations.append("agent created or moved commits; working tree will be normalized")
    if branch != meta["branch"]:
        violations.append(f"agent changed branch: {branch!r}")

    return violations


def normalize_after_agent(meta: dict[str, Any]) -> list[str]:
    run_dir = Path(meta["run_dir"])
    violations = policy_violations(meta)

    head = run_cmd(["git", "rev-parse", "HEAD"], run_dir)["stdout"].strip()
    if head != meta["base_commit"]:
        run_cmd(["git", "reset", "--mixed", meta["base_commit"]], run_dir)

    remotes = run_cmd(["git", "remote"], run_dir)["stdout"].splitlines()
    for remote in remotes:
        run_cmd(["git", "remote", "remove", remote], run_dir)

    return violations


def start_process_job(
    *,
    run_id: str,
    kind: str,
    argv: list[str],
    input_text: str | None = None,
    child_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    meta = load_meta(run_id)
    run_dir = Path(meta["run_dir"])

    with JOBS_LOCK:
        for job in JOBS.values():
            if job["run_id"] == run_id and job["proc"].poll() is None:
                raise RuntimeError(f"Run already has an active job: {job['job_id']}")

        job_id = f"{kind}-{uuid.uuid4().hex[:12]}"
        job_dir = JOB_ROOT / job_id
        job_dir.mkdir(parents=True)
        log_path = job_dir / "output.log"
        input_path = job_dir / "input.txt"
        input_path.write_text(input_text or "", encoding="utf-8")

        stdin_handle = input_path.open("r", encoding="utf-8") if input_text is not None else subprocess.DEVNULL
        output_handle = log_path.open("w", encoding="utf-8")

        proc = subprocess.Popen(
            argv,
            cwd=run_dir,
            env=child_env if child_env is not None else clean_env(),
            stdin=stdin_handle,
            stdout=output_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        JOBS[job_id] = {
            "job_id": job_id,
            "run_id": run_id,
            "kind": kind,
            "proc": proc,
            "stdin_handle": stdin_handle,
            "output_handle": output_handle,
            "log_path": str(log_path),
            "started_at": int(time.time()),
            "finalized": False,
        }

    return {"job_id": job_id, "run_id": run_id, "status": "running", "kind": kind}


def job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    proc: subprocess.Popen[str] = job["proc"]
    returncode = proc.poll()
    log_path = Path(job["log_path"])
    output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    tail = output[-12000:]

    if returncode is None:
        return {
            "job_id": job["job_id"],
            "run_id": job["run_id"],
            "kind": job["kind"],
            "status": "running",
            "output_tail": tail,
        }

    if not job["finalized"]:
        if job["stdin_handle"] is not subprocess.DEVNULL:
            job["stdin_handle"].close()
        job["output_handle"].close()
        meta = load_meta(job["run_id"])
        job["violations"] = normalize_after_agent(meta)
        job["finalized"] = True

    meta = load_meta(job["run_id"])
    return {
        "job_id": job["job_id"],
        "run_id": job["run_id"],
        "kind": job["kind"],
        "status": "completed" if returncode == 0 else "failed",
        "returncode": returncode,
        "changed_files": changed_files(meta),
        "policy_violations": job.get("violations", []),
        "output_tail": tail,
    }


@mcp.tool(annotations={
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
def list_projects() -> dict[str, Any]:
    """List allowlisted projects. Use before selecting a project."""
    items = []
    for name in sorted(CONFIG["projects"]):
        cfg = project_config(name)
        items.append(
            {
                "project": name,
                "source": str(cfg["source"]),
                "base_branch": cfg.get("base_branch", "main"),
                "checks": cfg.get("checks", []),
            }
        )
    return {"projects": items}


@mcp.tool(annotations={
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
})
def prepare_run(project: str, task_name: str) -> dict[str, Any]:
    """
    Create an isolated local clone for one task. This never changes the source repository,
    never keeps a remote, and refuses a dirty source repository.
    """
    cfg = project_config(project)
    source: Path = cfg["source"]
    base_branch = str(cfg.get("base_branch", "main"))

    dirty = run_cmd(["git", "status", "--porcelain"], source)
    if dirty["stdout"].strip():
        raise RuntimeError("Source repository is dirty; commit/stash changes first")

    base_result = run_cmd(["git", "rev-parse", base_branch], source)
    if not base_result["ok"]:
        raise RuntimeError(base_result["stderr"] or f"Cannot resolve {base_branch}")
    base_commit = base_result["stdout"].strip()

    slug = safe_slug(task_name)
    run_id = f"{project}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    run_dir = (RUN_ROOT / project / run_id).resolve()
    run_dir.parent.mkdir(parents=True, exist_ok=True)

    clone = run_cmd(
        [
            "git",
            "clone",
            "--local",
            "--no-hardlinks",
            "--single-branch",
            "--branch",
            base_branch,
            str(source),
            str(run_dir),
        ],
        HERE,
        timeout=300,
    )
    if not clone["ok"]:
        raise RuntimeError(clone["stderr"] or clone["stdout"])

    run_cmd(["git", "remote", "remove", "origin"], run_dir)
    branch = f"agent/{slug}-{uuid.uuid4().hex[:6]}"
    switched = run_cmd(["git", "switch", "-c", branch], run_dir)
    if not switched["ok"]:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise RuntimeError(switched["stderr"] or switched["stdout"])

    meta = {
        "run_id": run_id,
        "project": project,
        "task_name": task_name,
        "run_dir": str(run_dir),
        "base_branch": base_branch,
        "base_commit": base_commit,
        "branch": branch,
        "created_at": int(time.time()),
    }
    save_meta(meta)
    return meta


@mcp.tool(annotations={
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
})
def start_copilot_initial(
    run_id: str,
    issue_text: str,
) -> dict[str, Any]:
    """
    Start the first implementation with Copilot. Copilot can only inspect and edit files
    in the isolated run; shell, web, GitHub MCP, remote control, commit, push and merge
    are unavailable.
    """
    meta = load_meta(run_id)
    run_dir = Path(meta["run_dir"])

    prompt = f"""You are the INITIAL IMPLEMENTER for this isolated repository.

Task:
{issue_text}

Rules:
- Read AGENTS.md, README files, ROADMAP and relevant docs if present.
- Implement only the requested scope.
- Add or update tests where appropriate.
- Do not use shell commands.
- Do not access the network.
- Do not commit, push, create PRs, change branches, or edit Git configuration.
- Do not read files outside the current repository.
- Finish with a concise report: changed files, decisions, remaining uncertainty.
"""

    argv = [
        "copilot",
        "-C",
        str(run_dir),
        "-p",
        prompt,
        "-s",
        "--no-ask-user",
        "--disable-builtin-mcps",
        "--disallow-temp-dir",
        "--available-tools=view,glob,grep,apply_patch,create,edit",
        "--allow-tool=read,write",
        "--deny-tool=shell,url",
        "--secret-env-vars=COPILOT_PROVIDER_API_KEY",
        "--output-format=text",
    ]
    return start_process_job(
        run_id=run_id,
        kind="copilot",
        argv=argv,
        child_env=copilot_env(),
    )


@mcp.tool(annotations={
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
})
def start_codex_fix(
    run_id: str,
    findings: str,
) -> dict[str, Any]:
    """
    Start a correction pass with Codex after ChatGPT has reviewed the current diff.
    Codex is restricted to workspace-write with network disabled.
    """
    meta = load_meta(run_id)
    run_dir = Path(meta["run_dir"])

    prompt = f"""You are the CORRECTION IMPLEMENTER.

Apply only these reviewer findings:
{findings}

Rules:
- Preserve the original task scope.
- Inspect the current working tree and relevant tests.
- Make the minimum correct changes.
- Network access is forbidden.
- Do not commit, push, create PRs, change branches, or edit Git configuration.
- Do not weaken or delete tests merely to make checks pass.
- Finish with a concise report of fixes and checks attempted.
"""

    argv = [
        "codex",
        "-C",
        str(run_dir),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "-c",
        "sandbox_workspace_write.network_access=false",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-",
    ]
    return start_process_job(
        run_id=run_id,
        kind="codex",
        argv=argv,
        input_text=prompt,
    )


@mcp.tool(annotations={
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
def get_job(job_id: str) -> dict[str, Any]:
    """Poll an agent job and retrieve its output when complete."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise ValueError("Unknown job_id or server was restarted")
        return job_snapshot(job)


@mcp.tool(annotations={
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
def get_diff(run_id: str) -> dict[str, Any]:
    """Return the current diff from the immutable base commit plus changed-file metadata."""
    meta = load_meta(run_id)
    return {
        "run_id": run_id,
        "base_commit": meta["base_commit"],
        "changed_files": changed_files(meta),
        "policy_violations": policy_violations(meta),
        "diff": current_diff(meta),
    }


@mcp.tool(annotations={
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
def read_changed_file(run_id: str, path: str) -> dict[str, Any]:
    """Read one changed text file. Paths outside the isolated run are rejected."""
    meta = load_meta(run_id)
    cfg = project_config(meta["project"])
    run_dir = Path(meta["run_dir"]).resolve()

    rel = Path(path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Only safe relative paths are allowed")
    if path not in changed_files(meta):
        raise ValueError("File is not in the changed-file set")

    target = (run_dir / rel).resolve()
    if not target.is_relative_to(run_dir) or not target.is_file():
        raise ValueError("Invalid file path")
    data = target.read_bytes()
    limit = int(cfg.get("max_file_bytes", 200000))
    if len(data) > limit:
        raise ValueError(f"File exceeds {limit} bytes")
    if b"\x00" in data:
        raise ValueError("Binary files are not readable through this tool")

    return {"run_id": run_id, "path": path, "content": data.decode("utf-8", errors="replace")}


@mcp.tool(annotations={
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
def run_checks(run_id: str) -> dict[str, Any]:
    """
    Run only project-configured checks. Each check is executed through the Codex Linux
    OS sandbox; no arbitrary command can be supplied by ChatGPT.
    """
    meta = load_meta(run_id)
    cfg = project_config(meta["project"])
    run_dir = Path(meta["run_dir"])
    results = []

    for raw in cfg.get("checks", []):
        if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
            raise RuntimeError("Each configured check must be a non-empty argv list")
        argv = ["codex", "sandbox", "--", *raw]
        result = run_cmd(argv, run_dir, timeout=900)
        results.append(
            {
                "check": raw,
                "ok": result["ok"],
                "returncode": result["returncode"],
                "stdout_tail": result["stdout"][-8000:],
                "stderr_tail": result["stderr"][-4000:],
            }
        )

    return {
        "run_id": run_id,
        "all_passed": bool(results) and all(item["ok"] for item in results),
        "results": results,
        "policy_violations": policy_violations(meta),
    }


@mcp.tool(annotations={
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
})
def discard_run(run_id: str) -> dict[str, Any]:
    """Delete only the isolated run directory and its metadata. Source repositories are untouched."""
    meta = load_meta(run_id)
    with JOBS_LOCK:
        for job in JOBS.values():
            if job["run_id"] == run_id and job["proc"].poll() is None:
                raise RuntimeError("Cannot discard a run with an active job")
    run_dir = Path(meta["run_dir"]).resolve()
    if not run_dir.is_relative_to(RUN_ROOT):
        raise RuntimeError("Refusing to delete outside RUN_ROOT")
    shutil.rmtree(run_dir)
    meta_path(run_id).unlink(missing_ok=True)
    return {"run_id": run_id, "discarded": True}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
