#!/usr/bin/env bash
# Boot the built, read-enabled fastbootd on the phone FROM RAM. Run on the host
# (the Mac), with the phone in the BOOTLOADER (fastboot mode, not fastbootd).
#
#   ./scripts/boot-fastbootd.sh [--serial SERIAL] [--image IMG] [--yes] [--wait SECONDS]
#   ./docker/build.sh boot [same options]     # also assembles the image if needed
#
# The only command sent that changes anything is `fastboot boot IMG`: the
# bootloader loads the image into RAM and starts it. Nothing is flashed or
# written to any partition, and a reboot returns the phone to what is flashed.
# Every other command is a read-only `getvar`, plus one `fetch` probe that a
# freshly booted fastbootd refuses (it proves which fastbootd answered).
#
# IMG defaults to out/<device>/fastbootd-boot.img (scripts/make_ram_boot_image.py,
# run by docker/build.sh): the built kernel plus every built ramdisk, so the
# patched fastbootd in vendor_boot's ramdisk replaces the phone's copy.
#
# Not yet confirmed on komodo: that the bootloader starts a `fastboot boot`
# image in recovery mode (where fastbootd runs) rather than a normal Android
# boot. The script watches what comes up and says which happened.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
serial="" image="" yes=0 wait_s=180
while [ $# -gt 0 ]; do
  case "$1" in
    --serial|-s) serial="${2:?}"; shift 2 ;;
    --image) image="${2:?}"; shift 2 ;;
    --yes|-y) yes=1; shift ;;
    --wait) wait_s="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "!! unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done

die() { echo "!! $*" >&2; exit 1; }
command -v fastboot >/dev/null || die "fastboot is not on PATH (brew install android-platform-tools)"

# Which phone. (Plain loops: macOS ships bash 3.2.)
listed() { fastboot devices 2>/dev/null | awk 'NF {print $1}'; }
present() { listed | grep -qx "$1"; }
devices=$(listed)
count=$(printf '%s' "$devices" | grep -c . || true)
if [ -z "$serial" ]; then
  [ "$count" -gt 0 ] || die "no phone in fastboot mode. Hold Power + Volume Down to reach the bootloader."
  [ "$count" -eq 1 ] || die "$count phones in fastboot ($(printf "%s" "$devices" | tr "\n" " ")); pick one with --serial"
  serial="$devices"
else
  present "$serial" || die "$serial is not in fastboot mode"
fi
fb() { fastboot -s "$serial" "$@"; }
getvar() { fb getvar "$1" 2>&1 | sed -n "s/^$1: *//p" | head -1; }

# What it is and what state it is in (all read only).
product=$(getvar product)
unlocked=$(getvar unlocked)
userspace=$(getvar is-userspace)
slot=$(getvar current-slot)
bootloader=$(getvar version-bootloader)
maxdl=$(getvar max-download-size)
echo ">> $serial: product ${product:-?}, bootloader ${bootloader:-?}, slot ${slot:-?}, unlocked ${unlocked:-?}"

if [ "$userspace" = yes ]; then
  die "the phone is in fastbootd (userspace), but \`fastboot boot\` is a bootloader command.
   Run \`fastboot -s $serial reboot bootloader\`, then run this again."
fi
[ "$unlocked" = yes ] || die "the bootloader is not unlocked (unlocked: ${unlocked:-?}).
   The bootloader refuses to boot an unsigned image, and fastbootd refuses to
   read partitions, on a locked phone. This tool does not unlock it: unlocking
   wipes user data and is your decision."

device="${product:-komodo}"
[ -n "$image" ] || image="$root/out/$device/fastbootd-boot.img"
[ -f "$image" ] || die "no image at $image. Build it with ./docker/build.sh (or ./docker/build.sh boot)."
built_for=$(cat "$(dirname "$image")/lunch-target.txt" 2>/dev/null || true)
if [ -n "$built_for" ] && [ -n "$product" ] && [ "${built_for%%-*}" != "$product" ]; then
  die "$image was built for ${built_for%%-*}, but this phone is $product"
fi
size=$(wc -c < "$image" | tr -d ' ')
if [ -n "$maxdl" ] && [ "$size" -gt $((maxdl)) ]; then
  die "$image is $size bytes; the bootloader accepts at most $((maxdl))"
fi

echo ">> Will run: fastboot -s $serial boot $image"
echo "   ($size bytes, ${built_for:-unknown target}). RAM only: nothing is flashed,"
echo "   and rebooting the phone returns it to what is installed."
if [ "$yes" != 1 ]; then
  read -r -p "   Boot it? [type yes] " answer
  [ "$answer" = yes ] || die "not confirmed; nothing sent"
fi

fb boot "$image"

# The bootloader drops off USB when it starts the image; then wait for the
# image to come back as fastbootd (or as something else, and say what).
echo ">> Waiting for the bootloader to start the image"
start=$SECONDS
while present "$serial"; do
  [ $((SECONDS - start)) -lt 30 ] || die "the phone stayed in the bootloader; it did not start the image.
   Nothing was written. The bootloader screen may say why."
  sleep 1
done

echo ">> Waiting up to ${wait_s}s for fastbootd"
start=$SECONDS hinted=0 reached=0
while [ $((SECONDS - start)) -lt "$wait_s" ]; do
  sleep 2
  if present "$serial"; then
    sleep 2   # let it settle before asking
    if [ "$(getvar is-userspace)" = yes ]; then
      reached=1
      break
    fi
    die "the phone came back in the bootloader, so the image did not stay up
   (it may have crashed and reset). Nothing was written."
  fi
  if command -v adb >/dev/null && adb devices 2>/dev/null | awk '{print $1}' | grep -qx "$serial"; then
    die "the phone booted Android normally, not into fastbootd: komodo's bootloader
   starts a \`fastboot boot\` image as a normal boot, so the patched fastbootd
   cannot be reached this way. Nothing was written; \`adb reboot bootloader\`
   returns to the bootloader. The remaining route is flashing vendor_boot,
   which writes to the phone; that is your decision and this tool will not do it."
  fi
  if [ "$hinted" = 0 ] && [ $((SECONDS - start)) -ge 45 ]; then
    hinted=1
    echo "   Still waiting. If the phone shows the recovery menu, pick \"Enter fastboot\"."
  fi
done
if [ "$reached" != 1 ]; then
  die "the phone did not reach fastbootd within ${wait_s}s. Check its screen:
   - recovery menu: pick \"Enter fastboot\"; the phone then shows the fastbootd
     menu, and \`fastboot getvar is-userspace\` answers yes
   - stuck or blank: hold Power + Volume Down to return to the bootloader.
   Nothing was written either way."
fi

# Which fastbootd answered. A fresh boot has reading disabled, so this fetch is
# refused before anything is read; the refusal text identifies the build.
probe=$(mktemp)
reply=$(fb fetch devinfo "$probe" 2>&1 || true)
rm -f "$probe"
case "$reply" in
  *"until reading is enabled from the on-screen fastboot menu"*)
    echo ">> The read-enabled fastbootd is running on $serial." ;;
  *"only allowed on"*|*"not allowed on user build"*)
    die "the phone is in fastbootd, but it is the STOCK one (it answered: ${reply//$'\n'/ }).
   The built ramdisk did not replace the phone's. Nothing was written." ;;
  *)
    echo "   warning: could not identify this fastbootd; fetch said: ${reply//$'\n'/ }" ;;
esac

cat <<EOF

Next, on the phone's screen: scroll to "Enable reading (fetch any partition)"
(or "Enable all partitions" to read super/userdata whole), read the warning and
pick "Yes, enable reading". Then on the Mac:

  ./scripts/read-partitions.sh --serial $serial ~/pixel-partitions

or hash them on the phone without transferring them:

  ./scripts/hash-partitions.sh --serial $serial

When you are done, reboot the phone; the read-enabled fastbootd is gone.
EOF
