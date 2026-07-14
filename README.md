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
uv sync
./setup-secrets.sh
./create-demo.sh
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

## Safety boundary

- Tasks run in isolated local clones with Git remotes removed.
- Copilot receives only the allowlisted DeepSeek BYOK variables.
- Codex and checks do not receive the DeepSeek API key.
- Copilot has file read/write tools only; shell, URL access and built-in MCP
  servers are disabled.
- There are no commit, push, PR, merge, deployment, or GitHub issue tools.
