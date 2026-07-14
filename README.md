# Bee Dev MCP starter v2

This version supports GitHub Copilot CLI with a DeepSeek BYOK key and keeps that
key out of Codex, project checks, Git, and MCP tool responses.

## Quick start

```bash
cd ~/Projects
unzip ~/Downloads/bee-dev-mcp-starter-v2.zip
mv bee-dev-mcp-starter bee-dev-mcp
cd bee-dev-mcp

chmod +x start.sh setup-secrets.sh create-demo.sh
./setup-secrets.sh
./create-demo.sh
./start.sh setup
./start.sh
```

Local MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

Secrets are stored outside all repositories:

```text
~/.config/bee-dev-mcp/secrets.env
```

The directory is mode `700`; the file is mode `600`.

## Systemd workflow (recommended)

The stack runs as two systemd --user services:

| Service                    | Purpose                |
|----------------------------|------------------------|
| `bee-dev-mcp.service`      | FastMCP / MCP server   |
| `bee-dev-mcp-tunnel.service` | Tunnel / proxy      |

### Initial setup

```bash
cd /home/bee/Pro/bee-dev-mcp
chmod +x start.sh setup-secrets.sh create-demo.sh

# One-time secrets bootstrap (interactive — prompts for DeepSeek key)
./setup-secrets.sh

# (Optional) create demo project
./create-demo.sh

# Install systemd user units and sync dependencies
./start.sh setup
```

`setup` verifies the required programs and both environment files, runs
`uv sync --frozen`, installs the two user units, and reloads the user manager.
It does not start or enable either service. After setup, `./start.sh` (or the
equivalent `./start.sh up`) is the normal way to start the stack.

### Command interface

```bash
./start.sh up       # start both services (MCP → wait ready → tunnel)
./start.sh status   # show service status + endpoint health
./start.sh logs     # follow the combined logs for both services
./start.sh restart  # stop tunnel → stop MCP → start MCP → wait → start tunnel
./start.sh stop     # stop tunnel → stop MCP
./start.sh doctor   # read-only diagnostics (no env output)
```

### Transition from manual process

Before the first transition, stop any old manually started MCP and tunnel
processes with Ctrl+C in the terminals where they are running. Once the stack
has moved to systemd, stop its two services with `./start.sh stop`.

### Health endpoints

| Path       | Status | Body  |
|------------|--------|-------|
| `/healthz` | 200    | `live`  |
| `/readyz`  | 200    | `ready` |

These are unauthenticated custom routes on the FastMCP server. They do not
affect `/mcp` or any MCP tool auth.

## Safety boundary

- Tasks run in isolated local clones with Git remotes removed.
- Copilot receives only the allowlisted DeepSeek BYOK variables.
- Codex and checks do not receive the DeepSeek API key.
- Copilot has file read/write tools only; shell, URL access and built-in MCP
  servers are disabled.
- There are no commit, push, PR, merge, deployment, or GitHub issue tools.
