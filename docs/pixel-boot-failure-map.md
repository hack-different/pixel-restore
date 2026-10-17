# Where a Pixel goes unbootable, and how to detect it

A map of the places on a Tensor Pixel (Pixel 6 and newer) where a mismatch,
a rollback, or corruption can stop the phone booting, with what in this repo
can detect each one. It is a companion to `docs/titan-key-verification.md` and
`docs/modem-repair.md`.

Scope and honesty: the boot order and several of these interactions are
documented by Google only in part. Items marked (inferred) are reasoned from
the Cr50/AVB sources and this project's own recovery work, not from a Google
spec. Everything here is read-only to diagnose; nothing recovers a phone by
itself.

The severity that matters most: some partitions are **device-unique** (their
contents exist nowhere else), so corrupting them without a backup is
unrecoverable. The tool knows this set as `DEVICE_UNIQUE` in
`tools/pixel_modem.py`: devinfo, efs, efs_backup, persist, modem_userdata,
frp, misc, metadata, fips, dram_train, klog, mfg_data, userdata. Everything
else can be rewritten from a factory image.

## 1. AP bootloader anti-rollback (the classic brick)

The Tensor boot chain is BL1 -> PBL -> LDFW -> ABL (and the GSA/BL2 stages
between). Each stage carries an anti-rollback version. When a bootloader with
a **higher** rollback version has run, the phone will no longer accept a
bootloader below that floor, and a phone flashed below the floor drops into
"Pixel ROM Recovery" (Exynos USB boot) and will not reach fastboot.

- Interaction that bricks: flashing or falling back to a bootloader below the
  rollback floor; crossing a rollback *bump* is one-way and permanent.
- Detect: `getvar` the real counters `ap-ar-ns`, `ap-ar-s`, `gsa-bl1-ar`,
  `gsa-fw-ar`; `arb::counters_verdict` compares a candidate build to them and
  returns Safe/Unsafe. `arb::BUMPS` records the Google-announced bump months
  per model, and `arb::verdict` / `lowest_safe` pick the lowest build that is
  still safe. `restore` refuses a build below the floor unless
  `--cross-rollback-bump`.
- Note: a phone still below a bump may be the only kind whose ROM accepts the
  USB-bootable recovery packs, so crossing one also closes that escape hatch.

## 2. Slot A/B inconsistency

Two copies of the boot chain exist (slot A and B). The inactive slot matters:
if it holds a bootloader below the rollback floor and the phone ever falls
back to it, it bricks the same way as (1).

- Interaction: flashing only the active slot, or an interrupted update leaving
  the slots on different versions; retry-count exhaustion marking a slot
  unbootable.
- Detect: `getvar` `slot-successful`, `slot-unbootable`, `slot-retry-count`
  (parsed into per-slot objects by `structure_getvar` in `pixel_modem.py`).
- Mitigation already in code: `restore::install` flashes bootloader and radio
  to the *other* slot last, so neither slot is left below the level.

## 3. Titan M (GSC) firmware

Covered in depth in `docs/titan-key-verification.md`. Summary of the brick/
detect surface:

- RW rollback level below the chip's: the chip will not boot that RW. Detect:
  `titan --inspect` / the dry-run header compare; infomap cleared-bit count.
- Signing-key mismatch: an RW signed with a key the chip does not accept
  leaves that RW slot unbootable until rescued again. Detect: `titan
  --verify-keys`.
- A wedged or mis-rescued Titan can, via the root-of-trust handshake, stall AP
  boot (inferred; the halt-on-error decision is in closed ABL/LDFW). Detect:
  `oem gsc version` state fields, `titan` before/after reports.

## 4. Root-of-trust handshake (bootloader <-> Titan)

At boot the bootloader sends the verified-boot result to the Titan
(`SetRootOfTrust`, `SetBootState`). If the Titan is unresponsive or the
exchange fails, the bootloader may refuse to continue (inferred). This is why
repairing the Titan can restore booting even though the Titan does not verify
AP code. No direct fastboot probe; infer from the Titan being healthy
(section 3) plus a phone that halts early.

## 5. AVB / vbmeta (verified boot of the OS)

ABL verifies vbmeta and the OS against the root-of-trust key (built-in, or an
`avb_custom_key` on an unlocked phone). A locked phone whose OS is not signed
by the trusted key will not boot (red state).

- Interactions that brick a *locked* phone: OS/vbmeta not signed by the
  device's key; a custom AVB key set that does not match the flashed OS; a
  vbmeta rollback index below what the phone recorded; `vbmeta` flashed with
  verification disabled flags onto a locked device.
- Detect: `pixel_modem.py` `avb_header` parses the libavb vbmeta header
  (magic, versions, rollback index, flags) and `read-partitions` reports which
  partitions parse as vbmeta. Compare the key digest the way
  `titan-key-verification.md` describes.
- Caveat: changing the AVB key or lock state wipes userdata; it is not a
  recovery step.

## 6. Radio / modem (boots, but no service, blank IMEI)

A radio image from a different build than the bootloader is the usual cause of
a missing baseband or blank IMEI. The phone boots, but has no cellular.

- Detect: `getvar version-baseband` vs the bootloader build; `modem diagnose`;
  IMEI in devinfo via `devinfo_imeis` (Luhn-checked). `repair-modem` warns when
  the radio build does not match the bootloader.
- Device-unique risk: the IMEI and calibration live in devinfo/efs/persist,
  which no factory image restores. Back them up before touching them
  (`modem backup`, `modem read-partitions`).

## 7. Partition corruption, generally

- FBPK bootloader/radio packs: each entry carries a CRC32.
  `fbpk::Pack::verify_crc` (and `pixel-restore unpack`) flags a corrupt entry
  before it is ever flashed.
- ext4/filesystem partitions: `pixel_modem.py` `ext4_info` reads the
  superblock (block count, UUID) to tell a real filesystem from garbage.
- Partition size/shape: `structure_getvar` surfaces `partition-size`,
  `partition-type`, `is-logical`, so a zero-size or missing partition stands
  out.
- misc / bootloader-message: a corrupt `misc` partition can trap the phone in
  recovery or bootloader; it is device-unique only in that it holds transient
  boot state, and is safe to clear to the standard empty form.

## Quick triage order

1. Does it reach fastboot at all? No -> suspect (1)/(2) AP rollback, or (3)/(4)
   Titan. Yes -> continue.
2. `getvar`-all: check rollback counters (1), slot flags (2), partition sizes
   (7).
3. `titan --verify-keys` and `--inspect` (3).
4. Boots to a logo/recovery but no OS: AVB/vbmeta (5).
5. Boots to OS but no cellular: radio/IMEI (6).

Back up every device-unique partition (section intro) before any write.
