# Forensic kernel options for the komodo recovery/fastbootd build

What kernel-side changes are worth building into the self-contained komodo
kernel (Pixel 9 Pro XL, Tensor G4, AD1A.240530.030, kernel 6.1.x) that the
fastbootd work boots with a built-in initramfs (`fastbootd-read/`), so that it
can also take a **trustworthy forensic read** of the phone for the Titan M2 /
blank-IMEI investigation (`docs/titan-imei-persistence-hypothesis.md`,
`docs/titan-validation-trust-model.md`).

The rule for everything here is **observe, don't alter**: nothing below writes
to UFS, the modem, the GSC, fuses or any rollback counter, and anything that
could change state by accident is called out with how the build blocks it.

Status labels: **verified** = checked against a cited source; **inferred** =
reasoned, not checked on komodo. Nothing here has been run on the phone yet.

## Ranking

| # | Option | Forensic value | Build cost | Needs custom kernel? |
|---|--------|----------------|------------|----------------------|
| 1 | Write-blocker posture (block RO, no modem/Trusty storage, filtered UFS passthrough) | Prerequisite: keeps every other read admissible | Low | Partly (the passthrough filter) |
| 2 | Whole-LUN UFS reads + on-device SHA-256, UFS descriptors, RPMB write counter | High | Low-medium | No for reads; yes for the bsg filter |
| 3 | Read-only GSC client over the kernel SPI driver, allow-list enforced in-kernel | High | Medium | Yes (the allow-list) |
| 4 | `NUGGET_PARAM_READ32` probe of the GSC flash window (a full RO dump if honoured) | Decisive if it works; probably refused | Low once #3 exists | Yes |
| 5 | pstore/ramoops, bootconfig, DT `chosen`, debug-snapshot regions | Medium | Low | No |
| 6 | SoC and board identity (chip-id, board revision) for the EVT question | Medium, cheap | Very low | No |
| 7 | Enumerate every SPI bus and chip-select; dump only true SPI-NOR | Low-medium | Low to enumerate, high per device | No to enumerate |
| 8 | `/proc/kcore` and `/dev/mem` (no `/dev/kmem` exists on 6.1) | Low-medium | Low | Yes |
| 9 | Verify AVB/verity offline; sign our own modules | Medium (keeps the signal) | Low | Yes |
| 10 | In-kernel hashing | Low over what fastbootd already does | Low | Yes |

The recommended first build is **1 + 2 + 3 + 5 + 6** with the on-screen hash
display from §10. Run **4** only on Rick's explicit go.

## 1. Write-blocker posture (do this first)

A forensic boot that writes is worse than no boot, because it destroys the
"does a clean repair stick?" evidence (§4.5 of the hypothesis doc).

- **Block layer read-only at device add.** Patch the block layer so every disk
  and partition is created read-only (the approach of the Linux write-blocker
  patch, which adds a read-only check at a common low-level point in the block
  driver: [msuhanov/Linux-write-blocker](https://github.com/msuhanov/Linux-write-blocker),
  summarized at [forensics.wiki](https://forensics.wiki/linux_write_blocker/)).
  Setting `BLKROSET` from `/init` is a weaker fallback because anything that
  runs before it can write.
- **SCSI passthrough bypasses the block RO flag.** On UFS, `SG_IO` and the UFS
  BSG node send raw commands under the block layer. Build without
  `CONFIG_CHR_DEV_SG`, and patch the UFS BSG handler to pass only *read*
  query opcodes (read descriptor, read attribute, read flag). This matters
  most: UFS has **one-way** writes, such as `fPermanentWPEn` (permanent write
  protect) and the descriptor writes that change `bBootLunEn`. (Inferred from
  the JEDEC UFS model; check the opcode list in the 6.1 `ufs_bsg.c` before
  patching.)
- **Never mount anything read-write.** If a filesystem must be looked at, mount
  ext4 with `ro,noload` (a plain `ro` mount still replays the journal) and f2fs
  with `ro,norecovery`. Prefer not to mount at all: image and hash instead.
- **Keep the modem down.** Do not load the CP interface drivers (`cpif` and
  friends). A booted S5400 modem owns `efs`/`modem_userdata` and can rewrite NV,
  which is exactly the evidence we are trying to preserve.
- **No Trusty storage daemon.** Do not run `storageproxyd`; it writes RPMB and
  the TEE's persistent files on the AP's behalf.
- **No StrongBox/Weaver/KeyMint clients.** They send state-changing GSC
  commands. Only the allow-listed client in §3 talks to the GSC.

## 2. Whole-device UFS reads, hashed on the phone

The read-enabled fastbootd already reads and hashes partitions by name
(`fastboot oem sha256 <partition>`, `fastbootd-read/docs/design.md`). Rick
asked for whole-device reads, which add what per-partition reads miss:

- **Every LUN, not just named partitions.** On Tensor the UFS exposes several
  logical units: the main LUN (`userdata`, `super`, …), the two boot LUNs that
  hold the bootloader chain (`bl1`, `pbl`, `bl2`, `abl`, …), and LUNs for
  `persist`/`efs`/`devinfo`. Hashing each `/dev/block/sdX` whole covers the GPT
  headers, any **slack between partitions**, and anything hidden past the last
  GPT entry, which a by-name read never sees. (LUN layout inferred; enumerate
  `/sys/class/scsi_disk` on the phone.)
- **Skip the bulk of `userdata`.** It is file-based-encrypted and dominates the
  size. Hash every other LUN whole, and on the main LUN hash every region
  except the `userdata` extent.
- **UFS descriptors and attributes (read-only).** Through the filtered BSG node
  (or `ufs-utils`), record the device descriptor (vendor, product, firmware
  revision), the per-LU unit descriptors including **write-protect state**, the
  health descriptor (wear life), and `bBootLunEn` (which boot LUN the ROM boots).
  A boot LUN that is unexpectedly writable or switched is a finding.
- **RPMB write counter.** Reading the RPMB "read write counter" needs no key.
  The counter only goes up, so it shows how many authenticated writes the TEE
  has made. A jump across a period when the phone was off is a lead. (JEDEC
  RPMB model; inferred for this UFS part.)

Komodo's storage is a Samsung `KLUDG4UHGC-B0E1` (128 GB, UFS 3.1) per the
[iFixit Pixel 9 Pro XL chip ID](https://zh.ifixit.com/Guide/Google+Pixel+9+Pro+XL+Chip+ID/176196?lang=en).

## 3. Read-only GSC client over SPI

The Titan M2 sits on SPI to the AP and speaks the Nugget protocol
(`docs/tensor-boot-order.md`). The Pixel kernel's driver for it is a vendor
module (`gsc-spi.ko` in the Pixel 6 module list,
[bluejay-kernel modules.load](https://android.googlesource.com/device/google/bluejay-kernel/+/b1e9cf269/modules.load);
the komodo name is inferred to be the same). Our own initramfs can talk to it
directly, without `citadeld` or any Android HAL.

**The SPI link is a message bus, not a memory.** The GSC's flash is on the
chip itself, so there is no external flash part to dump over SPI. The chip's
firmware decides what to answer. The Nugget app commands are public in
[`app_nugget.h`](https://android.googlesource.com/platform/external/nos/host/generic/+/e2d28b1/nugget/include/app_nugget.h)
(verified):

| Allow (read) | Value | Why it helps |
|--------------|-------|--------------|
| `NUGGET_PARAM_VERSION` / `LONG_VERSION` | 0x0000 / 0x0007 | running image, all four images described |
| `NUGGET_PARAM_HEADER_RO_A` … `RW_B` | 0x0008–0x000b | the signed headers, decoded by `pixel-restore titan --inspect` |
| `NUGGET_PARAM_REPO_SNAPSHOT` | 0x000c | source revisions of the running image, to compare with factory |
| `NUGGET_PARAM_DEVICE_ID` | 0x0006 | chip identity |
| `NUGGET_PARAM_BOARD_ID` (get form only) | 0x000f | board ID, relevant to the EVT question |
| `NUGGET_PARAM_SJTAG_ALLOW` | 0x0013 | whether secure JTAG is allowed, an engineering-unit tell |
| `NUGGET_PARAM_CONSOLE` (no command) | 0xF002 | the chip's own recent console output |
| `NUGGET_PARAM_CYCLES_SINCE_BOOT`, `GET_LOW_POWER_STATS` | 0x0100, 0x0200 | harmless context |

| Block | Value | Why |
|-------|-------|-----|
| `FLASH_BLOCK`, `ENABLE_UPDATE`, `CHANGE_UPDATE_PASSWORD` | 0x0001, 0x0003, 0x0004 | write or arm firmware |
| `REBOOT`, `NUKE_FROM_ORBIT`, `AP_IS_REBOOTING` | 0x0002, 0x0005, 0x0011 | reboot or erase secrets |
| `STORAGE_WRITE`, `WRITE32`, `SET_SLEEP_MODE` | 0x0014, 0xF001, 0xF004 | change state |
| `AP_UART_PASSTHRU`, `RDD_CFG`, `SJTAG_USER_CONSENT` (set forms) | 0x000d, 0x000e, 0x0012 | change debug configuration |
| `GET_EVENT_RECORD` | 0x0010 | "retrieve one pending event record": reading **consumes** it, so it is blocked unless Rick decides the records are worth spending |
| `CONSOLE` with a command string, `SELFTEST`, `MODULE_TEST` | 0xF002, 0x0101, 0xF003 | run code on the chip |
| Every other app (AVB, keymaster, Weaver, …) | | all of the AVB rollback and lock commands live there |

**Enforce the allow-list in the kernel driver**, not only in the userspace
tool. Then a bug or a substituted binary in our own image still cannot send an
update or the one-way AVB rollback command. Patch `gsc-spi` so it rejects any
transaction whose app ID and param are not on the read list.

**Why this beats `fastboot oem gsc` alone.** ABL already reads the headers.
Asking the chip the same questions over a different AP software path, and
getting identical bytes, rules out an ABL-side fabrication. The console output
and repo snapshot are new data that ABL does not expose.

**Limit.** A compromised RO firmware answers every in-band query, so it can lie
here too. This path catches a lying *host* or bootloader, not a lying chip.

## 4. `READ32` over the GSC flash window (the RO dump, if it is honoured)

`NUGGET_PARAM_READ32` (0xF000) is documented as "Read a 32-bit value from
memory", with "NO sanity checking on this AT ALL", under the header's
"bringup / debug functions" section (verified). If the production Dauntless
build still answers it, reading 4 bytes at a time across the mapped flash
window gives **RO_A and RO_B byte for byte**. That is the §4.4 "device RO vs
factory RO" datum, without SuzyQ or board-level access. The window is
0x80000–0x17FFFF: flash appears at 0x80000 in the chip's address space,
`ro_base` = file offset + 0x80000 (`docs/titan-flash-layout.md`).

- **Probably refused** on a retail build (inferred: debug commands are usually
  compiled out). One probe at 0x80000 (the RO_A header, already known from
  factory) answers the question.
- **Risk.** With no address checking, a bad address can fault the chip and
  reset it. Probe only inside the known window, word-aligned, and only with
  Rick's explicit go, since a GSC reset on a chip suspected of a bad RO is not
  free.
- If it answers, compare the dump to the factory `d3m2.ec.bin` off-host, per
  the trust model. A compromised RO could still serve false bytes, so this
  still does not replace an out-of-host chip read.

## 5. pstore, last kernel log and boot reasons

- Enable `CONFIG_PSTORE_RAM` against the ramoops region in the phone's own DTB.
  At probe, ramoops keeps the previous boot's buffers as
  `/sys/fs/pstore/console-ramoops-0`, `dmesg-ramoops-*` and `pmsg-ramoops-0`.
  **Copy them before anything else**, because our boot's crashes would write
  the same region next time.
- Capture `/proc/bootconfig`, `/proc/cmdline` and `/sys/firmware/fdt` (the DTB
  as ABL handed it over, with the `androidboot.*` values ABL inserted:
  verified boot state, boot reason, slot, serial, revision).
- Pixel's debug-snapshot / `dbgcore` reserved regions hold last-reset reasons
  (`debug-snapshot-*.ko` and `dbgcore-dump.ko` in the module list above). Read
  them through their drivers' read paths, not by writing their control nodes.
- This serves the **restore-failure** hypothesis: a defect that "recurs
  identically" should leave the same panic or reset signature boot after boot.
- These work on the stock kernel too (the vendor_boot-flash fallback), since
  they are reads of existing nodes.

## 6. SoC and board identity (the EVT question)

- `gs-chipid` (in the module list) exposes the SoC product ID, revision and
  unique ID under sysfs. Record them. The exact paths need checking on komodo.
- The board revision ABL passes in bootconfig/DT (`androidboot.revision` and
  similar), set against `fastboot getvar hw-revision`, says directly whether
  this is a production board or an engineering one. Two independent sources
  that agree are stronger than either alone.
- **Fuses and rollback counters cannot be read from the kernel.** Tensor's
  secure OTP is readable only from the secure world (inferred). The real
  counters come from ABL (`getvar ap-ar-ns`, `ap-ar-s`, `gsa-bl1-ar`,
  `gsa-fw-ar`), as today. The GSC `BOARD_ID` and `SJTAG_ALLOW` reads in §3 are
  the chip-side complement.

## 7. Every SPI device, not just the GSC

Rick's point stands: the GSC is not the only SPI device. From the iFixit chip
ID, the SPI-attached candidates on komodo are:

| Part | Function | Readable flash worth dumping? |
|------|----------|-------------------------------|
| Google H1D3M2 | Titan M2 (the marking matches the `d3m2` flavor) | on-die; see §3–4 |
| ST54L | NFC controller + secure element | eSE flash is secure by design; record the ATR and firmware version only |
| Qorvo QM35725 | UWB transceiver | internal firmware flash behind a vendor bootloader; protocol-specific, low priority |
| fingerprint sensor, touch controller | not identified by iFixit | firmware is usually loaded from `/vendor/firmware`, so it is already covered by the UFS hashes in §2 (inferred) |

iFixit identifies **no discrete NOR flash or EEPROM** on the board, but its
list is not exhaustive (the fingerprint and touch parts are missing from it).
So the build should list what is really there instead of guessing:

- At boot, record every `/sys/bus/spi/devices/*` entry with its `modalias`,
  bus and chip-select, and the DT `compatible` of every SPI controller child.
  That is the authoritative list for this exact board.
- **SPI has no generic "read memory".** Each device speaks its own protocol,
  and the same bytes can mean "erase" on another part. Dump only nodes whose
  DT `compatible` is `jedec,spi-nor` (JEDEC read-ID `0x9F`, then read `0x03`),
  and send nothing to unclaimed chip-selects.
- The offline equivalent: decompile the AD1A DTB from `vendor_boot`/`dtbo`
  with `dtc` on a host that fetched the factory ZIP itself, and list the SPI
  nodes. (`dl.google.com` is blocked from the cloud session that wrote this
  doc, so this list has not been produced yet.)

## 8. Kernel memory: kmem, kcore, /dev/mem

- **`/dev/kmem` does not exist on 6.1.** It was removed in Linux 5.13
  ("drivers/char: remove /dev/kmem for good",
  [LKML](https://lkml.iu.edu/2103.3/00433.html)). Nothing to enable.
- `/proc/kcore` (`CONFIG_PROC_KCORE`) and `/dev/mem` (`CONFIG_DEVMEM` without
  `CONFIG_STRICT_DEVMEM`, lockdown LSM off) can be enabled in our kernel.
  Turning them on weakens nothing on the phone: they only expose the RAM of the
  kernel we booted.
- **What they can show.** Our own kernel's memory, which mostly checks that the
  image we built is the one that is running. Also the DT reserved-memory regions
  that may still hold the previous boot's data: ramoops, the bootloader log
  region, debug-snapshot. Whether ABL scrubs DRAM before `fastboot boot` is
  unknown, so this is worth one test, no more.
- **Risk.** Reading a secure carve-out (TrustZone, GSA, modem) through
  `/dev/mem` raises a bus error and reboots the phone. Restrict the reader to
  ranges the DTB marks as non-secure reserved memory.

## 9. AVB, dm-verity and module signing: keep the signal, don't mask it

- **Don't mount `system`/`vendor` through dm-verity.** Verify them instead. Hash
  each partition on the phone (§2) and check it against the vbmeta descriptors
  and Google's factory images off-host (`avbtool`-style verification). That
  measures integrity without depending on any runtime verity report.
- **Module signing.** AD1A vendor modules are signed with a build key we do not
  have. Rather than turning enforcement off, **build every module we load from
  the same AD1A source, sign them with our own key, and keep
  `CONFIG_MODULE_SIG_FORCE`**. Only our modules load, and a substituted module
  fails loudly. Log the SHA-256 of every loaded `.ko`.
- Our kernel boots with the phone in the unlocked/orange state it is already
  in. `fastboot boot` writes nothing on an unlocked Pixel, so this changes no
  lock, rollback or GSC state.

## 10. In-kernel hashing, and the screen as an out-of-host channel

Hashing in kernel space adds little: the userspace in our initramfs is our own
code too, and `fastboot oem sha256` already hashes on the phone. The real gain
is **where the result is shown**. Under Rick's rule ("assume any session on my
computers may lie"), a hash that travels back over USB can be rewritten by the
host. A hash **drawn on the phone's own screen** by the recovery UI, then
photographed and compared by hand with a value computed off-host, never passes
through his computers. Add a "show hashes" screen listing each LUN/partition
SHA-256 and the GSC header digests.

**The kernel image itself must be trusted too.** A lying host could swap the
image we `fastboot boot`. Build it reproducibly on two independent hosts,
compare the SHA-256 by the same off-host channel, and boot from a third
machine or check the hash of the file actually sent.

## Which option serves which hypothesis

| Hypothesis | Strongest options | What the result would show |
|------------|-------------------|----------------------------|
| Attack (non-factory RO or altered RW-key table) | 4, then 3 | RO bytes differ from factory; headers and repo snapshot differ |
| ~1% restore failure (corrupt or partial RO) | 5, 3 (console), 4 | the same reset signature every boot; GSC console errors near the RO_B boundary |
| EVT unit | 6, 3 (board ID, SJTAG allowed), 2 (UFS descriptors) | engineering board revision; SJTAG allowed; matching EVT firmware |

## Honest limits

- None of this is tested on komodo yet, and every option rides on the custom
  kernel actually booting (fastbootd route A). Options 2 (reads), 5 and 6 also
  work from the stock kernel via the vendor_boot fallback.
- Every in-band read of the GSC (§3–4) is answered by the chip's own firmware.
  Only an out-of-host read (CCD/UART, or board-level access) is independent of
  a compromised RO.
- Module names come from the Pixel 6 list, and the SPI part list comes from
  iFixit. Both need confirming against the komodo DTB and `/sys` on the phone.
