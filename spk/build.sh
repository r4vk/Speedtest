#!/usr/bin/env bash
set -euo pipefail

export COPYFILE_DISABLE=1

TAR_BIN="${TAR_BIN:-tar}"
TAR_FLAGS=()
if "${TAR_BIN}" --version 2>/dev/null | rg -qi "bsdtar"; then
  TAR_FLAGS+=(--no-mac-metadata --no-xattrs --no-acls --no-fflags)
fi

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
  echo "Usage: ./build.sh <version>  (np. ./build.sh 0.0.2)" >&2
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

# Sync docker build context from ../app into package payload
mkdir -p "${WORK_DIR}/package/docker"
rm -rf "${WORK_DIR}/package/docker/"*
cp -R "${ROOT_DIR}/../app/Dockerfile" "${WORK_DIR}/package/docker/Dockerfile"
cp -R "${ROOT_DIR}/../app/requirements.txt" "${WORK_DIR}/package/docker/requirements.txt"
cp -R "${ROOT_DIR}/../app/.dockerignore" "${WORK_DIR}/package/docker/.dockerignore" 2>/dev/null || true
cp -R "${ROOT_DIR}/../app/speedtest_app" "${WORK_DIR}/package/docker/speedtest_app"
cp -R "${ROOT_DIR}/../app/static" "${WORK_DIR}/package/docker/static"

# Build payload tarball
"${TAR_BIN}" "${TAR_FLAGS[@]}" -C "${WORK_DIR}/package" -czf "${WORK_DIR}/package.tgz" .

# Build SPK (tar archive)
"${TAR_BIN}" "${TAR_FLAGS[@]}" -C "${WORK_DIR}" -cf "${OUT_DIR}/${PKG}_${VERSION}.spk" INFO package.tgz scripts conf

echo "Built: ${OUT_DIR}/${PKG}_${VERSION}.spk"
