#!/usr/bin/env bash
# One command, run from the HOST (macOS or Linux): sync GrapheneOS, generate
# the komodo vendor files, apply the read-enable patches, build, verify, and
# copy the images out.
#
#   ./docker/build.sh            # sync + vendor + patch + build (re-runs are incremental)
#   ./docker/build.sh shell      # a shell in the container, tree at /aosp
#
# All of the work happens inside the container (scripts/container-build.sh).
# Nothing here talks to a phone.
#
# Where the source tree lives:
#   By default in a Docker named volume ($VOLUME). On a Mac that is the right
#   place: it is a case-sensitive Linux filesystem inside Docker's VM, which
#   AOSP needs and a normal APFS folder is not. Give Docker Desktop enough disk
#   (Settings > Resources > Disk usage limit): about 300 GB for the tree, the
#   build output and ccache. Set SRC to use a host directory instead (a Linux
#   host, or a case-sensitive APFS volume).
#
# Variables (all optional):
#   BRANCH        GrapheneOS manifest branch (default: 17, what the patches target)
#   TAG           a GrapheneOS release tag instead of BRANCH; the manifest's
#                 signature is verified against grapheneos.org/allowed_signers
#   MANIFEST_URL  default: https://github.com/GrapheneOS/platform_manifest.git
#   DEVICE        default: komodo
#   VARIANT       default: userdebug (it compiles `fastboot fetch` into fastbootd)
#   LUNCH_TARGET  default: $DEVICE-cur-$VARIANT
#   SYNC          1 = repo init + sync (default), 0 = build the tree as it is
#   VENDOR        auto (default: run adevtool once per manifest revision), 1, 0
#   JOBS          repo sync jobs (default: 8)
#   VOLUME        source volume name (default: fastbootd-read-src)
#   SRC           host path for the tree instead of VOLUME
#   CCACHE_DIR    host path for ccache (default: volume fastbootd-read-ccache)
#   ARTIFACTS     where images are copied (default: fastbootd-read/out/$DEVICE)
#   PLATFORM      container platform (default: linux/amd64)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
DEVICE="${DEVICE:-komodo}"
PLATFORM="${PLATFORM:-linux/amd64}"
VOLUME="${VOLUME:-fastbootd-read-src}"
ARTIFACTS="${ARTIFACTS:-$repo_root/out/$DEVICE}"
mode="${1:-build}"
case "$mode" in
  build|shell) ;;
  *) echo "usage: $0 [build|shell]" >&2; exit 2 ;;
esac

command -v docker >/dev/null || { echo "!! docker is not installed" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "!! the Docker daemon is not running" >&2; exit 1; }

if [ -n "${SRC:-}" ]; then
  mkdir -p "$SRC"
  src_mount="$SRC"
else
  docker volume create "$VOLUME" >/dev/null
  src_mount="$VOLUME"
fi
if [ -n "${CCACHE_DIR:-}" ]; then
  mkdir -p "$CCACHE_DIR"
  ccache_mount="$CCACHE_DIR"
else
  docker volume create fastbootd-read-ccache >/dev/null
  ccache_mount=fastbootd-read-ccache
fi
mkdir -p "$ARTIFACTS"

echo ">> Building Docker image (fastbootd-read-builder)"
docker build --platform "$PLATFORM" -t fastbootd-read-builder "$here"

tty=()
[ -t 0 ] && [ -t 1 ] && tty=(-it)
env_args=(-e DEVICE="$DEVICE")
for v in BRANCH TAG MANIFEST_URL VARIANT LUNCH_TARGET SYNC VENDOR JOBS; do
  [ -n "${!v:-}" ] && env_args+=(-e "$v=${!v}")
done

run=(docker run --rm "${tty[@]}" --platform "$PLATFORM"
  -v "$src_mount":/aosp
  -v "$repo_root":/patches:ro
  -v "$ccache_mount":/ccache
  -v "$ARTIFACTS":/artifacts
  "${env_args[@]}"
  fastbootd-read-builder)

if [ "$mode" = shell ]; then
  exec "${run[@]}" bash
fi
echo ">> Source tree: $src_mount   artifacts: $ARTIFACTS"
"${run[@]}" bash /patches/scripts/container-build.sh
