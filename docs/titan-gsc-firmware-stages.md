# Titan M2 (GSC) firmware stages: EVT / proto vs retail

A comparison of the GSC firmware **variants that Google ships inside each Pixel
factory image**, and what actually distinguishes an engineering (EVT / proto)
image from the retail one. It is a companion to
`docs/titan-imei-persistence-hypothesis.md`, `docs/titan-ro-rw-disclosure.md`,
`docs/titan-flash-layout.md`, and `docs/titan-key-verification.md`.

## Each factory bundle ships three GSC images

Every Dauntless factory image inspected here carries three GSC firmware files
side by side:

- `d3m2.ec.bin` — the **retail** (production) firmware the phone actually runs.
- `evt.ec.bin` — an **EVT** (Engineering Validation Test) image.
- `proto11.ec.bin` — a **proto** (prototype) image.

All three are 1 MiB (`0x100000`) and carry the same four-image A/B layout
(RO_A / RW_A / RO_B / RW_B).

## What is the same across all three (and across devices)

Inspected with `pixel-restore titan --inspect` on six builds (komodo
`cp1a.260405.005`, `cp1a.260505.005`, `install-2026042100`, `beta-cp21.260330.008`,
`ad1a.240530.030`; husky `cp3a.260905.009`):

- **Signing keys are identical across proto / evt / retail, and across komodo
  (Pixel 9 Pro XL) and husky (Pixel 8 Pro):** every RO is signed by `894b7d29`
  (3070-bit, "development by Cr50 rule"), every RW by `68f379bd` (2048-bit,
  "production by Cr50 rule"), and every RO carries the same three-key accepted-RW
  table (`952b3485`, `d985131d`, `68f379bd`). Every header's key id matches its
  own modulus.
- **Consequence:** the signer key tells you **nothing** about build stage or even
  which device — a dev-rule RO alongside a prod-rule RW is the shared baseline,
  not a tamper signal, and not an "engineering image" signal.

## What differs: version and infomap

The build stage is encoded in the **RO major version** and the **RW
version/infomap**, not the key:

| Variant   | RO version | RW version | RW infomap (first word) |
|-----------|-----------|-----------|--------------------------|
| proto11   | 0.**0**.x | 0.3.1     | `fffffff8`               |
| evt       | 0.**1**.x | 0.3.2     | `fffffff8`               |
| d3m2 (retail) | 0.**2**.x | 0.2.2 | `fffffffc`               |

(The RO *minor* tracks the build — 109 / 111 / 114 across the set — but the RO
*major* is dead constant per stage: **proto = 0, evt = 1, retail = 2**, on every
build and both devices.)

Two discriminators fall out:

1. **RO major version = stage.** `0.0.*` proto, `0.1.*` evt, `0.2.*` retail.
2. **RW infomap and major.** Retail RW is `0.2.2` with infomap `fffffffc…`;
   both engineering images carry a **higher RW major (3)** and infomap
   `fffffff8…` (one additional low bit cleared: `…fc` = `…11111100`, `…f8` =
   `…11111000`). The infomap is the chip's rollback / board-flag bitmap.

## Forensic significance

- **"0.2.2" is the normal retail RW version.** A GSC reporting RW `0.2.2` is on
  retail firmware. The bricked `0.2.2` state in
  `docs/titan-imei-persistence-hypothesis.md` is therefore a **retail-versioned**
  chip, not an engineering image — the brick happened to a production GSC.
- **To detect an EVT/proto GSC on a retail device, check the RO major version
  and the RW infomap/major — not the signing key.** A production phone whose RO
  reports major `0` or `1`, or whose RW carries the `fffffff8` infomap / major 3,
  is running an engineering image where retail is expected. That is the anomaly
  to look for; the key id will look identical on both.

## Hypothesis: an engineering→retail rollback conflict (candidate for the ~1%)

*Clearly a hypothesis — the infomap semantics are not fully decoded here.*

The engineering RW images carry a **higher major (3)** than retail (2), and a
different infomap bit. If the chip's anti-rollback keys on the RW major or on
that infomap bit, then a part that has ever run an **engineering RW (major 3)**
could **refuse a retail RW (major 2)** on a later restore — the retail image is
"older" by the rollback rule. That would present exactly as *"the Titan does not
restore correctly,"* and a write that the chip rejects mid-flash is a plausible
source of the **SPI errors in the recovery process** reported for ~1% of devices
(see observation 3 in the persistence hypothesis). It would also recur, because
an RW-only `.rec` rescue cannot lower the rollback floor.

This is a benign-fault explanation that competes with (and is not exclusive to)
the compromise theory: a unit that shipped — or was ever serviced — with an
engineering GSC, then taken down a retail restore path, is a candidate for the
population-level failure without any attacker. It needs confirmation:

- Decode the infomap bits against the anti-rollback counter the bootloader
  reports (`fastboot oem gsc …`), and establish whether major/infomap gates
  rollback on Dauntless.
- On a failing device, capture the RW major/infomap the chip currently holds and
  the version of the image the restore tried to write; a major-3 chip rejecting a
  major-2 retail image confirms the mechanism.

## Honest limits

- Observed on six factory builds and two devices (komodo, husky); not an
  exhaustive survey, and all from public factory images, not a failing device.
- `KNOWN_KEYIDS` is observational, not an authoritative Google list.
- The infomap is treated as a rollback/flag bitmap; its exact bit semantics on
  Titan M2 are **not** decoded here, so the rollback-conflict mechanism is a
  reasoned hypothesis, not a demonstrated one.
- The ~1% / SPI-error figure it would explain is itself an unverified
  single-source insider claim (see the persistence hypothesis).

## Reproduce

    for v in proto11 evt d3m2; do
      pixel-restore titan --inspect <factory-dir>/$v.ec.bin
    done

Compare the RO major version, the RW version, and the RW infomap across the
three; the keys will be identical.
