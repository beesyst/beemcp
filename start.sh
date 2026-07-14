#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${HOME}/.config/bee-dev-mcp"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SECRETS_ENV="${CONFIG_DIR}/secrets.env"
TUNNEL_ENV="${CONFIG_DIR}/tunnel.env"
MCP_SVC="bee-dev-mcp.service"
TUNNEL_SVC="bee-dev-mcp-tunnel.service"
HEALTHZ_URL="http://127.0.0.1:8000/healthz"
READYZ_URL="http://127.0.0.1:8000/readyz"

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

check_active() {
  systemctl --user is-active "$1" >/dev/null 2>&1 || die "Service is not active: $1"
}

cmd_setup() {
  local command
  for command in systemctl journalctl uv curl tunnel-client; do
    require_cmd "$command"
  done
  require_file "${SECRETS_ENV}"
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
  systemctl --user start "${TUNNEL_SVC}"
  check_active "${MCP_SVC}"
  check_active "${TUNNEL_SVC}"
}

cmd_up() {
  if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]] || ! units_installed; then
    cmd_setup
  fi
  start_stack
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
  echo "Stack restarted"
}

cmd_stop() {
  systemctl --user stop "${TUNNEL_SVC}" || true
  systemctl --user stop "${MCP_SVC}" || true
  echo "Stack stopped"
}

cmd_doctor() {
  local -a problems=()
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
  [[ -r "${SECRETS_ENV}" ]] || problems+=("required env file missing: secrets.env")
  [[ -r "${TUNNEL_ENV}" ]] || problems+=("required env file missing: tunnel.env")
  systemctl --user show-environment >/dev/null 2>&1 || problems+=("systemd user manager unavailable")
  systemctl --user is-active "${MCP_SVC}" >/dev/null 2>&1 || problems+=("service inactive: ${MCP_SVC}")
  systemctl --user is-active "${TUNNEL_SVC}" >/dev/null 2>&1 || problems+=("service inactive: ${TUNNEL_SVC}")
  curl --fail --silent --output /dev/null "${HEALTHZ_URL}" || problems+=("health endpoint unavailable")
  curl --fail --silent --output /dev/null "${READYZ_URL}" || problems+=("readiness endpoint unavailable")

  if ((${#problems[@]})); then
    echo "Doctor found ${#problems[@]} problem(s):" >&2
    printf ' - %s\n' "${problems[@]}" >&2
    return 1
  fi
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
