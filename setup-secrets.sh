#!/usr/bin/env bash
set -euo pipefail

config_dir="${HOME}/.config/bee-dev-mcp"
secrets_file="${config_dir}/secrets.env"

mkdir -p "$config_dir"
chmod 700 "$config_dir"
umask 077

read -rsp "DeepSeek API key: " deepseek_key
echo
[[ -n "$deepseek_key" ]] || {
  echo "DeepSeek API key cannot be empty" >&2
  exit 1
}

mcp_token="$(openssl rand -hex 32)"

{
  printf 'export BEE_MCP_TOKEN=%q\n' "$mcp_token"
  printf 'export COPILOT_PROVIDER_TYPE=%q\n' "anthropic"
  printf 'export COPILOT_PROVIDER_BASE_URL=%q\n' "https://api.deepseek.com/anthropic"
  printf 'export COPILOT_PROVIDER_API_KEY=%q\n' "$deepseek_key"
  printf 'export COPILOT_MODEL=%q\n' "deepseek-v4-flash"
  printf 'export COPILOT_OFFLINE=%q\n' "true"
} > "$secrets_file"

chmod 600 "$secrets_file"
unset deepseek_key

echo "Secrets saved to: $secrets_file"
echo "Permissions: $(stat -c '%a' "$secrets_file")"
echo
echo "MCP token for ChatGPT tool calls:"
printf '%s\n' "$mcp_token"
