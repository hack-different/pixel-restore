#!/usr/bin/env bash
# Convenience wrapper: once the read-enabled fastbootd is running on the phone
# AND you have enabled reading from its on-screen menu, pull the small
# partitions with the existing pixel-restore tool. Read only.
#
#   ./scripts/read-partitions.sh [--serial 47191FDAS0056M] [out_dir]
#
# Requires: the phone in (modified) fastbootd, host `fastboot` on PATH, and a
# built `pixel-restore` (cargo build --release) from the modem tool checkout.
set -euo pipefail
OUTDIR="${*: -1}"; case "$OUTDIR" in -*|"") OUTDIR="$HOME/pixel-partitions";; esac
echo ">> Confirm the phone shows the modified fastbootd and you selected"
echo "   'Enable reading' (or 'Enable all partitions') on its screen."
echo ">> fastboot devices:"; fastboot devices || true
echo ">> Reading partitions with pixel-restore (no writes) into $OUTDIR"
# The IMEI thread owns tools/pixel_modem.py; this only calls the existing CLI.
pixel-restore modem read-partitions --fastboot --out "$OUTDIR" "$@" || {
  echo "!! read-partitions failed. If you see 'Fetch is only allowed on [...]',"
  echo "   reading is not enabled yet: pick it from the fastbootd menu and retry."
  exit 1
}
echo ">> Done. See $OUTDIR/report.json"
