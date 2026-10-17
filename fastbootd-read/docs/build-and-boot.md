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
komodo device config or kernel. Two realistic bases:

- **GrapheneOS** — officially supports komodo; follow grapheneos.org/build to
  `repo init`/`repo sync` its tree and set up the komodo target. Recommended.
- **AOSP + Google's Pixel device/vendor binaries** for komodo, if you maintain
  such a tree.

The `bootable/recovery` patch is cut against GrapheneOS `17`; the `system/core`
patch against AOSP `main`. `scripts/apply-patches.sh` dry-runs first and tells
you if your tree differs so you can apply by hand.

Build **userdebug**. It defines `FB_ENABLE_FETCH` (so `fetch` is compiled in)
and keeps the recovery and fastbootd processes able to share the tmpfs policy
file.

## Build

```sh
# one-time: sync a komodo-capable tree per GrapheneOS/AOSP docs, into $SRC
SRC=/path/to/tree LUNCH_TARGET=komodo-userdebug ./docker/build.sh
```

`build.sh` builds the image, applies the patches in the container, then runs
`m fastbootd recovery bootimage vendorbootimage`. Outputs land in
`$OUT` (`.../out/target/product/komodo`): `boot.img`, `vendor_boot.img`.

Verify the patch is actually in the binary (no phone involved):

```sh
OUT=/path/to/tree/out/target/product/komodo ./scripts/make-boot-image.sh
```

It greps the built `fastbootd`/recovery ramdisk for the patched string and
fails loudly if the rebuild did not take.

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
