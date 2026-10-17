# The Titan M2 RO's table of RW keys

Each Titan M image starts with a Cr50 `SignedHeader`. RO_A and RO_B carry, in
their code, a **table of the RSA public keys they accept for RW_A/RW_B**. So
the decision about which RW images can boot is split across two layers:

- The **boot ROM** checks each RO image against the RO signing key
  (`894b7d29` on komodo), which is fused into the chip.
- **RO** checks each RW image against its own embedded key table.

`pixel-restore titan --inspect <d3m2.ec.bin>` decodes both. It needs no phone.
This complements `docs/titan-key-verification.md`, which reads key *ids* from
the chip.

## What a signed header carries

| Offset | Field | Notes |
| --- | --- | --- |
| 420 | `keyid` | Cr50's key id: the Montgomery constant −n⁻¹ mod 2³² of the signer's modulus |
| 424 | `key[96]` | The signer's RSA modulus, 96 little-endian words. A 2048-bit key leaves the top 32 words zero. |
| 808 | `image_size` | The signed length, header included |

So a key id is not just a label: it can be **recomputed from the modulus**.
`--inspect` does this for every header and reports "key id matches its
modulus". Each key also gets a SHA-256 fingerprint of its modulus (big-endian,
leading zeros dropped), which identifies the key itself rather than its 32-bit
id.

## The RW key table inside RO

Each entry is a Cr50 RSA key struct of 98 little-endian words:

    n0inv (= key id) | n[96] (modulus) | e (65537)

In every `d3m2.ec.bin` and `evt.ec.bin` checked (komodo cp1a.260405.005,
cp1a.260505.005, install-2026042100, beta cp21.260330.008; husky
cp3a.260905.009), both RO_A and RO_B carry the same three entries, at
RO-relative offsets 0x16780, 0x16908 and 0x16a90:

| Key id | Size | Cr50 rule | Used by shipped RW? |
| --- | --- | --- | --- |
| `952b3485` | 2048-bit, e=65537 | production | no RW seen signed with it |
| `d985131d` | 2048-bit, e=65537 | production | no RW seen signed with it |
| `68f379bd` | 2048-bit, e=65537 | production | yes: RW_A and RW_B in every image checked |

The table sits inside the RO's signed range (`image_size` 0x17000), so it is
covered by the RO signature and changes only when RO does. The RO signing key
`894b7d29` (≈3072-bit, "development" by the Cr50 bit-2 rule) does not appear in
the table: RW keys and RO keys are separate sets.

This explains the observation in `titan-key-verification.md` of a
development-keyed RO running a production-keyed RW. The bit-2 rule describes
how each key was generated. Which RW keys RO accepts is set by this table, and
all three entries are production-by-rule.

How the table is found: `--inspect` scans each image body for a word that is
the n0inv of the odd word after it, followed by a modulus of 1024 bits or more.
A random match is about one in 2³² per word, and every hit so far has
recomputed to its stored key id. The scan does not depend on the table being
at a fixed offset.

## What it prints

    RO_A at 0x0: signed by 894b7d29 (3070-bit, sha256 c15f3c6fa5744887), key id matches its modulus; ...
      accepts RW signed by 3 keys:
        952b3485 at 0x16780 (2048-bit, e=65537, sha256 237b9331c11cb707); ...
        d985131d at 0x16908 (2048-bit, e=65537, sha256 a23a0cb787d5d21f); ...
        68f379bd at 0x16a90 (2048-bit, e=65537, sha256 fb0d1f976c7f3729); ...
    RW_A at 0x20000: signed by 68f379bd (2048-bit, sha256 fb0d1f976c7f3729), key id matches its modulus; ...
    ...
    RW_A key 68f379bd is accepted by RO_A, RO_B

An RW image counts as accepted only when both its key id and its modulus
fingerprint match a table entry. A file whose RW key no RO accepts is reported
as `NOT in any RO's key table here`.

`pixel-restore titan --verify-keys --firmware <file>` also prints the RO table
from that firmware, and for each of the chip's RW key ids whether it is in the
table. The chip itself reports only key ids through `oem gsc header`, not the
table, so the table always comes from a firmware file.

## `ro_base` is a mapped address, not a flash offset

In a full 1 MiB `d3m2.ec.bin` the four images sit at file offsets 0x0 (RO_A),
0x20000 (RW_A), 0x80000 (RO_B) and 0xa0000 (RW_B). Their `ro_base` fields read
0x80000, 0xa0000, 0x100000 and 0x120000. So `ro_base` is the image's address
with the flash mapped at 0x80000, and **`ro_base 0x80000` is RO_A**, not RO_B.
RO_B is at `ro_base 0x100000`. The RO_B-at-the-midpoint layout is still true,
but the evidence is RO_B's file offset 0x80000, not `ro_base`.
`docs/titan-flash-layout.md` and `docs/titan-ro-rw-disclosure.md` have been
corrected to say so.

## Not established

- **The chip's own table.** That the chip's RO enforces this table is inferred
  from the Cr50 loader design, which looks up RW keys by key id in an RO-held
  list. The table is decoded from firmware files, not read back from the chip.
- **The other two keys.** Why `952b3485` and `d985131d` are listed is not
  known. They could be for rotation, other boards, or older RW. No Google source
  describes them.
