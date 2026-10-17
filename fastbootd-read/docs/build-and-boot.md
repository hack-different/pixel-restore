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
   `m fastbootd recovery bootimage vendorbootimage vendorkernelbootimage`.
5. Runs `scripts/make-boot-image.sh`, which greps the built
   `fastbootd`/recovery ramdisk for the patched string and fails loudly if the
   rebuild did not take.
6. Copies `boot.img`, `vendor_boot.img`, `vendor_kernel_boot.img`, the
   `fastbootd` binary and a pinned `manifest.xml` to
   `fastbootd-read/out/komodo/`.

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

Re-runs are incremental: before re-syncing, the patches are reverted so repo
can update `system/core` and `bootable/recovery`, then they are applied again.

## Boot it TEMPORARILY (no flash)

Prerequisites: the **bootloader is unlocked** (reading is refused on a locked
device by design), and `fastboot` is on your PATH. Confirm lock state first:

```sh
fastboot getvar unlocked      # want: unlocked: yes
```

The intent is to run the modified fastbootd from RAM via `fastboot boot`, so
nothing on the phone changes. The exact image to boot and whether komodo's
bootloader enters fastbootd from `fastboot boot` is the one step that must be
confirmed on your device — it depends on komodo's boot/vendor_boot layout and
cannot be validated from a cloud container. Work it out with the phone in hand:

1. Build as above; keep `$OUT`.
2. From the running system or bootloader, enter stock fastbootd once to learn
   the normal path: `adb reboot fastboot` (or `fastboot reboot fastboot` from
   bootloader).
3. Try booting the rebuilt image from RAM and watch whether the phone comes up
   in the **modified** fastbootd (the on-screen menu will show the two new
   entries). The image and `fastboot boot` invocation are komodo-specific;
   `make-boot-image.sh` prints the artifacts to work from.

If `fastboot boot` will not enter the modified fastbootd on komodo, the only
alternative is flashing (e.g. the inactive slot), which **writes to the phone**.
komodo has no USB-boot rescue, so that decision — and the go-ahead — is yours;
nothing in this repo flashes on its own.

## Use it

On the phone's fastbootd screen, scroll to **Enable reading** (or **Enable all
partitions**), read the warning, pick **Yes**. Then from the Mac:

```sh
./scripts/read-partitions.sh --serial 47191FDAS0056M ~/pixel-partitions
```

That calls the existing `pixel-restore modem read-partitions --fastboot`, which
now gets data instead of a refusal. Everything is read only.

## Reset

The policy lives in a tmpfs file and the whole fastbootd is running from RAM:
reboot the phone and it is gone. Nothing persists.
