#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${HOME}/.config/bee-dev-mcp"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
TUNNEL_ENV="${CONFIG_DIR}/tunnel.env"
MCP_SVC="bee-dev-mcp.service"
TUNNEL_SVC="bee-dev-mcp-tunnel.service"
MCP_BASE_URL="http://127.0.0.1:8000"
HEALTHZ_URL="${MCP_BASE_URL}/healthz"
READYZ_URL="${MCP_BASE_URL}/readyz"
VERSIONZ_URL="${MCP_BASE_URL}/versionz"
MCP_URL="${MCP_BASE_URL}/mcp"

die() { echo "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required CLI not found: $1"
}

require_file() {
  [[ -r "$1" ]] || die "Required file is missing or unreadable: $1"
}

units_installed() {
  [[ -f "${SYSTEMD_DIR}/${MCP_SVC}" && -f "${SYSTEMD_DIR}/${TUNNEL_SVC}" ]]
}

wait_ready() {
  local attempt
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if curl --fail --silent --output /dev/null "${READYZ_URL}"; then
      return 0
    fi
    (( attempt == 30 )) || sleep 1
  done
  return 1
}

check_tools() {
  "${SCRIPT_DIR}/.venv/bin/python" -B \
    "${SCRIPT_DIR}/scripts/check_mcp_tools.py" \
    --url "${MCP_URL}"
}

check_active() {
  systemctl --user is-active "$1" >/dev/null 2>&1 || die "Service is not active: $1"
}

cmd_setup() {
  local command
  for command in systemctl journalctl uv curl tunnel-client; do
    require_cmd "$command"
  done
  require_file "${TUNNEL_ENV}"
  require_file "${SCRIPT_DIR}/ops/systemd/${MCP_SVC}"
  require_file "${SCRIPT_DIR}/ops/systemd/${TUNNEL_SVC}"

  (cd "${SCRIPT_DIR}" && uv sync --frozen)
  [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]] || die "uv sync did not create .venv/bin/python"
  install -d -m 0755 "${SYSTEMD_DIR}"
  install -m 0644 "${SCRIPT_DIR}/ops/systemd/${MCP_SVC}" "${SYSTEMD_DIR}/${MCP_SVC}"
  install -m 0644 "${SCRIPT_DIR}/ops/systemd/${TUNNEL_SVC}" "${SYSTEMD_DIR}/${TUNNEL_SVC}"
  systemctl --user daemon-reload
  echo "Setup complete"
}

start_stack() {
  systemctl --user start "${MCP_SVC}"
  if ! wait_ready; then
    echo "MCP readiness check failed" >&2
    journalctl --user -u "${MCP_SVC}" --no-pager -n 40 >&2 || true
    return 1
  fi
  if ! check_tools; then
    echo "MCP tool contract check failed, stopping MCP" >&2
    systemctl --user stop "${MCP_SVC}" || true
    return 1
  fi
  systemctl --user start "${TUNNEL_SVC}"
  check_active "${MCP_SVC}"
  check_active "${TUNNEL_SVC}"
}

cmd_up() {
  if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]] || ! units_installed; then
    cmd_setup
  fi
  start_stack
  curl --fail --silent "${VERSIONZ_URL}"
  echo ""
  echo "ChatGPT connector schema is not verified by local doctor."
  echo "After tool schema changes, refresh the Bee Dev MCP app actions and start a new chat."
  echo "Stack is up"
}

cmd_status() {
  systemctl --user status "${MCP_SVC}" || die "MCP service status failed"
  systemctl --user status "${TUNNEL_SVC}" || die "Tunnel service status failed"
  curl --fail --silent --output /dev/null "${HEALTHZ_URL}" || die "Health check failed"
  curl --fail --silent --output /dev/null "${READYZ_URL}" || die "Readiness check failed"
}

cmd_logs() {
  journalctl --user -u "${MCP_SVC}" -u "${TUNNEL_SVC}" -f "$@"
}

cmd_restart() {
  systemctl --user stop "${TUNNEL_SVC}" || true
  systemctl --user stop "${MCP_SVC}" || true
  start_stack
  curl --fail --silent "${VERSIONZ_URL}"
  echo ""
  echo "ChatGPT connector schema is not verified by local doctor."
  echo "After tool schema changes, refresh the Bee Dev MCP app actions and start a new chat."
  echo "Stack restarted"
}

cmd_stop() {
  systemctl --user stop "${TUNNEL_SVC}" || true
  systemctl --user stop "${MCP_SVC}" || true
  echo "Stack stopped"
}

cmd_doctor() {
  local -a problems=()
  local -a lines=()
  local command path
  for command in systemctl journalctl uv curl tunnel-client; do
    command -v "$command" >/dev/null 2>&1 || problems+=("required CLI missing: $command")
  done
  [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]] || problems+=("virtualenv python missing")
  for path in \
    "${SCRIPT_DIR}/ops/systemd/${MCP_SVC}" \
    "${SCRIPT_DIR}/ops/systemd/${TUNNEL_SVC}"; do
    [[ -r "$path" ]] || problems+=("source unit missing: ${path##*/}")
  done
  for path in "${SYSTEMD_DIR}/${MCP_SVC}" "${SYSTEMD_DIR}/${TUNNEL_SVC}"; do
    [[ -r "$path" ]] || problems+=("installed unit missing: ${path##*/}")
  done
  [[ -r "${TUNNEL_ENV}" ]] || problems+=("required env file missing: tunnel.env")
  systemctl --user show-environment >/dev/null 2>&1 || problems+=("systemd user manager unavailable")

  if curl --fail --silent --output /dev/null "${HEALTHZ_URL}"; then
    lines+=("Local MCP health: OK")
  else
    problems+=("health endpoint unavailable")
  fi

  if curl --fail --silent --output /dev/null "${READYZ_URL}"; then
    lines+=("Local MCP readiness: OK")
  else
    problems+=("readiness endpoint unavailable")
  fi

  if systemctl --user is-active "${MCP_SVC}" >/dev/null 2>&1; then
    lines+=("MCP service: active")
  else
    problems+=("service inactive: ${MCP_SVC}")
  fi

  if check_tools; then
    lines+=("Local MCP tool contract: OK")
  else
    problems+=("MCP tool contract check failed")
  fi

  if systemctl --user is-active "${TUNNEL_SVC}" >/dev/null 2>&1; then
    lines+=("Tunnel service: active")
  else
    problems+=("service inactive: ${TUNNEL_SVC}")
  fi

  if ((${#problems[@]})); then
    for line in "${lines[@]}"; do echo "$line"; done
    echo ""
    echo "Doctor found ${#problems[@]} problem(s):" >&2
    printf ' - %s\n' "${problems[@]}" >&2
    return 1
  fi
  for line in "${lines[@]}"; do echo "$line"; done
  echo "ChatGPT connector schema: NOT VERIFIED — refresh app actions after schema changes"
  echo "Doctor: all checks passed"
}

case "${1:-up}" in
  setup) cmd_setup ;;
  up) cmd_up ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  restart) cmd_restart ;;
  stop) cmd_stop ;;
  doctor) cmd_doctor ;;
  *) die "Usage: ${0##*/} {setup|up|status|logs|restart|stop|doctor}" ;;
esac
