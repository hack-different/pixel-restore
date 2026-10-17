#!/usr/bin/env bash
# One command, run from the HOST (macOS or Linux): sync GrapheneOS, generate
# the komodo vendor files, apply the read-enable patches, build, verify, and
# copy the images out.
#
#   ./docker/build.sh            # sync + vendor + patch + build (re-runs are incremental)
#   ./docker/build.sh shell      # a shell in the container, tree at /aosp
#   ./docker/build.sh boot [...] # boot the result on the phone from RAM
#
# The build happens inside the container (scripts/container-build.sh) and does
# not talk to a phone. `boot` runs scripts/boot-fastbootd.sh on this host, which
# only ever `fastboot boot`s the image into RAM; see its --help.
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
#   CHOWN         1 = re-give the tree to the unprivileged build user (the build
#                 runs as `node`, uid 1000, because adevtool refuses root)
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
[ $# -gt 0 ] && shift
case "$mode" in
  build|shell|boot) ;;
  *) echo "usage: $0 [build|shell|boot [boot-fastbootd.sh options]]" >&2; exit 2 ;;
esac
boot_image="$ARTIFACTS/fastbootd-boot.img"
if [ "$mode" = boot ] && [ -f "$boot_image" ]; then
  exec "$repo_root/scripts/boot-fastbootd.sh" --image "$boot_image" "$@"
fi

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
for v in BRANCH TAG MANIFEST_URL VARIANT LUNCH_TARGET SYNC VENDOR JOBS CHOWN; do
  [ -n "${!v:-}" ] && env_args+=(-e "$v=${!v}")
done

run=(docker run --rm ${tty[@]+"${tty[@]}"} --platform "$PLATFORM"
  -v "$src_mount":/aosp
  -v "$repo_root":/patches:ro
  -v "$ccache_mount":/ccache
  -v "$ARTIFACTS":/artifacts
  "${env_args[@]}"
  fastbootd-read-builder)

if [ "$mode" = shell ]; then
  exec "${run[@]}" bash
fi
if [ "$mode" = boot ]; then
  # Built before the RAM-boot image existed: assemble it from the artifacts.
  echo ">> Assembling $boot_image"
  "${run[@]}" python3 -I /patches/scripts/make_ram_boot_image.py /artifacts
  exec "$repo_root/scripts/boot-fastbootd.sh" --image "$boot_image" "$@"
fi
echo ">> Source tree: $src_mount   artifacts: $ARTIFACTS"
"${run[@]}" bash /patches/scripts/container-build.sh
