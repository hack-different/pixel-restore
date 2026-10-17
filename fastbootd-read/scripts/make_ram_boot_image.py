#!/usr/bin/env python3
"""Assemble fastbootd-boot.img: one image `fastboot boot` can load into RAM
that carries the read-enabled fastbootd.

    make_ram_boot_image.py <artifacts_dir> [mkbootimg_dir]

Why a new image: on komodo (BOARD_MOVE_RECOVERY_RESOURCES_TO_VENDOR_BOOT) the
recovery binary and fastbootd live in the vendor_boot ramdisk, and `fastboot
boot` only replaces boot.img; the bootloader still loads vendor_boot from the
phone. So this builds a boot image (header v4) whose kernel is the built
boot.img's and whose ramdisk is every built ramdisk, in boot order:

    vendor_boot fragments, vendor_kernel_boot, init_boot

The bootloader puts the boot image's ramdisk after the ramdisks it loads from
the phone, and the kernel unpacks concatenated archives in order, so the built
files (including system/bin/fastbootd and the kernel modules that match the
built kernel) replace the phone's copies. It only writes a local file.

mkbootimg_dir holds AOSP's mkbootimg.py and unpack_bootimg.py
(default: /aosp/system/tools/mkbootimg, inside the build container).
"""

import os
import shlex
import subprocess
import sys
import tempfile


def unpack(tool, image, out):
    """Unpack `image` into `out`; return its mkbootimg arguments as a dict of
    flag -> list of values."""
    text = subprocess.run(
        [sys.executable, tool, "--boot_img", image, "--out", out, "--format=mkbootimg"],
        check=True, capture_output=True, text=True,
    ).stdout
    args, words = {}, shlex.split(text)
    for i, word in enumerate(words):
        if word.startswith("--"):
            value = words[i + 1] if i + 1 < len(words) and not words[i + 1].startswith("--") else ""
            args.setdefault(word, []).append(value)
    return args


def vendor_ramdisks(args):
    """Ramdisk files of a vendor_boot-format image, in fragment order."""
    files = args.get("--vendor_ramdisk_fragment", []) or args.get("--vendor_ramdisk", [])
    return [f for f in files if f and os.path.getsize(f) > 0]


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    art = sys.argv[1]
    mkb = sys.argv[2] if len(sys.argv) == 3 else "/aosp/system/tools/mkbootimg"
    unpack_tool = os.path.join(mkb, "unpack_bootimg.py")
    mkboot_tool = os.path.join(mkb, "mkbootimg.py")
    for tool in (unpack_tool, mkboot_tool):
        if not os.path.isfile(tool):
            sys.exit(f"!! missing {tool}")

    def image(name, required):
        path = os.path.join(art, name)
        if os.path.isfile(path):
            return path
        if required:
            sys.exit(f"!! missing {path}; run docker/build.sh first")
        print(f"   note: no {name}; the phone's own copy will be used")
        return None

    boot = image("boot.img", True)
    vboot = image("vendor_boot.img", True)
    vkboot = image("vendor_kernel_boot.img", False)
    initboot = image("init_boot.img", False)

    with tempfile.TemporaryDirectory() as tmp:
        boot_args = unpack(unpack_tool, boot, os.path.join(tmp, "boot"))
        kernel = os.path.join(tmp, "boot", "kernel")
        if not os.path.isfile(kernel) or os.path.getsize(kernel) == 0:
            sys.exit(f"!! {boot} has no kernel")

        parts = vendor_ramdisks(unpack(unpack_tool, vboot, os.path.join(tmp, "vboot")))
        if not parts:
            sys.exit(f"!! {vboot} has no ramdisk")
        if vkboot:
            parts += vendor_ramdisks(unpack(unpack_tool, vkboot, os.path.join(tmp, "vkboot")))
        if initboot:
            unpack(unpack_tool, initboot, os.path.join(tmp, "initboot"))
            generic = os.path.join(tmp, "initboot", "ramdisk")
            if os.path.isfile(generic) and os.path.getsize(generic) > 0:
                parts.append(generic)
        boot_ramdisk = os.path.join(tmp, "boot", "ramdisk")
        if os.path.isfile(boot_ramdisk) and os.path.getsize(boot_ramdisk) > 0:
            parts.append(boot_ramdisk)

        ramdisk = os.path.join(tmp, "ramdisk.combined")
        with open(ramdisk, "wb") as out:
            for part in parts:
                with open(part, "rb") as f:
                    out.write(f.read())

        output = os.path.join(art, "fastbootd-boot.img")
        cmd = [sys.executable, mkboot_tool, "--header_version", "4",
               "--kernel", kernel, "--ramdisk", ramdisk, "--output", output]
        for flag in ("--os_version", "--os_patch_level", "--cmdline"):
            if boot_args.get(flag, [""])[0]:
                cmd += [flag, boot_args[flag][0]]
        subprocess.run(cmd, check=True)

        print(">> Wrote", output, f"({os.path.getsize(output)} bytes)")
        print("   kernel from boot.img; ramdisk = " + " + ".join(
            os.path.relpath(p, tmp) for p in parts))


if __name__ == "__main__":
    main()
