#!/usr/bin/env bash
# Azure App Service (Oryx) prepends /agents/python to PYTHONPATH. That directory
# ships a stub typing_extensions that shadows antenv's package and breaks
# anyio/fastapi imports of typing_extensions.sentinel (microsoft/Oryx#2685).
set -euo pipefail

echo "startup.sh: original PYTHONPATH=${PYTHONPATH:-}"

# Drop every /agents/python entry while keeping antenv and other paths.
_cleaned=""
IFS=':' read -r -a _parts <<< "${PYTHONPATH:-}"
for _p in "${_parts[@]}"; do
  case "$_p" in
    ""|/agents/python|/agents/python/*) continue ;;
  esac
  if [ -z "$_cleaned" ]; then
    _cleaned="$_p"
  else
    _cleaned="${_cleaned}:${_p}"
  fi
done
export PYTHONPATH="$_cleaned"

echo "startup.sh: cleaned PYTHONPATH=${PYTHONPATH:-}"

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
