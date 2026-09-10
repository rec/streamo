#!/bin/bash

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Error: run this script as root" >&2
    exit 1
fi
if [[ $# -ne 1 || ${#1} -lt 20 ]]; then
    echo "Usage: $0 STREAMO_IMAGE_TOKEN" >&2
    exit 1
fi

readonly TOKEN="$1"
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
