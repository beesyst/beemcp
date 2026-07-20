# Bee Dev Read-Only Review

ChatGPT read-only architecture and code review gateway through MCP.

## Safety boundary

- No file writes — all tools are read-only.
- No agents — Copilot and Codex are never invoked.
- No project checks — no tests, linters, or build commands are run.
- No Git write operations — no commit, push, merge, branch switch, or reset.
- No GitHub access — no issue, PR, or API calls.
- Only allowlisted projects and their Git worktrees can be accessed.

## Projects and context modes

Configured in `projects.yml`. Each project has A/B/C context modes:

- **Mode A** — key architecture files (ROADMAP, SDLC, SECURITY, templates).
- **Mode B** — extends A with source code entry points and configuration.
- **Mode C** — extends B with a bounded repository tree listing.

Projects can also declare `related` projects whose files are loaded
automatically (e.g. beeagent loads beeagent-rop ROADMAP).

## Worktree discovery

Git worktrees are automatically detected. A worktree at
`/home/bee/Pro/_worktrees/beeagent-pr123` becomes target `beeagent-pr123`.

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
- Contains `target`, `mode`, `snapshot_id`, `page`
- Malformed cursor → ValueError
- Cursor from another target → ValueError
- Cursor from another mode → ValueError
- Cursor from a stale snapshot (HEAD/status/inventory changed) → ValueError
- Page out of range → ValueError
- `snapshot_id` is a deterministic hash of target, mode, branch, HEAD, full inventory, and SHA-256 of the actual safe diff
- Changing content of a dirty/staged file invalidates the cursor
- Each page ≤ `limits.max_review_page_chars` (default 30000)
- Total safe diff bounded by `limits.max_diff_chars`; if exceeded, `truncated=true`
- Truncation marker is included in the budget

### Manifest pagination

`get_review_manifest` returns a paginated JSON string in `content`:
1. Call `get_review_manifest(target, mode, cursor="")`
2. Read page 0 content, concatenate partial JSON
3. Repeat with `next_cursor` until `has_more=false`
4. Parse the full concatenated JSON as the complete manifest

Manifest pagination uses the same `max_review_page_chars` boundary.

### Backward compatibility

`get_review_bundle` still exists but returns:
- The same manifest as `get_review_manifest` (direct dict, not paginated)
- Only the first page of diff (as `content`)
- `protocol: "paginated"`, `cursor`, `next_cursor`, `has_more`
- No aggregate `file_contents`
- No `context`, `related_context`, `related_omitted_files`, `git_status`
- Instructions direct client to `get_review_bundle_page` for continuation

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

1. Add an entry to `projects.yml` under `projects:` with `source`,
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
./start.sh doctor     # read-only diagnostics
```

## Health endpoints

| Path       | Status | Body  |
|------------|--------|-------|
| `/healthz` | 200    | `live`  |
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

`setup` verifies required programs and tunnel environment, runs
`uv sync --frozen`, installs the two user units, and reloads the user manager.

No secrets file is required — the MCP server no longer uses API tokens,
DeepSeek keys, or Copilot credentials.
