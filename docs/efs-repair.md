# Repairing a blanked modem `efs`

`tools/pixel_efs.py` reads a Tensor Pixel's modem partitions, says which of
them are blank or damaged, prepares a reviewed plan of what to write back, and
flashes it from the bootloader only on an explicit `--apply`. It complements
`docs/modem-repair.md` (IMEI diagnosis, devinfo correction, radio reflash) and
`pixel-restore repair-modem --restore` (flashing a known-good backup by hand).

## What `efs` is and what blanking it does

The Shannon modem keeps its data outside the firmware image, in four small
partitions that factory images and OTAs never write:

| partition | mount | holds |
| --- | --- | --- |
| `efs` | `/mnt/vendor/efs` | the modem's NV data: calibration, settings and identity-related items, as files such as `nv_normal.bin`, `nv_normal.bin.md5` and `nv_protected.bin` |
| `efs_backup` | `/mnt/vendor/efs_backup` | a copy of `efs`, rewritten from `efs` by the modem (`AT+GOOGBACKUPNV`) |
| `modem_userdata` | `/mnt/vendor/modem_userdata` | modem run-time data (contents undocumented) |
| `persist` | `/mnt/vendor/persist` | per-device data for several subsystems, including `modem/cpsha`, the hash the modem's IMEI check compares against |

Their fstab lines carry `wait,check,formattable`: ext4 on gs101 (Pixel 6,
`device/google/gs101/conf/fstab.gs101`) and f2fs from zuma on (Pixel 8,
`device/google/zuma/conf/fstab.efs`; Pixel 9's `fstab.efs` is not published,
but its `init.efs.rc` mounts `/vendor/etc/fstab.efs` the same way). `formattable` means
`fs_mgr_mount_all` formats the partition itself when the mount fails and it
finds the partition wiped (`system/core/fs_mgr/fs_mgr.cpp`). So a zero-filled
`efs` does not stop Android: it comes up as an empty filesystem, and the modem
recreates its NV files from built-in defaults. What those defaults lack is this
phone's calibration and provisioning, which is why "blank efs" shows up as a
missing or zero IMEI, no baseband, or no service rather than as a boot failure.

The reviewed upstream notes agree with that picture (`pixel-restore modem
sources`; bitdomo/restore_imei README, tested on a Pixel 9 Pro XL):

- deleting `nv_normal.bin` and friends from `/mnt/vendor/efs` has no lasting
  effect: they are regenerated on the next boot;
- emptying all of `/mnt/vendor/efs` lost the IMEI until a factory reset, after
  which it was back (the author assumes the reset restores `efs` from
  `efs_backup`);
- emptying both `efs` and `efs_backup` left the IMEI in place (it is also kept
  in `devinfo`, see `docs/modem-repair.md`) but all connectivity gone, with only
  a manual 2G attach working: the calibration was lost for good.

Two consequences drive the tool's design. The surviving mirror is the best
source for a blanked copy, and a phone that has lost both copies with no
backup cannot be recalibrated by anything in this repository; it can only be
given empty filesystems so that it boots and keeps whatever identity `devinfo`
still carries.

## Reading the partitions

```sh
python3 tools/pixel_efs.py fetch --out .pixel-restore/efs/before      # fastboot fetch (fastbootd)
python3 tools/pixel_efs.py fetch --out .pixel-restore/efs/before --adb # rooted adb shell instead
```

`fetch` reads `efs`, `efs_backup`, `modem_userdata`, `persist` and `devinfo`
into a new private directory (mode 0700) with `report.json` naming the phone
(serial, product, unlock state) and the partition sizes the bootloader
reports. Each read is checked against that size. The stock bootloader refuses
`fastboot fetch` for these partitions, so this needs either the read-enabled
fastbootd built in `fastbootd-read/` (reading on, from its menu) or an Android
with root. Nothing is written to the phone. `--serial` is only needed with
more than one phone attached.

The images hold the phone's modem identity and calibration: keep them out of
version control and off shared drives.

## Inspecting them

```sh
python3 tools/pixel_efs.py inspect .pixel-restore/efs/before
```

Each image is classified as:

- **blank**: every byte 0x00 (`zero`) or 0xFF (`erased`), or nothing read;
- **ext4**: superblock parsed, the directory tree walked with the read-only
  ext4 reader in `tools/android_fs.py`; files are listed and the known modem
  NV file names noted. A filesystem larger than its image, or a tree that
  does not walk, is **damaged**; one with no files is **empty**;
- **f2fs**: both superblock copies parsed (version, block count, label, UUID)
  and compared; contents are not listed (there is no f2fs reader here);
- **unknown**: anything else, reported with how many distinct byte values the
  first MiB has (random-looking data is usually an encrypted or scrambled
  partition, not a wiped one).

`wiped_for_fs_mgr` says whether the first 4 KiB are zero, which is what makes
Android format a `formattable` partition at boot. The cross-checks compare
`efs` with `efs_backup`: same size, byte-identical, which files only one of
them has, same-name files whose sizes differ, which copy ext4 says was written
last, and which NV files are missing. `--json` and `--out` give the report as
JSON; on a pipe JSON is the default.

## Deciding what to write

```sh
python3 tools/pixel_efs.py plan --images .pixel-restore/efs/before --out .pixel-restore/efs/plan
python3 tools/pixel_efs.py plan --images ... --out ... --backup .pixel-restore/backups/before-repair
python3 tools/pixel_efs.py plan --images ... --out ... --prefer efs_backup
python3 tools/pixel_efs.py plan --images ... --out ... --allow-empty
```

For each of the four modem partitions, in this order of preference:

1. **healthy**: kept. When both `efs` and `efs_backup` are healthy but differ,
   both are kept too; `--prefer efs|efs_backup` overwrites the other copy with
   the one named (look at `last_written` and the file lists first).
2. **mirror**: a blank, damaged or empty `efs` is rewritten from a healthy
   `efs_backup` of the same size, and the other way round.
3. **backup**: a folder from `pixel-restore modem backup` (its `manifest.json`
   is verified and must name this phone's serial), or any folder of
   `<partition>.img` files with `--backup-is-this-phone`, supplies a healthy
   copy of the same size. A backup from another phone is refused: its `efs`
   would give this one the wrong identity and calibration.
4. **empty**, only with `--allow-empty` and only for `efs`, `efs_backup` and
   `modem_userdata`: a zero image the size of the partition, which Android
   formats at the next boot. `persist` is never emptied because its fstab
   line is not public and it holds more than modem data. This gets a phone
   with no surviving copy to boot and the modem to defaults; the calibration
   is not recovered and the plan says so.

Anything with no source is listed as **unresolved** and left alone; an empty
filesystem with no source is kept, since it mounts as it is. The plan folder
holds `plan.json` (the phone's serial and product, every decision with its
reason, the partition sizes, the hash of each image to write) and a copy of
each image to flash, so what is reviewed is exactly what is written.

## Flashing the plan

```sh
python3 tools/pixel_efs.py apply --plan .pixel-restore/efs/plan           # dry run: shows the writes
python3 tools/pixel_efs.py apply --plan .pixel-restore/efs/plan --apply   # from the bootloader
```

Before writing, `apply` re-verifies every image against the plan, then checks
that the phone in fastboot has the plan's serial and product, that the
bootloader is unlocked (a locked one refuses to flash `efs`, and this tool
does not unlock, which wipes data), that it is the bootloader rather than
fastbootd (`--allow-fastbootd` overrides), and that each partition is the size
of its image. It then asks for the serial to be typed (`--yes` skips that)
and runs `fastboot flash <partition> <image>` for each action, nothing else.
It never reboots on its own. Fastboot cannot read the partitions back; a
second `fetch` through fastbootd or rooted adb is how to verify the result.

After `fastboot reboot`, let Android come up (it formats any zero image on
the way), then check the IMEI with `*#06#` and run `pixel-restore modem
diagnose`. If the IMEI is right but check-in or service still fails, the next
places to look are the radio build (`repair-modem` / `reflash-radio`) and
`devinfo` (`docs/modem-repair.md`), not another `efs` write.

## For the Pixel 9 Pro XL (komodo) in this project

The phone hangs at the boot animation after the stock flash and shows no IMEI,
and there is no earlier backup of its `efs`. The order is: boot the
read-enabled fastbootd and `fetch`; if `efs` is blank and `efs_backup` is a
filesystem with NV files in it, the mirror plan is the whole repair; if both
are blank, `--allow-empty` gets it booting with the modem at defaults, and
whatever identity `devinfo` still holds is what it will show. The boot hang
itself is not explained by a blank `efs` (Android formats one), so a `fetch`
that finds both copies healthy points the investigation elsewhere, for example
`docs/pixel-boot-failure-map.md`.

## Outside this tool

Writing IMEI or other NV items into the modem (`AT+GOOGSETNV`, factory
bootmode, `cpsha` rewriting) is deliberately not here; see the review findings
in `docs/modem-repair.md`. Building an `efs` from scratch with the right
contents is not possible without the phone's own calibration, and no public
source documents the NV file formats.

## Tests

```sh
python3 -m unittest tools/test_pixel_efs.py
```

The tests build ext4 images with `mke2fs`/`debugfs` when installed (otherwise
those cases are skipped), synthesise f2fs superblocks and blank images, and
drive `fetch`, `plan` and `apply` against a fake `fastboot` script. No phone,
adb or network is used.
