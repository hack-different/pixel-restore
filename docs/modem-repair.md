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
pixel-restore modem diagnose --serial YOUR_ADB_SERIAL
```

Every device command requires a serial. `diagnose` reads build, baseband and
bootloader properties and classifies recent Google Play services log messages.
It does not request root or save raw logs/accounts/tokens. Its optional `--out`
JSON includes the device serial and fingerprint. Logs can be historical: retry
sign-in immediately before diagnosis to associate the findings with that attempt.

The motivating failure is Google's check-in rejecting a malformed IMEI before
account entry. A network error displayed by Play Store alone does not establish
this diagnosis. `invalid_imei_checkin: true` identifies the matching log message.

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
root, unlock, factory-reset, flash firmware, switch slots, or change bootmode.

```sh
pixel-restore modem backup --serial YOUR_ADB_SERIAL --out .pixel-restore/backups/before-repair
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
pixel-restore modem restore-devinfo --serial YOUR_ADB_SERIAL \
  --plan .pixel-restore/plans/restore-original
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
pixel-restore modem restore-devinfo --serial YOUR_ADB_SERIAL \
  --plan .pixel-restore/plans/restore-original \
  --apply --confirm-original-imeis --allow-unverified-build
```

Before writing, this revalidates every backup, the exact serial/build/properties,
the live devinfo hash/size, and the image's byte-level scope. It uploads into a
unique staging directory, checks the staged hash, writes **devinfo only**, and
verifies the resulting device hash. It never automatically reboots. A failed
write/readback is reported as failure, not success; keep the backup and investigate
before rebooting. There is no automatic rollback that could conceal a partial
write. After a successful write, reboot manually, retry sign-in and diagnose again.

EFS/NV repair, cpsha rewriting, unknown helper binaries, and bootloader unlocks
remain outside this command. If devinfo already contains the right values or
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
