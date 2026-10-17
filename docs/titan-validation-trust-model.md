# Trust model for Titan/GSC validation: assume the analysis host may lie

Every GSC conclusion in this repo so far — "retail RW is `0.2.2`", "dev-RO +
prod-RW is the factory baseline", the EVT/proto/retail stage table in
`docs/titan-gsc-firmware-stages.md`, the forensics in
`docs/titan-imei-persistence-hypothesis.md` — was computed by running
`pixel-restore titan --inspect` on local `.bin` files **on one machine**. This
document states the uncomfortable consequence and the only sound defense.

## The threat

**Assume the machine doing the validation may be compromised and may lie.** A
hostile host can defeat our analysis in at least three ways, none of which the
analysis itself can detect from the inside:

1. **Tamper the input.** Edit the `.bin` before we read it — for example
   **stitch an EVT image into a retail `d3m2.ec.bin`** (or vice versa) so the
   stage markers no longer mean what we think, deliberately making the
   conclusions wrong or ambiguous.
2. **Tamper the tool.** Modify the `pixel-restore` binary (or the Rust source,
   or the Python helper) so `--inspect` prints whatever the attacker wants,
   regardless of the bytes.
3. **Fake the output.** Intercept and rewrite the tool's output, or the shell
   results the agent sees, so even an honest tool and honest file are reported
   dishonestly.

Because the agent reading the results runs *on the same host*, it cannot
bootstrap trust from that host: a machine that lies about the firmware can lie
about the hash of the firmware, about the tool, and about the verification of
the tool. **No single-host check is sufficient.**

## Defenses, weakest to strongest

### 1. Internal cross-field consistency (weak; host-dependent)
Genuine images pair their fields in fixed ways (from
`docs/titan-gsc-firmware-stages.md`): proto = RO `0.0.x` / RW `0.3.1` /
infomap `fffffff8`; evt = RO `0.1.x` / RW `0.3.2` / `fffffff8`; retail =
RO `0.2.x` / RW `0.2.2` / `fffffffc`. A combination that matches **no** genuine
variant — e.g. a retail-major RO (`0.2.x`) next to an engineering-major RW
(`0.3.x`), which is exactly what a clumsy EVT-into-retail stitch would produce —
is evidence of tampering. Useful, but it only helps **if the tool output is
honest**, so it does not survive threats 2–3.

### 2. Independent input integrity (medium)
Anchor every `.bin` to Google's published artifacts rather than trusting the
local copy: `tools/pixel_gsc_fetch.py` reads the GSC firmware **directly from
`dl.google.com` by byte range** and records SHA-256s, and the factory ZIPs carry
Google-published SHA-256s (captured in the firmware index). A stitched or edited
local file will not match the independently fetched bytes. **Caveat:** if the
fetch and comparison run on the suspect host, that host can lie about the fetch
too — so this is only as strong as the machine it runs on.

### 3. Cross-machine reproduction (strong; the actual trust anchor)
Run the validation on **two or more independent machines in different trust
domains** — e.g. a second physical computer and/or an ephemeral cloud instance —
each **fetching its own inputs independently from Google**, then **diff the
results**:

- the SHA-256 of each GSC image, and
- the full decoded `--inspect` output (per-image key id, key-matches-modulus,
  accepted-RW-key table, `ro_base`, version, infomap).

If independent hosts **agree**, the conclusion is trustworthy; the agreement,
not any one host's word, is the anchor. If they **disagree**, at least one host
is lying, and that disagreement is itself the finding. This is the only defense
that survives a fully hostile analysis host (threats 1–3), short of a compromise
of Google's distribution or of every machine at once.

## What this requires of the tooling (and where we already are)

- **Deterministic, portable validation.** `pixel-restore titan` is
  standard-library Rust and `tools/pixel_gsc_fetch.py` is standard-library
  Python 3.9+ — both already run standalone on another machine with no project
  tree or network except the Google fetch. Keep it that way: no hidden state, no
  machine-specific inputs, stable output text that diffs cleanly.
- **Hash-identified inputs.** Always record and compare the SHA-256 of each
  image alongside the decoded fields, so two machines are provably looking at the
  same bytes.
- **A re-runnable bundle.** The validation for a given claim should be a short,
  copyable command sequence (image hash in, decoded fields out) that a second
  party can run blind and compare, without trusting this repo's checkout on the
  suspect host.

## Procedure (per claim)

1. On machine A and machine B independently: fetch the GSC image from Google
   (`pixel_gsc_fetch`), record its SHA-256, run `pixel-restore titan --inspect`.
2. Exchange and diff: the SHA-256 and the decoded field set must be identical.
3. Only a claim that reproduces **identically on independent hosts** is treated
   as established. Anything that differs is quarantined as "host-dependent —
   possible tamper," not reported as a finding.

## Honest limits

- This defends against a **single lying host**, which is the stated threat. It
  does **not** defend against a compromised Google distribution, a supply-chain
  compromise of the tool's dependencies, or simultaneous compromise of every
  machine used.
- Agreement across hosts proves the *bytes and decode* match, not that the
  firmware is benign — it removes the "the machine lied to us" failure mode so
  that the *security* conclusions (compromise vs factory) can be reasoned about
  on trustworthy data.
- All current conclusions in the companion docs were produced on a **single
  host** and therefore carry this caveat until independently reproduced.
