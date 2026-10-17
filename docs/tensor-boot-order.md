# The Tensor boot order and what each image is

This maps the boot images on a Google Tensor Pixel (gs101 / Pixel 6 and the
later gs201 / Zuma parts, including komodo) to their place in the boot chain
and their job. It is a companion to `docs/pixel-boot-failure-map.md`, which
uses this chain to locate where a phone goes unbootable.

Honesty note: Google does not publish a Tensor boot spec. The ARM naming
(BL1/BL2/BL31/BL32/BL33 and their exception levels) is documented by ARM
Trusted Firmware and is solid. The Tensor-specific image names (pbl, gsa,
ldfw, tzsw, abl) and their order are taken from the Exynos USB-boot tooling,
the eshard reverse-engineering blog, and this project's own ROM-recovery logs.
Where a mapping is reasoned rather than documented it is marked (inferred).
ABL and LDFW internals are closed; the GSA and TZSW are closed TrustZone code.

## The ARM Trusted Firmware frame (documented)

Tensor is an ARM SoC and follows the ARM Trusted Firmware-A stage naming.
From the TF-A firmware-design reference:

| TF-A name | Role | Exception level |
|-----------|------|-----------------|
| `AP_BL1`  | AP Boot ROM — the first code, runs after cold boot on the primary CPU | EL3 (boot) |
| `AP_BL2`  | AP RAM Firmware — loads and verifies the next images | S-EL1 |
| `AP_BL31` | EL3 Runtime Firmware — the Secure Monitor, installed resident at EL3 | EL3 |
| `AP_BL32` | Secure-EL1 Payload — the TEE / TrustZone OS (e.g. OP-TEE, or here Trusty) | S-EL1 |
| `AP_BL33` | Normal World Firmware — the normal-world bootloader (here ABL) | EL2/EL1 |

This confirms the reading Rick gave in the thread: the `BL` numbers are stage
numbers, and **BL31 is the EL3 runtime (the secure monitor)**, not "BL3 point
1". BL32 is the secure payload at Secure-EL1. The eshard blog, reasoning from
the same ARM spec, guessed the Pixel 6 chain as "PBL -> BL1 -> BL2 -> BL31
with TZSW and ABL a bit later," and quotes the TF-A comments that BL1 is "only
called by the primary cpu after a cold boot" and BL2 "runs entirely in S-EL1."

## The Tensor images, in boot order

Names as they appear in factory `bootloader-*.img` FBPK packs and the Exynos
USB-boot (EUB) transfer logs: `pbl`, `bl1`, `bl2`, `abl`, `bl31`, `tzsw`,
`gsa`, `ldfw` (the transfer log lists them; it does not itself state the run
order). Putting them in execution order:

1. **PBL / irom — the mask ROM (burned into the SoC).** This is the primary
   boot loader fused into silicon; it cannot be reflashed and is the hardware
   root of trust for the AP chain. It verifies and loads BL1. In Exynos USB
   download mode it is the PBL that speaks the EUB protocol: the phone
   enumerates as USB `18d1:4f00` "Pixel ROM Recovery" and the PBL requests
   `bl1` from the host, and reports faults as `irom_booting_failure:` with a
   register dump. (Rick: "PBL is the burned-in ROM" — matches; irom is the ROM
   that reports the failure.)
2. **BL1 — AP boot ROM (flashable first stage).** Loaded and verified by the
   PBL. Its header carries a check byte (observed at offset 0x410) and a
   rollback version; a bad or too-old BL1 drops the phone back to ROM
   recovery (`bl1 header fail`). BL1 is the stage the USB-bootable recovery
   packs replace to get a dead phone talking again. Runs in the EL3 boot flow.
3. **BL2 — trusted boot firmware.** Loads and verifies the later images.
   Runs at Secure-EL1 (per TF-A). On Tensor this is the stage that pulls in
   the secure-world and GSA pieces.
4. **LDFW — Loadable Device Firmware (secure).** A secure-world firmware blob.
   In the EUB logs `ldfw` and `bl31` carry a different header class than the
   rest (check field `02040000` vs `00040000` for bl1/pbl/bl2/abl/gsa/tzsw),
   which is consistent with them being the EL3/secure-monitor-class images
   (inferred from the header delta, not documented).
5. **GSA — Google Security Applet (secure-world / TrustZone applet).** Rick's
   reading: "GSA is the Google security applet that runs in the TZ." It is a
   Trusty/TEE applet, part of the secure payload family (BL32 class). See the
   GSA↔GSC section below for how it relates to the Titan M.
6. **BL31 — EL3 runtime / Secure Monitor.** Installed resident at EL3; it
   stays running under everything and brokers Secure Monitor Calls (SMC)
   between the normal and secure worlds for the life of the boot. (Rick:
   "31 is likely EL3 first applet" — correct, it is the EL3 runtime.)
7. **TZSW — TrustZone software (the TEE OS).** Rick: "we assume TZSW is the
   TrustZone software" — yes, this is the Secure-EL1 TEE operating system
   (BL32 class, Trusty on Pixel) that hosts GSA and the other trusted apps.
8. **ABL — Android Bootloader (normal world, BL33).** The last bootloader
   stage and the one the user sees: it draws the bootloader/fastboot UI,
   handles `fastboot` commands, reads `misc`/bootloader-message, and performs
   Android Verified Boot (AVB) on `vbmeta` and the OS. It runs in the normal
   world and is re-rootable only at the OS layer (`avb_custom_key`); it cannot
   re-root itself, BL1 or LDFW, whose root is fused into the SoC.

After ABL the chain hands off to the Linux kernel (under the pKVM hypervisor
on recent Pixels) and Android user space.

The Pixel 8 (husky) ROM-recovery enumeration seen in this project walks these
in two passes — `BL1 DPM GCF GCFB EPBL EPBB BL2 BL2B`, a re-enumerate, then
`GSA1 GSAF ABL ABLB TZSW TZSB LDFW LDFB BL31 BL3B`, then fastboot — i.e. the
A/B copies of the same stages (`*B`/`*F` being the second slot/variant), which
matches the order above.

## Where root of trust lives in this chain

- **AP chain (PBL -> BL1 -> BL2 -> BL31/LDFW -> ABL):** each stage verifies the
  next by signature, anchored in the fused PBL/irom root. This is the chain
  that the anti-rollback counters (`ap-ar-ns`, `ap-ar-s`, `gsa-bl1-ar`,
  `gsa-fw-ar`) protect; crossing a rollback bump closes the door on older
  images permanently. The Titan M does **not** verify AP code.
- **OS layer (ABL -> kernel/Android):** ABL runs AVB against the verified-boot
  key (built-in, or an `avb_custom_key` on an unlocked phone). This is the only
  layer a user can re-root, and doing so wipes userdata.
- **Titan M (GSC):** a physically separate chip with its own fused root; its
  ROM verifies its own RW firmware by signature. It is a consumer of the AP's
  verified-boot result, pushed to it by the bootloader, not a verifier of AP
  code. (See `docs/titan-key-verification.md`.)

## Does GSA (the TZ applet) mediate access to the GSC (Titan M)?

Short answer from the public reversing literature: **not for the main data
path.** The strongest primary source (Melotti et al., "Reversing and Fuzzing
the Google Titan M Chip", 2021, on Titan M / Citadel) traces the normal-world
path as:

    app -> Android Keystore daemon -> HAL -> citadeld (vndbinder IPC)
         -> /dev/citadel0 kernel driver -> libnos_datagram / libnos_transport
         -> SPI -> the chip

The transport is SPI; messages are Protobuf-encoded over the Nugget ("nos")
protocol. That paper describes **no** TrustZone component or GSA sitting in the
wire between Android and the chip — the StrongBox/keymaster path reaches the
GSC through `citadeld` in the normal world, and the GSC enforces its own access
control (rate limiting, and it only honours certain commands by state).

So the relationship is better described as **cooperating peers, not a proxy**:

- The **bootloader** (not GSA) holds the privileged channel: the root-of-trust
  push (`SetRootOfTrust` / `SetBootState`, "Only callable by the Bootloader" in
  the nugget keymaster proto) and most AVB/`fastboot oem gsc` commands are
  bootloader-mode only.
- The **TEE / GSA** produces the signed auth tokens (Gatekeeper
  `HardwareAuthToken`, Weaver) that the GSC then verifies — so the TEE and the
  GSC trust each other's signatures, but the GSA is not a mandatory broker that
  every GSC message passes through.
- Caveat (inferred): the exact Tensor (gs101/gs201) wiring is not public, and
  Melotti's work is on the earlier Titan M/Citadel. It is plausible some
  Tensor-era secure operations are brokered through Trusty, but there is no
  public source showing GSA as a required gateway for GSC access. One
  lower-quality secondary source claims a "mediated serial interface" between
  the Titan M2 and a Tensor security core, but it is internally inconsistent
  and should not be relied on.

## The GSC's physical channels: SPI and UART

The chip exposes (at least) two interfaces, which matters for both the boot
consult and for rescue:

- **SPI — the data bus to the AP.** This is the production link the boot
  stages and Android use: Protobuf-framed nos/nugget messages ride SPI, and on
  the Android side the path is `citadeld -> /dev/citadel0 -> libnos_transport
  -> SPI` (above). It is how the bootloader reaches the GSC to push the
  root-of-trust and how StrongBox/Weaver/keymaster reach it at runtime.
- **UART — the console / rescue channel.** The Cr50/GSC family exposes a debug
  UART carrying the AP, EC and GSC consoles, reached physically with a SuzyQ
  (CCD) cable. This is the channel the **UART rescue mode** uses: it accepts an
  Intel-HEX RW image to recover a chip whose RW is wedged (the same rescue the
  `pixel-restore titan` flow drives through `fastboot stage` + `oem gsc
  rescue`/`reset` when the phone still reaches fastboot). CCD Open (to unlock
  the richer consoles) needs physical presence plus dev mode or a CCD password.

(Rick: "expect the GSC to have SPI and UART channels" — yes, SPI for the data
path and UART for the console/rescue path.)

## History: Citadel over XBL (pre-Tensor Pixels)

The predecessor chip, **Citadel** (the original Titan M, Pixel 3 through 5),
rode on Qualcomm SoCs. There the real boot stage name **XBL** (Qualcomm's
eXtensible Boot Loader) applies, and XBL consulted Citadel over SPI during
boot. That is the "XBL consults the Titan M over SPI" pattern. Tensor replaced
the Qualcomm boot stack, so **there is no XBL on a Tensor Pixel** — the
equivalent stages are PBL/BL1/BL2/BL31/LDFW/ABL above — but the design carried
over: a boot stage still consults the GSC over SPI and pushes the verified-boot
result to it. So "XBL" is the correct term for the Citadel/Qualcomm era and a
loose synonym (for the AP-side boot code) on Tensor.

## Sources

- ARM Trusted Firmware-A, Firmware Design (stage names + exception levels):
  https://trustedfirmware-a.readthedocs.io/en/latest/design/firmware-design.html
- eshard, "Pixel 6: Booting up" (speculative chain, TF-A quotes):
  https://www.eshard.com/blog/pixel6-bootloader
- JoshuaDoes/tensor-usbdl issue #24 (EUB protocol, image names, BL1 header,
  irom failure): https://github.com/JoshuaDoes/tensor-usbdl/issues/24
- Melotti et al., "Reversing and Fuzzing the Google Titan M Chip" (2021)
  (SPI/nos transport, citadeld path, bootloader-only commands):
  https://conand.me/publications/melotti-titanm-2021.pdf
- This project's own ROM-recovery stage logs (husky two-pass enumeration).
