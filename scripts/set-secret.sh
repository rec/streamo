#!/bin/bash

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Error: run this script as root" >&2
    exit 1
fi
TOKEN=''
if [[ $# -eq 0 ]]; then
    if [[ ! -r /dev/tty ]]; then
        echo "Error: an interactive terminal or --token-file is required" >&2
        exit 1
    fi
    read -r -s -p 'Room token: ' TOKEN </dev/tty
    printf '\n' >/dev/tty
elif [[ $# -eq 2 && $1 == --token-file ]]; then
    if [[ ! -f $2 || ! -r $2 ]]; then
        echo "Error: token file must be a readable regular file" >&2
        exit 1
    fi
    mode=$(stat -c '%a' -- "$2")
    if (( (8#$mode & 077) != 0 )); then
        echo "Error: token file must not be accessible to group or others" >&2
        exit 1
    fi
    IFS= read -r TOKEN <"$2" || [[ -n $TOKEN ]]
else
    echo "Usage: $0 [--token-file PRIVATE_FILE]" >&2
    exit 1
fi

if [[ ${#TOKEN} -lt 20 ]]; then
    echo "Error: room token must be at least 20 characters" >&2
    exit 1
fi

readonly TOKEN
readonly DATA_DIRECTORY="/home/ax/streamo-image-data"
mapfile -t POOLS < <(
    grep -rlE '^[[:space:]]*user[[:space:]]*=[[:space:]]*ax[[:space:]]*$' \
        /etc/php/*/fpm/pool.d
)
if [[ ${#POOLS[@]} -ne 1 ]]; then
    echo "Error: expected one PHP-FPM pool for ax" >&2
    exit 1
fi

readonly POOL="${POOLS[0]}"
readonly PHP_VERSION="$(basename "$(dirname "$(dirname "$(dirname "${POOL}")")")")"
readonly TEMPORARY="$(mktemp "${POOL}.XXXXXXXX")"

install -d -o ax -g ax -m 0700 "${DATA_DIRECTORY}"
sed \
    -e '/^[[:space:]]*env\[STREAMO_IMAGE_TOKEN\][[:space:]]*=/d' \
    -e '/^[[:space:]]*env\[STREAMO_IMAGE_DATA_DIR\][[:space:]]*=/d' \
    "${POOL}" >"${TEMPORARY}"
printf '\nenv[STREAMO_IMAGE_TOKEN] = %s\nenv[STREAMO_IMAGE_DATA_DIR] = %s\n' \
    "${TOKEN}" "${DATA_DIRECTORY}" >>"${TEMPORARY}"
chown root:root "${TEMPORARY}"
chmod --reference="${POOL}" "${TEMPORARY}"
mv -f "${TEMPORARY}" "${POOL}"
systemctl reload "php${PHP_VERSION}-fpm"
