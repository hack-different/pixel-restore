#!/usr/bin/env bash
# Hash partitions ON THE PHONE with the read-enabled fastbootd and print the
# digests, without transferring the partitions. Read only.
#
#   ./scripts/hash-partitions.sh [--serial S] [--algo sha256|sha1|sha512]
#                                [--out FILE] [--check FILE] [partition ...]
#
# Default partitions: devinfo efs efs_backup modem_userdata persist. Slotted
# partitions need their suffix (boot_a, vendor_boot_b, ...).
#
# Output is in sha256sum format ("<digest>  <partition>"), so a backup can be
# compared with `sha256sum efs.img`. --out also writes it to FILE. --check
# compares against such a file (lines for other partitions are ignored) and
# exits non-zero on any mismatch.
#
# Needs the phone in the read-enabled fastbootd (./docker/build.sh boot) with
# "Enable reading" picked on its screen. Each partition is one
# `fastboot oem <algo> <partition>`; the phone reads the whole partition and
# answers with "<algo> <hex digest> <partition> <size>".
set -euo pipefail

serial="" algo=sha256 out="" check="" parts=()
while [ $# -gt 0 ]; do
  case "$1" in
    --serial|-s) serial="${2:?}"; shift 2 ;;
    --algo) algo="${2:?}"; shift 2 ;;
    --out) out="${2:?}"; shift 2 ;;
    --check) check="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "!! unknown option $1 (see --help)" >&2; exit 2 ;;
    *) parts+=("$1"); shift ;;
  esac
done
case "$algo" in sha256|sha1|sha512) ;; *) echo "!! --algo must be sha256, sha1 or sha512" >&2; exit 2 ;; esac
[ ${#parts[@]} -gt 0 ] || parts=(devinfo efs efs_backup modem_userdata persist)
[ -z "$check" ] || [ -f "$check" ] || { echo "!! no file $check" >&2; exit 2; }

die() { echo "!! $*" >&2; exit 1; }
command -v fastboot >/dev/null || die "fastboot is not on PATH"
if [ -z "$serial" ]; then
  devices=$(fastboot devices 2>/dev/null | awk 'NF {print $1}')
  count=$(printf '%s' "$devices" | grep -c . || true)
  [ "$count" -eq 1 ] || die "$count phones in fastboot; pick one with --serial"
  serial="$devices"
fi
[ "$(fastboot -s "$serial" getvar is-userspace 2>&1 | sed -n 's/^is-userspace: *//p')" = yes ] \
  || die "$serial is not in fastbootd. Boot the read-enabled one with ./docker/build.sh boot."

[ -z "$out" ] || : > "$out"
failed=0 mismatched=0
for part in "${parts[@]}"; do
  reply=$(fastboot -s "$serial" oem "$algo" "$part" 2>&1 || true)
  line=$(printf '%s\n' "$reply" | sed -n "s/^(bootloader) \($algo [0-9a-f]* [^ ]* [0-9]*\)\$/\1/p" | tail -1)
  if [ -z "$line" ]; then
    why=$(printf '%s\n' "$reply" | sed -n 's/.*FAILED (remote: .\(.*\).)$/\1/p' | head -1)
    echo "!! $part: ${why:-$(printf '%s' "$reply" | tr '\n' ' ')}" >&2
    case "$why" in
      *"until reading is enabled"*) echo "   Pick \"Enable reading\" on the phone's fastbootd menu, then retry." >&2 ;;
      *"Unable to do OEM command"*|*"Unrecognized"*) echo "   This fastbootd has no hashing; is it the read-enabled one?" >&2 ;;
    esac
    failed=$((failed + 1))
    continue
  fi
  read -r _ digest _ size <<< "$line"
  printf '%s  %s\n' "$digest" "$part"
  [ -z "$out" ] || printf '%s  %s\n' "$digest" "$part" >> "$out"
  echo "   ($size bytes)" >&2
  if [ -n "$check" ]; then
    want=$(awk -v p="$part" '{n=$2; sub(/^\*/, "", n); sub(/.*\//, "", n); sub(/\.img$/, "", n)} n == p {print $1; exit}' "$check")
    if [ -z "$want" ]; then
      echo "   $part: not in $check" >&2
    elif [ "$want" = "$digest" ]; then
      echo "   $part: matches $check" >&2
    else
      echo "!! $part: DIFFERS from $check ($want)" >&2
      mismatched=$((mismatched + 1))
    fi
  fi
done

[ "$failed" = 0 ] || echo "!! $failed partition(s) could not be hashed" >&2
[ "$mismatched" = 0 ] || echo "!! $mismatched partition(s) differ from $check" >&2
[ "$failed" = 0 ] && [ "$mismatched" = 0 ]
