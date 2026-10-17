//! Titan M (GSC) repair from the bootloader: send the chip's RW firmware
//! (`<gsc-flavor>.bin`, d3m2.bin on a Titan M2, or a rescue `.rec`) with
//! `fastboot stage <file>`, `fastboot oem gsc rescue` and
//! `fastboot oem gsc reset`.
//!
//! Nothing is sent without `--apply` and the serial typed back. Titan
//! anti-rollback is one-way, so the image is held to what the chip runs now:
//! an older build tag or signed-header version needs `--allow-downgrade`; a
//! lower rollback level is refused (the chip will not boot it); a higher one
//! needs `--allow-rollback-bump`, since booting it burns INFO bits for good.
//! The only `oem gsc` commands sent are `rescue` and `reset`, plus the
//! read-only `version`, `header` and `avb state`; never `avb rb`.
//!
//! Android re-flashes the Titan from the vendor image at boot, so a
//! downgrade only lasts if the vendor image carries the same firmware.

use crate::{fastboot, fbpk, fetch, restore};
use anyhow::{bail, ensure, Context, Result};
use clap::Args;
use sha2::{Digest, Sha256};
use std::borrow::Cow;
use std::io::{IsTerminal, Write};
use std::path::{Path, PathBuf};

#[derive(Args)]
pub struct TitanArgs {
    /// <flavor>.bin (d3m2.bin) or ec.rec itself, a folder holding it, a
    /// bootloader/radio .img, or the factory ZIP.
    #[arg(long, short)]
    firmware: Option<PathBuf>,

    /// Decode the Titan M headers, build tags and keys in this .bin or .rec
    /// (each image's signer, and the RW keys each RO accepts) and stop;
    /// needs no phone.
    #[arg(long, value_name = "FILE", conflicts_with_all = ["firmware", "build", "apply"])]
    inspect: Option<PathBuf>,

    /// Instead of --firmware, look in this build's factory image for the
    /// phone (downloaded and cached as `restore` does). Default: the newest.
    #[arg(long, short, conflicts_with = "firmware")]
    build: Option<String>,

    /// fastboot serial, when more than one phone is attached.
    #[arg(long, short)]
    serial: Option<String>,

    /// New folder for the image and the before/after reports.
    /// Default: titan-<serial>.
    #[arg(long, short)]
    out: Option<PathBuf>,

    /// Send stage, rescue and reset. Asks for the serial unless --confirm.
    #[arg(long)]
    apply: bool,

    /// The phone's serial, to confirm --apply without a prompt.
    #[arg(long, value_name = "SERIAL", requires = "apply")]
    confirm: Option<String>,

    /// Allow an RW image older than the chip's (same rollback level only).
    #[arg(long)]
    allow_downgrade: bool,

    /// Allow an image with a higher rollback level than the chip; one-way.
    #[arg(long)]
    allow_rollback_bump: bool,

    /// Allow an image signed with a key the chip's RW images do not use.
    /// The chip decides which keys it accepts; one it rejects leaves that
    /// RW slot unbootable until rescued again.
    #[arg(long)]
    allow_other_key: bool,

    /// Read the chip's signing key ids and report them, without sending
    /// anything. With --expect-keyid or --firmware it also checks the chip's
    /// RW key against that expected value and fails on a mismatch.
    #[arg(long, conflicts_with_all = ["inspect", "apply"])]
    verify_keys: bool,

    /// Expected RW signing key id (8 hex digits), repeatable. Used by
    /// --verify-keys; the check passes only if every RW image on the chip is
    /// signed with one of these.
    #[arg(long, value_name = "HEX")]
    expect_keyid: Vec<String>,
}

/// The images `oem gsc header` describes.
const GSC_IMAGES: [&str; 4] = ["ro_a", "ro_b", "rw_a", "rw_b"];
/// The only state-changing `oem gsc` commands this sends.
const GSC_WRITES: [&str; 2] = ["rescue", "reset"];

// Cr50's SignedHeader (chromiumos/platform/ec chip/g/signed_header.h), which
// starts every Titan M image: 1024 bytes, little-endian, magic fffffffd on a
// Titan M2 (Dauntless) and, inferred, fffffffe on a Titan M (Citadel).
const HEADER_SIZE: usize = 1024;
const MAGICS: [u32; 2] = [0xFFFF_FFFD, 0xFFFF_FFFE];
const KEYID: usize = 420;
/// The signer's RSA public modulus, 96 little-endian words (3072 bits; a
/// 2048-bit key leaves the top 32 words zero).
const MODULUS: usize = 424;
const KEY_WORDS: usize = 96;
const IMAGE_SIZE: usize = 808;
const RO_BASE: usize = 812;
const RO_MAX: usize = 816;
const INFOMAP: usize = 844; // four words
const EPOCH: usize = 860; // then major, minor

type Version = (u32, u32, u32);

fn dotted(v: Version) -> String {
    format!("{}.{}.{}", v.0, v.1, v.2)
}

/// Titan M generation by the first letter of `gsc-flavor`, after their Cr50
/// board names (inferred, not documented by Google).
pub fn chip_name(flavor: &str) -> &'static str {
    match flavor.chars().next() {
        Some('c') => "Titan M (Citadel)",
        Some('d') => "Titan M2 (Dauntless)",
        _ => "unknown Titan M",
    }
}

/// Cr50's G_SIGNED_FOR_PROD convention: a production key's id has bit D2 set,
/// a development key's does not. Enforced at Google's key-generation time;
/// inferred to hold for the Titan M2, not documented.
pub fn prod_by_rule(keyid: u32) -> bool {
    keyid & 4 != 0
}

/// Titan M signing key ids we have seen on genuine hardware, so a verify can
/// say more than dev-or-prod. Observed, not an authoritative Google list; add
/// to it as real factory images and phones are read.
pub const KNOWN_KEYIDS: &[(u32, &str)] = &[
    (0x68f3_79bd, "komodo RW (observed 2026-10-17)"),
    (0x894b_7d29, "komodo RO (observed 2026-10-17)"),
    (
        0x952b_3485,
        "in the komodo/husky RO's RW key table; no RW seen signed with it",
    ),
    (
        0xd985_131d,
        "in the komodo/husky RO's RW key table; no RW seen signed with it",
    ),
];

/// A one-line note on a key id: the dev/prod rule and any known match.
pub fn keyid_note(keyid: u32) -> String {
    let rule = if prod_by_rule(keyid) {
        "production by Cr50 rule"
    } else {
        "development by Cr50 rule"
    };
    match KNOWN_KEYIDS.iter().find(|(k, _)| *k == keyid) {
        Some((_, label)) => format!("{rule}; known: {label}"),
        None => format!("{rule}; not in the known-key table"),
    }
}

fn parse_keyid(text: &str) -> Result<u32> {
    let t = text.trim();
    let t = t.strip_prefix("0x").unwrap_or(t);
    u32::from_str_radix(t, 16).with_context(|| format!("'{text}' is not an 8-hex-digit key id"))
}

/// `(bootloader) name: value` (or `name=value`) lines of an oem reply.
pub fn fields(text: &str) -> Vec<(String, String)> {
    text.lines()
        .filter_map(|l| {
            let l = l.trim().strip_prefix("(bootloader)")?.trim();
            let (k, v) = [": ", "=", ":"].iter().find_map(|s| l.split_once(s))?;
            (!k.trim().is_empty()).then(|| (k.trim().to_string(), v.trim().to_string()))
        })
        .collect()
}

/// `nugget_v0.0.ab15203921-02e2916-r` -> (15203921, "02e2916"): the Android
/// build the firmware came from, which orders builds, and its revision.
pub fn parse_tag(tag: &str) -> Option<(u64, String)> {
    let rest = tag.strip_prefix("nugget_v")?;
    let (_, after) = rest.split_once(".ab")?;
    let (build, after) = after.split_once('-')?;
    let hash: String = after
        .chars()
        .take_while(|c| c.is_ascii_hexdigit())
        .collect();
    (build.bytes().all(|b| b.is_ascii_digit())
        && !build.is_empty()
        && (7..=40).contains(&hash.len()))
    .then(|| (build.parse().ok()).map(|b| (b, hash)))
    .flatten()
}

#[derive(Debug, Clone, PartialEq)]
pub struct Running {
    pub name: String,
    pub booted: bool,
    pub version: String,
    pub tag: String,
    pub state: String,
    pub build: Option<u64>,
}

/// `oem gsc version`: `RW_A: * 0.2.2/nugget_v0.0.ab15203921-02e2916-r ok`,
/// the star marking the image the chip booted.
pub fn parse_running(text: &str) -> Vec<Running> {
    fields(text)
        .into_iter()
        .filter_map(|(name, value)| {
            let value = value.trim();
            let (booted, value) = match value.strip_prefix('*') {
                Some(v) => (true, v.trim()),
                None => (false, value),
            };
            let mut words = value.split_whitespace();
            let (version, tag) = words.next()?.split_once('/')?;
            ensure_digits(version)?;
            Some(Running {
                name: name.to_ascii_lowercase(),
                booted,
                version: version.to_string(),
                tag: tag.to_string(),
                state: words.next().unwrap_or("").to_string(),
                build: parse_tag(tag).map(|t| t.0),
            })
        })
        .collect()
}

fn ensure_digits(v: &str) -> Option<()> {
    (v.split('.').count() > 1
        && v.split('.')
            .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit())))
    .then_some(())
}

#[derive(Debug, Clone, PartialEq)]
pub struct Build {
    pub build: u64,
    pub hash: String,
    pub tag: String,
}

/// Every distinct build tag an image carries as text, oldest first.
pub fn image_builds(data: &[u8]) -> Vec<Build> {
    let mut out: Vec<Build> = Vec::new();
    let needle = b"nugget_v";
    let mut i = 0;
    while let Some(pos) = data[i..].windows(needle.len()).position(|w| w == needle) {
        let start = i + pos;
        let end = data[start..]
            .iter()
            .position(|&b| !(b.is_ascii_alphanumeric() || b == b'.' || b == b'_' || b == b'-'))
            .map_or(data.len(), |n| start + n);
        let tag = String::from_utf8_lossy(&data[start..end]).into_owned();
        if let Some((build, hash)) = parse_tag(&tag) {
            if !out.iter().any(|b| b.tag == tag) {
                out.push(Build { build, hash, tag });
            }
        }
        i = start + needle.len();
    }
    out.sort_by(|a, b| (a.build, &a.tag).cmp(&(b.build, &b.tag)));
    out
}

fn u32_at(d: &[u8], off: usize) -> u32 {
    u32::from_le_bytes(d[off..off + 4].try_into().unwrap())
}

/// Number of cleared bits in an infomap: Cr50 clears them to raise the
/// image's rollback level (inferred for Titan M).
fn cleared_bits(words: &[u32]) -> u32 {
    words.iter().map(|w| w.count_zeros()).sum()
}

#[derive(Debug, Clone, PartialEq)]
pub struct Header {
    pub offset: usize,
    pub ro_base: u32,
    pub version: Version,
    pub rollback_level: u32,
    pub keyid: u32,
}

/// Every plausible Titan M SignedHeader in an image.
pub fn signed_headers(data: &[u8]) -> Vec<Header> {
    let mut out = Vec::new();
    let mut off = 0;
    while off + HEADER_SIZE <= data.len() {
        if MAGICS.contains(&u32_at(data, off)) {
            let size = u32_at(data, off + IMAGE_SIZE) as usize;
            let (base, max) = (u32_at(data, off + RO_BASE), u32_at(data, off + RO_MAX));
            let v = (
                u32_at(data, off + EPOCH),
                u32_at(data, off + EPOCH + 4),
                u32_at(data, off + EPOCH + 8),
            );
            if (HEADER_SIZE..=data.len() - off).contains(&size)
                && base < max
                && v.0.max(v.1).max(v.2) < 0x10000
            {
                let words: Vec<u32> = (0..4)
                    .map(|i| u32_at(data, off + INFOMAP + 4 * i))
                    .collect();
                out.push(Header {
                    offset: off,
                    ro_base: base,
                    version: v,
                    rollback_level: cleared_bits(&words),
                    keyid: u32_at(data, off + KEYID),
                });
            }
        }
        off += 4;
    }
    out
}

/// Cr50's key id: the Montgomery constant -n^-1 mod 2^32 of the key's
/// modulus, from its low word. Every signed header's `keyid` is this value
/// for the modulus in its `key` field, and the RO's RW key table stores it
/// in front of each modulus, so key ids can be recomputed from key material.
pub fn n0inv(n0: u32) -> u32 {
    // Newton's iteration for 1/n0 mod 2^32: an odd n0 is its own inverse to
    // 3 bits, and each step doubles the correct bits.
    let mut inv = n0;
    for _ in 0..5 {
        inv = inv.wrapping_mul(2u32.wrapping_sub(n0.wrapping_mul(inv)));
    }
    inv.wrapping_neg()
}

/// An RSA public key found in an image.
#[derive(Debug, Clone, PartialEq)]
pub struct RsaKey {
    /// Where the key's 96 modulus words start.
    pub offset: usize,
    /// Recomputed from the modulus with `n0inv`.
    pub keyid: u32,
    pub bits: u32,
    /// The public exponent, when the image stores one after the modulus.
    pub exponent: Option<u32>,
    /// SHA-256 of the modulus as big-endian bytes, leading zeros dropped.
    pub fingerprint: String,
}

fn rsa_key(data: &[u8], offset: usize, exponent: Option<u32>) -> RsaKey {
    let mut be: Vec<u8> = data[offset..offset + 4 * KEY_WORDS]
        .iter()
        .rev()
        .copied()
        .skip_while(|&b| b == 0)
        .collect();
    if be.is_empty() {
        be.push(0);
    }
    let bits = (be.len() as u32 - 1) * 8 + (8 - be[0].leading_zeros());
    RsaKey {
        offset,
        keyid: n0inv(u32_at(data, offset)),
        bits,
        exponent,
        fingerprint: format!("{:x}", Sha256::digest(&be)),
    }
}

/// The key that signed the image whose header is at `off`. Its `keyid`
/// should equal the header's; a mismatch means the header is not Cr50-shaped.
pub fn header_key(data: &[u8], off: usize) -> RsaKey {
    rsa_key(data, off + MODULUS, None)
}

/// Bytes per entry of an RO's RW key table: n0inv, 96 modulus words, then
/// the public exponent.
const KEY_ENTRY: usize = 4 * (KEY_WORDS + 2);

/// The RSA keys an image carries in its body as Cr50 key structs
/// (`n0inv`, 96 modulus words, exponent). In an RO image these are the keys
/// it accepts for RW: on Titan M2 each RO has a table of three 2048-bit
/// keys, one of which signs the shipped RW. Found by shape (a word that is
/// the n0inv of the odd word after it, then a modulus of 1024 bits or
/// more), so a random match is about one in 2^32 per word.
pub fn embedded_keys(data: &[u8], h: &Header) -> Vec<RsaKey> {
    let size = u32_at(data, h.offset + IMAGE_SIZE) as usize;
    let end = (h.offset + size).min(data.len());
    let mut out = Vec::new();
    let mut off = h.offset + HEADER_SIZE;
    while off + KEY_ENTRY <= end {
        let n0 = u32_at(data, off + 4);
        if n0 & 1 == 1 && u32_at(data, off) == n0inv(n0) {
            let e = u32_at(data, off + 4 + 4 * KEY_WORDS);
            let key = rsa_key(data, off + 4, [3, 65537].contains(&e).then_some(e));
            if key.bits >= 1024 {
                out.push(key);
                off += KEY_ENTRY;
                continue;
            }
        }
        off += 4;
    }
    out
}

/// What the image's headers say about keys: each header's signer, each
/// RO's table of accepted RW keys, and whether every RW image in the file
/// is signed with a key some RO in the file accepts. Images are named by
/// role (RO if it carries a key table) and order, A before B.
pub fn key_report(data: &[u8], headers: &[Header]) -> Vec<String> {
    let tables: Vec<Vec<RsaKey>> = headers.iter().map(|h| embedded_keys(data, h)).collect();
    let mut seen = (0, 0);
    let names: Vec<String> = tables
        .iter()
        .map(|t| {
            let (kind, n) = if t.is_empty() {
                seen.1 += 1;
                ("RW", seen.1)
            } else {
                seen.0 += 1;
                ("RO", seen.0)
            };
            match n {
                1 => format!("{kind}_A"),
                2 => format!("{kind}_B"),
                _ => format!("{kind}_{n}"),
            }
        })
        .collect();
    let short = |f: &str| f[..16].to_string();
    let mut lines = Vec::new();
    for ((h, table), name) in headers.iter().zip(&tables).zip(&names) {
        let signer = header_key(data, h.offset);
        let consistent = if signer.keyid == h.keyid {
            "matches its modulus".to_string()
        } else {
            format!("but its modulus gives {:08x}", signer.keyid)
        };
        lines.push(format!(
            "{name} at {:#x}: signed by {:08x} ({}-bit, sha256 {}), key id {consistent}; {}",
            h.offset,
            h.keyid,
            signer.bits,
            short(&signer.fingerprint),
            keyid_note(h.keyid)
        ));
        if !table.is_empty() {
            lines.push(format!("  accepts RW signed by {} keys:", table.len()));
            for k in table {
                let e = k.exponent.map(|e| format!(", e={e}")).unwrap_or_default();
                lines.push(format!(
                    "    {:08x} at {:#x} ({}-bit{e}, sha256 {}); {}",
                    k.keyid,
                    k.offset - 4,
                    k.bits,
                    short(&k.fingerprint),
                    keyid_note(k.keyid)
                ));
            }
        }
    }
    let ros: Vec<(&String, &Vec<RsaKey>)> = names
        .iter()
        .zip(&tables)
        .filter(|(_, t)| !t.is_empty())
        .collect();
    if !ros.is_empty() {
        for ((h, table), name) in headers.iter().zip(&tables).zip(&names) {
            if !table.is_empty() {
                continue;
            }
            let signer = header_key(data, h.offset);
            let by: Vec<&str> = ros
                .iter()
                .filter(|(_, t)| {
                    t.iter()
                        .any(|k| k.keyid == h.keyid && k.fingerprint == signer.fingerprint)
                })
                .map(|(n, _)| n.as_str())
                .collect();
            lines.push(if by.is_empty() {
                format!(
                    "{name} key {:08x} is NOT in any RO's key table here",
                    h.keyid
                )
            } else {
                format!(
                    "{name} key {:08x} is accepted by {}",
                    h.keyid,
                    by.join(", ")
                )
            });
        }
    }
    lines
}

/// The SignedHeader fields worth comparing, in the form `field_text` gives
/// the chip's: hex words as 8 digits, versions and the build time in decimal.
pub fn header_fields(data: &[u8], off: usize) -> Vec<(&'static str, String)> {
    let w = |o: usize| u32_at(data, off + o);
    let words = |o: usize| {
        (0..4)
            .map(|i| format!("{:08x}", w(o + 4 * i)))
            .collect::<String>()
    };
    let timestamp = u64::from_le_bytes(data[off + 872..off + 880].try_into().unwrap());
    vec![
        ("magic", format!("{:08x}", w(0))),
        ("keyid", format!("{:08x}", w(KEYID))),
        ("img_size", format!("{:#x}", w(IMAGE_SIZE))),
        ("ro_base", format!("{:#010x}", w(RO_BASE))),
        ("ro_max", format!("{:#010x}", w(RO_MAX))),
        ("rx_base", format!("{:#010x}", w(820))),
        ("rx_max", format!("{:#010x}", w(824))),
        ("fusemap", words(828)),
        ("infomap", words(INFOMAP)),
        ("epoch", w(EPOCH).to_string()),
        ("major", w(EPOCH + 4).to_string()),
        ("minor", w(EPOCH + 8).to_string()),
        ("timestamp", timestamp.to_string()),
        (
            "board_id",
            format!("{:08x} {:08x} {:08x}", w(996), w(1000), w(1004)),
        ),
    ]
}

/// A chip header value in `header_fields` form, so the two compare as text.
pub fn field_text(name: &str, raw: &str) -> String {
    let hex = |v: &str| {
        let v = v.trim();
        let v = v.strip_prefix("0x").unwrap_or(v);
        let d: String = v.chars().take_while(|c| c.is_ascii_hexdigit()).collect();
        u64::from_str_radix(&d, 16).ok()
    };
    let shown = match name {
        "fusemap" | "infomap" => Some(
            raw.chars()
                .filter(|c| c.is_ascii_hexdigit())
                .collect::<String>()
                .to_ascii_lowercase(),
        ),
        "magic" | "keyid" => hex(raw).map(|v| format!("{v:08x}")),
        "img_size" => hex(raw).map(|v| format!("{v:#x}")),
        "ro_base" | "ro_max" | "rx_base" | "rx_max" => hex(raw).map(|v| format!("{v:#010x}")),
        "epoch" | "major" | "minor" | "timestamp" => hex(raw).map(|v| v.to_string()),
        "board_id" => Some(
            raw.split_whitespace()
                .filter_map(hex)
                .map(|v| format!("{v:08x}"))
                .collect::<Vec<_>>()
                .join(" "),
        ),
        _ => None,
    };
    shown.unwrap_or_else(|| raw.trim().to_string())
}

/// (field, chip value, image value).
pub type FieldDiff = (String, String, String);

/// Field by field, a header in the image against the chip's image at the
/// same address: (chip image, [(field, chip value, file value)]) for the
/// fields that differ, or None when no chip image sits at that address.
pub fn header_delta(
    chip: &[ChipHeader],
    data: &[u8],
    h: &Header,
) -> Option<(String, Vec<FieldDiff>)> {
    let ours = chip.iter().find(|c| c.ro_base == Some(h.ro_base))?;
    let diffs = header_fields(data, h.offset)
        .into_iter()
        .filter_map(|(name, file)| {
            let raw = ours.raw.iter().find(|(k, _)| k == name)?;
            let chip = field_text(name, &raw.1);
            (chip != file).then(|| (name.to_string(), chip, file))
        })
        .collect();
    Some((ours.name.to_ascii_uppercase(), diffs))
}

/// One image's `oem gsc header` (`hdr.<field>: <hex>`), the fields the
/// checks need.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ChipHeader {
    pub name: String,
    pub ro_base: Option<u32>,
    pub version: Option<Version>,
    pub rollback_level: Option<u32>,
    pub keyid: Option<u32>,
    /// Every field as the bootloader printed it, `hdr.` dropped.
    pub raw: Vec<(String, String)>,
}

fn leading_hex(v: &str) -> Option<u32> {
    let v = v.trim();
    let v = v.strip_prefix("0x").unwrap_or(v);
    let digits: String = v.chars().take_while(|c| c.is_ascii_hexdigit()).collect();
    u32::from_str_radix(&digits, 16).ok()
}

pub fn parse_chip_header(name: &str, text: &str) -> ChipHeader {
    let mut h = ChipHeader {
        name: name.to_string(),
        ..Default::default()
    };
    let (mut e, mut ma, mut mi) = (None, None, None);
    for (k, v) in fields(text) {
        let k = k.strip_prefix("hdr.").unwrap_or(&k).to_string();
        h.raw.push((k.clone(), v.clone()));
        match k.as_str() {
            "ro_base" => h.ro_base = leading_hex(&v),
            "keyid" => h.keyid = leading_hex(&v),
            "epoch" => e = leading_hex(&v),
            "major" => ma = leading_hex(&v),
            "minor" => mi = leading_hex(&v),
            "infomap" => {
                let hex: String = v.chars().filter(|c| c.is_ascii_hexdigit()).collect();
                let words: Vec<u32> = (0..hex.len() / 8)
                    .filter_map(|i| u32::from_str_radix(&hex[i * 8..i * 8 + 8], 16).ok())
                    .collect();
                if !words.is_empty() {
                    h.rollback_level = Some(cleared_bits(&words));
                }
            }
            _ => {}
        }
    }
    if let (Some(e), Some(ma), Some(mi)) = (e, ma, mi) {
        h.version = Some((e, ma, mi));
    }
    h
}

#[derive(Debug)]
pub struct BuildCheck {
    pub floor_name: String,
    pub floor: u64,
    pub same_as_booted: bool,
    pub downgrade: bool,
}

/// Every build tag in the image against the newest RW build the chip reports.
pub fn rollback_check(
    running: &[Running],
    builds: &[Build],
    allow_downgrade: bool,
) -> Result<BuildCheck> {
    let rw: Vec<&Running> = running
        .iter()
        .filter(|r| r.name.starts_with("rw") && r.build.is_some())
        .collect();
    let Some(top) = rw.iter().max_by_key(|r| r.build) else {
        bail!("the Titan M reported no RW build (oem gsc version); without it there is no floor, so nothing will be sent");
    };
    let floor = top.build.unwrap();
    let Some(oldest) = builds.first() else {
        bail!(
            "no nugget build tag in the image; its age cannot be checked, so it will not be sent"
        );
    };
    let downgrade = oldest.build < floor;
    ensure!(
        !downgrade || allow_downgrade,
        "the image carries build ab{} ({}), older than {} ab{floor} on the chip; pass --allow-downgrade to send it",
        oldest.build,
        oldest.tag,
        top.name.to_ascii_uppercase()
    );
    let booted = rw.iter().find(|r| r.booted).and_then(|r| r.build);
    Ok(BuildCheck {
        floor_name: top.name.to_ascii_uppercase(),
        floor,
        same_as_booted: booted.is_some_and(|b| builds.iter().any(|x| x.build == b)),
        downgrade,
    })
}

/// The image's RW headers against the chip's: refuse a lower version unless
/// allow_downgrade, a lower rollback level always, and a higher one unless
/// allow_bump. It must also be signed with a key the chip's RW images use:
/// RO only boots RW signed with the key it trusts, so on a chip with
/// development keys a retail image would fail. A header at an RO image's
/// address is RO and not compared.
pub fn header_check(
    chip: &[ChipHeader],
    image: &[Header],
    allow_bump: bool,
    allow_downgrade: bool,
    allow_other_key: bool,
) -> Result<(Version, u32)> {
    let rw: Vec<(Version, u32)> = chip
        .iter()
        .filter(|h| h.name.starts_with("rw"))
        .filter_map(|h| Some((h.version?, h.rollback_level?)))
        .collect();
    ensure!(
        !rw.is_empty(),
        "the Titan M reported no RW header (oem gsc header); without its version and rollback \
         level there is no floor, so nothing will be sent"
    );
    ensure!(
        !image.is_empty(),
        "no Titan M signed header in the image; refusing"
    );
    let floor_version = rw.iter().map(|r| r.0).max().unwrap();
    let floor_level = rw.iter().map(|r| r.1).max().unwrap();
    let ro_bases: Vec<u32> = chip
        .iter()
        .filter(|h| h.name.starts_with("ro"))
        .filter_map(|h| h.ro_base)
        .collect();
    let checked: Vec<&Header> = image
        .iter()
        .filter(|h| !ro_bases.contains(&h.ro_base))
        .collect();
    ensure!(
        !checked.is_empty(),
        "the image holds only RO headers; rescue writes RW, refusing"
    );
    let keys: Vec<u32> = chip
        .iter()
        .filter(|h| h.name.starts_with("rw"))
        .filter_map(|h| h.keyid)
        .collect();
    for h in checked {
        ensure!(
            keys.is_empty() || keys.contains(&h.keyid) || allow_other_key,
            "header at {:#x} is signed with key {:08x}, but the chip's RW images use {}; \
             the chip may reject it. Pass --allow-other-key to send it anyway",
            h.offset,
            h.keyid,
            keys.iter()
                .map(|k| format!("{k:08x}"))
                .collect::<Vec<_>>()
                .join(", ")
        );
        let at = format!(
            "header at {:#x} ({}, rollback level {})",
            h.offset,
            dotted(h.version),
            h.rollback_level
        );
        ensure!(
            h.version >= floor_version || allow_downgrade,
            "{at} is older than RW {} on the chip; pass --allow-downgrade to send it",
            dotted(floor_version)
        );
        ensure!(
            h.rollback_level >= floor_level,
            "{at} is below the chip's rollback level {floor_level}; it would not boot"
        );
        ensure!(
            h.rollback_level <= floor_level || allow_bump,
            "{at} raises the rollback level from {floor_level}, which cannot be undone; pass \
             --allow-rollback-bump if that is intended"
        );
    }
    Ok((floor_version, floor_level))
}

/// The bytes an image holds: Intel HEX (the form UART rescue takes) decoded,
/// anything else as it is.
pub fn rescue_payload(data: &[u8]) -> Result<Cow<'_, [u8]>> {
    if !data.starts_with(b":") {
        return Ok(Cow::Borrowed(data));
    }
    let mut chunks: Vec<(u64, Vec<u8>)> = Vec::new();
    let mut base = 0u64;
    for line in data
        .split(|b| b.is_ascii_whitespace())
        .filter(|l| !l.is_empty())
    {
        let rec = line
            .strip_prefix(b":")
            .and_then(|h| std::str::from_utf8(h).ok())
            .filter(|h| h.len() % 2 == 0)
            .and_then(|h| {
                (0..h.len())
                    .step_by(2)
                    .map(|i| u8::from_str_radix(&h[i..i + 2], 16).ok())
                    .collect::<Option<Vec<u8>>>()
            });
        let Some(rec) = rec.filter(|r| {
            r.len() >= 5
                && r.len() == r[0] as usize + 5
                && r.iter().fold(0u8, |a, b| a.wrapping_add(*b)) == 0
        }) else {
            bail!("the image starts like Intel HEX but has a malformed record");
        };
        let addr = u16::from_be_bytes([rec[1], rec[2]]) as u64;
        let payload = &rec[4..rec.len() - 1];
        match rec[3] {
            0 => chunks.push((base + addr, payload.to_vec())),
            2 | 4 => {
                let v = payload.iter().fold(0u64, |a, b| a << 8 | *b as u64);
                base = if rec[3] == 2 { v << 4 } else { v << 16 };
            }
            1 => break,
            _ => {}
        }
    }
    ensure!(!chunks.is_empty(), "the Intel HEX image holds no data");
    let start = chunks.iter().map(|c| c.0).min().unwrap();
    let end = chunks.iter().map(|c| c.0 + c.1.len() as u64).max().unwrap();
    let mut out = vec![0u8; (end - start) as usize];
    for (a, d) in chunks {
        let o = (a - start) as usize;
        out[o..o + d.len()].copy_from_slice(&d);
    }
    Ok(Cow::Owned(out))
}

/// Is this file or pack entry the firmware for `flavor`? `.rec` is the rescue
/// image Pixels with a Titan M ship (citadel ec.rec); on a Titan M2
/// `<flavor>.bin` is the RW firmware.
pub fn wanted(name: &str, flavor: &str) -> bool {
    let base = name.rsplit('/').next().unwrap_or(name).to_ascii_lowercase();
    [
        format!("{flavor}.bin"),
        format!("{flavor}.ec.bin"),
        format!("{flavor}.rec"),
        format!("{flavor}.ec.rec"),
        flavor.to_string(),
    ]
    .contains(&base)
}

fn is_rescue(where_: &str) -> bool {
    where_.to_ascii_lowercase().ends_with(".rec")
}

/// The firmware for `flavor`, where it was found, and the device a factory
/// ZIP is for. Nested image ZIPs are unpacked into `scratch` while read.
pub fn find_firmware(
    path: &Path,
    flavor: &str,
    scratch: &Path,
) -> Result<(Vec<u8>, String, Option<String>)> {
    let mut hits: Vec<(Vec<u8>, String)> = Vec::new();
    let mut product = None;
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    if path.is_dir() {
        let mut stack = vec![path.to_path_buf()];
        while let Some(dir) = stack.pop() {
            for e in std::fs::read_dir(&dir)? {
                let p = e?.path();
                if p.is_dir() {
                    stack.push(p);
                } else if wanted(&p.to_string_lossy(), flavor) {
                    hits.push((std::fs::read(&p)?, p.display().to_string()));
                }
            }
        }
    } else if path.is_file() {
        let data = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
        if data.starts_with(b"PK\x03\x04") {
            product = name
                .split('-')
                .next()
                .filter(|s| !s.is_empty())
                .map(str::to_string);
            hits = zip_hits(std::io::Cursor::new(data), &name, flavor, scratch)?;
        } else if data.starts_with(b"FBPK") {
            let pack = fbpk::Pack::parse(data)?;
            hits = pack
                .data_entries()
                .filter(|e| wanted(&e.name, flavor))
                .map(|e| (pack.entry_data(e).to_vec(), format!("{name}:{}", e.name)))
                .collect();
        } else if name.to_ascii_lowercase().contains(flavor)
            || ["ec.rec", "ec.bin"].contains(&name.to_ascii_lowercase().as_str())
        {
            hits.push((data, path.display().to_string()));
        } else {
            bail!("{name} is not named for this chip's flavor {flavor}");
        }
    } else {
        bail!("{} does not exist", path.display());
    }
    // Rescue writes one RW image, which is what a .rec is.
    if hits.iter().any(|h| is_rescue(&h.1)) {
        hits.retain(|h| is_rescue(&h.1));
    }
    ensure!(
        !hits.is_empty(),
        "no {flavor}.rec or {flavor}.bin in {}. If it ships inside vendor.img, extract it and pass \
         the file itself with --firmware",
        path.display()
    );
    ensure!(
        hits.iter().all(|h| h.0 == hits[0].0),
        "different firmware files match: {}",
        hits.iter()
            .map(|h| h.1.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    );
    let (data, where_) = hits.swap_remove(0);
    Ok((data, where_, product))
}

fn zip_hits<R: std::io::Read + std::io::Seek>(
    reader: R,
    label: &str,
    flavor: &str,
    scratch: &Path,
) -> Result<Vec<(Vec<u8>, String)>> {
    let mut zip = zip::ZipArchive::new(reader).with_context(|| format!("opening {label}"))?;
    let mut hits = Vec::new();
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i)?;
        let name = entry.name().to_string();
        let base = name.rsplit('/').next().unwrap_or(&name).to_string();
        if wanted(&name, flavor) {
            let mut data = Vec::new();
            std::io::copy(&mut entry, &mut data)?;
            hits.push((data, format!("{label}:{name}")));
        } else if (base.starts_with("bootloader-") || base.starts_with("radio-"))
            && base.ends_with(".img")
        {
            let mut data = Vec::new();
            std::io::copy(&mut entry, &mut data)?;
            if data.starts_with(b"FBPK") {
                if let Ok(pack) = fbpk::Pack::parse(data) {
                    for e in pack.data_entries().filter(|e| wanted(&e.name, flavor)) {
                        hits.push((
                            pack.entry_data(e).to_vec(),
                            format!("{label}:{base}:{}", e.name),
                        ));
                    }
                }
            }
        } else if base.starts_with("image-") && base.ends_with(".zip") {
            // The zip crate needs to seek, so unpack the inner ZIP while reading it.
            std::fs::create_dir_all(scratch)?;
            let tmp = scratch.join(format!("{base}.part"));
            eprintln!(
                "unpacking {base} ({:.2} GB) to look inside ...",
                entry.size() as f64 / 1e9
            );
            std::io::copy(&mut entry, &mut std::fs::File::create(&tmp)?)?;
            let found = zip_hits(
                std::fs::File::open(&tmp)?,
                &format!("{label}:{base}"),
                flavor,
                scratch,
            );
            let _ = std::fs::remove_file(&tmp);
            hits.extend(found?);
        }
    }
    Ok(hits)
}

/// Refuse any state-changing `oem gsc` command but rescue and reset.
pub fn check_gsc_write(cmd: &str) -> Result<()> {
    ensure!(
        GSC_WRITES.contains(&cmd),
        "refusing `oem gsc {cmd}`: only rescue and reset are sent"
    );
    Ok(())
}

fn gsc_write(phone: &fastboot::Device, cmd: &str) -> Result<String> {
    check_gsc_write(cmd)?;
    let (ok, text) = phone.oem(&["gsc", cmd])?;
    ensure!(ok, "`fastboot oem gsc {cmd}` failed:\n{}", text.trim());
    Ok(text)
}

/// The chip's own account of its firmware: version lines and each header.
struct Snapshot {
    version: String,
    headers: Vec<(String, String)>,
    avb: String,
}

fn snapshot(phone: &fastboot::Device) -> Result<Snapshot> {
    let version = phone.oem(&["gsc", "version"])?.1;
    let mut headers = Vec::new();
    for image in GSC_IMAGES {
        headers.push((image.to_string(), phone.oem(&["gsc", "header", image])?.1));
    }
    let avb = phone.oem(&["gsc", "avb", "state"])?.1;
    Ok(Snapshot {
        version,
        headers,
        avb,
    })
}

fn write_report(path: &Path, title: &str, lines: &[String], snap: &Snapshot) -> Result<()> {
    let mut f =
        std::fs::File::create_new(path).with_context(|| format!("creating {}", path.display()))?;
    writeln!(f, "# {title}")?;
    for l in lines {
        writeln!(f, "{l}")?;
    }
    writeln!(f, "\n## oem gsc version\n{}", snap.version.trim())?;
    for (image, text) in &snap.headers {
        writeln!(f, "\n## oem gsc header {image}\n{}", text.trim())?;
    }
    writeln!(f, "\n## oem gsc avb state\n{}", snap.avb.trim())?;
    Ok(())
}

fn show(running: &[Running]) {
    for r in running {
        println!(
            "  {}: {} {}/{} {}",
            r.name.to_ascii_uppercase(),
            if r.booted { "*" } else { " " },
            r.version,
            r.tag,
            r.state
        );
    }
}

fn private_dir(path: &Path) -> Result<()> {
    std::fs::create_dir(path)
        .with_context(|| format!("creating {} (it must not exist yet)", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// `--inspect`: everything the image says about itself.
fn inspect(path: &Path) -> Result<()> {
    let data = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    let body = rescue_payload(&data)?;
    println!("{} ({} bytes)", path.display(), data.len());
    if body.len() != data.len() {
        println!("  Intel HEX, {} bytes of data", body.len());
    }
    for b in image_builds(&body) {
        println!("  carries {}", b.tag);
    }
    let headers = signed_headers(&body);
    ensure!(
        !headers.is_empty(),
        "no Titan M signed header in {}",
        path.display()
    );
    for h in &headers {
        println!(
            "  header at {:#x}, rollback level {}:",
            h.offset, h.rollback_level
        );
        for (field, value) in header_fields(&body, h.offset) {
            println!("    {field}: {value}");
        }
    }
    println!("  keys:");
    for l in key_report(&body, &headers) {
        println!("    {l}");
    }
    Ok(())
}

/// The report `--verify-keys` prints: a line per chip image, and whether the
/// RW keys match the expected set. `expected` empty means report only.
pub fn verify_keys(chip: &[ChipHeader], expected: &[u32]) -> (Vec<String>, bool) {
    let mut lines = Vec::new();
    let mut rw_keys = Vec::new();
    for h in chip {
        match h.keyid {
            Some(k) => {
                lines.push(format!(
                    "  {}: key {:08x} ({})",
                    h.name.to_ascii_uppercase(),
                    k,
                    keyid_note(k)
                ));
                if h.name.starts_with("rw") {
                    rw_keys.push(k);
                }
            }
            None => lines.push(format!(
                "  {}: no key id reported",
                h.name.to_ascii_uppercase()
            )),
        }
    }
    // A dev-keyed RO under a prod-keyed RW is the oddity worth calling out.
    if chip
        .iter()
        .any(|h| h.name.starts_with("ro") && h.keyid.is_some_and(|k| !prod_by_rule(k)))
        && rw_keys.iter().any(|k| prod_by_rule(*k))
    {
        lines.push("  note: RO is development-keyed while RW is production-keyed".into());
    }
    if expected.is_empty() {
        lines.push("  no expected key given (--expect-keyid or --firmware); reporting only".into());
        return (lines, true);
    }
    let want = expected
        .iter()
        .map(|k| format!("{k:08x}"))
        .collect::<Vec<_>>()
        .join(", ");
    let ok = !rw_keys.is_empty() && rw_keys.iter().all(|k| expected.contains(k));
    lines.push(if rw_keys.is_empty() {
        format!("  MISMATCH: the chip reported no RW key to check against expected {want}")
    } else if ok {
        format!("  OK: every RW key matches expected {want}")
    } else {
        let got = rw_keys
            .iter()
            .map(|k| format!("{k:08x}"))
            .collect::<Vec<_>>()
            .join(", ");
        format!("  MISMATCH: chip RW keys {got}, expected {want}")
    });
    (lines, ok)
}

/// `--verify-keys`: read the chip's keys and check them against what's
/// expected. Expected comes from --expect-keyid, or the RW key of a genuine
/// image passed with --firmware. Read-only.
fn run_verify_keys(a: &TitanArgs) -> Result<()> {
    let phone = fastboot::Device::open(a.serial.as_deref())?;
    let product = phone.getvar("product")?.unwrap_or_else(|| "?".into());
    let flavor = phone
        .getvar("gsc-flavor")?
        .filter(|f| {
            !f.is_empty()
                && f.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        })
        .context(
            "the bootloader reports no usable gsc-flavor; is this a Titan M phone in fastboot?",
        )?;
    println!(
        "{}: {product}, {} ({flavor})",
        phone.serial,
        chip_name(&flavor)
    );

    let chip: Vec<ChipHeader> = snapshot(&phone)?
        .headers
        .iter()
        .map(|(n, t)| parse_chip_header(n, t))
        .collect();
    let mut expected: Vec<u32> = a
        .expect_keyid
        .iter()
        .map(|s| parse_keyid(s))
        .collect::<Result<_>>()?;
    if let Some(fw) = &a.firmware {
        let scratch = std::env::temp_dir().join(format!("titan-verify-{}", std::process::id()));
        let found = find_firmware(fw, &flavor, &scratch);
        let _ = std::fs::remove_dir_all(&scratch);
        let (data, where_, _) = found?;
        let body = rescue_payload(&data)?;
        // Classify the image's headers with the chip's RO addresses: a header
        // not at an RO base is an RW image, and its key is what the chip's RW
        // should match.
        let ro_bases: Vec<u32> = chip
            .iter()
            .filter(|h| h.name.starts_with("ro"))
            .filter_map(|h| h.ro_base)
            .collect();
        for h in signed_headers(&body) {
            let key = u32_at(&body, h.offset + KEYID);
            if !ro_bases.contains(&h.ro_base) && !expected.contains(&key) {
                expected.push(key);
            }
        }
        ensure!(
            !expected.is_empty(),
            "no RW image found in {where_} to take an expected key from"
        );
        println!("expected RW key taken from {where_}");
        // The RO in that firmware also lists every RW key it would accept.
        let headers = signed_headers(&body);
        let accepted: Vec<u32> = headers
            .iter()
            .flat_map(|h| embedded_keys(&body, h))
            .map(|k| k.keyid)
            .fold(Vec::new(), |mut v, k| {
                if !v.contains(&k) {
                    v.push(k);
                }
                v
            });
        if !accepted.is_empty() {
            let list = accepted
                .iter()
                .map(|k| format!("{k:08x}"))
                .collect::<Vec<_>>()
                .join(", ");
            println!("its RO accepts RW keys: {list}");
            for h in chip.iter().filter(|h| h.name.starts_with("rw")) {
                if let Some(k) = h.keyid {
                    let verdict = if accepted.contains(&k) {
                        "in that RO's table"
                    } else {
                        "NOT in that RO's table"
                    };
                    println!(
                        "  chip {} key {k:08x}: {verdict}",
                        h.name.to_ascii_uppercase()
                    );
                }
            }
        }
    }
    let (lines, ok) = verify_keys(&chip, &expected);
    println!("Titan M signing keys:");
    for l in &lines {
        println!("{l}");
    }
    ensure!(ok, "key verification failed");
    Ok(())
}

pub fn run(a: TitanArgs) -> Result<()> {
    if let Some(path) = &a.inspect {
        return inspect(path);
    }
    if a.verify_keys {
        return run_verify_keys(&a);
    }
    let phone = fastboot::Device::open(a.serial.as_deref())?;
    let product = phone
        .getvar("product")?
        .context("the phone did not report its product")?;
    let flavor = phone
        .getvar("gsc-flavor")?
        .filter(|f| {
            !f.is_empty()
                && f.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        })
        .context(
            "the bootloader reports no usable gsc-flavor; is this a Titan M phone in fastboot?",
        )?;
    let bootloader = phone
        .getvar("version-bootloader")?
        .unwrap_or_else(|| "?".into());
    println!(
        "{}: {product}, {} ({flavor}), bootloader {bootloader}",
        phone.serial,
        chip_name(&flavor)
    );

    let out = a
        .out
        .clone()
        .unwrap_or_else(|| PathBuf::from(format!("titan-{}", phone.serial)));
    private_dir(&out)?;
    let scratch = out.join("scratch");
    let source = match &a.firmware {
        Some(p) => p.clone(),
        None => {
            let (images, _) = restore::list_images(&product, false)?;
            let list = restore::candidates(&images, a.build.as_deref(), false);
            let image = list.last().with_context(|| {
                format!(
                    "no build '{}' for {product}",
                    a.build.as_deref().unwrap_or("?")
                )
            })?;
            println!(
                "looking in factory image {} ({})",
                image.build,
                image.file_name()
            );
            restore::cached_zip(image)?
        }
    };
    let found = find_firmware(&source, &flavor, &scratch);
    let _ = std::fs::remove_dir(&scratch);
    let (data, where_, package) = found?;
    if let Some(pkg) = package.filter(|p| fetch::codename(p).is_ok()) {
        ensure!(
            pkg.eq_ignore_ascii_case(&product),
            "{} is for {pkg}; the phone is {product}",
            file_label(&source)
        );
    }

    let before = snapshot(&phone)?;
    let running = parse_running(&before.version);
    let chip: Vec<ChipHeader> = before
        .headers
        .iter()
        .map(|(n, t)| parse_chip_header(n, t))
        .collect();
    println!("Titan M firmware now (* = booted):");
    show(&running);

    let body = rescue_payload(&data)?;
    let builds = image_builds(&body);
    let headers = signed_headers(&body);
    let digest = {
        use sha2::{Digest, Sha256};
        Sha256::digest(&data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    };
    println!("image: {where_} ({} bytes, sha256 {digest})", data.len());
    for b in &builds {
        println!("  carries {}", b.tag);
    }
    for h in &headers {
        println!(
            "  header at {:#x}: base {:#010x}, version {}, rollback level {}, key {:08x}",
            h.offset,
            h.ro_base,
            dotted(h.version),
            h.rollback_level,
            h.keyid
        );
        match header_delta(&chip, &body, h) {
            Some((image, diffs)) if diffs.is_empty() => {
                println!("    same as the chip's {image} in every field it reports")
            }
            Some((image, diffs)) => {
                println!("    differs from the chip's {image}:");
                for (field, chip, file) in diffs {
                    println!("      {field}: chip {chip}, image {file}");
                }
            }
            None => println!("    no chip image at this address to compare with"),
        }
    }
    let check = rollback_check(&running, &builds, a.allow_downgrade)?;
    let (floor_version, floor_level) = header_check(
        &chip,
        &headers,
        a.allow_rollback_bump,
        a.allow_downgrade,
        a.allow_other_key,
    )?;
    println!(
        "floor {} ab{}, header {}, rollback level {floor_level}: image is {}",
        check.floor_name,
        check.floor,
        dotted(floor_version),
        if check.same_as_booted {
            "the same build as the booted RW"
        } else if check.downgrade {
            "OLDER (downgrade)"
        } else {
            "newer"
        }
    );

    let image = out.join(format!(
        "{flavor}{}",
        if is_rescue(&where_) { ".rec" } else { ".bin" }
    ));
    std::fs::write(&image, &data)?;
    let summary = vec![
        format!("serial: {}", phone.serial),
        format!("product: {product}"),
        format!("flavor: {flavor}"),
        format!("image: {where_}"),
        format!("sha256: {digest}"),
        format!(
            "floor: {} ab{} header {} rollback level {floor_level}",
            check.floor_name,
            check.floor,
            dotted(floor_version)
        ),
        format!("downgrade: {}", check.downgrade),
    ];
    write_report(&out.join("before.md"), "Titan M before", &summary, &before)?;

    let staged = image.to_string_lossy().into_owned();
    println!("plan:");
    println!("  fastboot -s {} stage {staged}", phone.serial);
    for cmd in GSC_WRITES {
        println!("  fastboot -s {} oem gsc {cmd}", phone.serial);
    }
    if !a.apply {
        println!(
            "dry run; phone unchanged. Saved {} and before.md in {}. To send, pass --apply.",
            image.display(),
            out.display()
        );
        return Ok(());
    }
    let answer = match a.confirm {
        Some(c) => c,
        None if std::io::stdin().is_terminal() => {
            print!(
                "Type the serial {} to send this firmware to the Titan M: ",
                phone.serial
            );
            std::io::stdout().flush()?;
            let mut line = String::new();
            std::io::stdin().read_line(&mut line)?;
            line.trim().to_string()
        }
        None => bail!("not on a terminal; pass --confirm SERIAL"),
    };
    ensure!(answer == phone.serial, "serial not confirmed; nothing sent");

    println!("Sending Titan M firmware; do not disconnect USB or power.");
    phone.stage(&image)?;
    for cmd in GSC_WRITES {
        print!("{}", gsc_write(&phone, cmd)?);
    }
    let after = snapshot(&phone)?;
    write_report(
        &out.join("after.md"),
        "Titan M after",
        &[format!("serial: {}", phone.serial)],
        &after,
    )?;
    let now = parse_running(&after.version);
    println!("Titan M firmware after reset:");
    show(&now);
    if now.is_empty() {
        println!(
            "the chip did not answer `oem gsc version` yet; run `fastboot oem gsc version` again"
        );
    }
    let bad: Vec<String> = now
        .iter()
        .filter(|r| r.state != "ok")
        .map(|r| r.name.to_ascii_uppercase())
        .collect();
    ensure!(bad.is_empty(), "not ok after reset: {}", bad.join(", "));
    Ok(())
}

fn file_label(p: &Path) -> String {
    p.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| p.display().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Rick's komodo, 2026-10-17.
    const VERSION: &str = "(bootloader) RO_A: * 0.2.114/9b4e3277 ok\n\
        (bootloader) RO_B: 0.2.112/5cfcbae6 ok\n\
        (bootloader) RW_A: * 0.2.2/nugget_v0.0.ab15203921-02e2916-r ok\n\
        (bootloader) RW_B: 0.2.2/nugget_v0.0.ab15116366-6c96681-r ok\nOKAY\n";
    const RW: u32 = 0x84000;
    const RO: u32 = 0x80000;
    // Rick's chip's key id, which the Cr50 rule calls development.
    const KEY: u32 = 0x894b_7d29;

    fn header(version: Version, level: u32, base: u32) -> Vec<u8> {
        let size = 4096usize;
        let mut h = vec![0x33u8; HEADER_SIZE];
        h[0..4].copy_from_slice(&0xFFFF_FFFDu32.to_le_bytes());
        h[KEYID..KEYID + 4].copy_from_slice(&KEY.to_le_bytes());
        h[IMAGE_SIZE..IMAGE_SIZE + 4].copy_from_slice(&(size as u32).to_le_bytes());
        h[RO_BASE..RO_BASE + 4].copy_from_slice(&base.to_le_bytes());
        h[RO_MAX..RO_MAX + 4].copy_from_slice(&(base + 0x3c000).to_le_bytes());
        for (i, v) in [version.0, version.1, version.2].iter().enumerate() {
            h[EPOCH + 4 * i..EPOCH + 4 * i + 4].copy_from_slice(&v.to_le_bytes());
        }
        let first = 0xFFFF_FFFFu32.checked_shl(level).unwrap_or(0);
        h[INFOMAP..INFOMAP + 4].copy_from_slice(&first.to_le_bytes());
        h[INFOMAP + 4..INFOMAP + 16].fill(0xff);
        h.resize(size, 0);
        h
    }

    fn tagged(tag: &str, version: Version, level: u32) -> Vec<u8> {
        let mut d = header(version, level, RW);
        d.extend_from_slice(&[0; 16]);
        d.extend_from_slice(tag.as_bytes());
        d.push(0);
        d
    }

    fn same() -> Vec<u8> {
        let mut d = header((0, 2, 2), 2, RO);
        d.extend(tagged("nugget_v0.0.ab15203921-02e2916-r", (0, 2, 2), 2));
        d
    }

    fn chip() -> Vec<ChipHeader> {
        let h = |name: &str, base: u32, v: Version, infomap: &str| {
            let text = format!(
                "(bootloader) hdr.ro_base: {base:x}\n(bootloader) hdr.epoch: {:x}\n(bootloader) hdr.major: {:x}\n\
                 (bootloader) hdr.minor: {:x}\n(bootloader) hdr.infomap: {infomap}\n(bootloader) hdr.keyid: {KEY:08x}\nOKAY\n",
                v.0, v.1, v.2
            );
            parse_chip_header(name, &text)
        };
        vec![
            h(
                "ro_a",
                RO,
                (0, 2, 114),
                "ffffffff ffffffff ffffffff ffffffff",
            ),
            h("rw_a", RW, (0, 2, 2), "fffffffc ffffffff ffffffff ffffffff"),
            h(
                "rw_b",
                RW + 0x40000,
                (0, 2, 2),
                "fffffffe ffffffff ffffffff ffffffff",
            ),
        ]
    }

    fn running() -> Vec<Running> {
        parse_running(VERSION)
    }

    #[test]
    fn parses_ricks_chip() {
        let r = running();
        assert_eq!(r.len(), 4);
        assert_eq!(
            r[2],
            Running {
                name: "rw_a".into(),
                booted: true,
                version: "0.2.2".into(),
                tag: "nugget_v0.0.ab15203921-02e2916-r".into(),
                state: "ok".into(),
                build: Some(15203921),
            }
        );
        assert!(r[0].booted && r[0].build.is_none());
        assert!(!r[3].booted);
    }

    #[test]
    fn parses_chip_headers() {
        let c = chip();
        assert_eq!(c[1].version, Some((0, 2, 2)));
        assert_eq!(c[1].rollback_level, Some(2));
        assert_eq!(c[1].ro_base, Some(RW));
        assert_eq!(c[0].rollback_level, Some(0));
    }

    #[test]
    fn same_build_passes() {
        let b = image_builds(&same());
        let c = rollback_check(&running(), &b, false).unwrap();
        assert!(c.same_as_booted && !c.downgrade);
        assert_eq!(c.floor, 15203921);
        assert_eq!(
            header_check(&chip(), &signed_headers(&same()), false, false, false).unwrap(),
            ((0, 2, 2), 2)
        );
    }

    #[test]
    fn older_build_needs_the_flag() {
        let old = image_builds(&tagged("nugget_v0.0.ab15116366-6c96681-r", (0, 2, 2), 2));
        assert!(rollback_check(&running(), &old, false)
            .unwrap_err()
            .to_string()
            .contains("--allow-downgrade"));
        assert!(rollback_check(&running(), &old, true).unwrap().downgrade);
        let mixed = [old.clone(), image_builds(&same())].concat();
        assert!(rollback_check(&running(), &mixed, false).is_err());
    }

    #[test]
    fn unreadable_image_or_chip_is_refused() {
        assert!(rollback_check(&running(), &[], true).is_err());
        assert!(rollback_check(&[], &image_builds(&same()), true).is_err());
        assert!(header_check(&chip(), &[], true, true, false).is_err());
        assert!(header_check(&[], &signed_headers(&same()), true, true, false).is_err());
    }

    #[test]
    fn older_header_needs_the_flag() {
        let h = signed_headers(&header((0, 2, 1), 2, RW));
        assert!(header_check(&chip(), &h, false, false, false)
            .unwrap_err()
            .to_string()
            .contains("older than RW 0.2.2"));
        assert!(header_check(&chip(), &h, false, true, false).is_ok());
    }

    #[test]
    fn lower_rollback_level_is_always_refused() {
        let h = signed_headers(&header((0, 2, 1), 1, RW));
        assert!(header_check(&chip(), &h, true, true, false)
            .unwrap_err()
            .to_string()
            .contains("would not boot"));
    }

    #[test]
    fn higher_rollback_level_needs_the_flag() {
        let h = signed_headers(&header((0, 2, 3), 3, RW));
        assert!(header_check(&chip(), &h, false, false, false)
            .unwrap_err()
            .to_string()
            .contains("cannot be undone"));
        assert!(header_check(&chip(), &h, true, false, false).is_ok());
    }

    #[test]
    fn other_signing_key_is_refused() {
        let mut d = header((0, 2, 2), 2, RW);
        d[KEYID..KEYID + 4].copy_from_slice(&0x1234_5678u32.to_le_bytes());
        let err = header_check(&chip(), &signed_headers(&d), true, true, false).unwrap_err();
        assert!(
            err.to_string().contains("signed with key 12345678"),
            "{err}"
        );
        assert!(header_check(&chip(), &signed_headers(&d), true, true, true).is_ok());
        assert_eq!(chip()[1].keyid, Some(KEY));
    }

    #[test]
    fn decodes_every_header_field() {
        let d = same();
        let f: std::collections::HashMap<_, _> = header_fields(&d, 4096).into_iter().collect();
        assert_eq!(f["magic"], "fffffffd");
        assert_eq!(f["keyid"], "894b7d29");
        assert_eq!(f["ro_base"], "0x00084000");
        assert_eq!(f["infomap"], "fffffffcffffffffffffffffffffffff");
        assert_eq!(
            (
                f["epoch"].as_str(),
                f["major"].as_str(),
                f["minor"].as_str()
            ),
            ("0", "2", "2")
        );
    }

    #[test]
    fn chip_values_compare_as_text() {
        // As Rick's bootloader and the IMEI thread's tree print them.
        assert_eq!(
            field_text("infomap", "FFFFFFFFFFFFFFFFFFFFFFFF1FFFFFFF"),
            "ffffffffffffffffffffffff1fffffff"
        );
        assert_eq!(field_text("ro_base", "0x000a0000"), "0x000a0000");
        assert_eq!(field_text("keyid", "68f379bd"), "68f379bd");
        assert_eq!(field_text("minor", "2"), "2");
        assert_eq!(
            field_text("board_id", "0 ffffffff 10"),
            "00000000 ffffffff 00000010"
        );
    }

    #[test]
    fn reports_the_fields_that_differ() {
        let d = same();
        let h = &signed_headers(&d)[1];
        let (image, diffs) = header_delta(&chip(), &d, h).unwrap();
        assert_eq!(image, "RW_A");
        assert!(diffs.is_empty(), "{diffs:?}");
        let mut other = chip();
        other[1].raw.push(("keyid".into(), "68f379bd".into()));
        other[1]
            .raw
            .retain(|(k, v)| k != "keyid" || v == "68f379bd");
        let (_, diffs) = header_delta(&other, &d, h).unwrap();
        assert_eq!(
            diffs,
            [(
                "keyid".to_string(),
                "68f379bd".to_string(),
                "894b7d29".to_string()
            )]
        );
        let lone = header((0, 2, 2), 2, 0x1234000);
        assert!(header_delta(&chip(), &lone, &signed_headers(&lone)[0]).is_none());
    }

    #[test]
    fn ro_only_image_is_refused() {
        let h = signed_headers(&header((0, 2, 114), 0, RO));
        assert!(header_check(&chip(), &h, true, true, false)
            .unwrap_err()
            .to_string()
            .contains("only RO"));
    }

    #[test]
    fn finds_headers_not_erased_flash() {
        let h = signed_headers(&same());
        assert_eq!(
            h.iter().map(|h| (h.offset, h.ro_base)).collect::<Vec<_>>(),
            [(0, RO), (4096, RW)]
        );
        assert!(signed_headers(&[0xff; 8192]).is_empty());
    }

    #[test]
    fn only_rescue_and_reset_are_sent() {
        assert!(check_gsc_write("rescue").is_ok() && check_gsc_write("reset").is_ok());
        for bad in ["avb rb 1", "avb", "stage", "recover", ""] {
            assert!(check_gsc_write(bad).is_err(), "{bad}");
        }
    }

    fn intel_hex(data: &[u8]) -> Vec<u8> {
        let mut recs = vec![vec![2u8, 0, 0, 4, 0, 8]];
        for (i, c) in data.chunks(16).enumerate() {
            let a = (i * 16) as u16;
            let mut r = vec![c.len() as u8];
            r.extend(a.to_be_bytes());
            r.push(0);
            r.extend_from_slice(c);
            recs.push(r);
        }
        recs.push(vec![0, 0, 0, 1]);
        let mut out = Vec::new();
        for mut r in recs {
            let sum = r.iter().fold(0u8, |a, b| a.wrapping_add(*b));
            r.push(sum.wrapping_neg());
            out.push(b':');
            out.extend(
                r.iter()
                    .map(|b| format!("{b:02X}"))
                    .collect::<String>()
                    .bytes(),
            );
            out.push(b'\n');
        }
        out
    }

    // A Cr50 key struct for a 2048-bit modulus whose low word is `n0`:
    // n0inv, 96 modulus words (top 32 zero), exponent.
    fn key_entry(n0: u32) -> Vec<u8> {
        let mut words = vec![n0inv(n0), n0];
        words.extend((1..63).map(|i| 0x0101_0101u32.wrapping_mul(i)));
        words.push(0xc000_0001); // top word: a full 2048-bit modulus
        words.extend([0u32; 32]);
        words.push(65537);
        words.iter().flat_map(|w| w.to_le_bytes()).collect()
    }

    // An RO image whose header is signed by `ro_n0` and whose body carries
    // a key table of `table` keys, then an RW image signed by `rw_n0`.
    fn keyed_image(ro_n0: u32, table: &[u32], rw_n0: u32) -> Vec<u8> {
        let signed = |n0: u32, base: u32| {
            let mut d = header((0, 2, 2), 0, base);
            let entry = key_entry(n0);
            d[KEYID..KEYID + 4].copy_from_slice(&entry[0..4]);
            d[MODULUS..MODULUS + 4 * KEY_WORDS].copy_from_slice(&entry[4..4 + 4 * KEY_WORDS]);
            d
        };
        let mut d = signed(ro_n0, RO);
        let mut at = HEADER_SIZE + 64;
        for n0 in table {
            let e = key_entry(*n0);
            d[at..at + e.len()].copy_from_slice(&e);
            at += e.len();
        }
        d.extend(signed(rw_n0, RW));
        d
    }

    #[test]
    fn n0inv_matches_real_titan_keys() {
        // (low modulus word, key id) from the komodo/husky d3m2.ec.bin RO
        // key table, cp1a.260505.005.
        for (n0, id) in [
            (0x1053_5bb3, 0x952b_3485),
            (0x5321_b8cb, 0xd985_131d),
            (0xb10b_766b, 0x68f3_79bd),
        ] {
            assert_eq!(n0inv(n0), id);
        }
        for n0 in [1u32, 3, 0xffff_ffff, 0x1234_5679] {
            assert_eq!(n0.wrapping_mul(n0inv(n0)), u32::MAX);
        }
    }

    #[test]
    fn decodes_the_ro_rw_key_table() {
        let d = keyed_image(
            0x0000_0013,
            &[0x1111_1111, 0x2222_2223, 0x3333_3335],
            0x3333_3335,
        );
        let headers = signed_headers(&d);
        assert_eq!(headers.len(), 2);
        let table = embedded_keys(&d, &headers[0]);
        assert_eq!(
            table.iter().map(|k| k.keyid).collect::<Vec<_>>(),
            [n0inv(0x1111_1111), n0inv(0x2222_2223), n0inv(0x3333_3335)]
        );
        assert!(table
            .iter()
            .all(|k| k.bits == 2048 && k.exponent == Some(65537)));
        assert!(embedded_keys(&d, &headers[1]).is_empty());
        let rw = header_key(&d, headers[1].offset);
        assert_eq!(rw.keyid, headers[1].keyid);
        assert_eq!(rw.fingerprint, table[2].fingerprint);

        let report = key_report(&d, &headers).join("\n");
        assert!(report.contains("RO_A at 0x0: signed by"), "{report}");
        assert!(report.contains("key id matches its modulus"), "{report}");
        assert!(report.contains("accepts RW signed by 3 keys"), "{report}");
        assert!(report.contains(&format!(
            "RW_A key {:08x} is accepted by RO_A",
            n0inv(0x3333_3335)
        )));
    }

    #[test]
    fn flags_an_rw_key_no_ro_accepts() {
        let d = keyed_image(0x0000_0013, &[0x1111_1111], 0x4444_4445);
        let report = key_report(&d, &signed_headers(&d)).join("\n");
        assert!(
            report.contains("is NOT in any RO's key table here"),
            "{report}"
        );
    }

    #[test]
    fn keyid_rule_and_notes() {
        assert!(prod_by_rule(0x68f3_79bd)); // bit 2 set
        assert!(!prod_by_rule(0x894b_7d29)); // bit 2 clear
        assert!(keyid_note(0x68f3_79bd).contains("production by Cr50 rule"));
        assert!(keyid_note(0x894b_7d29).contains("komodo RO"));
        assert!(keyid_note(0x1234_5678).contains("not in the known-key table"));
        assert_eq!(parse_keyid("0x68f379bd").unwrap(), 0x68f3_79bd);
        assert_eq!(parse_keyid(" 68F379BD ").unwrap(), 0x68f3_79bd);
        assert!(parse_keyid("nothex").is_err());
    }

    // A chip with RO dev-keyed and RW prod-keyed, like Rick's komodo.
    fn keyed_chip() -> Vec<ChipHeader> {
        let h = |name: &str, keyid: &str| {
            parse_chip_header(
                name,
                &format!(
                    "(bootloader) hdr.ro_base: 80000\n(bootloader) hdr.keyid: {keyid}\nOKAY\n"
                ),
            )
        };
        vec![
            h("ro_a", "894b7d29"),
            h("rw_a", "68f379bd"),
            h("rw_b", "68f379bd"),
        ]
    }

    #[test]
    fn verify_reports_and_checks_rw_keys() {
        let c = keyed_chip();
        let (lines, ok) = verify_keys(&c, &[]);
        assert!(ok); // no expected -> report only
        assert!(lines.iter().any(|l| l.contains("RO_A: key 894b7d29")));
        assert!(lines
            .iter()
            .any(|l| l.contains("development-keyed while RW is production-keyed")));

        let (_, ok) = verify_keys(&c, &[0x68f3_79bd]);
        assert!(ok);
        let (lines, ok) = verify_keys(&c, &[0xdead_beef]);
        assert!(!ok);
        assert!(lines
            .iter()
            .any(|l| l.contains("MISMATCH") && l.contains("deadbeef")));
    }

    #[test]
    fn verify_fails_when_chip_has_no_rw_key() {
        let ro_only = vec![parse_chip_header(
            "ro_a",
            "(bootloader) hdr.ro_base: 80000\n(bootloader) hdr.keyid: 894b7d29\nOKAY\n",
        )];
        let (_, ok) = verify_keys(&ro_only, &[0x68f3_79bd]);
        assert!(!ok);
    }

    #[test]
    fn decodes_intel_hex() {
        let d = same();
        assert_eq!(&*rescue_payload(&intel_hex(&d)).unwrap(), d.as_slice());
        assert_eq!(&*rescue_payload(&d).unwrap(), d.as_slice());
        let mut bad = intel_hex(&d);
        let n = bad.len();
        bad[n - 2] = b'0';
        bad[n - 3] = b'0';
        assert!(rescue_payload(&bad).is_err());
    }

    fn zip_bytes(files: &[(&str, &[u8])]) -> Vec<u8> {
        let mut buf = std::io::Cursor::new(Vec::new());
        {
            let mut z = zip::ZipWriter::new(&mut buf);
            for (name, data) in files {
                z.start_file(*name, zip::write::SimpleFileOptions::default())
                    .unwrap();
                z.write_all(data).unwrap();
            }
            z.finish().unwrap();
        }
        buf.into_inner()
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("titan-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn finds_firmware_in_a_factory_zip() {
        let dir = tmp("zip");
        let inner = zip_bytes(&[("vendor.img", b"x"), ("firmware/d3m2.bin", &same())]);
        let pack = crate::fbpk::tests::build(&[("abl", 1, false, b"a")]);
        let outer = zip_bytes(&[
            ("komodo/image-komodo.zip", &inner),
            ("komodo/bootloader-komodo-x.img", &pack),
        ]);
        let path = dir.join("komodo-cp3a.260905.009-factory.zip");
        std::fs::write(&path, outer).unwrap();
        let (data, where_, product) = find_firmware(&path, "d3m2", &dir.join("scratch")).unwrap();
        assert_eq!(data, same());
        assert!(
            where_.ends_with("image-komodo.zip:firmware/d3m2.bin"),
            "{where_}"
        );
        assert_eq!(product.as_deref(), Some("komodo"));
        assert!(!dir.join("scratch/image-komodo.zip.part").exists());
    }

    #[test]
    fn finds_firmware_in_a_bootloader_pack_and_prefers_rec() {
        let dir = tmp("pack");
        let pack =
            crate::fbpk::tests::build(&[("abl", 1, false, b"a"), ("d3m2", 1, false, &same())]);
        let path = dir.join("bootloader-komodo-x.img");
        std::fs::write(&path, pack).unwrap();
        assert_eq!(find_firmware(&path, "d3m2", &dir).unwrap().0, same());
        let z = dir.join("komodo-x-factory.zip");
        std::fs::write(
            &z,
            zip_bytes(&[
                ("vendor/firmware/d3m2.bin", b"bin"),
                ("vendor/firmware/d3m2.rec", &same()),
            ]),
        )
        .unwrap();
        let (data, where_, _) = find_firmware(&z, "d3m2", &dir).unwrap();
        assert_eq!(data, same());
        assert!(where_.ends_with("d3m2.rec"));
    }

    #[test]
    fn plain_files_must_be_named_for_the_chip() {
        let dir = tmp("plain");
        for n in ["d3m2.bin", "ec.rec", "c1.bin"] {
            std::fs::write(dir.join(n), same()).unwrap();
        }
        assert!(find_firmware(&dir.join("d3m2.bin"), "d3m2", &dir).is_ok());
        assert!(find_firmware(&dir.join("ec.rec"), "d3m2", &dir).is_ok());
        assert!(find_firmware(&dir.join("c1.bin"), "d3m2", &dir)
            .unwrap_err()
            .to_string()
            .contains("not named"));
        let empty = tmp("empty");
        assert!(find_firmware(&empty, "d3m2", &empty).is_err());
    }
}
