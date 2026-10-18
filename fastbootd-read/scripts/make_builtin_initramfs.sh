#!/usr/bin/env bash
# Assemble the root filesystem for a kernel with a BUILT-IN initramfs, so one
# `fastboot boot` image carries its own recovery + read-enabled fastbootd.
#
#   make_builtin_initramfs.sh <vendor_boot.img> <vendor_kernel_boot.img> <init_boot.img> <out.cpio>
#
# Why: on Pixel 7 and later the bootloader drops the boot image's ramdisk and
# builds one from the phone's init_boot + vendor_boot + vendor_kernel_boot, so
# a RAM boot never runs our userspace. A kernel built with
# CONFIG_INITRAMFS_SOURCE=<out.cpio> and CONFIG_INITRAMFS_FORCE=y unpacks its
# own root and ignores the bootloader's ramdisk (bootconfig still arrives).
#
# Layers, later ones win, the same order the bootloader would use:
#   1. vendor_boot ramdisk    the built recovery root (recovery, fastbootd, rc)
#   2. vendor_kernel_boot     /lib/modules from the SAME kernel build as the Image
#   3. init_boot ramdisk      the generic ramdisk: first-stage /init
#   4. /force_recovery        marker read by patch 0003: ignore force_normal_boot
#   5. recovery --fastboot    in system/etc/init/hw/init.rc: start in fastbootd
#
# Runs in the build container (needs python3, lz4, cpio and AOSP's
# unpack_bootimg.py, default /aosp/system/tools/mkbootimg). Writes only <out.cpio>.
set -euo pipefail

vb="${1:?usage: $0 <vendor_boot.img> <vendor_kernel_boot.img> <init_boot.img> <out.cpio>}"
vkb="${2:?}" ib="${3:?}" out="${4:?}"
mkb="${MKBOOTIMG_DIR:-/aosp/system/tools/mkbootimg}"
die() { echo "!! $*" >&2; exit 1; }
for t in python3 lz4 cpio; do command -v "$t" >/dev/null || die "$t is not installed"; done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
root="$tmp/root"
mkdir -p "$root"

# Unpack every ramdisk of an image into $root (fragments in table order).
layer() {
  local img="$1" name="$2" dir="$tmp/$2" n=0
  python3 "$mkb/unpack_bootimg.py" --boot_img "$img" --out "$dir" >/dev/null
  for rd in "$dir"/vendor_ramdisk[0-9]* "$dir"/ramdisk; do
    [ -s "$rd" ] || continue
    (cd "$root" && { lz4 -dc "$rd" 2>/dev/null || gzip -dc "$rd" 2>/dev/null || cat "$rd"; } \
      | cpio -idmu --quiet --no-absolute-filenames)
    n=$((n + 1))
  done
  [ "$n" -gt 0 ] || die "$img has no ramdisk"
  echo ">> layer $name: $n ramdisk(s) from $(basename "$img")"
}

layer "$vb" vendor_boot
layer "$vkb" vendor_kernel_boot
layer "$ib" init_boot

[ -e "$root/system/bin/fastbootd" ] || die "no system/bin/fastbootd: is $vb the fastbootd-read build?"
[ -e "$root/system/bin/recovery" ] || die "no system/bin/recovery in $vb"
[ -e "$root/init" ] || die "no /init after the init_boot layer"
nmod=$(find "$root/lib/modules" -name '*.ko' 2>/dev/null | wc -l)
[ "$nmod" -gt 0 ] || die "no kernel modules under /lib/modules from $vkb"

: > "$root/force_recovery"
rc="$root/system/etc/init/hw/init.rc"
sed -i 's#^service recovery /system/bin/recovery$#service recovery /system/bin/recovery --fastboot#' "$rc"
grep -q '^service recovery /system/bin/recovery --fastboot$' "$rc" \
  || die "could not make recovery start with --fastboot in $rc"

(cd "$root" && find . -mindepth 1 | LC_ALL=C sort | cpio -o -H newc -R 0:0 --quiet) > "$out"
echo ">> Wrote $out ($(stat -c %s "$out") bytes, $(find "$root" -mindepth 1 | wc -l) entries," \
  "$nmod modules, for kernel $(ls "$root/lib/modules" | grep -v '^modules' | head -1))"
