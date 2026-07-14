#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

secrets_file="${BEE_MCP_SECRETS_FILE:-${HOME}/.config/bee-dev-mcp/secrets.env}"
if [[ ! -r "$secrets_file" ]]; then
  echo "Secrets file not found: $secrets_file" >&2
  echo "Run: ./setup-secrets.sh" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$secrets_file"

required=(
  BEE_MCP_TOKEN
  COPILOT_PROVIDER_BASE_URL
  COPILOT_PROVIDER_API_KEY
  COPILOT_MODEL
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing variable in $secrets_file: $name" >&2
    exit 1
  fi
done

exec uv run python server.py
