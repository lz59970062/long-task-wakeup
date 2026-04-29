#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <github-https-url> [subdirectory]" >&2
  echo "Example: $0 https://github.com/lz59970062/long-task-wakeup.git" >&2
  exit 2
fi

repo_url="$1"
subdirectory="${2:-}"

if [[ "$repo_url" == git+* ]]; then
  spec="$repo_url"
else
  spec="git+${repo_url}"
fi

if [[ -n "$subdirectory" ]]; then
  spec="${spec}#subdirectory=${subdirectory}"
fi

python3 -m pip install "$spec"
