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
  scripts/      apply-patches, verify/assemble image, read-partitions wrapper
  docs/         design.md (mechanism) and build-and-boot.md (how to build + boot)
```

## Quick start

```sh
# 1. Sync a komodo-capable tree (GrapheneOS recommended) into $SRC, then:
SRC=/path/to/tree LUNCH_TARGET=komodo-userdebug ./docker/build.sh
# 2. Verify the patch landed in the binary:
OUT=$SRC/out/target/product/komodo ./scripts/make-boot-image.sh
# 3. Boot it temporarily on the unlocked phone (see docs/build-and-boot.md),
#    pick "Enable reading" on the phone screen, then:
./scripts/read-partitions.sh --serial <serial> ~/pixel-partitions
```

## Status

- **Verified:** the fetch-gating analysis against real upstream source, and that
  both patches apply cleanly (`git apply --check`) to their stated bases
  (`system/core` @ AOSP `main`; `bootable/recovery` @ GrapheneOS `17`).
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
