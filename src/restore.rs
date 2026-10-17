//! Restore a phone that reaches fastboot from a full factory image, picking
//! the lowest build that installs.
//!
//! Everything downloaded is cached under `<cache>/images/` (see
//! `packs::cache_dir`): the listing page, each candidate's bootloader
//! (pulled by byte range, a few MB) and the full factory ZIP once a build is
//! chosen, so a retry or a second phone needs no new downloads. Nothing is
//! flashed without `--apply`.
//!
//! The first build tried is the oldest the phone's anti-rollback level
//! accepts (`arb::lowest_safe`); if one fails to install, the next newer is
//! tried. When that level cannot be established, only builds whose
//! bootloader is at least the one the phone runs now are considered, since
//! flashing below the level leaves the phone in ROM recovery.

use crate::arb::{self, Verdict};
use crate::fetch::{self, Image};
use anyhow::{bail, ensure, Context, Result};
use clap::Args;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant, SystemTime};

/// Re-read Google's listing after this long; until then the cached copy is used.
const LISTING_MAX_AGE: Duration = Duration::from_secs(12 * 3600);

#[derive(Args)]
pub struct RestoreArgs {
    /// Codename or model. Default: what the phone in fastboot reports.
    #[arg(long)]
    device: Option<String>,

    /// Restore exactly this build (e.g. cp3a.260905.009) instead of
    /// searching upward from the lowest; still refused if it is below the
    /// phone's rollback floor. With no phone attached this only downloads it
    /// into the cache.
    #[arg(long, short)]
    build: Option<String>,

    /// fastboot serial, when more than one phone is attached.
    #[arg(long, short)]
    serial: Option<String>,

    /// Flash. Without it the tool picks the build, downloads and unpacks it,
    /// and prints what it would run.
    #[arg(long)]
    apply: bool,

    /// Erase user data (`fastboot -w`). Needed when the build is older than
    /// the Android the phone last ran, or it will not boot.
    #[arg(long)]
    wipe: bool,

    /// Include carrier- and region-specific builds.
    #[arg(long)]
    carrier: bool,

    /// Allow a build at or past an anti-rollback bump the phone has not
    /// taken yet. Crossing one is permanent: a phone still below it may be
    /// the only kind whose ROM accepts the USB-bootable recovery packs.
    #[arg(long)]
    cross_rollback_bump: bool,

    /// Re-read Google's listing even if the cached copy is fresh.
    #[arg(long)]
    refresh: bool,

    /// Give up after this many builds fail to install.
    #[arg(long, default_value_t = 3)]
    attempts: usize,
}

pub fn images_root() -> PathBuf {
    crate::packs::cache_dir().join("images")
}

fn build_dir(image: &Image) -> PathBuf {
    images_root().join(&image.device).join(&image.build)
}

/// Factory images for `device` and the listing page they came from, cached
/// when fresh, or when Google cannot be reached.
pub fn list_images(device: &str, refresh: bool) -> Result<(Vec<Image>, String)> {
    let path = images_root().join("listing.html");
    let age = std::fs::metadata(&path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| SystemTime::now().duration_since(t).ok());
    let html = match age {
        Some(a) if !refresh && a < LISTING_MAX_AGE => std::fs::read_to_string(&path)?,
        _ => match fetch::listing_html() {
            Ok(html) => {
                std::fs::create_dir_all(images_root())?;
                let tmp = path.with_extension("html.part");
                std::fs::write(&tmp, &html)?;
                std::fs::rename(&tmp, &path)?;
                html
            }
            Err(e) if age.is_some() => {
                eprintln!(
                    "warning: {e:#}; using the listing cached {} h ago",
                    age.unwrap().as_secs() / 3600
                );
                std::fs::read_to_string(&path)?
            }
            Err(e) => return Err(e),
        },
    };
    let images = fetch::parse_images(&html, device);
    ensure!(
        !images.is_empty(),
        "no factory images for '{device}' on {}; check the codename",
        fetch::IMAGES_PAGE
    );
    Ok((images, html))
}

/// The builds to try, oldest first, one row per build.
pub fn candidates(images: &[Image], build: Option<&str>, carrier: bool) -> Vec<Image> {
    if let Some(b) = build {
        let b = b.to_ascii_lowercase();
        return images
            .iter()
            .filter(|i| i.build == b)
            .take(1)
            .cloned()
            .collect();
    }
    let mut out: Vec<Image> = images
        .iter()
        .filter(|i| carrier || !fetch::is_carrier(i))
        .cloned()
        .collect();
    out.sort_by_key(|i| i.build_key());
    out.dedup_by(|a, b| a.build == b.build);
    out
}

/// `bootloader-husky-ripcurrent-17.0-15681527.img` -> `ripcurrent-17.0-15681527`,
/// the form `fastboot getvar version-bootloader` reports.
pub fn bootloader_version(path: &Path, device: &str) -> Option<String> {
    let name = path.file_name()?.to_str()?;
    let v = name
        .strip_prefix(&format!("bootloader-{device}-"))?
        .strip_suffix(".img")?;
    (!v.is_empty()).then(|| v.to_string())
}

/// Every number in a version string, in order, so `ripcurrent-14.5-11693900`
/// sorts before `ripcurrent-17.0-15681527`.
pub fn version_key(v: &str) -> Vec<u64> {
    v.split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect()
}

fn find_file(dir: &Path, prefix: &str, suffix: &str) -> Option<PathBuf> {
    std::fs::read_dir(dir)
        .ok()?
        .flatten()
        .map(|e| e.path())
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with(prefix) && n.ends_with(suffix))
        })
}

/// The first anti-rollback bump for `device` after `month`.
pub fn next_bump(device: &str, month: arb::Month) -> Option<arb::Month> {
    arb::BUMPS
        .iter()
        .find(|(d, _)| *d == device)
        .and_then(|(_, bumps)| bumps.iter().copied().filter(|b| *b > month).min())
}

/// Bootloader versions of the builds, newest first, down to the ones
/// carrying `phone` (what `arb` needs to place the phone). Each is a few MB
/// by byte range, then cached.
fn phone_bootloaders(
    images: &[Image],
    device: &str,
    phone: &str,
) -> Result<HashMap<String, String>> {
    let mut newest: Vec<&Image> = images.iter().collect();
    newest.sort_by_key(|i| std::cmp::Reverse(i.build_key()));
    let mut map = HashMap::new();
    for image in newest {
        let bl = cached_bootloader(image)?;
        let Some(v) = bootloader_version(&bl, device) else {
            continue;
        };
        let older = version_key(&v) < version_key(phone);
        map.insert(image.build.clone(), v);
        if older {
            break;
        }
    }
    Ok(map)
}

/// The build's bootloader-*.img, from the cache or by byte range.
fn cached_bootloader(image: &Image) -> Result<PathBuf> {
    let dir = build_dir(image);
    if let Some(p) = find_file(&dir, "bootloader-", ".img") {
        return Ok(p);
    }
    fetch::fetch_bootloader(image, &dir)
}

/// The full factory ZIP, from the cache when it already passed its
/// SHA-256 check, else downloaded (resuming a partial one).
fn cached_zip(image: &Image) -> Result<PathBuf> {
    let dir = images_root().join(&image.device);
    let path = dir.join(image.file_name());
    let marker = path.with_extension("zip.sha256");
    if let (Some(want), true) = (&image.sha256, path.is_file()) {
        if std::fs::read_to_string(&marker).is_ok_and(|got| got.trim() == want) {
            eprintln!("cached: {}", path.display());
            return Ok(path);
        }
    }
    let path = fetch::fetch_full(image, &dir)?;
    if let Some(sha) = &image.sha256 {
        std::fs::write(&marker, sha)?;
    }
    Ok(path)
}

#[derive(Debug, Default)]
struct Parts {
    bootloader: Option<PathBuf>,
    radio: Option<PathBuf>,
    image: Option<PathBuf>,
    flash_all: Option<PathBuf>,
}

/// Unpack what flashing needs from the factory ZIP into `dir`, skipping
/// files already there at the right size.
fn extract_parts(zip_path: &Path, dir: &Path) -> Result<Parts> {
    std::fs::create_dir_all(dir)?;
    let file = std::fs::File::open(zip_path)?;
    let mut archive =
        zip::ZipArchive::new(file).with_context(|| format!("reading {}", zip_path.display()))?;
    let mut parts = Parts::default();
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i)?;
        let name = entry.name().rsplit('/').next().unwrap_or("").to_string();
        let slot = if name.starts_with("bootloader-") && name.ends_with(".img") {
            &mut parts.bootloader
        } else if name.starts_with("radio-") && name.ends_with(".img") {
            &mut parts.radio
        } else if name.starts_with("image-") && name.ends_with(".zip") {
            &mut parts.image
        } else if name == "flash-all.sh" {
            &mut parts.flash_all
        } else {
            continue;
        };
        let out = dir.join(&name);
        let have = std::fs::metadata(&out).map(|m| m.len()).ok();
        if have != Some(entry.size()) {
            eprintln!("unpacking {name} ({:.2} GB) ...", entry.size() as f64 / 1e9);
            let tmp = out.with_extension("part");
            let mut dst = std::fs::File::create(&tmp)?;
            std::io::copy(&mut entry, &mut dst).with_context(|| format!("unpacking {name}"))?;
            std::fs::rename(&tmp, &out)?;
        }
        *slot = Some(out);
    }
    ensure!(
        parts.bootloader.is_some() && parts.image.is_some(),
        "{} has no bootloader-*.img or image-*.zip; is it a factory image?",
        zip_path.display()
    );
    Ok(parts)
}

/// `fastboot` lines in flash-all.sh that the standard sequence here does
/// not cover, so a build that needs something unusual is noticed.
pub fn unusual_flash_steps(script: &str) -> Vec<String> {
    script
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with("fastboot "))
        .filter(|l| {
            let words: Vec<&str> = l.split_whitespace().collect();
            !matches!(
                words.as_slice(),
                ["fastboot", "flash", "bootloader", _]
                    | ["fastboot", "flash", "radio", _]
                    | ["fastboot", "reboot-bootloader"]
                    | [
                        "fastboot",
                        "--slot=other",
                        "flash",
                        "bootloader" | "radio",
                        _
                    ]
                    | [
                        "fastboot",
                        "flash",
                        "--slot=other",
                        "bootloader" | "radio",
                        _
                    ]
                    | ["fastboot", "-w", "update", _]
                    | ["fastboot", "update", _]
            )
        })
        .map(String::from)
        .collect()
}

/// Serials `fastboot devices` lists.
pub fn parse_fastboot_devices(text: &str) -> Vec<String> {
    text.lines()
        .filter_map(|l| {
            let mut cols = l.split_whitespace();
            let serial = cols.next()?;
            matches!(cols.next(), Some("fastboot" | "fastbootd")).then(|| serial.to_string())
        })
        .collect()
}

/// The value of `name` in `fastboot getvar` output (which goes to stderr).
pub fn parse_getvar(text: &str, name: &str) -> Option<String> {
    text.lines().find_map(|l| {
        let l = l.trim();
        let l = l.strip_prefix("(bootloader)").map(str::trim).unwrap_or(l);
        let v = l.strip_prefix(name)?.strip_prefix(':')?.trim();
        (!v.is_empty()).then(|| v.to_string())
    })
}

fn fastboot_serials() -> Result<Vec<String>> {
    let out = Command::new("fastboot")
        .arg("devices")
        .output()
        .context("running fastboot; install Android platform-tools")?;
    Ok(parse_fastboot_devices(&String::from_utf8_lossy(
        &out.stdout,
    )))
}

fn getvar(serial: &str, name: &str) -> Option<String> {
    let out = Command::new("fastboot")
        .args(["-s", serial, "getvar", name])
        .output()
        .ok()?;
    let mut text = String::from_utf8_lossy(&out.stderr).into_owned();
    text.push_str(&String::from_utf8_lossy(&out.stdout));
    parse_getvar(&text, name)
}

/// Run a fastboot command with its output shown.
fn fastboot(serial: &str, args: &[&str]) -> Result<()> {
    println!("$ fastboot -s {serial} {}", args.join(" "));
    let status = Command::new("fastboot")
        .args(["-s", serial])
        .args(args)
        .status()
        .context("running fastboot")?;
    ensure!(
        status.success(),
        "fastboot {} failed ({status})",
        args.join(" ")
    );
    Ok(())
}

fn wait_for_fastboot(serial: &str, secs: u64) -> Result<()> {
    std::thread::sleep(Duration::from_secs(2));
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(secs) {
        if fastboot_serials()?.iter().any(|s| s == serial) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    bail!("{serial} did not come back to fastboot within {secs} s")
}

/// Anti-rollback counters the bootloader reports: application processor
/// (non-secure, secure) and GSA (BL1, firmware). A flashed bootloader that
/// raises one has crossed a rollback bump.
const AR_VARS: [&str; 4] = ["ap-ar-ns", "ap-ar-s", "gsa-bl1-ar", "gsa-fw-ar"];

type Counters = Vec<(&'static str, String)>;

fn read_counters(serial: &str) -> Counters {
    AR_VARS
        .iter()
        .filter_map(|n| getvar(serial, n).map(|v| (*n, v)))
        .collect()
}

pub fn format_counters(c: &[(&str, String)]) -> String {
    c.iter()
        .map(|(n, v)| format!("{n}={v}"))
        .collect::<Vec<_>>()
        .join(" ")
}

/// Counters that went up between `before` and `after`.
pub fn raised(before: &[(&str, String)], after: &[(&str, String)]) -> Vec<String> {
    let num = |v: &str| {
        let v = v.trim();
        v.strip_prefix("0x").map_or_else(
            || v.parse::<u64>().ok(),
            |h| u64::from_str_radix(h, 16).ok(),
        )
    };
    after
        .iter()
        .filter_map(|(n, a)| {
            let (_, b) = before.iter().find(|(m, _)| m == n)?;
            (num(a)? > num(b)?).then(|| format!("{n} {b} -> {a}"))
        })
        .collect()
}

/// Remember which counters a bootloader version runs with, in
/// `<cache>/images/<device>/anti-rollback.tsv`, as evidence for the floor.
fn record_counters(device: &str, bootloader: &str, c: &[(&str, String)]) {
    if c.is_empty() {
        return;
    }
    let path = images_root().join(device).join("anti-rollback.tsv");
    let line = format!("{bootloader}\t{}\n", format_counters(c).replace(' ', "\t"));
    let old = std::fs::read_to_string(&path).unwrap_or_default();
    if !old.contains(&line) {
        let _ = std::fs::create_dir_all(path.parent().unwrap());
        let _ = std::fs::write(&path, old + &line);
    }
}

/// The counters last recorded for `bootloader`, if any.
fn seen_counters(device: &str, bootloader: &str) -> Option<Counters> {
    let text =
        std::fs::read_to_string(images_root().join(device).join("anti-rollback.tsv")).ok()?;
    parse_counter_log(&text, bootloader)
}

pub fn parse_counter_log(text: &str, bootloader: &str) -> Option<Counters> {
    let line = text
        .lines()
        .rev()
        .find(|l| l.split('\t').next() == Some(bootloader))?;
    Some(
        line.split('\t')
            .skip(1)
            .filter_map(|f| {
                let (n, v) = f.split_once('=')?;
                let name = AR_VARS.iter().find(|a| **a == n)?;
                Some((*name, v.to_string()))
            })
            .collect(),
    )
}

struct Phone {
    serial: String,
    counters: Counters,
    product: Option<String>,
    bootloader: Option<String>,
    baseband: Option<String>,
    unlocked: bool,
    userspace: bool,
}

impl Phone {
    fn read(serial: &str) -> Phone {
        Phone {
            serial: serial.to_string(),
            counters: read_counters(serial),
            product: getvar(serial, "product"),
            bootloader: getvar(serial, "version-bootloader"),
            baseband: getvar(serial, "version-baseband"),
            unlocked: getvar(serial, "unlocked").as_deref() == Some("yes"),
            userspace: getvar(serial, "is-userspace").as_deref() == Some("yes"),
        }
    }
}

fn pick_phone(wanted: Option<&str>) -> Result<Option<Phone>> {
    let serials = match fastboot_serials() {
        Ok(s) => s,
        // Downloading into the cache needs no fastboot.
        Err(_) if wanted.is_none() => return Ok(None),
        Err(e) => return Err(e),
    };
    let serial = match (wanted, serials.as_slice()) {
        (Some(w), _) if serials.iter().any(|s| s == w) => w.to_string(),
        (Some(w), _) => bail!("{w} is not in fastboot (fastboot devices: {serials:?})"),
        (None, []) => return Ok(None),
        (None, [one]) => one.clone(),
        (None, many) => bail!(
            "{} phones in fastboot ({}); pick one with --serial",
            many.len(),
            many.join(", ")
        ),
    };
    Ok(Some(Phone::read(&serial)))
}

/// The standard flash-all sequence, checking each step took. The inactive
/// slot gets the same bootloader and radio last: one left below the
/// rollback level bricks the phone if it ever falls back to that slot.
fn install(phone: &Phone, parts: &Parts, want_bootloader: &str, wipe: bool) -> Result<()> {
    let s = &phone.serial;
    let path = |p: &Option<PathBuf>| p.as_ref().unwrap().to_string_lossy().into_owned();
    fastboot(s, &["flash", "bootloader", &path(&parts.bootloader)])?;
    fastboot(s, &["reboot-bootloader"])?;
    wait_for_fastboot(s, 90)?;
    let now = getvar(s, "version-bootloader");
    ensure!(
        now.as_deref() == Some(want_bootloader),
        "bootloader reports {} after flashing {want_bootloader}",
        now.as_deref().unwrap_or("nothing")
    );
    let after = read_counters(s);
    if let Some(prod) = &phone.product {
        record_counters(prod, want_bootloader, &after);
    }
    let up = raised(&phone.counters, &after);
    if !up.is_empty() {
        println!(
            "WARNING: bootloader {want_bootloader} raised anti-rollback counters ({}); \
             older builds can no longer boot on this phone",
            up.join(", ")
        );
    }
    if parts.radio.is_some() {
        fastboot(s, &["flash", "radio", &path(&parts.radio)])?;
        fastboot(s, &["reboot-bootloader"])?;
        wait_for_fastboot(s, 90)?;
    }
    fastboot(
        s,
        &[
            "--slot=other",
            "flash",
            "bootloader",
            &path(&parts.bootloader),
        ],
    )?;
    if parts.radio.is_some() {
        fastboot(s, &["--slot=other", "flash", "radio", &path(&parts.radio)])?;
    }
    let image = path(&parts.image);
    let mut args = vec![];
    if wipe {
        args.push("-w");
    }
    args.extend(["update", image.as_str()]);
    fastboot(s, &args)
}

pub fn run(a: RestoreArgs) -> Result<()> {
    let phone = pick_phone(a.serial.as_deref())?;
    let device = match (&a.device, &phone) {
        (Some(d), _) => fetch::codename(d)?,
        (None, Some(p)) => p
            .product
            .clone()
            .context("the phone did not report its product; pass --device")?,
        (None, None) => bail!(
            "no phone in fastboot; connect one, or pass --device and --build to just download"
        ),
    };
    match &phone {
        Some(p) => {
            println!(
                "{}: {} in fastboot, bootloader {}, baseband {}, {}",
                p.serial,
                p.product.as_deref().unwrap_or("?"),
                p.bootloader.as_deref().unwrap_or("?"),
                p.baseband.as_deref().unwrap_or("?"),
                if p.unlocked { "unlocked" } else { "locked" }
            );
            if !p.counters.is_empty() {
                println!("anti-rollback counters: {}", format_counters(&p.counters));
            }
            if let (Some(prod), Some(bl)) = (&p.product, &p.bootloader) {
                record_counters(prod, bl, &p.counters);
            }
            if let Some(prod) = &p.product {
                ensure!(*prod == device, "the phone is {prod}, not {device}");
            }
            ensure!(
                !p.userspace,
                "the phone is in fastbootd; run `fastboot reboot bootloader` first"
            );
            ensure!(
                p.unlocked || !a.apply,
                "the bootloader is locked and refuses to flash; this tool does not \
                 unlock it (that wipes the phone)"
            );
        }
        None => ensure!(
            !a.apply && a.build.is_some(),
            "no phone in fastboot; only `--device X --build B` without --apply works without one"
        ),
    }

    let (images, html) = list_images(&device, a.refresh)?;
    let mut list = candidates(&images, a.build.as_deref(), a.carrier);
    ensure!(
        !list.is_empty(),
        "no build '{}' for {device}; `pixel-restore fetch {device} --list` shows them",
        a.build.as_deref().unwrap_or("?")
    );

    // How far down the phone may go: Some(map) when arb can place it, None
    // when only "no older bootloader than now" is known to be safe.
    let mut checked: Option<HashMap<String, String>> = None;
    let mut phone_bl = String::new();
    if let Some(p) = &phone {
        phone_bl = p.bootloader.clone().context(
            "the phone did not report version-bootloader, so its rollback level is unknown",
        )?;
        let bootloaders = phone_bootloaders(&images, &device, &phone_bl)?;
        match arb::lowest_safe(&device, &phone_bl, &images, &bootloaders, &html, None) {
            Ok(choice) => {
                println!(
                    "the phone's bootloader is from {}; floor {}; lowest safe build {}",
                    choice.phone_build,
                    choice.floor.as_deref().unwrap_or("none"),
                    choice.image.build
                );
                println!("{}", choice.reason);
                if a.build.is_none() {
                    let lowest = choice.image.build_key();
                    list.retain(|i| i.build_key() >= lowest);
                }
                let phone_month = images
                    .iter()
                    .find(|i| i.build == choice.phone_build)
                    .and_then(arb::image_month);
                if let Some(bump) = phone_month.and_then(|m| next_bump(&device, m)) {
                    if !a.cross_rollback_bump {
                        println!(
                            "the phone has not taken the {} anti-rollback bump; staying below it \
                             (--cross-rollback-bump to allow)",
                            arb::month_name(bump)
                        );
                        list.retain(|i| arb::image_month(i).is_some_and(|m| m < bump));
                        ensure!(
                            !list.is_empty(),
                            "{} is at or past that bump; pass --cross-rollback-bump to flash it \
                             anyway, which cannot be undone",
                            a.build.as_deref().unwrap_or("every build")
                        );
                    }
                }
                checked = Some(bootloaders);
            }
            Err(e) => println!(
                "rollback floor unknown ({e:#}); only builds whose bootloader is at least \
                 {phone_bl} will be used"
            ),
        }
    }

    let mut failures = 0;
    for image in &list {
        let bl = cached_bootloader(image)?;
        let version = bootloader_version(&bl, &device)
            .with_context(|| format!("cannot read a version from {}", bl.display()))?;
        if let Some(p) = &phone {
            let p_counters = &p.counters;
            let refusal = match &checked {
                Some(map) => {
                    match arb::verdict(&device, &phone_bl, &images, map, &html, &image.build)? {
                        Verdict::Safe => None,
                        Verdict::Refused(why) => Some(why),
                    }
                }
                // Unknown floor: the phone's own bootloader is the only level
                // known to be both reachable and not past a new bump.
                None if version_key(&version) < version_key(&phone_bl) => Some(format!(
                    "bootloader {version} is older than the phone's {phone_bl}"
                )),
                None if version != phone_bl && !a.cross_rollback_bump => Some(format!(
                    "bootloader {version} is newer than the phone's {phone_bl} and the rollback \
                     level is unknown (--cross-rollback-bump to allow)"
                )),
                None => None,
            };
            // Counters seen with this bootloader before catch bumps Google
            // never announced.
            let refusal = refusal.or_else(|| {
                let seen = seen_counters(&device, &version)?;
                match arb::counters_verdict(p_counters, &seen) {
                    Verdict::Safe => None,
                    Verdict::Refused(why) => Some(why),
                }
            });
            if let Some(why) = refusal {
                ensure!(a.build.is_none(), "{why}");
                println!("skip {}: {why}", image.build);
                continue;
            }
        }
        println!(
            "\n{} ({}), bootloader {version}",
            image.build, image.description
        );
        println!(
            "(downloading accepts Google's factory-image terms, as on {})",
            fetch::IMAGES_PAGE
        );
        let zip = cached_zip(image)?;
        let parts = extract_parts(&zip, &build_dir(image))?;
        if let Some(script) = &parts.flash_all {
            for step in unusual_flash_steps(&std::fs::read_to_string(script)?) {
                println!(
                    "note: this build's flash-all.sh also runs `{step}`, which restore does not"
                );
            }
        }
        let Some(phone) = &phone else {
            println!("cached in {}", build_dir(image).display());
            return Ok(());
        };
        if !a.apply {
            println!(
                "would flash bootloader, radio, then `fastboot {}update` from {}",
                if a.wipe { "-w " } else { "" },
                build_dir(image).display()
            );
            if !a.wipe {
                println!("(add --wipe if this is older than the Android the phone last ran)");
            }
            println!("dry run; pass --apply to flash");
            return Ok(());
        }
        match install(phone, &parts, &version, a.wipe) {
            Ok(()) => {
                println!(
                    "\n{} installed {}; the phone is rebooting into Android.",
                    phone.serial, image.build
                );
                return Ok(());
            }
            Err(e) => {
                failures += 1;
                eprintln!("{} did not install: {e:#}", image.build);
                ensure!(
                    failures < a.attempts,
                    "giving up after {failures} failed builds"
                );
                wait_for_fastboot(&phone.serial, 60)
                    .context("the phone is not back in fastboot, so not trying the next build")?;
                println!("trying the next newer build");
            }
        }
    }
    bail!("no build for {device} installed")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn img(build: &str, description: &str) -> Image {
        Image {
            device: "husky".into(),
            build: build.into(),
            url: format!("https://dl.google.com/dl/android/aosp/husky-{build}-factory-0.zip"),
            sha256: None,
            description: description.into(),
        }
    }

    #[test]
    fn candidates_are_oldest_first_without_carrier_rows() {
        let images = [
            img("cp3a.260905.009", "17.0.0 (CP3A.260905.009, Sep 2026)"),
            img("ud1a.231105.004", "14.0.0 (UD1A.231105.004, Nov 2023)"),
            img(
                "cp3a.260905.009.v1",
                "17.0.0 (CP3A.260905.009.V1, Sep 2026, Verizon)",
            ),
            img("ap3a.241005.015", "15.0.0 (AP3A.241005.015, Oct 2024)"),
        ];
        let builds: Vec<String> = candidates(&images, None, false)
            .into_iter()
            .map(|i| i.build)
            .collect();
        assert_eq!(
            builds,
            ["ud1a.231105.004", "ap3a.241005.015", "cp3a.260905.009"]
        );
        assert_eq!(candidates(&images, None, true).len(), 4);
        let pinned = candidates(&images, Some("AP3A.241005.015"), false);
        assert_eq!(pinned[0].build, "ap3a.241005.015");
        assert!(candidates(&images, Some("nope"), false).is_empty());
    }

    #[test]
    fn bootloader_versions_compare_numerically() {
        let v = bootloader_version(
            Path::new("/x/bootloader-husky-ripcurrent-17.0-15681527.img"),
            "husky",
        );
        assert_eq!(v.as_deref(), Some("ripcurrent-17.0-15681527"));
        assert!(bootloader_version(Path::new("bootloader-shiba-x.img"), "husky").is_none());
        assert!(version_key("ripcurrent-14.5-11693900") < version_key("ripcurrent-17.0-15681527"));
        assert!(version_key("ripcurrent-9.1-1") < version_key("ripcurrent-10.0-1"));
        assert_eq!(
            version_key("ripcurrent-17.0-15681527"),
            version_key("ripcurrent-17.0-15681527")
        );
    }

    #[test]
    fn finds_the_bump_a_phone_has_not_taken() {
        assert_eq!(next_bump("shiba", (2024, 7)), Some((2025, 5)));
        assert_eq!(next_bump("shiba", (2025, 5)), None);
        assert_eq!(next_bump("oriole", (2022, 1)), Some((2022, 8)));
        assert_eq!(next_bump("komodo", (2024, 7)), None);
    }

    #[test]
    fn reads_the_counter_log() {
        let log =
            "ripcurrent-15.0-2\tap-ar-ns=2\tgsa-bl1-ar=2\nripcurrent-17.0-3\tap-ar-ns=3\tbogus=1\n";
        let c = parse_counter_log(log, "ripcurrent-17.0-3").unwrap();
        assert_eq!(c, vec![("ap-ar-ns", "3".to_string())]);
        assert_eq!(
            parse_counter_log(log, "ripcurrent-15.0-2").unwrap().len(),
            2
        );
        assert!(parse_counter_log(log, "ripcurrent-9.0-1").is_none());
    }

    #[test]
    fn notices_raised_counters() {
        let before = vec![
            ("ap-ar-ns", "2".to_string()),
            ("gsa-bl1-ar", "2".to_string()),
        ];
        let after = vec![
            ("ap-ar-ns", "3".to_string()),
            ("gsa-bl1-ar", "2".to_string()),
        ];
        assert_eq!(raised(&before, &after), ["ap-ar-ns 2 -> 3"]);
        assert!(raised(&before, &before).is_empty());
        assert!(raised(&[], &after).is_empty());
        assert_eq!(
            raised(&[("x", "0x2".into())], &[("x", "0x10".into())]),
            ["x 0x2 -> 0x10"]
        );
        assert_eq!(format_counters(&before), "ap-ar-ns=2 gsa-bl1-ar=2");
    }

    #[test]
    fn parses_fastboot_output() {
        let devices = "45171FDJG0010W\tfastboot\n44260DLJH000Q3\t fastbootd\n\nnoise\n";
        assert_eq!(
            parse_fastboot_devices(devices),
            ["45171FDJG0010W", "44260DLJH000Q3"]
        );
        let getvar = "(bootloader) version-bootloader: ripcurrent-17.0-15681527\nFinished. Total time: 0.001s\n";
        assert_eq!(
            parse_getvar(getvar, "version-bootloader").as_deref(),
            Some("ripcurrent-17.0-15681527")
        );
        assert_eq!(
            parse_getvar("product: husky\n", "product").as_deref(),
            Some("husky")
        );
        assert!(parse_getvar("product: \n", "product").is_none());
        assert!(parse_getvar("(bootloader) product-x: y\n", "product").is_none());
    }

    #[test]
    fn flags_flash_all_steps_restore_does_not_run() {
        let script =
            "#!/bin/sh\nfastboot flash bootloader bootloader-husky-ripcurrent-17.0-15681527.img\n\
            fastboot reboot-bootloader\nsleep 5\nfastboot flash radio radio-husky-g5300q.img\n\
            fastboot reboot-bootloader\nfastboot -w update image-husky-cp3a.260905.009.zip\n";
        assert!(unusual_flash_steps(script).is_empty());
        let extra = format!("{script}fastboot erase misc\n");
        assert_eq!(unusual_flash_steps(&extra), ["fastboot erase misc"]);
    }
}
