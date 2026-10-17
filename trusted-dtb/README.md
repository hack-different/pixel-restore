# Trusted device tree for zuma / komodo

Goal: boot our own kernel on komodo (Pixel 9 Pro XL, Tensor G4 = zuma pro)
with a **device tree we author from trusted source**, not the one the phone's
`vendor_boot` supplies. The phone's DTB is treated as adversarial: it may
omit a controller (a SPI bus and the chip on it), mark a device `disabled`,
or carve out `/reserved-memory` regions so Linux never maps them. A tree we
build ourselves from the zumapro SoC sources exposes the full hardware and
lets us decide what is reserved.

This is a no-write, RAM-boot path: `fastboot boot` takes only our kernel, and
our kernel ignores the phone's DTB for hardware topology. Nothing is flashed.

## Why the phone's own tree can't be trusted — and why dumping it doesn't help

You might think: RAM-boot once with the stock tree, dump `/sys/firmware/fdt`,
and edit from there. That recovers every bootloader fixup, but it **cannot
recover an omission**. If the stock tree never declared a SPI controller, the
kernel never probed it, so the dumped tree lacks it too. Omission is invisible
from inside the running system. The authoritative hardware list has to come
from outside the phone: the zumapro SoC device-tree sources and the Zuma TRM.

## What comes from where

| Fact | Source | Why |
|---|---|---|
| Hardware topology: every SPI/I2C/UART controller, their `reg`/IRQ, child devices, `status` | **our trusted tree** (zumapro `.dtsi` from source + our overrides) | this is what the phone may be lying about |
| `/reserved-memory` carveouts | **our trusted tree** | we decide what Linux may map |
| Actual RAM size (`/memory`) | bootloader DT (x0) | genuinely dynamic; already in memblock before the swap |
| `/chosen`: bootargs, bootconfig, initrd range, `kaslr-seed`, `rng-seed`, `androidboot.*` | bootloader DT (x0), grafted into our tree | can't be known ahead of boot; later code reads them from the live tree |

## The three pieces in this directory

1. `patches/0001-arm64-use-embedded-trusted-dtb.patch` — kernel patch. Links
   our blob into the image and, early in `setup_arch()`, repoints
   `initial_boot_params` at it after copying `/chosen` from the bootloader
   tree. Build-time option `CONFIG_PIXEL_TRUSTED_DTB`.
2. `tools/build_trusted_dtb.sh` — assembles the trusted DTB: compiles the
   zumapro base `.dts` from the kernel source with our overrides layered on,
   emitting `pixel-trusted.dtb` for the patch to `.incbin`.
3. `tools/komodo_overrides.dtsi` — the overrides: re-enable / re-add the
   controllers and child devices we believe stock suppressed, and drop the
   `/reserved-memory` regions we want readable. **Edit this** to target a
   specific node.

Plus `docs/enumerate-runbook.md`: build, RAM-boot, and test region by region
what actually becomes visible.

## The hard limit (read this before trusting a result)

A device tree tells Linux what to **try**. It does not grant access the
silicon withholds. On Tensor the real gate on the protected partitions
(modem, persist, Titan-backed storage) and on secure RAM is the
**SMMU/MPU and GSA/Titan firmware**, not the DT. Declaring a fenced region in
our tree lets the kernel attempt it; the hardware can still fault or
secure-reboot. So:

- A device the stock DT merely **omitted**, with no hardware fence → becomes
  visible the moment our tree declares it. This is the gap we close.
- A region **fenced in hardware/firmware** → our DT does not open it; that
  needs a different lever (and for the IMEI/modem partitions the practical
  path remains reading the *partition* via fastbootd, not mapping secure RAM).

Every newly-exposed region must be confirmed empirically (the runbook), not
assumed readable because the node now exists.

## Status

These files are a reviewed **starting point**, written against torvalds v6.1
`arch/arm64/kernel/setup.c` (komodo runs an ACK `android14-6.1` derivative;
the swap-point logic is the same but line numbers differ). The Fastbootd
thread owns the actual kernel build and should treat the patch as a diff to
adapt and compile, not as drop-in. Nothing here has been built or run against
hardware yet.

Reviewed-against-boot-path notes (confirm in the ACK tree before building):
- **The swap seam is correct only if `early_init_fdt_scan_reserved_mem()` is
  still called from inside `arm64_memblock_init()`.** Google sometimes
  reorders this; if it moved earlier than our swap, reserved-memory would be
  scanned from the bootloader tree instead of ours and the swap must move
  with it.
- `kaslr-seed` is a non-issue: KASLR consumes the bootloader FDT in `head.S`
  long before `setup_arch()`, and we preserve `/chosen` anyway.
- The blob being `.incbin`'d (kernel mapping, not early fixmap) is why the
  2 MB fixmap DTB limit doesn't apply — komodo's ~3 MB tree needs no
  `FDT_MAX_SIZE` change.
