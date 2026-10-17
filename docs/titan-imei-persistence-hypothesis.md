# Hypothesis: a persistent GSC-rooted attack behind recurring IMEI loss

**Status:** Hypothesis + confirmation methodology. **Not confirmed.** This
document states a working theory and the exact device forensics that would
confirm or refute it. It does not claim an attack has been proven; the evidence
in hand is circumstantial, and the decisive artifacts (a dump of the *affected*
device's GSC and modem regions) have not yet been collected.

**Companion documents:** `docs/titan-ro-rw-disclosure.md` (the RO/RW
write-protect bypass), `docs/titan-flash-layout.md`, `docs/titan-key-verification.md`,
`docs/modem-repair.md` (IMEI/EFS/persist repair), `docs/pixel-boot-failure-map.md`.

---

## 1. The observations

1. **A bricked GSC state on a production device.** A Dauntless (Titan M2) GSC
   was observed in a bricked `0.2.2` state on a *production* phone (not a lab or
   leaked image), recoverable only by a `.rec` **rescue** image. The researcher's
   working assumption, adopted here, is that this bricked state was the result of
   an **advanced attack**, not accidental corruption.
2. **IMEI loss recurring more than once.** The phone's IMEI / modem identity has
   been lost repeatedly — not a single corruption event, but a pattern that
   returns after repair.
3. **A reported ~1% population-level Titan-restore failure.** An insider is
   reported to have claimed that roughly **1% of Pixels do not restore their
   Titan correctly, leading to SPI errors in the recovery process.** This is an
   *unverified single-source claim* — recorded here as a lead, not a fact — but
   if even approximately true it means the failure mode is **not unique to this
   device**; it is a population-level phenomenon. The working premise adopted
   here (per the researcher) is that this device's issue is **not wholly unique**
   and is plausibly an instance of that broader class.

The question this document addresses: **are these the same class of problem — is
the IMEI recurrence the visible symptom of a GSC-level fault (whether a
persistent compromise or a systemic restore/SPI failure) that the ~1% claim says
is already happening at population scale?**

## 2. Why recurrence is the signal

A one-time IMEI loss is unremarkable: EFS/`devinfo`/`persist` corruption,
a bad flash, or a mismatched radio can all blank an IMEI once, and
`docs/modem-repair.md` exists precisely to repair that. What is *not* mundane is
**recurrence after a correct repair**. If the IMEI returns to a bad state after
`devinfo` is restored to known-good values and both EFS copies are intact, then
something with high privilege is **re-writing modem identity after the repair** —
i.e. persistence. `docs/modem-repair.md` already warns against the failure mode
of "repeatedly writing" `devinfo` when check-in keeps failing: that warning is a
symptom of exactly this — the repair does not stick because the cause is upstream
of `devinfo`.

## 3. The mechanism that would link the two

The GSC (Titan M2) is the hardware root of trust: it participates in the AP boot
handshake and in attestation (`docs/titan-key-verification.md`,
`docs/pixel-boot-failure-map.md` §2). A compromise of the GSC's **immutable RO
region** is therefore positioned to persist across AP reflashes and to influence
anything the GSC gates. Two facts already documented in this repo make a
persistent, rescue-surviving compromise plausible:

- **The RO/RW write-protect bypass** (`docs/titan-ro-rw-disclosure.md`): because
  `RO_B` is anchored to `flash_size/2` and that midpoint changed between Citadel
  (0x40000) and Dauntless (0x80000), a cross-generation interpretation can make a
  region that should be write-protected RO addressable as writable RW. That is a
  path to writing the supposedly-immutable RO region.
- **Rescue only rewrites RW.** The `titan` command's rescue path refuses an image
  that holds only RO headers — *"the image holds only RO headers; rescue writes
  RW, refusing"* (`src/titan.rs`), and a `.rec` is a single RW image
  (`src/titan.rs`: "Rescue writes one RW image, which is what a .rec is."). So a
  `.rec` rescue of a bricked GSC **replaces RW and leaves RO untouched.**

Put together: **if an attack plants code or a poisoned configuration in the GSC
RO region (enabled by the RO/RW bypass), then a `.rec` rescue cannot remove it** —
it only rewrites RW. The GSC comes back up with the compromised RO still in
place, re-asserts itself, and the symptoms (brick, or modem-identity corruption)
**recur**. That is a coherent explanation for both "it bricked again" and "the
IMEI went again after I fixed it."

This is a hypothesis, not a demonstrated exploit chain. It explains the observed
recurrence; it has not been proven on the device.

## 3a. Population framing: probably not unique, and a widened differential

Observation 3 (the reported ~1% Titan-restore failure with SPI errors in
recovery) changes how to read this. If a GSC-restore failure is already
happening to on the order of 1% of devices, then a single phone that bricks its
Titan and loses its modem identity is most likely **an instance of a known
class, not a bespoke one-off**. That cuts both ways and widens the differential
to (at least) two causes that produce the *same* recurrence symptom:

- **(A) Persistent compromise** in the GSC RO region (§3): rescue rewrites RW
  only, so the fault survives and recurs. This is the "advanced attack" premise.
- **(B) A systemic restore/SPI fault.** "SPI errors in the recovery process"
  points at the physical layer: the GSC's firmware lives in **SPI NOR flash**,
  and the restore/rescue path reads and writes that flash. A defect in that path
  — e.g. mishandling the RO/RW boundary during a write, or a flash-geometry
  assumption that is wrong for a fraction of parts (the very `flash_size/2`
  RO_B-placement sensitivity documented in `docs/titan-ro-rw-disclosure.md`) —
  would leave a slice of devices with a mis-restored or bricked GSC, and a
  mis-restored RO would then keep re-manifesting after each RW-only rescue, i.e.
  **recur the same way (A) does**.

Crucially, (A) and (B) are **not mutually exclusive and can look identical from
the outside**: the RO/RW layout confusion that enables (A) as an attack is also
a plausible root for (B) as an accidental ~1% failure. So the population claim
does not by itself decide attack-vs-defect — but it does say the mechanism is
probably **shared and reproducible**, which is good news for confirmation: a
reproducible ~1% failure can be studied across many devices, not just this one.

It also means **"the keys look standard" is a baseline check that passed, not an
all-clear** (see §4.1): the keyid pair matching the factory image rules out *that
particular* signature, but the thing that actually differs under either (A) or
(B) is elsewhere — an RW image that does not byte-match the genuine `68f379bd`,
`infomap`/rollback-bit deltas, SPI-level read/write errors, or IMEI/efs/persist
cleared with no matching reflash. Do not let a passed baseline check mask an
anomaly in those.

## 4. What would CONFIRM it (device forensics)

All of the following use tooling already in this repo (`pixel-restore titan
--inspect`, the key-table decode in `src/titan.rs`, `pixel-restore modem`, and
`tools/pixel_gsc_fetch.py` + the factory `firmware-index.json`).

### 4.1 Dump and decode the device's GSC headers
Capture all four images from the live chip and decode them:

    fastboot oem gsc header ro_a ; ro_b ; rw_a ; rw_b   # per image
    pixel-restore titan --inspect <each image>

For each image check:

- **Signer key id.** Is each image's `keyid` in the known-key table
  (`KNOWN_KEYIDS` in `src/titan.rs`: komodo RO `0x894b7d29`, komodo RW
  `0x68f379bd`, plus the two RW-table keys `0x952b3485` / `0xd985131d`)? A key id
  **not in the table at all** is the lead to chase. Note that **a dev-rule RO
  alongside a prod-rule RW is normal** on genuine Titan M hardware — the komodo
  RO (`0x894b7d29`) is dev-by-rule and the komodo RW (`0x68f379bd`) is
  prod-by-rule on a legitimate device — so the dev/prod rule (`keyid & 4`) by
  itself is **not** a tamper signal. What matters is whether the id is a
  recognized genuine key for this part.
- **Header-vs-modulus consistency.** `key_report` recomputes the signer id from
  the image's own RSA modulus and flags a mismatch ("*but its modulus gives
  …*"). A header that claims one key id while its modulus yields another is a
  strong tamper signal.
- **RO accepted-RW-key table.** Does each RO's embedded table of accepted RW
  keys match a clean factory RO's table, or has an **extra/foreign RW key** been
  added (which would let attacker-signed RW pass verification)?

### 4.2 Check the RO/RW offsets for the confusion in play
- Decode each image's `ro_base` and compare against the **clean factory image**
  for the same build and against the layout described in
  `docs/titan-ro-rw-disclosure.md` / `docs/titan-flash-layout.md` (which are the
  authoritative reference for what `ro_base` means and where each region sits —
  do not re-derive the offsets here). The finding is a device whose region
  placement diverges from the clean factory baseline — e.g. an RO image mapped
  into the RW span, or an RW image where RO should be — which is the RO/RW
  confusion of the disclosure observed on the device.

### 4.3 Check the version for a rollback
- Decode `epoch.major.minor`. The observed brick was `0.2.2`. Compare against the
  GSC version the phone's **factory build** actually ships (pull it with
  `tools/pixel_gsc_fetch.py` using the `firmware-index.json` entry for that exact
  build and `sha256`). A GSC running **below** the factory version for its build
  is a rollback — anti-rollback is supposed to forbid it, so a successful
  downgrade is itself evidence of a bypass.

### 4.4 Byte-compare the device RO against clean factory RO
- Fetch the clean `<flavor>.bin` / `.rec` for the exact build (now possible
  without downloading whole images, via `pixel_gsc_fetch` byte-range reads) and
  diff the **RO regions** against the device dump. Clean RO that matches factory
  byte-for-byte refutes an RO compromise; any divergence in RO is the headline
  finding.

### 4.5 The persistence test (the decisive one)
1. Back up EFS/`devinfo`/`persist`/`cpsha` (`pixel-restore modem backup`).
2. Do a *correct* repair: known-good `devinfo`, both EFS copies intact, matching
   radio; if the GSC is bricked, rescue RW.
3. Reboot, use the phone, re-run `pixel-restore modem diagnose --backup …`.
4. **If the IMEI/EFS re-corrupts after a clean repair and reboot, persistence is
   confirmed** — and because rescue only touched RW, a surviving compromise in RO
   is the leading explanation. If it stays fixed, the recurrence was operator
   error or a transient, not a persistent attack.

### 4.6 EFS / persist forensics
- Compare `efs` vs `efs_backup` (should be identical); confirm `cpsha` in
  `persist`; look for IMEI-shaped numbers that are *not* the device's own, or
  writes to modem NV that no repair step performed. `pixel-restore modem
  diagnose` and `read-partitions` already surface most of this.

### 4.7 SPI-error and population signal (tests observation 3)
- **Capture the recovery-time SPI error.** When a Titan restore/rescue fails,
  record the exact error and the GSC console/UART output around it
  (`docs/tensor-boot-order.md` describes the UART rescue channel). An SPI
  read/write/verify error names *where* in the flash the restore faulted — and
  whether that offset sits at or near the `flash_size/2` RO_B boundary is the
  direct link to the layout issue.
- **Correlate across devices/builds.** If the ~1% claim is real, the failures
  should cluster — by build, by GSC version, by codename, or by the Citadel↔
  Dauntless flash-size transition. The new `tools/pixel_gsc_fetch.py` +
  `firmware-index.json` make it cheap to pull the factory GSC for many builds and
  compare RO offsets/versions/keys at scale, to see whether the failing cohort
  shares a flash-geometry or version trait.
- A reproducible SPI fault at a predictable offset supports cause **(B)** (a
  systemic restore/layout defect); a clean SPI path with RO *content* divergence
  supports cause **(A)** (compromise). Both still recur through an RW-only rescue.

## 5. What would REFUTE it

- Device RO images: key ids are recognized genuine keys for this part (a
  dev-rule RO with a prod-rule RW is fine, not a flag), header matches modulus,
  accepted-RW-key tables identical to factory, `ro_base` offsets as expected,
  version at-or-above the factory build's GSC, and RO bytes matching the clean
  factory RO. **All clean ⇒ no RO compromise.**
- IMEI stays correct after one proper repair across multiple reboots and
  check-ins. **No recurrence ⇒ mundane corruption, not a persistent fault.**

If both hold, the bricked `0.2.2` was most likely a one-off corruption or a
failed update, and the IMEI losses were independent repair/flash accidents.
Note the asymmetry introduced by §3a: clean RO content refutes cause **(A)**
(compromise) but does **not** by itself refute cause **(B)** (a systemic
restore/SPI defect), since (B) can leave the RO content genuine while still
mis-restoring or faulting during recovery. Refuting the *attack* is not the same
as refuting the *fault*.

## 6. Honest limits

- The GSC dumps available in this project so far are **clean factory images**
  (`d3m2.ec.bin`, all 1 MiB), not a dump of the *affected* device. Nothing here
  has inspected the compromised chip.
- `KNOWN_KEYIDS` is explicitly "observed, not an authoritative Google list," so a
  key id "not in the table" means *unverified*, not *proven malicious* — it is a
  lead to chase, not a conviction.
- The RO/RW→persistence chain is reasoned from this repo's own reverse
  engineering and the rescue-writes-RW behavior; it is internally consistent but
  **not demonstrated** as an executed exploit.
- "Advanced attack" is the adopted premise for the brick, per the researcher; it
  is not independently established in this document.
- The ~1% Titan-restore / SPI-error figure is an **unverified, single-source
  insider claim** (observation 3). It is used here only to justify treating the
  issue as non-unique and to motivate population-level tests; no rate is
  confirmed, and the number should not be quoted as established.

## 7. Next step

Collect §4.1–§4.7 from the affected device (or its full backups). The single
most decisive datum is §4.5 (does a clean repair stick?) corroborated by §4.4
(does the device RO match clean factory RO?); §4.7 (the recovery-time SPI error
and whether failures cluster) is what tests the ~1% population claim and
separates cause (A) from (B). Until those exist, this remains a well-formed
hypothesis with a clear test, not a finding.

---

*Prepared for defensive forensics on the researcher's own device. Analysis only;
no exploit, and nothing here is submitted to any external party.*
