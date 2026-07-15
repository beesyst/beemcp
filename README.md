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

## Example ChatGPT requests

### Task preparation

```text
Используй только Bee Dev MCP Test.

Через Bee Dev MCP Test прочитай проект beeagent в режиме A.

Сравни ROADMAP beeagent и beeagent-rop.
Подготовь формулировку итерации, Issue, prompt реализации и prompt тестирования.

Ничего не изменяй и не запускай.
```

### Review

```text
Используй только Bee Dev MCP Test.

Посмотри изменённые файлы в beeagent-pr123.
Используй режим B.

Получи актуальный review bundle относительно main.
Проверь реализацию Issue #123.

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
