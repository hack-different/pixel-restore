# Verifying the Titan M signing key

The Titan M (GSC) is the security chip in recent Pixels. On a Pixel 9 Pro XL
(komodo) it reports as a Titan M2 (Dauntless), `gsc-flavor` `d3m2`. This note
is about reading the chip's firmware **signing key** and checking it against
an expected value. It is separate from the Android Verified Boot (AVB) root of
trust, which is covered at the end because the two are easy to confuse.

Everything here is read-only. Nothing in `pixel-restore titan --verify-keys`
sends a state-changing command to the chip.

## Two different keys

For how the chip's four images (RO_A/RW_A/RO_B/RW_B) are laid out in flash, and
the Citadel->Dauntless flash-size bug that could turn an RO region into an RW
one, see `docs/titan-flash-layout.md`.

There are two distinct "root of trust" keys on the phone, at two layers.

1. **The Titan M firmware signing key.** Each Titan M image (RO_A, RO_B, RW_A,
   RW_B) is an Cr50-style signed blob whose header carries a `keyid`. This is
   the RSA key that signed the chip's own firmware. The bootloader exposes it
   with `fastboot oem gsc header <image>` as `hdr.keyid`. This is what
   `--verify-keys` reads.

2. **The AVB / verified-boot key.** This is the key that signs `vbmeta` and the
   OS, which ABL checks when the phone boots. On an unlocked phone a user can
   replace it with `fastboot flash avb_custom_key`. The bootloader reports that
   key's SHA-256 digest to the Titan at boot via the nugget `SetBootState`
   `public_key` field, and the Titan binds Weaver and keystore to it. It is a
   different key from the firmware signing key and is **not** what
   `--verify-keys` checks. See "The AVB key" below.

## The Cr50 production-vs-development rule

Google's key-generation convention (from the Cr50 source, `G_SIGNED_FOR_PROD`)
is that a **production** key id has bit D2 set and a **development** key id has
it clear. It is a convention enforced at key-generation time, not a hardware
guarantee, and it is inferred to hold for the Titan M2, not documented.

So, by the rule alone:

- `68f379bd` -> bit 2 set -> production.
- `894b7d29` -> bit 2 clear -> development.

On Rick's komodo (observed 2026-10-17): RO_A is `894b7d29` (development by the
rule) while RW_A and RW_B are `68f379bd` (production). A development-keyed RO
booting a production-keyed RW is unusual; it is why we added this check. It may
indicate a development or engineering part, or that the ROM accepts a range of
keys matching a hardware mask. This is not yet confirmed.

## Reading and checking the key

Report the chip's keys (no expected value, read-only):

    pixel-restore titan --verify-keys

That prints each image's key id with the dev/prod rule and whether the id is in
the tool's small table of keys seen on genuine hardware, and flags the dev-RO /
prod-RW case.

Check against a value you already trust:

    pixel-restore titan --verify-keys --expect-keyid 68f379bd

The check passes only if every RW image on the chip is signed with one of the
`--expect-keyid` values. It exits non-zero on a mismatch, so it is usable in a
script.

Check against a genuine factory image (the strongest form, because "expected"
comes from a real Google-signed image rather than a value typed by hand):

    pixel-restore titan --verify-keys --firmware <komodo factory zip or d3m2.bin>

The tool extracts the RW image's key id from that firmware and uses it as the
expected value. Point `--firmware` at a factory image you trust.

## The known-key table

`--verify-keys` labels each id against a small built-in table of key ids we
have actually seen on genuine hardware (currently the two komodo values above).
This is **not** an authoritative Google list; it only lets the tool say "this
matches what we saw before" rather than "this is genuine". To build real
confidence, compare against a genuine factory image with `--firmware`, or add
ids extracted from several genuine images to the table.

## The AVB key (the other root of trust)

If what you actually want is to verify or change the **verified-boot** key, that
is the AVB custom key, not the Titan firmware key:

- Set it (unlocked bootloader, physical confirmation): `avbtool
  extract_public_key --key key.pem --output pkmd.bin` then `fastboot flash
  avb_custom_key pkmd.bin`. Clear it with `fastboot erase avb_custom_key`.
- The digest the Titan stores for it is `SHA256(pkmd.bin)`, which is the
  `public_key` the bootloader sends in `SetBootState`.
- Changing it changes the verified-boot state, which **wipes user data** and
  rebinds Weaver and keystore. It is not a recovery step.
- It re-roots only the OS layer (what ABL verifies). It cannot re-root ABL,
  LDFW or BL1 themselves; that root is fused into the Tensor SoC.

Reading the AVB key back from the chip (`GetBootInfo` `boot_key`) is a runtime
keymaster/keystore call, available on a booted device, not from the bootloader.
`--verify-keys` therefore does not cover it; it checks the Titan firmware key,
which the bootloader does expose.
