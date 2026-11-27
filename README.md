# BeeMCP Read-Only Review

Architecture and code review gateway for ChatGPT over MCP, with optional isolated local pytest runs.

## Quick start

```bash
git clone <repo-url> beemcp
cd beemcp
./start.sh
```

The first run creates `.env` from `.env.example`, sets mode `0600`, syncs dependencies, and validates the configuration. If required values are empty, it reports the full list. Fill only these values in repo-local `.env`:

- `BEEMCP_TUNNEL_ID`
- `CONTROL_PLANE_API_KEY`
- `BEEMCP_GITHUB_TOKEN`

Then run `./start.sh` again. It reconciles the runtime profile and units, applies `.env`, starts MCP, verifies its contract, and starts the tunnel. Manual `cp .env.example .env` and `chmod 600 .env` are not required. `./start.sh setup` remains available as an explicit setup command, but is not required for the normal first run.

Use `./start.sh doctor` after the stack is up.

### Configuration ownership

| File                                     | Purpose                                                           | Edited by user?                                                                                           |
| ---------------------------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `<repo>/.env`                            | Runtime, tunnel, and secrets configuration                        | **Yes**                                                                                                   |
| `<repo>/.env.example`                    | Public template tracked in Git                                    | No                                                                                                        |
| `<repo>/config/projects.yml`             | Project allowlist, local source paths, and optional `github_repo` | **Yes** — edit `source` for a different directory layout, add `github_repo: owner/repo` for GitHub access |
| `~/.config/systemd/user/beemcp*.service` | Installed systemd units                                           | **No** — auto-generated                                                                                   |
| `~/.config/beemcp/mcp.env`               | Generated MCP-only environment file (GitHub token only)           | **No** — auto-generated                                                                                   |
| `~/.config/tunnel-client/*.yaml`         | Tunnel profile                                                    | **No** — auto-generated                                                                                   |

- `.env` is Git-ignored, permissions `0600`.
- Project source paths are resolved relative to `config/projects.yml`; unavailable projects are checked when accessed.
- Generated systemd units and tunnel profiles are never edited manually.
- After changing `.env`, run `./start.sh`.

## Safety boundary

- The existing review tools retain their read-only contract.
- The optional test runner executes only configured local pytest suites and keeps worker writes inside an isolated temporary filesystem.
- No Git write operations — no commit, push, merge, branch switch, or reset.
- Bounded read-only GitHub access — only `get_github_context` for allowlisted repositories; no Issue/PR creation, modification, comments, or any write operations.
- Only allowlisted projects and their Git worktrees can be accessed.

## Projects and context modes

Configured in `config/projects.yml`. Each project has A/B/C context modes:

- **Mode A** — key architecture files (ROADMAP, SDLC, SECURITY, templates).
- **Mode B** — extends A with source code entry points and configuration.
- **Mode C** — extends B with a bounded repository tree listing.

Projects can also declare `related` projects whose files are loaded automatically (e.g. beeagent loads beeagent-rop ROADMAP).

### Directory expansion in `files`

`context_modes.<mode>.files` accepts relative paths to both files and directories. Directory entries are expanded recursively:

- All nested text files (respecting deny patterns) are included.
- Deduplicated — a file listed explicitly and also found under a directory
  entry appears only once.
- Expansion stops when `limits.max_tree_entries` is reached; the incomplete
  directory is reported in `omitted_files` with reason `tree_limit`.
- `max_file_bytes` and `max_context_chars` still apply after expansion.
- Directory symlinks are not traversed.

## Worktree discovery

Git worktrees are automatically detected. A worktree at `/home/bee/Pro/_worktrees/beeagent-pr123` becomes target `beeagent-pr123`. Each entry has the same metadata contract for main and linked worktrees: `target`, `project`, exact absolute normalized `path`, `branch`, `head`, `is_main`, and `dirty`.

Create a worktree manually:

```bash
git worktree add \
  /home/bee/Pro/_worktrees/beeagent-pr123 \
  -b feat/it21-enrich-review-tsv \
  main
```

## Review protocol (paginated)

Use the paginated protocol for all reviews:

1. **list_worktrees** — discover available worktrees
2. **get_review_manifest**(cursor="") — paginated Git inventory (concatenate all `content` pages, parse as JSON)
3. Verify target, branch, base_branch, **snapshot_id**
4. **get_review_bundle_page**(cursor="") — paginated safe diff
5. Repeat with **next_cursor** until **has_more=false**
6. **get_review_sources_page**(cursor="") — read all changed/untracked working-tree text;
   repeat with **next_cursor** until **has_more=false**, collecting content by `path`
7. Check `skipped_files` and compare snapshots across inventory, diff, and sources.
   Report incomplete coverage when files were skipped or snapshots differ. For a
   manifest item marked `readable_via: read_project_visual`, request the visual lazily

### Tool distinction

| Tool                      | Purpose                                                   |
| ------------------------- | --------------------------------------------------------- |
| `get_project_context`     | Project/process context (configured files)                |
| `get_review_manifest`     | Paginated Git inventory (concatenate content, parse JSON) |
| `get_review_bundle_page`  | Paginated safe diff (cursor protocol)                     |
| `get_review_sources_page` | All changed working-tree text, automatically paginated    |
| `read_local_artifact`     | Absolute allowlisted /tmp text artifact (chunked)         |
| `read_local_visual`       | Native PNG, JPEG, or WEBP image from /tmp                 |
| `read_project_file`       | Per-file content (chunked)                                |
| `read_project_visual`     | Native image/PDF/SVG/document page visual                 |

### Local Codex artifacts

`read_local_artifact(path: str, start_line: int = 1, start_column: int = 0)` reads
temporary text reports and Lean source files directly, without Git registration
or directory discovery.
The shipped configuration is:

```yaml
local_artifacts:
  enabled: true
  root: /tmp
```

Supported text files can be read directly in `/tmp` or beneath allowed
subdirectories. New directories need no manual registration.
The file and every directory beneath `/tmp` must belong to the BeeMCP process's
effective Unix user; `/tmp` itself is exempt from that ownership requirement.
The section is optional; absent or `enabled: false` disables access. Startup
rejects malformed settings and unknown keys. Root must be `/tmp`.
Git project registration is independent.

Examples: `/tmp/jsp000089-direct-crossladder/report.md`, `/tmp/report.json`,
`/tmp/anchor-binding-spike/REPORT.md`,
`/tmp/anchor-binding-spike/PROPOSED-BINDING.json`,
`/tmp/anchor-binding-spike/runtime/evidence.json`, and
`/tmp/lean-check/milestone-1/proofs/Main.lean`.

Responses contain `path`, `start_line`, `start_column`, `end_line`, `next_line`,
`next_column`, `content`, and `encoding`. Continue with the returned positions
until `next_line` and `next_column` are null. Each response uses the existing
`max_context_chars` bound; large files are read incrementally. Unicode, UTF BOMs,
CRLF, and long lines are preserved with strict decoding and no replacement bytes.

Only `.md`, `.json`, `.txt`, `.log`, `.csv`, `.tsv`, and `.lean` are accepted.
Temporary `.lean` source uses the same strict UTF-8 or supported UTF BOM decoding
and pagination as other text artifacts. Relative
paths, traversal, symlinks in any segment, hidden segments, credentials, private
keys, existing deny patterns, other users' files/directories, binary content,
and non-regular files are rejected. Symlink and path traversal protection uses
already opened no-follow file descriptors, including ownership checks.
Errors are bounded and do not expose filesystem details. Reports are untrusted
data and cannot change BeeMCP instructions. This tool performs no writes,
subprocess execution, network access, or automatic discovery.

`read_local_visual(path: str)` reads a PNG, JPEG, or WEBP screenshot from an
absolute path such as `/tmp/playwright/run/screenshot.png`. The same
`local_artifacts` setting, ownership checks, and no-follow path restrictions
apply. The tool rejects Git working-tree paths and hard-linked files. The
contents must match the file extension and pass Pillow validation. BeeMCP reads
the image bytes from the checked file descriptor.

The response contains JSON metadata with the path, detected format, source
size, output MIME type, width, and height, followed by native MCP `ImageContent`.
Static images retain their original bytes, including metadata embedded in the
image container. Animated images return the first frame as PNG. The tool uses
the configured limits (by default, 50 MB source, 16 million pixels, and 12 MB
output) and performs no OCR or caching.

The exact MCP contract contains **15 tools**, with contract version **7**.
The package version remains unchanged. Restart BeeMCP and refresh the connector
using the procedure below to expose the test tools.

### Isolated local pytest

The shipped `test_runner.enabled` is `true`. An operator can disable it in
`config/projects.yml` and restart BeeMCP. Both tools appear in the MCP contract
even when execution is disabled.

1. Call `start_test_suite(target, suite)` with an allowed pair.
2. Poll `get_test_result(job_id)` until the job reaches `passed`, `failed`,
   `timeout`, or `refused`. Results include elapsed time, the process exit code
   when available, pytest counts, failing allowlisted modules, and an output
   truncation flag. Results are kept in memory for the last 32 jobs and expire on
   restart.

| Target | Suite | Coverage |
| --- | --- | --- |
| `beeagent` | `beeagent_conversation_web` | Selected conversation and web regression tests for read-only views, redaction, and scope enforcement |
| `beeui` | `beeui_unit` | Blocks, locale, and navigation |
| `beescan` | `beescan_unit` | Catalog and domain |
| `beemcp` | `beemcp_unit` | BeeMCP review, local artifacts, GitHub mocks, lifecycle, and MCP transport |

Each suite uses its declared main repository. The job has an opaque
`snapshot_id` and reports `snapshot_sha256` once its source snapshot is ready.
The snapshot includes allowed tracked and non-ignored untracked files from the
current working tree. It skips hidden paths, private paths, symlinks and hard
links, with explicit exceptions for the public `.env.example` and `.gitignore`.
Editable dependencies use source snapshots at their existing absolute paths.
Keep credentials and other private data out of source files.

The runner requires Linux, rootless Bubblewrap with private mount, PID and network
namespaces, libseccomp, a local systemd user manager with cgroup v2, and the
existing Python 3.14 virtual environments. When BeeMCP's virtual environment
lacks pytest, it uses the installed pytest modules from BeeUI's virtual
environment. The runner never installs dependencies. Source snapshots and the
allowlisted runtime mounts are read-only. The test process receives a fresh
environment and can write to a size-limited tmpfs. The network namespace only
has loopback, which BeeMCP uses to test its local HTTP transport. A failed
sandbox check returns `refused` without running pytest on the host.

One job may run at a time. Additional starts return `refused` with reason
`busy`. Suite timeouts cap runtime at 180 seconds. CPU, memory and process
limits apply to the whole cgroup, with additional per-process file size and
descriptor limits and a bounded output buffer. The MCP response contains
structured diagnostics and omits raw stdout, stderr and tracebacks.

### Cursor / snapshot behavior

- Cursor is opaque to the client (canonical unpadded base64url-encoded JSON;
  the server also accepts legacy padded base64url cursors)
- Contains `kind` (`"manifest"`, `"diff"`, or `"sources"`), `target`, `mode`, `snapshot_id`, `page`
- Diff cursors also contain `segment` and `offset` for page-accurate streaming
- Source cursors also contain `file_index`, `line` (1-based), and `column` (0-based)
- Malformed cursor → ValueError (the public error is bounded; decoder and
  validation stages distinguish base64, UTF-8/JSON, structure, identity,
  snapshot, segment, offset, and page failures internally)
- Cursor kind mismatch → ValueError
- Cursor from another target → ValueError
- Cursor from another mode → ValueError
- Cursor from a stale snapshot (HEAD/status/inventory changed) → ValueError
- Page out of range → ValueError
- `snapshot_id` is a deterministic hash of target, mode, branch, HEAD, full inventory, the actual safe diff, readable changed source bytes, and index object IDs (including oversized files)
- Changing content of a dirty/staged file invalidates the cursor
- Each page ≤ `limits.max_review_page_chars` (default 30000)

### Diff pagination (get_review_bundle_page)

`get_review_bundle_page` uses segment-based streaming instead of pre-building the entire diff:

1. The diff is split into **committed**, **staged**, **unstaged**, and one
   deterministic segment per safe untracked file
2. Each page reads up to `max_review_page_chars` characters, starting from the
   cursor's `segment`/`offset` position
3. No aggregate `max_diff_chars` truncation — the entire diff is available page
   by page
4. The last page reports `has_more=false`, `next_cursor=null`, `truncated=false`
5. The backward-compatible `get_review_bundle` still applies `max_diff_chars`
   and reports `truncated`

### Source pagination (get_review_sources_page)

`get_review_sources_page(target: str, mode: str, cursor: str = "")` reuses the
manifest inventory, deduplicates paths, and walks them in sorted order. Each
response contains one file chunk or one explicit skip. Large text files remain
readable even when the diff inventory marks them `oversized`.

Responses include `target`, `project`, `mode`, `snapshot_id`, `cursor`,
`page_number`, `path`, `source: "working_tree"`, `index_may_differ`, `start_line`,
`start_column`, `end_line`, `next_line`, `next_column`, `content`, `skipped_files`,
`next_cursor`, `has_more`, and `truncated: false`; successful text reads also
include `encoding`. `end_line` is the last line touched; `next_line`/`next_column`
identify a continuation within this file, or are null at EOF. `next_cursor`
automatically moves into the next path at EOF. A line longer than the character
budget continues at a column offset. Line numbers count LF characters; original
CRLF and CR characters are preserved. Text is strictly decoded using the existing
encoding policy (UTF-8 or supported BOM); BOMs are encoding markers, not content.

Skips use `[{"path": "...", "reason": "deleted"}]` (other reasons include
`denied`, `escaping`, `binary`, `visual`, `renderable`, `unsupported_encoding`,
and `broken`). If an encoding failure is discovered after earlier chunks, discard
that file's partial content and record the skip. An empty inventory returns
`path: null`, empty content/skips, `has_more: false`, and `next_cursor: null`.
The character budget bounds `content`; response metadata is additional.

For ChatGPT: call with an empty cursor, collect each chunk under its `path`, record
every skip, and pass `next_cursor` unchanged to the next call until `has_more` is
false. Never treat a cursor error as completion. Restart all review streams if a
snapshot becomes stale or differs between inventory, diffs, and sources. Source
pages check the snapshot both before and after reading, including the final page.

Source pages contain **current working-tree content**, including committed,
staged, unstaged, and untracked changed paths. `index_may_differ` is conservative:
it is true for unstaged or untracked paths. Use **diff pagination** to distinguish
base/HEAD/index/working-tree changes; source pages do not substitute for Git diffs.
Existing `read_project_file` remains available for individual paths.

### get_project_context metadata

`get_project_context` now returns additional fields:

- `path` — absolute path to the target root
- `branch` — current branch name
- `head` — current HEAD commit hash
- `is_main` — whether the target is the main project source
- `dirty` — whether the worktree has uncommitted changes

### list_worktrees path

Each `list_worktrees` entry includes an exact absolute normalized `path`. Main and linked worktrees have the same response contract.

### Manifest pagination

`get_review_manifest` returns a paginated JSON string in `content`:

1. Call `get_review_manifest(target, mode, cursor="")`
2. Read page 0 content, concatenate partial JSON
3. Repeat with `next_cursor` until `has_more=false`
4. Parse the full concatenated JSON as the complete manifest

Manifest pagination uses the same `max_review_page_chars` boundary.

### Backward compatibility

`get_review_bundle` still exists but returns:

- A bounded diff limited by `max_diff_chars`
- `protocol: "bounded"`, `next_cursor: null`, and no continuation cursor
- No manifest or manifest fields
- A note directing full inventory to `get_review_manifest(cursor="")` and full
  diff to `get_review_bundle_page(cursor="")`

`max_diff_chars` does not limit the combined paginated diff. Every `get_review_bundle_page` response is bounded by `max_review_page_chars`; the final page always has `truncated=false`.

## Text and visual artifacts

Use `read_project_file` for text/code, including CSV/TSV and SVG source. Text is decoded strictly (UTF BOMs or UTF-8); BeeMCP does not silently replace bad bytes. Use `read_project_visual` for PNG, JPEG, WEBP, SVG, and one requested page or normalized crop of a PDF. BMP/TIFF/GIF are validated then normalized to PNG; animated GIF uses frame 1. The result is native MCP image content for the client/model to interpret; BeeMCP does not OCR, reconstruct diagrams, or turn layout into a text substitute.

PDF rendering is lazy: no entire document is rendered during context or review inventory. A response includes bounded page metadata and its supplementary text layer. Request a selected crop at a higher DPI when examining a formula or diagram. DOCX/XLSX/PPTX and ODT/ODS/ODP follow the same page path only when a local headless `soffice` (LibreOffice) executable is available; otherwise the tool returns `renderer_unavailable`. Arbitrary binaries, archives, databases, and executables remain unsupported and are never base64-dumped.

Rendered PDF/SVG/office images are cached outside projects at `~/.cache/beemcp/` (or `BEEMCP_CACHE_DIR`), keyed by source content and render settings. Repository inventory uses Git and tracked search uses `git grep`, so mode-C trees and searches avoid opening every repository file.

## Example ChatGPT requests

### Task preparation

```text
Используй только BeeMCP Test.

Через BeeMCP Test прочитай проект beeagent в режиме A.

Сравни ROADMAP beeagent и beeagent-rop.
Подготовь формулировку итерации, Issue, prompt реализации и prompt тестирования.

Ничего не изменяй и не запускай.
```

### Review (new protocol)

```text
Используй только BeeMCP Test.

Посмотри изменённые файлы в beeagent-pr123.
Используй режим B.

1. list_worktrees project=beeagent
2. get_review_manifest target=beeagent-pr123 mode=B cursor=""
   — читать все страницы, собрать JSON
3. get_review_bundle_page target=beeagent-pr123 mode=B cursor=""
4. Повторяй шаг 3 с next_cursor до has_more=false
5. Читай get_review_sources_page до has_more=false; проверь skipped_files и snapshot_id

Не изменяй проект и не запускай агентов.
```

## GitHub context (`get_github_context`)

BeeMCP can read GitHub Issues and Pull Requests from allowlisted repositories. This is a **bounded read-only** operation:

- Only `get_github_context` — no write operations.
- Only repositories listed in `github_repo` under each project in
  `config/projects.yml`.
- Only exact URL formats:
  `https://github.com/<owner>/<repo>/issues/<number>` or
  `https://github.com/<owner>/<repo>/pull/<number>`.

### Token setup

1. Create a fine-grained personal access token (PAT) at:
   https://github.com/settings/tokens?type=beta
2. Set repository access to the allowlisted repositories.
3. Set these permissions to **Read-only**:
   - Metadata: Read-only
   - Issues: Read-only
   - Pull requests: Read-only
4. Copy the token into `<repo>/.env`:
   ```env
   BEEMCP_GITHUB_TOKEN=github_pat_...
   ```
5. Run `./start.sh restart` and `./start.sh doctor`.

### Repository allowlist

Each project in `config/projects.yml` can declare an optional `github_repo`:

```yaml
projects:
  beeui:
    source: ../../beeui
    github_repo: beesyst/beeui
    base_branch: main
```

Only repositories with `github_repo` are accessible.

### Example usage in ChatGPT

The `FULL_APPROVED_ISSUE` and `CURRENT_PR_OR_NONE` fields accept GitHub URLs directly:

```text
FULL_APPROVED_ISSUE:

https://github.com/beesyst/beeui/issues/97

CURRENT_PR_OR_NONE:

https://github.com/beesyst/beeui/pull/95
```

ChatGPT calls `get_github_context` with the URL and receives:

- **Issue**: title, body, state, author, labels, paginated comments, metadata.
- **PR**: title, body, state, draft/merged flags, author, base/head refs,
  paginated conversation comments, submitted reviews, and inline review comments.

### Response size limit

The response respects `limits.max_context_chars`. The main Issue/PR body is never silently truncated. If the body alone exceeds the limit, the call fails with an error. Comments, reviews, and inline review comments may be omitted when the budget is exhausted, with corresponding `*_truncated` flags set.

### Safe token passing

The GitHub token is read from the repo-local `.env` and written into a separate generated file at `~/.config/beemcp/mcp.env` with permissions `0600`. This file contains only `BEEMCP_GITHUB_TOKEN` and is used by the MCP systemd unit via `EnvironmentFile=`. The tunnel service continues to use the full repo-local `.env`. Other secrets (`CONTROL_PLANE_API_KEY`, tunnel ID) are never exposed to the MCP process.

## How to add a new project

1. Add an entry to `config/projects.yml` under `projects:` with `source`,
   `base_branch`, and `context_modes`.
2. Optionally add `github_repo: owner/repo` for GitHub context access.
3. Define A/B/C modes and optionally `related` projects.
4. Verify with `./start.sh restart` and check `/healthz`.

## How to check the stack

```bash
./start.sh setup      # install systemd units and sync dependencies
./start.sh restart    # stop tunnel → stop MCP → start MCP → wait → start tunnel
./start.sh status     # show service status + endpoint health
./start.sh logs       # follow the combined logs for both services
./start.sh stop       # stop tunnel → stop MCP
./start.sh doctor     # read-only diagnostics (includes MCP tool contract check)
```

## MCP tool contract check

`start.sh` verifies the MCP tool contract automatically:

- **doctor** (`./start.sh doctor`): after health/readiness checks, runs
  `scripts/check_mcp_tools.py` to verify all expected tools are registered.
- **start_stack** (used by `up`/`restart`): after readiness wait but before
  starting the tunnel, runs the tool contract check. If it fails, MCP is
  stopped and the stack does not come up, preventing a broken tunnel.

The contract utility uses the real Streamable HTTP lifecycle: `initialize`, then `tools/list`. It runs against the local MCP URL before the tunnel starts. After changing a tool schema, restart both MCP and tunnel. If ChatGPT still shows an old schema, refresh or reconnect the BeeMCP app/connector.

## Health endpoints

| Path        | Status | Body                  |
| ----------- | ------ | --------------------- |
| `/healthz`  | 200    | `live`                |
| `/readyz`   | 200    | `ready`               |
| `/versionz` | 200    | service identity JSON |

## Tool schema changed but ChatGPT still shows old tools

After modifying tool declarations, the ChatGPT connector may cache stale schema. The local `doctor` checks **localhost and tunnel process state only**; it cannot verify the remote ChatGPT snapshot.

Exact sequence to propagate changes:

1. `./start.sh restart`
2. Run `./start.sh doctor` and confirm the local contract contains 15 tools
3. Open the BeeMCP app settings in ChatGPT
4. Click **Refresh** for actions/tools
5. Confirm the updated contract is shown
6. Start a new chat
7. Verify that `get_review_manifest`, `get_review_bundle_page`,
   `read_project_visual`, `get_github_context`, `read_local_artifact`, and
   `read_local_visual`, `start_test_suite`, and `get_test_result` are available

Important:

- `doctor` checks localhost and tunnel process state, **not** the client-side
  ChatGPT snapshot.
- `get_review_bundle` cannot replace paginated review tools.
- A schema with fewer than 15 tools is stale.
- BeeAgent prompts and skills must not be relaxed because of a stale connector
  schema.
- The server cannot automatically update the ChatGPT connector — manual
  app Refresh is required after every tool schema change.
  | `/readyz` | 200 | `ready` |

## Systemd workflow

The stack runs as two systemd --user services:

| Service                 | Purpose              |
| ----------------------- | -------------------- |
| `beemcp.service`        | FastMCP / MCP server |
| `beemcp-tunnel.service` | Tunnel / proxy       |

### Initial setup

```bash
cd /home/bee/Pro/beemcp
./start.sh setup
```

`setup` verifies required programs and `.env`, runs `uv sync --frozen`, generates the tunnel-client profile (from `.env` values), generates a separate MCP environment file at `~/.config/beemcp/mcp.env` containing only `BEEMCP_GITHUB_TOKEN`, renders systemd units from portable `ops/systemd/*.service.in` templates, installs them, and reloads the user manager.

The API key (`CONTROL_PLANE_API_KEY`) is never written into the tunnel profile (YAML uses `env:CONTROL_PLANE_API_KEY`), and never passed to the Python MCP server process (only host/port are set as Environment= directives and the isolated `mcp.env` file). The tunnel unit reads the full `.env` via `EnvironmentFile=`.
