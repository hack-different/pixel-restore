#!/usr/bin/env bash
# The whole build, run INSIDE the fastbootd-read-builder container. Start it
# with docker/build.sh, which mounts:
#   /aosp       the GrapheneOS tree (created on first run)
#   /patches    this fastbootd-read directory (read only)
#   /artifacts  where the finished images are copied
#   /ccache     compiler cache
#
# Steps: repo init + sync -> adevtool vendor files -> apply patches -> build
# -> verify the patch is in the binary -> copy images to /artifacts
# -> assemble fastbootd-boot.img (scripts/make_ram_boot_image.py).
# Each step is safe to re-run. Nothing here talks to a phone.
set -euo pipefail

MANIFEST_URL="${MANIFEST_URL:-https://github.com/GrapheneOS/platform_manifest.git}"
BRANCH="${BRANCH:-17}"
TAG="${TAG:-}"
DEVICE="${DEVICE:-komodo}"
VARIANT="${VARIANT:-userdebug}"
LUNCH_TARGET="${LUNCH_TARGET:-$DEVICE-cur-$VARIANT}"
SYNC="${SYNC:-1}"
VENDOR="${VENDOR:-auto}"
JOBS="${JOBS:-8}"
PATCHES=/patches
ART=/artifacts

step() { echo; echo "==> $*"; }

# adevtool refuses to run as root, and nothing else here needs root. Give the
# tree and ccache to the image's unprivileged `node` user (uid 1000) once, then
# run this script again as that user. CHOWN=1 repeats the ownership fix, e.g.
# after running commands as root in `docker/build.sh shell`.
BUILD_USER=node
if [ "$(id -u)" = 0 ]; then
  owned=/aosp/.fastbootd-read-owner
  if [ "$(cat "$owned" 2>/dev/null)" != "$BUILD_USER" ] || [ "${CHOWN:-0}" = 1 ]; then
    step "Giving /aosp and /ccache to $BUILD_USER (once; a synced tree takes a few minutes)"
    chown -R "$BUILD_USER:$BUILD_USER" /aosp /ccache
    echo "$BUILD_USER" > "$owned"
    chown "$BUILD_USER:$BUILD_USER" "$owned"
  fi
  # A bind mount from macOS accepts writes from any uid; on Linux, own it.
  chown "$BUILD_USER:$BUILD_USER" /artifacts 2>/dev/null || true
  exec runuser -u "$BUILD_USER" -- env HOME="/home/$BUILD_USER" bash "$0" "$@"
fi
cd /aosp

# repo reads only the user's own ~/.gitconfig (not the image's system config)
# and asks interactively for anything missing there, which hangs the build.
git config --global --get color.ui >/dev/null || git config --global color.ui false
git config --global --get user.name >/dev/null || git config --global user.name "fastbootd-read builder"
git config --global --get user.email >/dev/null || git config --global user.email "builder@localhost"

step "Checking resources"
free_gb=$(df -Pk /aosp | awk 'NR==2 {print int($4 / 1048576)}')
mem_gb=$(awk '/MemTotal/ {print int($2 / 1048576)}' /proc/meminfo)
echo "   free disk: ${free_gb} GB   memory: ${mem_gb} GB   cpus: $(nproc)"
if [ ! -d .repo ] && [ "$free_gb" -lt 250 ]; then
  echo "!! A first sync + build needs about 250 GB free; raise Docker's disk limit."
  exit 1
fi
[ "$mem_gb" -ge 32 ] || echo "   warning: GrapheneOS recommends 32 GB+ of memory; the build may be killed."


if [ "$SYNC" = 1 ] || [ ! -d .repo ]; then
  rev="$BRANCH"
  [ -n "$TAG" ] && rev="refs/tags/$TAG"
  if [ -d .repo ]; then
    step "Reverting the fastbootd-read patches so repo can update those projects"
    "$PATCHES/scripts/apply-patches.sh" --revert /aosp "$PATCHES"
  fi
  step "repo init $MANIFEST_URL -b $rev"
  repo init -u "$MANIFEST_URL" -b "$rev"
  if [ -n "$TAG" ]; then
    step "Verifying the manifest tag signature"
    mkdir -p ~/.ssh
    curl -fsSL https://grapheneos.org/allowed_signers > ~/.ssh/grapheneos_allowed_signers
    git -C .repo/manifests config gpg.ssh.allowedSignersFile ~/.ssh/grapheneos_allowed_signers
    git -C .repo/manifests verify-tag "$(git -C .repo/manifests describe)"
  fi
  step "repo sync -j$JOBS"
  # A sync interrupted by the network resumes when re-run.
  for attempt in 1 2 3; do
    repo sync -j"$JOBS" --force-sync && break
    [ "$attempt" = 3 ] && { echo "!! repo sync failed three times"; exit 1; }
    echo "   sync failed; retrying ($attempt/3)"
  done
fi

# Soong runs build steps in nsjail. If nsjail cannot start, Soong carries on
# unsandboxed but prints a warning that makes adevtool abort. Run Soong's own
# self-test with the tree's nsjail first, so that fails here with a fix.
nsjail=prebuilts/build-tools/linux-x86/bin/nsjail
# shellcheck disable=SC2016 # the hostname check runs inside the sandbox
if [ -x "$nsjail" ] && ! "$nsjail" -Q -H android-build -e -u nobody -g nogroup -R / -B /tmp \
    --disable_clone_newcgroup -- /bin/bash -c '[ "$(hostname)" = android-build ]' >/dev/null 2>&1; then
  echo "!! The build's nsjail sandbox cannot start in this container, so adevtool"
  echo "   would fail with \"Build sandboxing disabled due to nsjail error\"."
  echo "   docker/build.sh passes --security-opt seccomp=unconfined, apparmor=unconfined"
  echo "   and systempaths=unconfined for it. If this host still refuses, run with"
  echo "   DOCKER_RUN_OPTS=--privileged ./docker/build.sh"
  exit 1
fi

# envsetup.sh, lunch and m are not written for `set -eu`; check each step instead.
set +eu
source build/envsetup.sh >/dev/null || exit 1

manifest_rev=$(git -C .repo/manifests rev-parse HEAD)
marker=".fastbootd-read-vendor-$DEVICE"
if [ "$VENDOR" = 1 ] || { [ "$VENDOR" = auto ] && [ "$(cat "$marker" 2>/dev/null)" != "$manifest_rev" ]; }; then
  step "Generating $DEVICE vendor files with adevtool (downloads the stock factory image)"
  yarn --cwd vendor/adevtool/ install || exit 1
  if command -v adevtool >/dev/null; then
    adevtool generate-all -d "$DEVICE" || exit 1
  else
    vendor/adevtool/bin/run generate-all -d "$DEVICE" || exit 1
  fi
  echo "$manifest_rev" > "$marker"
else
  step "Vendor files for $DEVICE are current (VENDOR=1 to regenerate)"
fi

step "Putting the patched projects on the fastbootd-read branch"
# A named topic branch shows `repo status` which projects carry local changes.
repo start fastbootd-read system/core bootable/recovery || exit 1

step "Applying the fastbootd-read patches"
"$PATCHES/scripts/apply-patches.sh" /aosp "$PATCHES" || exit 1

step "lunch $LUNCH_TARGET"
lunch "$LUNCH_TARGET" || exit 1

step "Building fastbootd, recovery and the boot images"
m fastbootd recovery bootimage initbootimage vendorbootimage vendorkernelbootimage \
  unpack_bootimg mkbootimg || exit 1

step "Verifying the patch is in the built fastbootd"
WORK="$ART/verify" "$PATCHES/scripts/make-boot-image.sh" || exit 1

step "Copying images to the artifacts directory"
for f in boot.img init_boot.img vendor_boot.img vendor_kernel_boot.img recovery.img; do
  if [ -f "$OUT/$f" ]; then cp -v "$OUT/$f" "$ART/"; fi
done
find "$OUT" -path "$OUT/obj" -prune -o -name fastbootd -type f -print \
  | head -1 | xargs -r -I{} cp -v {} "$ART/fastbootd"
repo manifest -r -o "$ART/manifest.xml" 2>/dev/null
echo "$LUNCH_TARGET" > "$ART/lunch-target.txt"

step "Assembling fastbootd-boot.img for a RAM-only \`fastboot boot\`"
python3 -I "$PATCHES/scripts/make_ram_boot_image.py" "$ART" /aosp/system/tools/mkbootimg || exit 1

echo
echo ">> Done. Images are in the artifacts directory (docker/build.sh prints its host path)."
echo "   manifest.xml pins the exact source revisions that were built."
echo "   To boot it on the phone from RAM (nothing is flashed): ./docker/build.sh boot"
