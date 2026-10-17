# Security disclosure: Titan M RO/RW region confusion from the Citadel→Dauntless flash-size change

**Status:** Confirmed; historical; vendor-mitigated.
**Component:** Google Titan M (Citadel, H1) and Titan M2 (Dauntless) security chips, as shipped in Google Pixel phones.
**Class:** Write-protect bypass / read-only-region integrity (CWE-1282-adjacent: boundary computed from a size constant that changed between parts).
**Researcher:** Rick Mark (https://blog.rickmark.me/untitled-2/).
**Write-up date:** 2026-10-17.
**CVE:** none assigned (historical, already mitigated by the vendor in later firmware).

---

## 1. Summary

The Titan M firmware flash is divided into two update slots (A and B) by cutting
the non-volatile memory in half, with a write-protected read-only (RO) region at
the **start of each half** followed by a writable (RW) region. The address of the
second slot's RO region (`RO_B`) is not a fixed constant — it is the flash
midpoint, `flash_size / 2`. `RO_A`, by contrast, is pinned to offset `0`.

When Google moved from Citadel (Titan M) to Dauntless (Titan M2) it **doubled the
flash size**. Because `RO_B` is anchored to the midpoint, its absolute address
doubled with it. This makes the two parts disagree about where `RO_B` lives: a
Citadel induced to interpret a Dauntless region map (for example, Dauntless
firmware written into a Citadel's A slot) resolves the Dauntless `RO_B` offset to
an address that the Citadel treats as **writable RW_A space**. A region that is
supposed to be read-only on one part is addressable as read-write on the other.
That breaks the immutability guarantee the RO region is supposed to provide.

The vendor subsequently closed this by having later Dauntless builds check an
eFUSE that refuses to boot Dauntless code on a Citadel part.

## 2. Affected products

- Titan M (Citadel / H1): Pixel 3 – Pixel 5 era.
- Titan M2 (Dauntless): Pixel 6 and later (the `d3m2`/Dauntless GSC).

The issue is a *cross-part* interpretation hazard between the two generations; it
is not a defect reachable on a single, correctly-matched part running its own
firmware under normal conditions.

## 3. Technical detail

### 3.1 The layout rule

Carried over from the Chromebook EC / Cr50 design:

    slot A (lower half)            slot B (upper half)
    +----------+----------------+  +----------+----------------+
    | RO_A     | RW_A           |  | RO_B     | RW_B           |
    +----------+----------------+  +----------+----------------+
    ^ 0x0                          ^ flash_size / 2

- `RO_A` is pinned to offset `0x0` and does not move with flash size.
- `RO_B` is pinned to `flash_size / 2`, so its absolute address scales with the
  total flash size.
- Each RO region is the write-protected front of its half; the RW region follows.

### 3.2 The size change

| Region | Citadel (512 KiB = 0x80000) | Dauntless (1024 KiB = 0x100000) |
|--------|-----------------------------|---------------------------------|
| RO_A   | 0x00000                     | 0x00000                         |
| RW_A   | ~0x20000 .. 0x3FFFF         | ~0x20000 .. 0x7FFFF             |
| RO_B   | 0x40000 (= 512K/2)          | 0x80000 (= 1024K/2)            |
| RW_B   | above RO_B .. 0x7FFFF       | above RO_B .. 0xFFFFF          |

Dauntless has "non-volatile memory that is twice the size of the Citadel"
(willem, 39c3 2025). Doubling the flash moved `RO_B` from `0x40000` to `0x80000`.

### 3.3 The confusion

The Dauntless `RO_B` offset `0x80000` is **past the Citadel's own midpoint
(`0x40000`)** and falls inside the span the Citadel treats as `RW_A`. So if a
Citadel is driven with a Dauntless region map — e.g. Dauntless firmware flashed
into the Citadel A slot — the bytes the Dauntless layout labels `RO_B` are, to
the Citadel, ordinary writable `RW_A` content. The write-protection that should
cover an RO region is not applied, because each part computes the RO/RW boundary
from its *own* `flash_size/2`, and those midpoints differ. `RO_A` (pinned at `0`)
stays consistent across both parts and is unaffected.

### 3.4 Impact

The RO regions hold the immutable, write-protected root of the chip's own boot
chain (the RO bootloader that verifies and launches the RW firmware). An
attacker able to make the chip resolve an RO region into writable space could
undermine that write-protect/immutability property — the security property the
split RO/RW design exists to provide. Exploitability depends on being able to
induce the cross-part interpretation (physical/firmware-flashing access to the
chip), so this is a local, high-privilege-precondition integrity issue, not a
remote one.

## 4. Evidence and confirmation status

- **Layout design** (A/B halves, RO at the front of each half): documented for
  the Chromebook EC / Cr50 and described in the willem 39c3 talk and the Melotti
  reversing paper (which confirms four images — two RO, two RW — with A/B
  duplication).
- **`RO_B` at the midpoint on the 1 MiB part**: confirmed from the Dauntless
  firmware layout. A komodo `d3m2.ec.bin` is exactly 1 MiB, and its four images
  sit at file offsets `0x0` (RO_A), `0x20000` (RW_A), `0x80000` (RO_B, exactly
  `1024K / 2`) and `0xa0000` (RW_B). Every komodo and husky `d3m2.ec.bin` and
  `evt.ec.bin` checked has this layout (`pixel-restore titan --inspect`).
  - *Correction:* an earlier version of this section cited an image reporting
    `ro_base 0x00080000` as RO_B at the midpoint. `ro_base` is the image's
    mapped address, with the flash at `0x80000` in the chip's address space,
    so `ro_base` = file offset + `0x80000`. `ro_base 0x80000` is **RO_A**, and
    RO_B reports `0x100000`. The midpoint conclusion is unchanged, but it rests
    on the file offsets above, not on that `ro_base` value.
  - *Not shown by this:* that `RO_B` moved with the flash size from Citadel to
    Dauntless. That needs the same layout read from a Citadel image (expected
    RO_B at `0x40000` in 512 KiB), which this project has not checked.
- **The cross-part RO_B→RW_A overlap and the eFUSE fix**: from the researcher's
  own reverse-engineering write-up. These specifics are not published in a Google
  advisory; the precise Citadel `RW_A` extent that `RO_B` overlaps comes from the
  RE, not a vendor table.

Overall: the mechanism is confirmed and internally consistent, `RO_B` at the
midpoint of the 1 MiB Dauntless flash is corroborated by the firmware layout,
the Citadel side of the size change is not yet checked here, and the vendor mitigation
(eFUSE) is documented by the researcher. It is **not** independently confirmed by
a Google-published advisory.

## 5. Mitigation

Later Dauntless firmware builds check an eFUSE that blocks booting Dauntless code
on a Citadel part, which prevents the cross-part interpretation that triggers the
overlap. (Exact build/version introducing the check is not established here.)

For anyone handling these parts: do not flash firmware of one generation onto the
other, and verify the chip generation (`gsc-flavor`) and the per-image `ro_base`
with `pixel-restore titan --inspect` before writing anything. On Dauntless,
RO_A reports `ro_base 0x80000` and RO_B `0x100000`.

## 6. Timeline

- Pre-2021: Citadel (Titan M) ships on Qualcomm-based Pixels.
- 2021+: Dauntless (Titan M2) ships on Tensor Pixels with doubled flash.
- (date per researcher): cross-part RO/RW confusion identified; vendor adds eFUSE
  generation check.
- 2026-10-17: written up as this disclosure from the researcher's analysis plus
  confirming device observations in this project.

## 7. References

- Rick Mark, "Google: From the Citadel to the Dauntless": https://blog.rickmark.me/untitled-2/
- willem, "Reverse engineering the Pixel TitanM2 firmware" (39c3, 2025): https://greptalks.ai/c/39c3-2025/39c3-068/
- Melotti et al., "Reversing and Fuzzing the Google Titan M Chip" (2021): https://conand.me/publications/melotti-titanm-2021.pdf
- Companion layout reference: `docs/titan-flash-layout.md`
- Key verification and the two roots of trust: `docs/titan-key-verification.md`

---

*This advisory documents a historical, vendor-mitigated issue on the author's own
devices, written for defensive and educational purposes. Offsets marked observed
are from this project's captures; items attributed to the researcher's write-up
are RE analysis, not vendor-confirmed.*
