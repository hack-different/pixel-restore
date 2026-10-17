# Build, boot, and enumerate — trusted-DTB kernel for komodo

No device writes. Everything is RAM-boot via `fastboot boot`; a bad image
reboots cleanly and changes nothing on flash.

## 0. One-time: capture the baseline (for comparison, not as the source)

Boot the current fastbootd-read kernel (stock tree) once and keep the phone's
real tree for diffing — **not** as the base we build from (it can't show
omissions):

```
# from our fastbootd / recovery shell, or a RAM-booted kernel with it:
cat /sys/firmware/fdt > /tmp/phone-stock.dtb      # exact bootloader tree
dtc -I dtb -O dts /tmp/phone-stock.dtb > phone-stock.dts
```

Pull it host-side. Diff against our trusted `.dts` to see exactly which
nodes/reservations we added or removed.

## 1. Build the trusted DTB (host-side, no device)

```
cd trusted-dtb/tools
./build_trusted_dtb.sh /path/to/zumapro-kernel-src ./out
# -> ./out/pixel-trusted.dtb   (check size < 2 MiB)
cp ./out/pixel-trusted.dtb /path/to/zumapro-kernel-src/arch/arm64/boot/
```

Start with `komodo_overrides.dtsi` containing **one** change (e.g. one SPI
controller to `okay`). Confirm the labels/reg against the SoC dtsi first.

## 2. Build the kernel with the patch

```
cd /path/to/zumapro-kernel-src
git apply /path/to/trusted-dtb/patches/0001-arm64-use-embedded-trusted-dtb.patch
# adjust context if the ACK tree's setup.c differs
# enable the option:
echo 'CONFIG_PIXEL_TRUSTED_DTB=y' >> <your defconfig fragment>
# build per the fastbootd-read Docker build, producing our Image + modules
```

Keep the built-in initramfs (our fastbootd/recovery rootfs + AD1A-matched
`.ko`) exactly as route (A) in the fastbootd-read plan — this patch is
orthogonal to it.

## 3. RAM-boot

```
# with the phone in bootloader fastboot:
fastboot boot our-boot.img      # kernel = our Image, ramdisk ignored by Pixel 7+
```

Watch `fastboot oem dmesg` (our capture) for:
- `pixel-dtb: using embedded trusted device tree` — the swap took.
- any `pixel-dtb:` crit line — blob bad or /chosen copy failed (fail-safe
  keeps the bootloader tree; topology changes won't apply).
- the target controller probing (e.g. `spi ... : ...` registering) and the
  child device binding.
- faults: `Unhandled fault`, `SError`, SMMU fault, or an immediate
  secure-reboot → that region/device is hardware-fenced; the DT change alone
  won't reach it.

## 4. Enumerate what became visible

From our fastbootd / shell:

```
ls /sys/bus/spi/devices/         # did the child appear?
ls /sys/class/*/ /sys/bus/*/devices/   # new controllers/devices
dmesg | grep -iE 'spi|i2c|probe|fault|smmu|denied'
cat /sys/firmware/fdt | dtc -I dtb -O dts - | less   # the LIVE tree is now ours
```

For a newly-exposed memory region, confirm it is actually mapped/readable
before trusting any dump:

```
cat /proc/iomem | grep -i <region>     # is it claimed/mapped now?
# read only through a proper interface (a bound driver, or /dev/mem IFF
# CONFIG_STRICT_DEVMEM allows and the region is in iomem) — a read that
# faults means hardware still fences it.
```

## pKVM / pvmfw decision (make this before step 1)

The bootloader loads pvmfw/pKVM into a `/reserved-memory` carveout, and route
(A)'s built-in initramfs depends on pKVM being intact. Two valid configs:

- **Keep pKVM:** leave the pvmfw carveout node in the trusted tree. Safe
  default; the hypervisor still interposes.
- **Disable pKVM:** turn protected-KVM off in the defconfig *and* then you may
  reclaim its carveout. For a forensic build this also widens EL1 visibility
  (nothing between the kernel and the hardware). Likely the preferred combo
  here.

What you must not do: delete the pvmfw carveout while pKVM is still enabled —
pKVM init faults or its region is stomped and the phone won't boot. The
override template calls this out; decide explicitly.

## 5. Iterate

One change at a time. If a controller won't probe, it is almost always a
missing pinctrl/clock/power-domain the stock tree also disabled — copy that
state from the SoC dtsi into the override and rebuild. If a region faults, it
is fenced below the DT; stop expecting the DT to open it and note it as
firmware-protected.

## What a clean result looks like

- `pixel-dtb: using embedded trusted device tree` in dmesg.
- The target node present in the live `/sys/firmware/fdt` **and** a bound
  driver / populated `/sys` entry.
- A readback that returns data without a fault.

Only then is the device genuinely one the stock tree was hiding with no
hardware fence behind it.
