# Building on a Mac, and booting it temporarily on komodo

## Why a container

AOSP and GrapheneOS cannot be built on macOS: the platform build requires a
case-sensitive filesystem and Linux-only host tools. Build inside the Linux
container in `docker/`. On Apple Silicon run it as `linux/amd64` (the default in
`docker/build.sh`) because the AOSP prebuilt host toolchain is x86_64;
emulation works but is slow, so a native x86_64 Linux machine (local or cloud)
is much faster for a full platform tree.

## Which source tree

komodo (Pixel 9 Pro XL) is **not** buildable from plain AOSP — AOSP has no
komodo device config or kernel. The build uses **GrapheneOS**, which supports
komodo and generates the Pixel vendor files with its `adevtool`. Both patches
apply cleanly to GrapheneOS `17`. `scripts/apply-patches.sh` dry-runs first,
skips a patch that is already applied, and stops without changing anything if
your tree differs.

Build **userdebug**. It defines `FB_ENABLE_FETCH` (so `fetch` is compiled in)
and keeps the recovery and fastbootd processes able to share the tmpfs policy
file.

## Build

```sh
./docker/build.sh
```

That one command builds the container, then inside it
(`scripts/container-build.sh`):

1. `repo init -u https://github.com/GrapheneOS/platform_manifest.git -b 17`
   and `repo sync` (retried up to three times; an interrupted sync resumes).
2. `adevtool generate-all -d komodo`, which downloads the stock factory image
   and extracts the vendor files. This runs once per manifest revision.
3. `repo start fastbootd-read` on `system/core` and `bootable/recovery`, then
   applies the two patches there.
4. `lunch komodo-cur-userdebug` and
   `m fastbootd recovery bootimage initbootimage vendorbootimage vendorkernelbootimage`.
5. Runs `scripts/make-boot-image.sh`, which greps the built
   `fastbootd`/recovery ramdisk for the patched string and fails loudly if the
   rebuild did not take.
6. Copies `boot.img`, `init_boot.img`, `vendor_boot.img`,
   `vendor_kernel_boot.img`, the `fastbootd` binary and a pinned `manifest.xml`
   to `fastbootd-read/out/komodo/`.
7. Assembles `fastbootd-boot.img` there, the image `./docker/build.sh boot`
   loads into RAM (see below).

The tree lives in the Docker volume `fastbootd-read-src`. On a Mac that matters:
it is a case-sensitive Linux filesystem, which AOSP needs and a normal APFS
folder is not. Raise Docker Desktop's limits first (Settings > Resources): disk
to about 300 GB and memory to 32 GB or more. The first run takes hours,
especially under amd64 emulation on Apple Silicon.

Useful variables (all listed at the top of `docker/build.sh`):

```sh
TAG=<release tag> ./docker/build.sh   # a release instead of branch 17; the tag signature is verified
SYNC=0 ./docker/build.sh              # rebuild without re-syncing
VENDOR=1 ./docker/build.sh            # force adevtool to regenerate vendor files
SRC=/path/on/linux ./docker/build.sh  # keep the tree in a host directory instead
./docker/build.sh shell               # a shell in the container, tree at /aosp
```

The build runs as the image's unprivileged `node` user (uid 1000), because
adevtool refuses to run as root. On the first run, the tree from an earlier
root-run sync is handed to that user once, which takes a few minutes for a full
tree. If you later run commands as root in `./docker/build.sh shell`, use
`CHOWN=1 ./docker/build.sh` to repeat that step.

Re-runs are incremental: before re-syncing, the patches are reverted so repo
can update `system/core` and `bootable/recovery`, then they are applied again.

## Boot it TEMPORARILY (no flash)

Put the phone in the **bootloader** (Power + Volume Down; not fastbootd), plug it
in, and run:

```sh
./docker/build.sh boot              # or: ./scripts/boot-fastbootd.sh [--serial S]
```

Before sending anything that changes state, it checks, all read only:
`fastboot` is on PATH; exactly one phone is in fastboot (or `--serial`); it is in
the bootloader (`is-userspace: no`); `unlocked: yes`; the image exists, was
built for this product, and fits `max-download-size`. It shows the command and
asks you to type `yes` (`--yes` skips the prompt).

Then it runs exactly one state-changing command:

```sh
fastboot -s <serial> boot out/komodo/fastbootd-boot.img
```

The bootloader loads the image into RAM and starts it. Nothing is flashed, and
a reboot returns the phone to what is installed.

### Why a special image

On komodo the recovery binary and fastbootd live in the **vendor_boot**
ramdisk (`BOARD_MOVE_RECOVERY_RESOURCES_TO_VENDOR_BOOT`), and the generic
ramdisk is in **init_boot**. `fastboot boot` only replaces boot.img; the
bootloader still loads vendor_boot, vendor_kernel_boot and init_boot from the
phone. Booting the plain boot.img would therefore bring up the phone's stock
fastbootd.

`scripts/make_ram_boot_image.py` (run at the end of the build, or by
`build.sh boot` for an older build) makes `fastbootd-boot.img`: a header-v4
boot image with the built kernel and one ramdisk that concatenates every built
ramdisk in boot order: vendor_boot fragments, vendor_kernel_boot, init_boot.
The bootloader places the boot image's ramdisk after the ones it loads from the
phone, and the kernel unpacks concatenated archives in order. So the built
files win: `system/bin/fastbootd`, the recovery binary with the new menu, and
kernel modules that match the built kernel.

### What it does after `fastboot boot`

It waits for the phone to leave the bootloader and come back over USB, then
reports:

- **Read-enabled fastbootd** (`is-userspace: yes`, and a `fetch` probe is
  refused with the patched "until reading is enabled from the on-screen
  fastboot menu" message): success. It prints the next step.
- **Stock fastbootd** (the refusal lacks that text): the built ramdisk did not
  replace the phone's.
- **Android booted** (seen with `adb`): the bootloader started the image as a
  normal boot, not recovery. In that case `fastboot boot` cannot reach the
  patched fastbootd on komodo.
- **Back in the bootloader**, or **nothing within `--wait` seconds** (default
  180): the image did not come up. If the phone shows the recovery menu, pick
  **Enter fastboot**.

The probe is refused before anything is read, because reading starts disabled
on every boot.

### Not yet confirmed on komodo

Two things can only be learned with the phone in hand:
- whether komodo's bootloader starts a `fastboot boot` image in recovery mode,
  where fastbootd runs;
- whether the built kernel comes up with the dtb from the phone's own
  vendor_boot. This is most likely when the phone already runs the same
  GrapheneOS build.

The command tells you which of these happened. Either way nothing is written,
and a reboot (Power + Volume Down for the bootloader) undoes it.

If `fastboot boot` cannot reach the patched fastbootd, the only alternative is
flashing vendor_boot (e.g. on the inactive slot), which **writes to the phone**.
komodo has no USB-boot rescue, so that decision and the go-ahead are yours. No
script here flashes.

## Use it

On the phone's fastbootd screen, scroll to **Enable reading** (or **Enable all
partitions**), read the warning, pick **Yes**. Then from the Mac:

```sh
./scripts/read-partitions.sh --serial 47191FDAS0056M ~/pixel-partitions
```

That calls the existing `pixel-restore modem read-partitions --fastboot`, which
now gets data instead of a refusal. Everything is read only.

To compare partitions without pulling them, hash them on the phone:

```sh
./scripts/hash-partitions.sh --serial 47191FDAS0056M                  # devinfo efs efs_backup modem_userdata persist
./scripts/hash-partitions.sh --algo sha512 boot_a vendor_boot_a super # any readable partition, slot suffix included
./scripts/hash-partitions.sh --out now.sha256 --check backup.sha256   # backup.sha256 from: sha256sum *.img
```

Each partition is one `fastboot oem sha256 <partition>`. The phone reads it and
answers with the digest and size. The output is in `sha256sum` format, and
`--check` exits non-zero if any digest differs.

## Reset

The policy lives in a tmpfs file and the whole fastbootd is running from RAM:
reboot the phone and it is gone. Nothing persists.
