# Bee Dev Read-Only Review

ChatGPT read-only architecture and code review gateway through MCP.

## Quick start

```bash
git clone <repo-url> bee-dev-mcp
cd bee-dev-mcp
./start.sh
```

The first run creates `.env` from `.env.example`, sets mode `0600`, syncs
dependencies, and validates the configuration. If required values are empty,
it reports the full list. Fill only these values in repo-local `.env`:

- `BEE_DEV_MCP_TUNNEL_ID`
- `CONTROL_PLANE_API_KEY`

Then run `./start.sh` again. It reconciles the runtime profile and units, applies
`.env`, starts MCP, verifies its contract, and starts the tunnel. Manual
`cp .env.example .env` and `chmod 600 .env` are not required. `./start.sh setup`
remains available as an explicit setup command, but is not required for the
normal first run.

Use `./start.sh doctor` after the stack is up.

### Configuration ownership

| File | Purpose | Edited by user? |
|---|---|---|
| `<repo>/.env` | Runtime, tunnel, and secrets configuration | **Yes** |
| `<repo>/.env.example` | Public template tracked in Git | No |
| `<repo>/config/projects.yml` | Project allowlist and local source paths | **Yes** — edit `source` for a different directory layout |
| `~/.config/systemd/user/bee-dev-mcp*.service` | Installed systemd units | **No** — auto-generated |
| `~/.config/tunnel-client/*.yaml` | Tunnel profile | **No** — auto-generated |

- `.env` is Git-ignored, permissions `0600`.
- Project source paths are resolved relative to `config/projects.yml`; unavailable projects are checked when accessed.
- Generated systemd units and tunnel profiles are never edited manually.
- After changing `.env`, run `./start.sh`.

## Migration from legacy config

If you had a previous setup with `~/.config/bee-dev-mcp/tunnel.env` and
`~/.config/tunnel-client/bee-dev-mcp-test.yaml`:

1. Copy your tunnel ID from the old tunnel-client profile.
2. Copy your `CONTROL_PLANE_API_KEY` from the old `tunnel.env`.
3. Write both values into the repo-local `.env`.
4. Run `./start.sh setup`, `./start.sh restart`, `./start.sh doctor`.
5. After confirming the stack works, delete the old files manually:
   `rm -rf ~/.config/bee-dev-mcp`

Credentials are **not** copied automatically.

## Safety boundary

- No file writes — all tools are read-only.
- No agents — Copilot and Codex are never invoked.
- No project checks — no tests, linters, or build commands are run.
- No Git write operations — no commit, push, merge, branch switch, or reset.
- No GitHub access — no issue, PR, or API calls.
- Only allowlisted projects and their Git worktrees can be accessed.

## Projects and context modes

Configured in `config/projects.yml`. Each project has A/B/C context modes:

- **Mode A** — key architecture files (ROADMAP, SDLC, SECURITY, templates).
- **Mode B** — extends A with source code entry points and configuration.
- **Mode C** — extends B with a bounded repository tree listing.

Projects can also declare `related` projects whose files are loaded
automatically (e.g. beeagent loads beeagent-rop ROADMAP).

### Directory expansion in `files`

`context_modes.<mode>.files` accepts relative paths to both files and
directories. Directory entries are expanded recursively:

- All nested text files (respecting deny patterns) are included.
- Deduplicated — a file listed explicitly and also found under a directory
  entry appears only once.
- Expansion stops when `limits.max_tree_entries` is reached; the incomplete
  directory is reported in `omitted_files` with reason `tree_limit`.
- `max_file_bytes` and `max_context_chars` still apply after expansion.
- Directory symlinks are not traversed.

## Worktree discovery

Git worktrees are automatically detected. A worktree at
`/home/bee/Pro/_worktrees/beeagent-pr123` becomes target `beeagent-pr123`.
Each entry has the same metadata contract for main and linked worktrees:
`target`, `project`, exact absolute normalized `path`, `branch`, `head`,
`is_main`, and `dirty`.

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
6. For each existing changed/untracked safe file, use **read_project_file** if needed
7. REVIEW INCOMPLETE only after exhausting bundle pagination + per-file fallback

### Tool distinction

| Tool | Purpose |
|------|---------|
| `get_project_context` | Project/process context (configured files) |
| `get_review_manifest` | Paginated Git inventory (concatenate content, parse JSON) |
| `get_review_bundle_page` | Paginated safe diff (cursor protocol) |
| `read_project_file` | Per-file content (chunked) |

### Cursor / snapshot behavior

- Cursor is opaque to the client (base64-encoded JSON)
- Contains `kind` (either `"manifest"` or `"diff"`), `target`, `mode`, `snapshot_id`, `page`
- Diff cursors also contain `segment` and `offset` for page-accurate streaming
- Malformed cursor → ValueError
- Cursor kind mismatch → ValueError
- Cursor from another target → ValueError
- Cursor from another mode → ValueError
- Cursor from a stale snapshot (HEAD/status/inventory changed) → ValueError
- Page out of range → ValueError
- `snapshot_id` is a deterministic hash of target, mode, branch, HEAD, full inventory, and SHA-256 of the actual safe diff
- Changing content of a dirty/staged file invalidates the cursor
- Each page ≤ `limits.max_review_page_chars` (default 30000)

### Diff pagination (get_review_bundle_page)

`get_review_bundle_page` uses segment-based streaming instead of pre-building
the entire diff:

1. The diff is split into **committed**, **staged**, **unstaged**, and one
   deterministic segment per safe untracked file
2. Each page reads up to `max_review_page_chars` characters, starting from the
   cursor's `segment`/`offset` position
3. No aggregate `max_diff_chars` truncation — the entire diff is available page
   by page
4. The last page reports `has_more=false`, `next_cursor=null`, `truncated=false`
5. The backward-compatible `get_review_bundle` still applies `max_diff_chars`
   and reports `truncated`

### get_project_context metadata

`get_project_context` now returns additional fields:
- `path` — absolute path to the target root
- `branch` — current branch name
- `head` — current HEAD commit hash
- `is_main` — whether the target is the main project source
- `dirty` — whether the worktree has uncommitted changes

### list_worktrees path

Each `list_worktrees` entry includes an exact absolute normalized `path`.
Main and linked worktrees have the same response contract.

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

`max_diff_chars` does not limit the combined paginated diff. Every
`get_review_bundle_page` response is bounded by `max_review_page_chars`; the
final page always has `truncated=false`.

## Example ChatGPT requests

### Task preparation

```text
Используй только Bee Dev MCP Test.

Через Bee Dev MCP Test прочитай проект beeagent в режиме A.

Сравни ROADMAP beeagent и beeagent-rop.
Подготовь формулировку итерации, Issue, prompt реализации и prompt тестирования.

Ничего не изменяй и не запускай.
```

### Review (new protocol)

```text
Используй только Bee Dev MCP Test.

Посмотри изменённые файлы в beeagent-pr123.
Используй режим B.

1. list_worktrees project=beeagent
2. get_review_manifest target=beeagent-pr123 mode=B cursor=""
   — читать все страницы, собрать JSON
3. get_review_bundle_page target=beeagent-pr123 mode=B cursor=""
4. Повторяй шаг 3 с next_cursor до has_more=false
5. Для каждого changed/untracked safe файла читай read_project_file при необходимости

Не изменяй проект и не запускай агентов.
```

## How to add a new project

1. Add an entry to `config/projects.yml` under `projects:` with `source`,
   `base_branch`, and `context_modes`.
2. Define A/B/C modes and optionally `related` projects.
3. Verify with `./start.sh restart` and check `/healthz`.

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

The contract utility uses the real Streamable HTTP lifecycle: `initialize`,
then `tools/list`. It runs against the local MCP URL before the tunnel starts.
After changing a tool schema, restart both MCP and tunnel. If ChatGPT still
shows an old schema, refresh or reconnect the Bee Dev MCP app/connector.

## Health endpoints

| Path         | Status | Body  |
|--------------|--------|-------|
| `/healthz`   | 200    | `live`  |
| `/readyz`    | 200    | `ready` |
| `/versionz`  | 200    | service identity JSON |

## Tool schema changed but ChatGPT still shows old tools

After modifying tool declarations, the ChatGPT connector may cache stale
schema. The local `doctor` checks **localhost and tunnel process state only**;
it cannot verify the remote ChatGPT snapshot.

Exact sequence to propagate changes:

1. `./start.sh restart`
2. `./start.sh doctor` — confirm local tools/list contains eight tools
3. Open the Bee Dev MCP app settings in ChatGPT
4. Click **Refresh** for actions/tools
5. Confirm the updated contract is shown
6. Start a new chat
7. Verify that `get_review_manifest` and `get_review_bundle_page` are available

Important:

- `doctor` checks localhost and tunnel process state, **not** the client-side
  ChatGPT snapshot.
- `get_review_bundle` cannot replace paginated review tools.
- A schema with only six tools is insufficient for a complete review.
- BeeAgent prompts and skills must not be relaxed because of a stale connector
  schema.
- The server cannot automatically update the ChatGPT connector — manual
  app Refresh is required after every tool schema change.
| `/readyz`  | 200    | `ready` |

## Systemd workflow

The stack runs as two systemd --user services:

| Service                    | Purpose                |
|----------------------------|------------------------|
| `bee-dev-mcp.service`      | FastMCP / MCP server   |
| `bee-dev-mcp-tunnel.service` | Tunnel / proxy      |

### Initial setup

```bash
cd /home/bee/Pro/bee-dev-mcp
./start.sh setup
```

`setup` verifies required programs and `.env`, runs `uv sync --frozen`,
generates the tunnel-client profile (from `.env` values), renders systemd
units from portable `ops/systemd/*.service.in` templates, installs them, and
reloads the user manager.

The API key (`CONTROL_PLANE_API_KEY`) is never written into the tunnel profile
(YAML uses `env:CONTROL_PLANE_API_KEY`), and never passed to the Python MCP
server process (only host/port are set as Environment= directives in the MCP
unit). The tunnel unit reads the full `.env` via `EnvironmentFile=`.
