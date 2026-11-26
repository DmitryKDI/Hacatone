#!/usr/bin/env bash
set -euo pipefail

prefix="${DEV_TOOLS_PREFIX:-/workspace/.local}"
arch="$(uname -m)"
case "$(uname -s):$arch" in
  Linux:x86_64)
    rtk_sha=44aa460c4827d206d11405c652832c9f7082edccfbc3a9f15a69a851e8a9e8b4
    deslop_sha=64a6b59c249188c2b2d69e0eb9e1298afe8f7321bdf9313640ea54e1f423a236
    ;;
  Linux:aarch64)
    rtk_sha=b01860ae2d3f44e0f7fc4431a95dd66b29b4bd87ec7f5e60aff13681b05db5dd
    deslop_sha=dba3ba5e34b6f309441108353e5ea50160899c946a8db249bdaaf2ba761e0e65
    ;;
  *)
    printf 'Unsupported platform: %s %s\n' "$(uname -s)" "$arch" >&2
    exit 1
    ;;
esac

npm install --global --prefix "$prefix" --cache "${TMPDIR:-/tmp}/hacatone-npm-cache" @fission-ai/openspec@2.2.0

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT

install_release() {
  local release_url="$1" archive="$2" checksum="$3" executable="$4"
  curl --fail --silent --show-error --location "$release_url/$archive" --output "$temp_dir/$archive"
  printf '%s  %s\n' "$checksum" "$temp_dir/$archive" | sha256sum --check
  tar -xzf "$temp_dir/$archive" -C "$temp_dir" "$executable"
  install -m 755 "$temp_dir/$executable" "$prefix/bin/$executable"
}

install_release https://github.com/rtk-ai/rtk/releases/download/v1.2.1 \
  "rtk-oss-$arch-unknown-linux-musl.tar.gz" "$rtk_sha" rtk
install_release https://github.com/deterministic-machines/deslop-please/releases/download/v0.1.6 \
  "deslop-please-$arch-unknown-linux-musl-v0.1.6.tar.gz" "$deslop_sha" deslop-please

OPENSPEC_TELEMETRY=0 "$prefix/bin/openspec" --version
"$prefix/bin/rtk" --version
"$prefix/bin/deslop-please" --version
