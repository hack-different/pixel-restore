#!/usr/bin/env bash
# Build a read-enabled fastbootd + recovery for komodo inside the Docker image.
#
# Run from the HOST (macOS). It builds the image, then runs the build inside the
# container against an AOSP/GrapheneOS tree you have already `repo sync`-ed.
#
#   SRC=/path/to/aosp-or-grapheneos-tree ./docker/build.sh
#
# Variables:
#   SRC           Host path of the synced platform tree (required).
#   LUNCH_TARGET  lunch combo (default: komodo-userdebug). For GrapheneOS use the
#                 channel target its build docs name, e.g. komodo-cur-userdebug.
#                 userdebug matters: it defines FB_ENABLE_FETCH (so `fetch` is
#                 compiled in) and keeps the recovery/fastbootd domains able to
#                 share the tmpfs policy file.
#   CCACHE_DIR    Host ccache dir (default: $SRC/.ccache).
#   PLATFORM      Container platform (default: linux/amd64 for the x86_64
#                 AOSP host toolchain; override only on a native x86_64 host).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
: "${SRC:?Set SRC to your synced AOSP/GrapheneOS tree}"
LUNCH_TARGET="${LUNCH_TARGET:-komodo-userdebug}"
CCACHE_DIR="${CCACHE_DIR:-$SRC/.ccache}"
PLATFORM="${PLATFORM:-linux/amd64}"
mkdir -p "$CCACHE_DIR"

echo ">> Building Docker image (fastbootd-read-builder)"
docker build --platform "$PLATFORM" -t fastbootd-read-builder "$here"

echo ">> Building $LUNCH_TARGET in container (SRC=$SRC)"
docker run --rm -it --platform "$PLATFORM" \
  -v "$SRC":/aosp \
  -v "$repo_root":/patches:ro \
  -v "$CCACHE_DIR":/ccache \
  -e CCACHE_DIR=/ccache \
  -e LUNCH_TARGET="$LUNCH_TARGET" \
  fastbootd-read-builder \
  bash -lc '
    set -euo pipefail
    cd /aosp
    /patches/scripts/apply-patches.sh /aosp /patches
    ccache -M 50G >/dev/null || true
    source build/envsetup.sh
    lunch "$LUNCH_TARGET"
    # Rebuild the daemon, the recovery binary, and the ramdisk-carrying images.
    m fastbootd recovery bootimage vendorbootimage
    echo ">> Build done. Artifacts under: $OUT"
    ls -la "$OUT"/boot.img "$OUT"/vendor_boot.img 2>/dev/null || true
  '
