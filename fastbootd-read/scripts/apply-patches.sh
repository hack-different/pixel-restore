#!/usr/bin/env bash
# Apply the read-enabled fastbootd patches to a platform tree.
#   apply-patches.sh <AOSP_ROOT> <THIS_REPO_ROOT>
# Idempotent-ish: it dry-runs first and refuses if a patch does not apply cleanly
# (e.g. already applied). AOSP is multi-repo, so we apply with plain `patch`
# from the tree root rather than git.
set -euo pipefail
AOSP_ROOT="${1:?usage: apply-patches.sh <AOSP_ROOT> <REPO_ROOT>}"
REPO_ROOT="${2:?usage: apply-patches.sh <AOSP_ROOT> <REPO_ROOT>}"
cd "$AOSP_ROOT"
for p in \
  "$REPO_ROOT/patches/0001-system-core-fastbootd-runtime-fetch-policy.patch" \
  "$REPO_ROOT/patches/0002-bootable-recovery-fastbootd-fetch-menu.patch"; do
  echo ">> Checking $(basename "$p")"
  if ! patch -p1 --dry-run --silent < "$p"; then
    echo "!! $(basename "$p") does not apply cleanly here."
    echo "   It may already be applied, or your tree differs from the base"
    echo "   (system/core @ AOSP main; bootable/recovery @ GrapheneOS-17)."
    echo "   Review and apply by hand, then re-run the build."
    exit 1
  fi
done
for p in \
  "$REPO_ROOT/patches/0001-system-core-fastbootd-runtime-fetch-policy.patch" \
  "$REPO_ROOT/patches/0002-bootable-recovery-fastbootd-fetch-menu.patch"; do
  echo ">> Applying $(basename "$p")"
  patch -p1 < "$p"
done
echo ">> Patches applied."
