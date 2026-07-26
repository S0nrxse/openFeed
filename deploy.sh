#!/usr/bin/env bash
set -euo pipefail

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${STACK_DIR}/compose.yml"
LOCAL_COMPOSE_FILE="${STACK_DIR}/compose.local.yml"
ENV_FILE="${OPENFEED_ENV_FILE:-${STACK_DIR}/.env}"

fail() { echo "error: $*" >&2; exit 1; }
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || fail "docker not found"
docker compose version >/dev/null 2>&1 || fail "docker compose not available"
[[ -f "${COMPOSE_FILE}" ]] || fail "compose file missing: ${COMPOSE_FILE}"
[[ -f "${ENV_FILE}" ]] || fail "environment file missing: ${ENV_FILE} (copy .env.example to .env and configure it)"

COMPOSE_ARGS=(--env-file "${ENV_FILE}" -f "${COMPOSE_FILE}")
if [[ -f "${LOCAL_COMPOSE_FILE}" ]]; then
  COMPOSE_ARGS+=(-f "${LOCAL_COMPOSE_FILE}")
fi

if [[ $# -eq 0 ]]; then
  docker compose "${COMPOSE_ARGS[@]}" up -d
  exit $?
fi

docker compose "${COMPOSE_ARGS[@]}" "$@"
