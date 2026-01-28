#!/usr/bin/env bash
set -euo pipefail

export COPYFILE_DISABLE=1

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
  echo "Usage: ./build.sh <version>  (np. ./build.sh 0.0.1)" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(mktemp -d)"
OUT_DIR="${ROOT_DIR}/dist"

mkdir -p "${OUT_DIR}"

PKG="r4vk-speedtest"

cleanup() { rm -rf "${WORK_DIR}"; }
trap cleanup EXIT

cp -R "${ROOT_DIR}/src/." "${WORK_DIR}/"

# Render INFO
sed \
  -e "s/@VERSION@/${VERSION}/g" \
  "${ROOT_DIR}/src/INFO.in" > "${WORK_DIR}/INFO"
rm -f "${WORK_DIR}/INFO.in"

# Build payload tarball
tar --no-mac-metadata --no-xattrs --no-acls --no-fflags \
  -C "${ROOT_DIR}/src/package" -czf "${WORK_DIR}/package.tgz" .

# Build SPK (tar archive)
tar --no-mac-metadata --no-xattrs --no-acls --no-fflags \
  -C "${WORK_DIR}" -cf "${OUT_DIR}/${PKG}_${VERSION}.spk" INFO package.tgz scripts

echo "Built: ${OUT_DIR}/${PKG}_${VERSION}.spk"
