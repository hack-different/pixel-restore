# Titan M flash layout and the Citadel -> Dauntless size bug

This documents the A/B flash layout of the Titan M family and the memory-layout
confusion that the Citadel (Titan M) -> Dauntless (Titan M2) flash-size jump
created. It is a companion to `docs/titan-key-verification.md`.

Honesty note: the layout *design* (A/B halves, RO at the front of each half) is
documented for the Chromebook EC / Cr50 and described in Rick's own
reverse-engineering write-up and the willem 39c3 talk. The specific byte
offsets below are either observed on Rick's komodo chip or reasoned from the
"cut the flash in half" rule; Google publishes no Dauntless layout table, and
Rick flags his own cross-chip analysis as "incomplete analysis and conjecture."

## The layout rule

Google carried over the Chromebook EC design: the chip's non-volatile flash is
cut in half, one half per update slot, with a read-only (write-protected)
region at the **start** of each half followed by the writable (RW) region:

    slot A (lower half)            slot B (upper half)
    +----------+----------------+  +----------+----------------+
    | RO_A     | RW_A           |  | RO_B     | RW_B           |
    +----------+----------------+  +----------+----------------+
    ^0                             ^ flash_size / 2

The two placements behave differently as flash size changes:

- **RO_A is pinned to offset 0** — the very start of the chip. It does not move
  when the flash size changes. (Rick: "RO_A is still at the start of the chip.")
- **RO_B is pinned to the start of the *second half*, i.e. offset
  `flash_size / 2`** — so its absolute address *does* depend on the total flash
  size. (Rick: "because RO_B is at the beginning of the half, it differed based
  on the size of the storage.")

## The size jump

- **Citadel (Titan M):** flash size S. RO_B sits at `S/2`.
- **Dauntless (Titan M2):** "non-volatile memory that is twice the size of the
  Citadel" (willem 39c3 talk). Flash size `2S`, so RO_B moves to `S` (the new
  midpoint), which is *double* the Citadel RO_B offset.

Using the figures from this project's notes (Citadel 512K, Dauntless 1024K):

| Region | Citadel (512K = 0x80000) | Dauntless (1024K = 0x100000) |
|--------|--------------------------|------------------------------|
| RO_A   | 0x00000                  | 0x00000                      |
| RW_A   | ~0x20000 .. 0x3FFFF      | ~0x20000 .. 0x7FFFF          |
| RO_B   | 0x40000 (= 512K/2)       | 0x80000 (= 1024K/2)          |
| RW_B   | above RO_B .. 0x7FFFF    | above RO_B .. 0xFFFFF         |

## The bug (confusing a chip about RO_* location)

Because RO_B's address is the midpoint, the two chips disagree about where RO_B
lives. If a Citadel is made to interpret a Dauntless region map (or Dauntless
firmware is flashed into a Citadel's A region), the Dauntless RO_B offset
(`0x80000`) is **past the Citadel's midpoint** and lands inside what the Citadel
treats as RW space. In Rick's words from his write-up, "the RO_B region can end
up placed into the Dauntless RW_A region" — a region that is read-only on one
chip becomes writable on the other. That is the "larger size made RO regions
into RW regions" effect: it is a cross-interpretation mismatch driven entirely
by the size-dependent `flash_size/2` placement of RO_B, while RO_A (pinned at 0)
stays consistent.

Google later closed this by having newer Dauntless builds **check an eFUSE that
blocks booting Dauntless code on a Citadel** (timing unclear; from Rick's
write-up).

## What the komodo firmware shows

The evidence is the layout of the full-flash image, not the `ro_base` field.
A komodo `d3m2.ec.bin` (Dauntless) is exactly 1 MiB (0x100000), and
`pixel-restore titan --inspect` finds its four images at these **file
offsets**:

| Image | File offset | `ro_base` |
| --- | --- | --- |
| RO_A | 0x00000 | 0x080000 |
| RW_A | 0x20000 | 0x0a0000 |
| RO_B | 0x80000 (= 1 MiB / 2) | 0x100000 |
| RW_B | 0xa0000 | 0x120000 |

RO_B starts exactly at the midpoint of the 1 MiB flash, and RO_A at 0. This
directly corroborates the `flash_size / 2` rule on the 1 MiB part. The same
layout holds in every komodo and husky `d3m2.ec.bin` and `evt.ec.bin` checked;
see `docs/titan-ro-key-table.md`.

`ro_base` is the image's **mapped address**: the flash appears at 0x80000 in the
chip's address space, so `ro_base` = file offset + 0x80000. An image reporting
`ro_base 0x80000` is therefore **RO_A**, not RO_B. An earlier version of this
note read `ro_base 0x80000` as RO_B at the midpoint; that was a misreading. The
midpoint conclusion stands, but on the file-offset evidence above. When reading
the chip with `fastboot oem gsc header`, RO_A reports `ro_base 0x80000` and RO_B
reports `0x100000`.

## Verdict

The mechanism is internally consistent, matches the documented A/B-halving
design, matches Rick's prior reverse-engineering write-up, and is directly
corroborated by the komodo firmware layout (RO_B at file offset `0x80000` =
flash_size/2 in the 1 MiB image). It is **not**
independently confirmed by any Google source, and the precise Citadel RW_A
extent that RO_B overlaps is from Rick's RE, not a published table. Treat it as
confirmed-in-mechanism, observed-offset-corroborated, Google-unconfirmed.

## Sources

- Rick Mark, "Google: From the Citadel to the Dauntless"
  (the flash-size / RO_B-into-RW_A analysis, eFUSE fix):
  https://blog.rickmark.me/untitled-2/
- willem, "Reverse engineering the Pixel TitanM2 firmware" (39c3, 2025)
  (Dauntless flash "twice the size," upper/lower A/B split):
  https://greptalks.ai/c/39c3-2025/39c3-068/
- Melotti et al., "Reversing and Fuzzing the Google Titan M Chip" (2021)
  (four images: two RO, two RW, A/B duplication):
  https://conand.me/publications/melotti-titanm-2021.pdf
- This project's decode of komodo and husky `d3m2.ec.bin` and `evt.ec.bin` (RO_B at
  file offset `0x80000` in the 1 MiB image; `ro_base` = offset + 0x80000).
