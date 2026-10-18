# Modem and Google sign-in repair tools

These tools complement ROM recovery: they work with an Android-booted Pixel
reachable through ADB. They do not use EUB or a USB-bootable BL1.

## Commands

Build with `cargo build --release`, then use `pixel-restore modem --help`.
The Python helper is embedded in the Rust binary, so an installed binary does
not need the source tree. Python 3.9+ is required; set `PIXEL_RESTORE_PYTHON`
if the interpreter is not called `python3`. The helper also works directly:

```sh
python3 tools/pixel_modem.py --help
pixel-restore modem sources
pixel-restore modem diagnose
```

Every device command talks to one phone by serial. `--serial` is optional:
with exactly one phone attached (in adb, or in fastboot for the fastboot
commands) that phone is used, and with several the command stops and asks for
`--serial` rather than picking one. `diagnose` reads build, baseband and
bootloader properties and classifies recent Google Play services log messages.
It does not request root or save raw logs/accounts/tokens. Its optional `--out`
JSON includes the device serial and fingerprint. Logs can be historical: retry
sign-in immediately before diagnosis to associate the findings with that attempt.

With the phone in fastboot instead (it is the only place `diagnose` finds it,
or `--fastboot` is given), it reports what the bootloader exposes: product,
bootloader and baseband versions, unlock state, slot, hardware revision and
battery, with findings for a missing baseband, a locked bootloader and an
unsupported model. Sign-in logs need Android, so that part is skipped.
It reads `fastboot getvar all`, the read-only `fastboot oem get_config all`,
and the Titan M queries `fastboot oem gsc version`, `fastboot oem gsc header`
for `ro_a`, `ro_b`, `rw_a` and `rw_b`, and `fastboot oem gsc avb state`. Their
answers land under the `gsc` (Titan M) variables as `firmware_version`,
`firmware_headers` and `avb_state`: parsed into fields when the bootloader
prints `name: value` lines, otherwise kept as raw lines.
Partition variables become one object per partition (per-slot sizes
under `slots`), slot variables become slot objects, and only variables shown
nowhere else are listed under `variables`. Modem partitions (`modem`, `efs`,
`efs_backup`, `persist`, `devinfo` ...) and modem variables appear only in the
`modem` section. Findings add missing or zero-sized modem partitions and
whether the active slot has a modem. The bootloader exposes no IMEI, EFS
contents or modem logs; those need Android (and root for EFS).

On a terminal the report prints as a tree with friendly names (`devinfo` is
"Manufacturer Device Info", `efs` "Encrypted File Storage"), the raw key in
parentheses, and problems in red; `--json`, a pipe or `NO_COLOR` change that.
`--out` always writes JSON.

`--package <factory.zip>` (fastboot) lists which of the phone's partitions that
factory image rewrites (bootloader and radio pack entries, the images in
`image-*.zip`, and `super` via `super_empty`), which it does not, split into
partitions unique to this phone (`devinfo`, `efs`, `persist`, `userdata` ...)
and others, and what is only in the package (usually logical partitions).

### Checking the modem partitions agree

```sh
pixel-restore modem diagnose --pull .pixel-restore/backups/check   # adb + root: back up, then check
pixel-restore modem diagnose --backup .pixel-restore/backups/check # check an existing backup
```

In fastboot only `--backup` works (fastboot cannot read partitions), and the
backup is also compared with the IMEIs in the bootloader's oem config. The
check parses devinfo's IMEI records, recognises ext4 images (label, UUID,
clean flag) and blank ones, and reports whether: both devinfo IMEIs are valid
and distinct; `efs` and `efs_backup` are identical; `cpsha` is present in
persist; the devinfo IMEIs appear in efs/persist and in which encoding (ASCII,
UTF-16, TBCD, 3GPP mobile identity); any other IMEI-shaped numbers are stored
there; and the oem config IMEIs match devinfo. IMEIs are shown as the last four
digits unless `--show-identifiers` is given. Nothing is written to the phone.

The motivating failure is Google's check-in rejecting a malformed IMEI before
account entry. A network error displayed by Play Store alone does not establish
this diagnosis. `invalid_imei_checkin: true` identifies the matching log message.

### Reading every small partition

```sh
cargo run -- modem read-partitions --out ~/pixel-partitions
```

Reads every partition up to `--max-size` (default `100M`) into a new private
folder, then reports each image's format from its magic numbers: boot and
vendor_boot images, vbmeta (with its rollback index), appended AVB footers,
devinfo (IMEIs masked unless `--show-identifiers`), FBPK, sparse, device-tree
tables, ELF, GPT, ext4, f2fs, erofs, super metadata, Tensor boot-chain stages,
blank images and unrecognized ones. Any IMEI-shaped number with a valid check
digit is listed per partition. `report.json` is saved next to the images.

From Android it reads `/dev/block/by-name` with `dd` and needs root. From
fastboot (`--fastboot`, or when the phone is only in fastboot) it uses
`fastboot fetch` (slotted partitions as `--slot a fetch boot`), which the
bootloader usually refuses; fastbootd
(`fastboot reboot fastboot`) allows it for some partitions. Refusals are
listed under `unreadable`. Nothing is written to the phone.

## Upstream source tools

Fetch the inspected revisions into an ignored local directory:

```sh
pixel-restore modem fetch-source restore-imei --out .pixel-restore/upstream/restore-imei
pixel-restore modem fetch-source pixel-kit --out .pixel-restore/upstream/pixel-kit
pixel-restore modem fetch-source shannon --out .pixel-restore/upstream/shannon
```

This uses Git over HTTPS, checks the pinned commit, disables checkout hooks and
never executes upstream scripts or initializes submodules. Existing destinations
are rejected. A failed fetch may leave an incomplete directory; choose a new
path or inspect/remove that directory before retrying. Git and network access
are needed only for fetching sources. Changing a pin requires another review.

| Source | Inspected commit | Integration |
| --- | --- | --- |
| [bitdomo/restore_imei](https://github.com/bitdomo/restore_imei) | `2132f425d3dcc6baa2e06d151f316f6e2ec8c4d5` | Original-IMEI workflow, partition inventory, limitations; scripts available for inspection |
| [not-GIANT/Pixel-Kit](https://github.com/not-GIANT/Pixel-Kit) | `6adb667041cf9207e2ad7340ed5f1b79e5d6372b` | Named devinfo record layout and backup workflow; GUI available separately |
| [davwheat/Shannon scripts](https://github.com/davwheat/shannon-pixel-modem-nvitem-enabler-scripts) | `e1d1751a1f74ec9b683d39b611cb6720a8f4d8b6` | Modem NV command reference and model-specific limitations |

No upstream code or binaries are bundled. The helper independently implements
diagnostics, backup validation and narrowly scoped devinfo preparation. The first
two repositories had no LICENSE file at review; they remain separate downloads,
not code incorporated under this project's MIT license. Shannon is GPL-3.0.

### Review findings that affect repairs

* **bitdomo's Linux code differs from its README.** `start.sh` always invokes
  `efs_imei_write.sh`. That script writes both modem IMEIs, overwrites
  `/mnt/vendor/persist/modem/cpsha`, and updates EFS backup. It does not implement
  the README's claimed compare-and-revert hash gate or optional EFS prompt.
  Several critical results are unchecked. Do not treat the README as an
  execution contract. The author reports Pixel 9 Pro XL and Pixel 7 testing;
  that does not validate this Android 17 build.
* **Pixel-Kit's CPID path is broader than original-IMEI restoration.** It writes
  modem NV items and may execute `lexipwn fiximeisha`. Its legacy patch function
  picks the first two digit runs, which cannot identify an all-F invalid field.
  This integration instead requires named, unique, bounded DIUS/DIFR records.
* **Shannon scripts are feature-specific.** Pixel 6/7 NV settings cannot be
  assumed to work on Pixel 9. Its reset script renames modem NV files; it is not
  an automatic solution for a malformed devinfo identifier.

## Back up before preparing a repair

Root must already be available and granted to the ADB shell. This tool does not
root, unlock, factory-reset, switch slots, or change bootmode; firmware is only
flashed by `reflash-radio --apply` (below).

```sh
pixel-restore modem backup --out .pixel-restore/backups/before-repair
pixel-restore modem verify-backup .pixel-restore/backups/before-repair
```

The backup reads `devinfo`, `efs`, `efs_backup`, `persist`, and a separate `cpsha`
copy. Each partition's length and device SHA-256 before/after transfer must match
the local file. An actively changing partition causes a failure. These reads
are not a filesystem-wide atomic snapshot. `manifest.json` is written only
when all backups succeed; a partial backup cannot prepare or apply a repair.

Private directories use mode 0700 and CLI-created files use a restrictive umask.
Backups contain unique device identity material: keep them outside version
control. Use this phone's own backups, not images from another phone.

## Prepare a devinfo correction offline

Currently limited to **Pixel 9 Pro XL (`komodo`)**. No Android version has been
hardware-validated by this integration; Android 17 remains explicitly unverified.

Create a private JSON file using the phone's *actual original* identifiers from
its original packaging or reliable ownership records. Do not derive IMEI2 from
IMEI1; the tool never generates identifiers. Substitute the real values below:

```json
{
  "serial": "YOUR_ADB_SERIAL",
  "imei1": "YOUR_ORIGINAL_15_DIGIT_IMEI1",
  "imei2": "YOUR_ORIGINAL_15_DIGIT_IMEI2",
  "confirmed_original": true
}
```

`confirmed_original` is your attestation, not cryptographic proof. The helper
can verify syntax/check digits and matching serials, but cannot independently
prove those numbers were assigned by the manufacturer.

```sh
pixel-restore modem prepare \
  --backup .pixel-restore/backups/before-repair \
  --original-imeis .pixel-restore/original-imeis.json \
  --out .pixel-restore/plans/restore-original
pixel-restore modem restore-devinfo --plan .pixel-restore/plans/restore-original
```

Both commands leave the phone unchanged. Preparation validates the complete
backup and both IMEIs, then patches only the 15 ASCII bytes in each named IMEI
field. It preserves record terminators, all other bytes, and partition length.
Unknown layouts, duplicate keys, malformed lengths, missing records, identical
IMEIs and already-correct values are rejected. It writes original/restored
images plus a plan with offsets, hashes and changed-byte count.

## Explicitly apply a reviewed plan

Writing a critical partition can leave the phone unusable if interrupted. The
upstream method may also require EFS recovery; this command only corrects
`devinfo` and does not claim to fix every modem failure.

Only after reviewing the plan, confirming the original identifiers and accepting
unverified build compatibility, the explicit write command is:

```sh
pixel-restore modem restore-devinfo --plan .pixel-restore/plans/restore-original \
  --apply --confirm-original-imeis --allow-unverified-build
```

Before writing, this revalidates every backup, the exact serial/build/properties,
the live devinfo hash/size, and the image's byte-level scope. It uploads into a
unique staging directory, checks the staged hash, writes **devinfo only**, and
verifies the resulting device hash. It never automatically reboots. A failed
write/readback is reported as failure, not success; keep the backup and investigate
before rebooting. There is no automatic rollback that could conceal a partial
write. After a successful write, reboot manually, retry sign-in and diagnose again.

### From fastboot, without root

A phone that will not boot Android, or has no root, can take the same reviewed
plan from the bootloader. The bootloader must already be unlocked.

```sh
pixel-restore modem restore-devinfo --plan .pixel-restore/plans/restore-original \
  --apply --confirm-original-imeis --allow-unverified-build --fastboot --no-readback
```

It checks the plan and backup exactly as above, that the phone in fastboot has
the plan's serial and model, that the bootloader is unlocked and, when the
bootloader reports it, that `devinfo` is the size of the prepared image. Then it
runs `fastboot flash devinfo` and nothing else. Fastboot cannot read partitions,
so the live-hash check (is the plan stale?) and the readback verification are
impossible here; `--no-readback` is the explicit acknowledgement. Make the
backup and plan fresh, from the phone's current state, before using it.

## Reflash the modem firmware from fastboot

A missing baseband after a flash usually means the radio image does not match
the bootloader build. This rewrites the modem firmware only; the IMEI lives in
devinfo/efs/persist, which it does not touch.

```sh
pixel-restore modem reflash-radio --image komodo-xxxx-factory-xxxx.zip          # dry run
pixel-restore modem reflash-radio --image komodo-xxxx-factory-xxxx.zip --apply
```

`--image` takes `radio-*.img` or the factory ZIP (the radio is extracted to
`--out`). It refuses a radio for another model or a locked bootloader, prints
the current bootloader and baseband, and with `--apply` flashes `radio`, reboots
into the bootloader and fails if the baseband it then reports is not the one
flashed. Use the factory image whose bootloader the phone is running.

## Lowest build the phone still accepts

For phones with no public USB-bootable BL1 (Pixel 6, 9 and 10 families), the
fallback is reinstalling an older bootloader and radio from fastboot. Going
below the phone's anti-rollback level can leave it in ROM recovery with no way
out, so `arb::lowest_safe()` in `src/arb.rs` picks the target (and
`arb::verdict()` checks any one build):

- The phone's level is that of the bootloader it is running
  (`fastboot getvar version-bootloader`), located in a listed factory image by
  the `bootloader-<product>-<version>.img` name inside the ZIP. A bootloader no
  listed image carries (beta, OTA-only) is refused, not guessed.
- `arb::BUMPS` lists the months Google announced a bump: Pixel 6/6 Pro/6a in Aug
  2022 and May 2025, Pixel 8/8 Pro/8a in May 2025, Pixel 10 series in May 2026.
  The floor is the latest bump at or before the phone's build; the answer is
  the oldest generic (non-carrier) image at or above it.
- The factory image page is read for "anti-roll back" warnings. One naming the
  phone's family in a month the table lacks, or a page with no such warnings at
  all, stops the selection until the table is updated.

What this cannot see is a bump Google never announced. Flash the current slot
first, reboot to the bootloader and confirm `version-bootloader` before
writing the radio and the other slot; an inactive slot below the level bricks
the phone if it ever falls back to it.

EFS/NV repair, cpsha rewriting, unknown helper binaries, and bootloader unlocks
remain outside this command. A blanked or damaged `efs`/`efs_backup` partition
is handled by `tools/pixel_efs.py` (`docs/efs-repair.md`), which rewrites it
from its surviving mirror or this phone's backup. If devinfo already contains the right values or
restoring it does not resolve check-in, stop and diagnose modem/EFS state rather
than repeatedly writing it. Hardware damage or loss of both EFS copies is not
recoverable by the upstream restore-imei tool's stated method.

## Tests

```sh
python3 -m unittest discover -s tools -p 'test_pixel_modem.py'
cargo test --locked
```

Python tests use synthetic partition records and mocked ADB: they exercise
malformed/duplicate records, transfer failures, modified backups, wrong serials,
changed builds, stale live images, out-of-scope modifications, explicit write
gates and failed readback. They do not modify a real phone. Source fetches can
be tested separately; none of the upstream scripts is executed by this project.
