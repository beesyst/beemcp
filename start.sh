#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"
ENV_EXAMPLE="${PROJECT_ROOT}/.env.example"
VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
COMMAND="${1:-up}"

cd "${PROJECT_ROOT}"

if [[ ! -f "${ENV_FILE}" ]]; then
  [[ -f "${ENV_EXAMPLE}" ]] \
    || {
      echo "[error] .env.example not found: ${ENV_EXAMPLE}" >&2
      exit 1
    }

  echo "[init] .env not found, creating from .env.example"
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
fi

chmod 0600 "${ENV_FILE}"

if ! command -v uv >/dev/null 2>&1; then
  command -v curl >/dev/null 2>&1 \
    || {
      echo "[error] curl is required to install uv" >&2
      exit 1
    }

  echo "[install] uv not found, installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if [[ ! -x "${VENV_PYTHON}" ]] \
   || [[ "${COMMAND}" == "up" ]] \
   || [[ "${COMMAND}" == "setup" ]] \
   || [[ "${COMMAND}" == "restart" ]]; then
  echo "[run] syncing dependencies from uv.lock..."
  uv sync --frozen
fi

[[ -x "${VENV_PYTHON}" ]] \
  || {
    echo "[error] virtualenv Python not found: ${VENV_PYTHON}" >&2
    exit 1
  }

exec "${VENV_PYTHON}" "${PROJECT_ROOT}/config/start.py" "$@"
