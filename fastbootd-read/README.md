# fastbootd-read — a read-enabled fastbootd for partition backup

Stock fastbootd lets `fastboot fetch` read only three partitions
(`vendor_boot*`). On komodo (Pixel 9 Pro XL) that makes
`pixel-restore modem read-partitions --fastboot` fail for every partition the
IMEI-repair workflow needs to inspect (`devinfo`, `efs`, `efs_backup`,
`persist`, the boot chain, ...).

This subproject rebuilds fastbootd so the phone's **owner** can widen reading to
any partition — strictly read only, and only after confirming it on the phone's
own screen. It targets a **temporary, RAM-only** fastbootd (no flashing), for an
**unlocked** komodo.

## What it adds

Two new entries on the on-screen fastboot menu, each behind a warning and a
Cancel/confirm screen:

- **Enable reading** — `fastboot fetch` works for any partition fastbootd can
  open by name. The per-read size cap stays.
- **Enable all partitions** — as above, and the size cap is lifted too, so large
  partitions (`super`, `userdata`) can be read whole.

Either one also allows **hashing on the phone**:
`fastboot oem sha256 <partition>` (or `sha1`, `sha512`) returns the digest
without transferring the partition. `scripts/hash-partitions.sh` drives it.

Both are **read only**. No flash, erase or write path is changed. The default is
unchanged (stock three-entry allow-list). Nothing over USB can flip the toggle —
only the person physically at the phone. Reading is still refused on a locked
bootloader, exactly as upstream.

## Layout

```
fastbootd-read/
  patches/      git-apply-clean patches for AOSP system/core + bootable/recovery
  aosp-new/     the two new daemon source files, for reference
  docker/       Linux build environment (Dockerfile + build.sh) — macOS can't build AOSP
  scripts/      container-build (sync/vendor/patch/build), apply-patches,
                verify image, make_ram_boot_image, boot-fastbootd (RAM boot),
                read-partitions and hash-partitions wrappers
  docs/         design.md (mechanism) and build-and-boot.md (how to build + boot)
```

## Quick start

```sh
# 1. One command: syncs GrapheneOS (branch 17) into a Docker volume, generates
#    the komodo vendor files with adevtool, applies the patches, builds
#    komodo-cur-userdebug, verifies the patch is in the binary, and copies the
#    images to fastbootd-read/out/komodo/. Re-runs are incremental.
./docker/build.sh
# 2. Phone in the bootloader, unlocked: load the patched fastbootd into RAM
#    (`fastboot boot`, never flashes). It checks the phone first and confirms
#    which fastbootd came up. See docs/build-and-boot.md.
./docker/build.sh boot
# 3. On the phone's screen pick "Enable reading", then read partitions:
./scripts/read-partitions.sh --serial <serial> ~/pixel-partitions
#    ...or just hash them on the phone (sha256sum format; --check compares
#    against e.g. `sha256sum *.img > backup.sha256` from an earlier backup):
./scripts/hash-partitions.sh --serial <serial> --check backup.sha256
```

Needs Docker with about 300 GB of disk and 32 GB+ of memory for the VM. The
first run takes hours. `BRANCH`, `TAG`, `DEVICE`, `SYNC=0`, `SRC=<host dir>`
and the others are listed at the top of `docker/build.sh`;
`./docker/build.sh shell` opens a shell on the tree.

## Status

- **Verified:** the fetch-gating analysis against real upstream source, and that
  both patches apply cleanly to GrapheneOS `17` (`system/core` and
  `bootable/recovery`, checked 2026-10-17).
- **Needs the phone + a Linux builder (your step):** compiling for komodo, and
  confirming on-device that `fastboot boot` enters the modified fastbootd. This
  was developed in a cloud container with no Android tree, no USB and no device,
  so none of it has been compiled or run on hardware yet.

## Safety

- Read only by construction — writing is never enabled.
- Opt-in from the phone screen, with a warning and a confirmation, default off.
- RAM-only target: a reboot clears both the policy and the modified fastbootd.
- komodo has no USB-boot rescue. Nothing here flashes; any decision to flash is
  the owner's, made with the phone in hand.

See `docs/design.md` for the mechanism and `docs/build-and-boot.md` to build.
