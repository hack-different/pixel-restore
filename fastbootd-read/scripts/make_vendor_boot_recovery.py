#!/usr/bin/env python3
"""Put the built read-enabled recovery/fastbootd into the phone's OWN
vendor_boot, the way OrangeFox for Tensor installs on Pixel 7 and later:

    make_vendor_boot_recovery.py <stock vendor_boot.img> <built vendor_boot.img> <out.img> [mkbootimg_dir]

On these phones `fastboot boot` ignores the boot image's ramdisk (the
bootloader rebuilds it from init_boot, vendor_boot and vendor_kernel_boot on
the phone), so recovery can only be replaced by flashing vendor_boot. This
keeps everything of the stock image: header, kernel command line, bootconfig
and its ramdisk, including first_stage_ramdisk, which a normal Android boot
uses. It appends one more cpio archive to the ramdisk with the built
recovery root minus first_stage_ramdisk, so the built recovery, fastbootd
and their libraries win in recovery mode and nothing changes for Android.

Take the stock image from the phone itself (read only, from the bootloader):

    fastboot fetch vendor_boot_b vendor_boot_b.stock.img

and keep it: flashing it back restores the phone exactly. This script only
writes <out.img>; flashing it is a separate, deliberate step.

Needs python3, lz4 and cpio (all in the build container) and AOSP's
mkbootimg.py/unpack_bootimg.py (default /aosp/system/tools/mkbootimg).
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

PARTITION_SIZE = 64 << 20  # vendor_boot_a/_b on Tensor Pixels


def unpack(tool, image, out):
    """Unpack `image` into `out`; return its mkbootimg arguments as a list."""
    text = subprocess.run(
        [sys.executable, tool, "--boot_img", image, "--out", out, "--format=mkbootimg"],
        check=True, capture_output=True, text=True,
    ).stdout
    return shlex.split(text)


def fragments(args):
    """The ramdisk files named in vendor_boot mkbootimg arguments, in order."""
    out = []
    for i, word in enumerate(args):
        if word in ("--vendor_ramdisk", "--vendor_ramdisk_fragment") and i + 1 < len(args):
            out.append(args[i + 1])
    return [f for f in out if os.path.getsize(f) > 0]


def main():
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    stock, built, output = sys.argv[1:4]
    mkb = sys.argv[4] if len(sys.argv) == 5 else "/aosp/system/tools/mkbootimg"
    unpack_tool = os.path.join(mkb, "unpack_bootimg.py")
    mkboot_tool = os.path.join(mkb, "mkbootimg.py")
    for tool in ("lz4", "cpio"):
        if not shutil.which(tool):
            sys.exit(f"!! {tool} is not installed")
    if os.path.getsize(stock) != PARTITION_SIZE:
        print(f"   note: {stock} is {os.path.getsize(stock)} bytes, not a whole 64 MiB partition")

    with tempfile.TemporaryDirectory() as tmp:
        stock_args = unpack(unpack_tool, stock, os.path.join(tmp, "stock"))
        if stock_args.count("--vendor_ramdisk_fragment") > 1:
            sys.exit("!! the stock vendor_boot has several ramdisk fragments; this script "
                     "expects komodo's single one")
        built_parts = fragments(unpack(unpack_tool, built, os.path.join(tmp, "built")))
        if not built_parts:
            sys.exit(f"!! {built} has no ramdisk")

        # The built recovery root, without first_stage_ramdisk (the stock
        # one stays, so a normal boot is untouched).
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        for part in built_parts:
            data = subprocess.run(["lz4", "-dc", part], check=True, capture_output=True).stdout
            subprocess.run(["cpio", "-idm", "--quiet"], input=data, cwd=root, check=True)
        shutil.rmtree(os.path.join(root, "first_stage_ramdisk"), ignore_errors=True)
        for need in ("system/bin/recovery", "system/bin/fastbootd"):
            if not os.path.exists(os.path.join(root, need)):
                sys.exit(f"!! {built} has no {need}")
        names = subprocess.run(["find", ".", "-mindepth", "1"], cwd=root, check=True,
                               capture_output=True, text=True).stdout
        names = "\n".join(sorted(n[2:] for n in names.split("\n") if n)) + "\n"
        archive = subprocess.run(["cpio", "-o", "-H", "newc", "-R", "0:0", "--quiet"],
                                 input=names.encode(), cwd=root, check=True,
                                 capture_output=True).stdout
        overlay = os.path.join(tmp, "overlay.lz4")
        subprocess.run(["lz4", "-l", "-12", "-q", "-f", "-", overlay], input=archive, check=True)

        # Rebuild the stock image with its fragment followed by the overlay,
        # as one fragment: same type, name and board id as the stock entry.
        args = list(stock_args)
        i = args.index("--vendor_ramdisk_fragment")
        merged = os.path.join(tmp, "ramdisk.merged")
        with open(merged, "wb") as out:
            for part in (args[i + 1], overlay):
                with open(part, "rb") as f:
                    out.write(f.read())
        args[i + 1] = merged
        subprocess.run([sys.executable, mkboot_tool, *args, "--vendor_boot", output], check=True)

    size = os.path.getsize(output)
    if size > PARTITION_SIZE:
        os.remove(output)
        sys.exit(f"!! the result is {size} bytes; vendor_boot holds {PARTITION_SIZE}")
    # Pad to the whole partition, as the stock image is, so no stale bytes
    # (such as the old AVB footer) stay behind it after flashing and a
    # `fastboot fetch` of the partition compares equal to this file. The
    # vbmeta hash of vendor_boot no longer matches either way: an unlocked
    # bootloader warns and boots, as with any custom vendor_boot.
    with open(output, "r+b") as f:
        f.truncate(PARTITION_SIZE)
    print(f">> Wrote {output} ({size} bytes used of {PARTITION_SIZE}, zero-padded)")
    print("   stock header, cmdline, bootconfig and first_stage_ramdisk; built recovery on top")


if __name__ == "__main__":
    main()
