#!/bin/bash

set -euo pipefail
umask 022

readonly IMAGEMAGICK_VERSION="7.1.2-31"
readonly APPIMAGE_NAME="ImageMagick-${IMAGEMAGICK_VERSION}-gcc-x86_64.AppImage"
readonly APPIMAGE_SHA256="b22dee096a68e7eb6771a6f98c16490ea78d399ffa27f34ca2ec4f02e707fd9c"
readonly DOWNLOAD_URL="https://github.com/ImageMagick/ImageMagick/releases/download/${IMAGEMAGICK_VERSION}/${APPIMAGE_NAME}"
readonly INSTALL_ROOT="/opt/ax-images"
readonly RELEASE_DIR="${INSTALL_ROOT}/releases/${IMAGEMAGICK_VERSION}"
readonly CONFIG_DIR="${INSTALL_ROOT}/etc/ImageMagick-7"
readonly WORK_DIR="${INSTALL_ROOT}/work"
readonly WRAPPER="${INSTALL_ROOT}/bin/magick"

fail() {
    echo "Error: $*" >&2
    exit 1
}

if [[ ${EUID} -ne 0 ]]; then
    fail "run this script as root"
fi

if [[ $# -ne 1 ]]; then
    fail "usage: $0 VIRTUALMIN_USER"
fi

readonly APP_USER="$1"
if ! getent passwd "${APP_USER}" >/dev/null; then
    fail "user does not exist: ${APP_USER}"
fi
if [[ $(id -u "${APP_USER}") -eq 0 ]]; then
    fail "the PHP-FPM user must not be root"
fi
readonly APP_GROUP="$(id -gn "${APP_USER}")"

if [[ $(uname -m) != "x86_64" ]]; then
    fail "this installer requires an x86_64 server"
fi

for command in base64 cat chmod chown curl getent grep id install ln mktemp mv \
    readlink rmdir runuser sha256sum timeout uname unlink; do
    command -v "${command}" >/dev/null || fail "required command is missing: ${command}"
done

install -d -o root -g root -m 0755 \
    "${INSTALL_ROOT}" \
    "${INSTALL_ROOT}/bin" \
    "${INSTALL_ROOT}/etc" \
    "${INSTALL_ROOT}/releases" \
    "${WORK_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 \
    "${WORK_DIR}/incoming" \
    "${WORK_DIR}/output" \
    "${WORK_DIR}/temporary"

if [[ -e "${RELEASE_DIR}" && ! -x "${RELEASE_DIR}/AppRun" ]]; then
    fail "incomplete installation already exists at ${RELEASE_DIR}"
fi

if [[ ! -e "${RELEASE_DIR}" ]]; then
    staging_dir="$(mktemp -d /tmp/ax-images-install.XXXXXXXX)"
    appimage="${staging_dir}/${APPIMAGE_NAME}"
    echo "Downloading ImageMagick ${IMAGEMAGICK_VERSION}..."
    curl \
        --proto '=https' \
        --tlsv1.2 \
        --fail \
        --location \
        --silent \
        --show-error \
        --output "${appimage}" \
        "${DOWNLOAD_URL}"
    printf '%s  %s\n' "${APPIMAGE_SHA256}" "${appimage}" | sha256sum --check -
    chmod 0755 "${appimage}"

    echo "Extracting the AppImage without FUSE..."
    (
        cd "${staging_dir}"
        "${appimage}" --appimage-extract >/dev/null
    )
    [[ -x "${staging_dir}/squashfs-root/AppRun" ]] || \
        fail "the downloaded AppImage did not extract correctly"

    mv "${staging_dir}/squashfs-root" "${RELEASE_DIR}"
    mv "${appimage}" "${RELEASE_DIR}/${APPIMAGE_NAME}"
    rmdir "${staging_dir}"
else
    echo "ImageMagick ${IMAGEMAGICK_VERSION} is already installed."
fi

install -d -o root -g root -m 0755 "${CONFIG_DIR}"
policy_staging="$(mktemp "${CONFIG_DIR}/.policy.xml.XXXXXXXX")"
cat >"${policy_staging}" <<'POLICY'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policymap>
<policymap>
  <policy domain="resource" name="thread" value="2"/>
  <policy domain="resource" name="time" value="15"/>
  <policy domain="resource" name="file" value="32"/>
  <policy domain="resource" name="memory" value="128MiB"/>
  <policy domain="resource" name="map" value="256MiB"/>
  <policy domain="resource" name="area" value="25MP"/>
  <policy domain="resource" name="disk" value="512MiB"/>
  <policy domain="resource" name="list-length" value="1"/>
  <policy domain="resource" name="width" value="8192"/>
  <policy domain="resource" name="height" value="8192"/>
  <policy domain="resource" name="temporary-path" value="/opt/ax-images/work/temporary"/>
  <policy domain="system" name="max-memory-request" value="128MiB"/>
  <policy domain="system" name="memory-map" value="anonymous"/>
  <policy domain="system" name="symlink" rights="none" pattern="follow"/>
  <policy domain="cache" name="memory-map" value="anonymous"/>
  <policy domain="delegate" rights="none" pattern="*"/>
  <policy domain="filter" rights="none" pattern="*"/>
  <policy domain="module" rights="none" pattern="*"/>
  <policy domain="module" rights="read|write" pattern="{JPEG,PNG,WEBP}"/>
  <policy domain="coder" rights="none" pattern="*"/>
  <policy domain="coder" rights="read|write" pattern="{JPEG,PNG,WEBP}"/>
  <policy domain="path" rights="none" pattern="-"/>
  <policy domain="path" rights="none" pattern="fd:*"/>
  <policy domain="path" rights="none" pattern="@*"/>
  <policy domain="path" rights="none" pattern="*../*"/>
  <policy domain="path" rights="none" pattern="*"/>
  <policy domain="path" rights="read|write" pattern="/opt/ax-images/work/*"/>
</policymap>
POLICY
chmod 0644 "${policy_staging}"
chown root:root "${policy_staging}"
mv -f "${policy_staging}" "${CONFIG_DIR}/policy.xml"
[[ -d "${RELEASE_DIR}/usr/etc/ImageMagick-7" ]] || \
    fail "the extracted AppImage has no ImageMagick configuration directory"
install -o root -g root -m 0644 \
    "${CONFIG_DIR}/policy.xml" \
    "${RELEASE_DIR}/usr/etc/ImageMagick-7/policy.xml"

wrapper_staging="$(mktemp "${INSTALL_ROOT}/bin/.magick.XXXXXXXX")"
cat >"${wrapper_staging}" <<'WRAPPER'
#!/bin/bash

set -euo pipefail

export MAGICK_CONFIGURE_PATH="/opt/ax-images/etc/ImageMagick-7"
export MAGICK_TEMPORARY_PATH="/opt/ax-images/work/temporary"

exec /usr/bin/timeout --signal=KILL 20s \
    /opt/ax-images/current/AppRun "$@"
WRAPPER
chmod 0755 "${wrapper_staging}"
chown root:root "${wrapper_staging}"
mv -f "${wrapper_staging}" "${WRAPPER}"

if [[ -e "${INSTALL_ROOT}/current" && ! -L "${INSTALL_ROOT}/current" ]]; then
    fail "${INSTALL_ROOT}/current exists and is not a symbolic link"
fi
current_staging="${INSTALL_ROOT}/.current.$$"
ln -s "releases/${IMAGEMAGICK_VERSION}" "${current_staging}"
mv -Tf "${current_staging}" "${INSTALL_ROOT}/current"

test_png="${WORK_DIR}/temporary/.installation-test.png"
test_jpeg="${WORK_DIR}/temporary/.installation-test.jpg"
test_webp="${WORK_DIR}/temporary/.installation-test.webp"
test_svg="${WORK_DIR}/temporary/.installation-test.svg"
test_blocked="${WORK_DIR}/temporary/.installation-test-blocked.png"

printf '%s' \
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=' \
    | base64 --decode >"${test_png}"
runuser -u "${APP_USER}" -- "${WRAPPER}" "${test_png}" -strip "${test_jpeg}"
runuser -u "${APP_USER}" -- "${WRAPPER}" "${test_png}" -strip "${test_webp}"
printf '%s\n' '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>' \
    >"${test_svg}"
if runuser -u "${APP_USER}" -- \
    "${WRAPPER}" "${test_svg}" "${test_blocked}" >/dev/null 2>&1; then
    fail "the security policy unexpectedly allowed SVG input"
fi

unlink "${test_png}"
unlink "${test_jpeg}"
unlink "${test_webp}"
unlink "${test_svg}"
[[ ! -e "${test_blocked}" ]] || unlink "${test_blocked}"

chmod 0755 "${WORK_DIR}"
chmod 0750 "${WORK_DIR}/incoming" \
    "${WORK_DIR}/output" \
    "${WORK_DIR}/temporary"

echo
"${WRAPPER}" -version
echo
echo "Enabled upload formats:"
"${WRAPPER}" -list format | grep -E '^ *\*? *(JPEG|PNG|WEBP)' || true
echo
echo "Installed ${WRAPPER} for PHP-FPM user ${APP_USER}."
echo "Ubuntu's /usr/bin/convert and PHP configuration were not changed."
