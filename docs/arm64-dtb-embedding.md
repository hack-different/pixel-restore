# Can an arm64 kernel carry its own device tree?

Context: the fastbootd-read work on komodo (Pixel 9 Pro XL, Tensor G4,
AD1A.240530.030, kernel 6.1.x) wants a self-contained RAM-bootable kernel
(built-in initramfs with our recovery/fastbootd and AD1A-matched modules) so
nothing has to be written to the phone. This note answers whether the device
tree blob (DTB) can be part of that package, or whether it always comes from
flash.

**Short answer:** the claim holds. On arm64 the kernel has no supported way to
use a DTB it carries itself; the bootloader must hand one over in `x0`, and on
a Pixel 7+ `fastboot boot` takes only the kernel from the sent image, so the
DTB comes from the phone's own `vendor_boot` (plus `dtbo`). For komodo this is
not a blocker as long as the kernel is built from the AD1A source the
on-phone DTB was built with. Embedding only matters if we diverge from the
stock DTB, and the only realistic ways to do that are a chainloading shim or a
flash write.

## 1. The arm64 boot protocol requires the bootloader's DTB

`Documentation/arch/arm64/booting.rst`, section "Setup the device tree"
(marked **MANDATORY**): the primary CPU enters the kernel with
`x0 = physical address of device tree blob (dtb) in system RAM`. The DTB must
be 8-byte aligned, at most 2 MB, and outside any 2 MB region needing special
mapping attributes.

The kernel does exactly that and nothing else: `head.S` saves `x0` as
`__fdt_pointer`, and `setup_machine_fdt(dt_phys)` in
`arch/arm64/kernel/setup.c` maps that address. If it is not a valid FDT the
kernel prints "invalid device tree blob ... Please check your bootloader" and
spins in `cpu_relax()` forever; there is no fallback.

## 2. Kernel-side "built-in" or "appended" DTB options

| Option | Arch | arm64? |
|---|---|---|
| `CONFIG_ARM_APPENDED_DTB` (+ `ARM_ATAG_DTB_COMPAT`) | ARM32 only, `arch/arm/Kconfig`: zImage decompressor finds a DTB concatenated after itself | **No.** arm64 has no decompressor wrapper (`Image` is raw; `Image.gz`/`.lz4` is decompressed by the bootloader) |
| `CONFIG_GENERIC_BUILTIN_DTB` / `BUILTIN_DTB_ALL` | Generic kbuild support merged around v6.13 ("kbuild: add generic support for built-in boot DTBs"); selected by riscv, loongarch, mips, xtensa | **No.** Not selected by `arch/arm64/Kconfig` even on current mainline (checked 2026-10-17), and absent from 6.1 entirely |
| `CONFIG_EFI_ARMSTUB_DTB_LOADER` (`dtb=` on the EFI stub command line) | arm64 EFI stub | Only when booted as an EFI application by UEFI firmware. The Pixel bootloader is not UEFI and jumps to `Image` via the booting.rst protocol, so the stub never runs. Also deprecated upstream |

So "arm64 doesn't take an appended/built-in DTB the way ARM32 did" is correct,
for 6.1 and for mainline.

The Qualcomm "`Image.gz-dtb`" format looks like a counterexample but is not:
there the Qualcomm LK/ABL bootloader scans past the gzip stream for the
appended DTB and passes it in `x0`. The kernel still only reads `x0`. Tensor's
bootloader has no such convention that we know of.

## 3. Android boot image and what the Pixel bootloader honors

- Boot image header v2 has `dtb_size`/`dtb_addr` in `boot.img`. From v3 on the
  DTB fields moved to the **vendor_boot** header; v3/v4 `boot.img` has no DTB
  field at all (AOSP "Boot image header" page).
- komodo uses v4: `boot` (kernel), `init_boot` (generic ramdisk),
  `vendor_boot` (vendor ramdisks, cmdline, bootconfig, DTB) and `dtbo`
  (board overlays).
- The Tensor bootloader picks the base DTB from `vendor_boot`, applies the
  matching overlay from `dtbo` for the board ID/revision, then fixes it up
  (memory, `/chosen` bootargs and bootconfig, initrd range, KASLR seed,
  serial number, AVB state) before entering the kernel. Those fixups are why
  a DTB built off-device is not a drop-in replacement even if we could pass
  one. (Overlay selection by board ID is the standard Pixel `dtbo` layout;
  we have not traced it in ripcurrentpro.)
- Since Pixel 7, `fastboot boot` uses only the kernel from the sent image and
  rebuilds the rest from the phone's own `init_boot`/`vendor_boot`/
  `vendor_kernel_boot` (see the fastbootd-read findings). A v2-style
  `fastboot boot --dtb` therefore has nowhere to land: there is no field for
  it in v4 and the bootloader would ignore it anyway. We have not tested a
  v2 header on komodo; given the observed behavior we expect it to be refused
  or treated the same.

## 4. Wrapping the DTB into the kernel image anyway

Possible in principle, none supported:

1. **Chainloading shim.** Prepend a small arm64 stub that carries an
   arm64 `Image` header (so the bootloader accepts it), relocates itself if
   needed, builds the final DTB and jumps to the real kernel with `x0`
   pointing at it. To be correct it must merge the bootloader's fixups from
   the incoming `x0` DTB (`/chosen`, `/memory`, reserved-memory, initrd
   range, KASLR seed, serial) into the embedded one, which is essentially
   writing a mini bootloader. It works for RAM boot because the kernel is the
   one thing `fastboot boot` does take from us. Brittle and only worth it if
   we need a DT the phone does not have.
2. **Patching the kernel (most practical if we need a custom DT).** We
   build the kernel, so a local patch can add a build-time option (it cannot
   be a command-line flag: bootargs live in the DT's `/chosen`, so nothing on
   the command line is readable before the DT is chosen). Sketch, roughly
   100-200 lines in `arch/arm64/kernel/setup.c` plus an `.incbin` of the
   blob:
   - keep the normal early scan of the bootloader's `x0` DTB, so memory,
     `/chosen` (bootargs, bootconfig, `androidboot.*`), KASLR/RNG seeds and
     reserved-memory come from the real fixed-up tree;
   - after `arm64_memblock_init()` has reserved memory, point
     `initial_boot_params` at the embedded blob (with `/chosen` copied over
     via libfdt) just before `unflatten_device_tree()`, so drivers bind
     against our tree;
   - the embedded blob must keep the same `/reserved-memory` carve-outs,
     so it should start from the phone's real final DT, not one rebuilt
     from source. Get it by RAM-booting once with the stock DT and dumping
     `/sys/firmware/fdt` from our fastbootd: that captures the dtbo overlay
     and every bootloader fixup.
   Cost: the kernel diverges from AD1A source by one small, auditable patch.
3. **Initramfs / first-stage init.** Too late: the DT is unflattened in
   `setup_arch()`, long before the initramfs is unpacked, and every
   platform driver binds against it. Init cannot replace the base DT.
4. **kexec with `--dtb`.** arm64 `kexec_file_load` / kexec-tools accept a
   DTB for the next kernel, so our RAM-booted kernel could kexec into
   itself with a custom DTB. Needs `CONFIG_KEXEC(_FILE)` (not in GKI, but we
   build our own), and Tensor devices (IOMMUs, display, modem, GSA, Titan
   link) are not guaranteed to survive a kexec without a full reset.
   Experimental at best.

## 5. Runtime overlays (dtbo)

`CONFIG_OF_OVERLAY` lets a running kernel apply overlays (mainline has no
userspace interface; Android kernels have used out-of-tree configfs ones).
Overlays can only add or change nodes and properties after boot; they cannot
swap the base DT, and drivers that already probed against the old nodes do
not re-probe cleanly. Useful for adding a node our fastbootd needs, not for
replacing the device tree.

## 6. What this means for komodo

- The DT is the interface between the bootloader-provided hardware
  description and the drivers/modules built from the same release. The
  on-phone DTB in `vendor_boot_b` is the AD1A one, so a kernel and modules
  built from the AD1A kernel source match it. **Embedding is not needed for
  route (A).**
- Check once before building: confirm the active slot's `vendor_boot`/`dtbo`
  really are AD1A (e.g. compare a `fastboot fetch vendor_boot_b` hash, or
  `fastboot getvar` build info, against the AD1A factory image). If the
  phone ever takes an OTA, the DTB changes and the kernel must be rebuilt
  from the matching source.
- Only if we need a DT change (a node the stock DTB lacks, a disabled
  device) is embedding relevant. In order of preference: a runtime overlay
  for additive changes; otherwise route (B) and flash a modified
  `vendor_boot`/`dtbo` with a stock backup; a kernel patch (4.2) or the shim (4.1) only if a
  no-write path is mandatory.

## Sources

- Linux `Documentation/arch/arm64/booting.rst`, "Setup the device tree":
  https://docs.kernel.org/arch/arm64/booting.html
- `arch/arm64/kernel/setup.c` `setup_machine_fdt()`, `arch/arm/Kconfig`
  `ARM_APPENDED_DTB`, `drivers/of/Kconfig` `GENERIC_BUILTIN_DTB`, and
  `arch/{arm64,riscv,loongarch,mips,xtensa}/Kconfig` (torvalds/linux master,
  read 2026-10-17)
- "kbuild: add generic support for built-in boot DTBs" (LKML, Sept 2024)
- AOSP "Boot image header":
  https://source.android.com/docs/core/architecture/bootloader/boot-image-header
- Fastbootd-read thread findings on Pixel 7+ `fastboot boot` (AOSP recovery
  README, XDA "fastboot boot ignores ramdisks")
