#!/usr/bin/env bash
# Apply (or revert) the read-enabled fastbootd patches to a platform tree.
#   apply-patches.sh [--revert] <AOSP_ROOT> <THIS_REPO_ROOT>
# Idempotent: a patch that is already applied is skipped (or, with --revert,
# one that is not applied). It dry-runs everything first and changes nothing if
# any patch neither applies nor is already in place, e.g. when your tree differs
# from the base. AOSP is multi-repo, so we use plain `patch` from the tree root.
set -euo pipefail
revert=0
if [ "${1:-}" = --revert ]; then revert=1; shift; fi
AOSP_ROOT="${1:?usage: apply-patches.sh [--revert] <AOSP_ROOT> <REPO_ROOT>}"
REPO_ROOT="${2:?usage: apply-patches.sh [--revert] <AOSP_ROOT> <REPO_ROOT>}"
cd "$AOSP_ROOT"
patches=(
  "$REPO_ROOT/patches/0001-system-core-fastbootd-runtime-fetch-policy.patch"
  "$REPO_ROOT/patches/0002-bootable-recovery-fastbootd-fetch-menu.patch"
  "$REPO_ROOT/patches/0003-system-core-init-force-recovery-marker.patch"
)

todo=()
for p in "${patches[@]}"; do
  name=$(basename "$p")
  if patch -p1 -R --dry-run --silent -f < "$p" >/dev/null 2>&1; then
    applied=1
  elif patch -p1 --dry-run --silent -f < "$p" >/dev/null 2>&1; then
    applied=0
  else
    echo "!! $name neither applies nor is already applied here."
    echo "   Your tree differs from the base the patches were cut against"
    echo "   (GrapheneOS 17: system/core and bootable/recovery). Review and apply"
    echo "   by hand, then re-run the build."
    exit 1
  fi
  if [ "$revert" = 1 ]; then
    [ "$applied" = 1 ] && todo+=("$p") || echo ">> $name is not applied"
  else
    [ "$applied" = 0 ] && todo+=("$p") || echo ">> $name is already applied"
  fi
done

for p in "${todo[@]+"${todo[@]}"}"; do
  if [ "$revert" = 1 ]; then
    echo ">> Reverting $(basename "$p")"
    patch -p1 -R --no-backup-if-mismatch < "$p"
  else
    echo ">> Applying $(basename "$p")"
    patch -p1 --no-backup-if-mismatch < "$p"
  fi
done
echo ">> Patches $([ "$revert" = 1 ] && echo reverted || echo applied)."
