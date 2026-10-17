#!/usr/bin/env bash
# Assemble a candidate, fastboot-bootable image carrying the read-enabled
# fastbootd/recovery, and VERIFY the patch actually made it into the binary.
#
#   OUT=/path/to/aosp/out/target/product/komodo ./scripts/make-boot-image.sh
#
# This NEVER touches the phone and NEVER flashes. It only produces a local
# image file and checks it. Whether komodo's bootloader will enter fastbootd
# from `fastboot boot` of this image must be confirmed on-device; see
# docs/build-and-boot.md. If it will not, deciding whether to flash is the
# owner's call and is deliberately not automated here.
set -euo pipefail
OUT="${OUT:?Set OUT to \$OUT from the build, e.g. .../out/target/product/komodo}"
work="${WORK:-$PWD/fastbootd-read-image}"
mkdir -p "$work"

vboot="$OUT/vendor_boot.img"
boot="$OUT/boot.img"
[ -f "$vboot" ] || { echo "!! missing $vboot"; exit 1; }
[ -f "$boot" ]  || { echo "!! missing $boot"; exit 1; }

# unpack_bootimg / mkbootimg ship in the tree and in $OUT host tools.
UNPACK="$(command -v unpack_bootimg || echo "$OUT/../../../host/linux-x86/bin/unpack_bootimg")"
MKBOOT="$(command -v mkbootimg    || echo "$OUT/../../../host/linux-x86/bin/mkbootimg")"

echo ">> Unpacking vendor_boot to inspect the recovery/fastbootd ramdisk"
rm -rf "$work/vboot"; mkdir -p "$work/vboot"
"$UNPACK" --boot_img "$vboot" --out "$work/vboot" >/dev/null

echo ">> Verifying the read-enable patch is present in the built fastbootd"
# The daemon binary lands in the recovery ramdisk as system/bin/fastbootd.
# Our patch adds this exact user-facing string; grep the built artifacts for it.
needle='until reading is enabled from the on-screen fastboot menu'
if grep -aqR "$needle" "$OUT/recovery" "$OUT/system/bin/fastbootd" "$work/vboot" 2>/dev/null; then
  echo "   OK: read-enabled fastbootd found."
else
  echo "!! Could not confirm the patched string in the build output."
  echo "   Re-run the build (scripts/apply-patches.sh + m fastbootd recovery) and retry."
  exit 1
fi

echo
echo ">> Build artifacts are ready under: $OUT"
echo "   boot.img:        $boot"
echo "   vendor_boot.img: $vboot"
echo
echo ">> Next, on the phone (unlocked bootloader), enter fastbootd and boot it"
echo "   TEMPORARILY. See docs/build-and-boot.md for the exact, validated-on-"
echo "   device sequence. No step in this repo writes to the phone."
