#!/usr/bin/env bash

set -euo pipefail

if [[ $# -eq 2 && $1 == --dry-run ]]; then
    dry_run=(--dry-run)
    target=$2
elif [[ $# -eq 1 ]]; then
    dry_run=()
    target=$1
else
    echo "Usage: $0 [--dry-run] USER@HOST:DESTINATION" >&2
    exit 1
fi

if [[ $target != *@*:* ]]; then
    echo "Error: target must be USER@HOST:DESTINATION" >&2
    exit 1
fi

echo "Copying web/foto.php to $target"
source_file="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/web/foto.php"
rsync -rtvp "${dry_run[@]}" --chown=ax:ax -- "$source_file" "$target"
