# BeeMCP

**Read-only repository review and isolated test execution for ChatGPT via the Model Context Protocol (MCP).**

BeeMCP gives ChatGPT controlled access to local Git repositories, code changes, documentation, review artifacts, and selected GitHub Issues and pull requests. It can also run a small allowlist of tests in an isolated sandbox **without granting write access to your source repositories**.

## Features

- **Repository context:** configurable projects, A/B/C context modes, and Git worktree discovery.
- **Reliable code review:** paginated Git inventory, diffs, and changed-file contents with snapshot consistency checks.
- **Files and evidence:** bounded text reads, local `/tmp` reports, and selected images, PDFs, and office documents.
- **GitHub context:** read-only access to Issues, PRs, reviews, and comments in allowlisted repositories.
- **Optional test execution:** fixed pytest suites in a Bubblewrap sandbox, with resource limits and structured results.
- **No code edits:** no shell access, file-writing API, Git mutations, or GitHub write operations.

## Requirements

- Linux with `systemd --user`, Git, and curl.
- Python **3.14.4** (the version required by `pyproject.toml`).
- A working `tunnel-client` installation and credentials for its control plane; these are **not provided by this repository**.
- [`uv`](https://docs.astral.sh/uv/) (automatically installed by `start.sh` if absent).
- For the optional test runner: rootless Bubblewrap, libseccomp, systemd/cgroup v2, and existing Python environments with the required test dependencies.
- Optional: LibreOffice (`soffice`) for Office document previews.

> **Deployment note:** BeeMCP is a self-hosted gateway, not a hosted ChatGPT service. The included deployment workflow depends on an externally configured MCP tunnel. A public clone alone does not grant tunnel or ChatGPT connector access.

## Quick start

```bash
git clone https://github.com/beesyst/beemcp.git
cd beemcp
./start.sh
```

On first launch, the script creates a private `.env` from `.env.example` and reports missing configuration values. Edit `.env` and supply:

```dotenv
BEEMCP_TUNNEL_ID=your-tunnel-id
CONTROL_PLANE_API_KEY=your-control-plane-key
BEEMCP_GITHUB_TOKEN=your-read-only-github-token
```

Other host, port, tunnel profile, and control-plane URL defaults are provided by `.env.example`. Configure the project paths in `config/projects.yml` for your machine, then start and verify:

```bash
./start.sh
./start.sh doctor
```

A healthy local installation reports **15 MCP tools, contract v7**, and active MCP and tunnel services. This check does **not** verify whether ChatGPT has refreshed the connector schema.

## Configure projects

Projects, accessible files, GitHub repositories, and limits are defined in `config/projects.yml`. Paths in `source` are relative to that configuration file.

Example project entry (add it under the existing `projects:` mapping):

```yaml
myproject:
  source: ../../my-project
  base_branch: main
  github_repo: example-org/my-project # optional
  context_modes:
    A:
      files: [README.md, docs/ARCHITECTURE.md]
    B:
      extends: A
      files: [src, tests]
    C:
      extends: B
      tree_roots: [src, tests]
```

- **A:** architecture, policies, and high-level documentation.
- **B:** A plus implementation files and relevant contracts.
- **C:** B plus a bounded repository tree.

The gateway discovers both main repositories and linked Git worktrees. It resolves exact worktree identity before review; it does not infer a target from a branch name.

### GitHub permissions

Create a [fine-grained GitHub token](https://github.com/settings/tokens?type=beta) limited to the configured repositories, with **read-only** permissions for metadata, Issues, and pull requests. Put it only in `.env`; never commit secrets.

## Available MCP tools

| Area                 | Tools                                                                                                                |
| -------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Projects             | `list_projects`, `list_worktrees`, `get_project_context`                                                             |
| Review               | `get_review_manifest`, `get_review_bundle_page`, `get_review_sources_page`, `get_review_bundle` (bounded legacy API) |
| Source and artifacts | `read_project_file`, `read_local_artifact`, `read_local_visual`, `read_project_visual`, `search_project`             |
| GitHub               | `get_github_context`                                                                                                 |
| Tests                | `start_test_suite`, `get_test_result`                                                                                |

**15 tools · contract v7.** Review and reading tools are read-only. `start_test_suite` executes only configured tests in an isolated worker; it cannot accept arbitrary commands or paths.

### Code review workflow

For a complete review:

1. Use `list_worktrees` to identify the exact target and branch.
2. Read all pages from `get_review_manifest(target, mode, cursor="")` and assemble the JSON inventory.
3. Read all pages from `get_review_bundle_page` using each returned `next_cursor`.
4. Read all pages from `get_review_sources_page` to inspect changed and untracked text.
5. Verify matching `snapshot_id` values; investigate `skipped_files` and incomplete coverage.

A changed snapshot invalidates its cursors. Restart the review rather than combining results from different revisions. `get_review_bundle` is a **bounded compatibility API**, not a substitute for paginated review.

### Local artifacts and visual inspection

- `read_local_artifact`: allowlisted text files under `/tmp` (`.md`, `.json`, `.txt`, `.log`, `.csv`, `.tsv`, `.lean`).
- `read_local_visual`: validated PNG, JPEG, and WEBP screenshots under `/tmp`.
- `read_project_visual`: selected images and pages/crops from PDF or supported office files.

Reads are size-bounded and enforce path, ownership, file-type, and symlink checks. Visual tools return native MCP image data; they do not perform OCR. Office rendering requires a local `soffice` executable.

## Isolated test runner

The current configuration enables the runner (`test_runner.enabled: true`). Set it to `false` in `config/projects.yml` to disable execution; the tools remain discoverable but refuse starts.

| Target     | Suite                       | Scope                                        |
| ---------- | --------------------------- | -------------------------------------------- |
| `beeagent` | `beeagent_conversation_web` | Selected conversation and Web UI regressions |
| `beeui`    | `beeui_unit`                | Blocks, locale, navigation                   |
| `beescan`  | `beescan_unit`              | Catalog and domain                           |
| `beemcp`   | `beemcp_unit`               | Gateway and transport regressions            |

Usage:

1. Call `start_test_suite(target, suite)` with an allowlisted pair.
2. Poll `get_test_result(job_id)` until completion.
3. Inspect `state`, `exit_code`, test counts, and bounded diagnostic fields.

Only one test job runs at a time. Jobs have timeouts and CPU, memory, process, file, and output limits. The runner copies eligible working-tree sources—including permitted dirty and untracked files—into **read-only snapshots**; writable scratch space is limited to sandbox storage. It exposes neither arbitrary host files nor external network routes. On sandbox failure, execution is refused rather than falling back to host pytest.

**These are selected suites, not full project test coverage.** No external security labs or unrestricted network tests are exposed through BeeMCP.

## Connect to ChatGPT

After starting BeeMCP, configure or refresh the BeeMCP MCP app/connector in your ChatGPT workspace using your provisioned tunnel connection.

When the tool schema changes:

1. Run `./start.sh restart` and `./start.sh doctor`.
2. Open the BeeMCP app/connector settings in ChatGPT.
3. Refresh its actions/tools, or reconnect if refresh is unavailable.
4. Start a new chat and verify `start_test_suite` and `get_test_result` are visible.

`doctor` validates the **local** MCP endpoint and tool contract. It cannot validate ChatGPT's cached connector schema.

Example requests:

```text
Use BeeMCP to review the current changes in myproject (mode B).
Check the complete manifest, diff and changed sources. Do not edit files.
```

```text
Use BeeMCP to run the allowlisted beeui_unit suite.
Report the exit code, passed, failed and skipped counts. Do not edit files.
```

## Operations

```bash
./start.sh setup    # Install/configure user services
./start.sh           # Start the stack (default: up)
./start.sh status    # Service and endpoint status
./start.sh doctor    # Health, readiness, and MCP contract checks
./start.sh logs      # Follow service logs
./start.sh restart   # Restart MCP and tunnel
./start.sh stop      # Stop both services
```

| Endpoint    | Purpose                                 |
| ----------- | --------------------------------------- |
| `/healthz`  | Liveness                                |
| `/readyz`   | Readiness                               |
| `/versionz` | Git HEAD and MCP tool-contract identity |

The stack uses two `systemd --user` services: `beemcp.service` and `beemcp-tunnel.service`. Generated units and tunnel profiles should be managed through `start.sh`, not edited manually.

### Run the BeeMCP test suite

```bash
uv run python -m unittest discover -s tests -v
```

The gateway's own development tests use **unittest**. Its optional isolated test runner executes selected **pytest** suites in other project environments.

## Security model

- **Read-only project access:** no APIs for file editing, shell execution, commits, pushes, merges, or branch changes.
- **Strict allowlists:** project roots, GitHub repositories, local artifact types, and available test suites are explicit.
- **Secret isolation:** `.env`, key files, private paths, and Git metadata are excluded from source snapshots; tunnel credentials are not passed to the MCP server process.
- **Sandboxed execution:** Bubblewrap namespaces, read-only mounts, network isolation, and systemd/cgroup limits; failure is fail-closed.
- **Untrusted inputs:** repository files, GitHub content, logs, and test output are treated as data, not instructions.

The gateway is intended for trusted operators. As with any tool that can execute tests, review the configured suite allowlist and the host's sandbox capabilities before enabling it.

## License

No license is declared in the repository yet. Add an appropriate `LICENSE` file before public release; a public GitHub repository does not automatically grant reuse rights.
