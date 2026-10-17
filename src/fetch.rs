//! Find and download Pixel factory images from Google's public listing at
//! <https://developers.google.com/android/images>. Every image on that page
//! is a `https://dl.google.com/dl/android/aosp/<device>-<build>-factory-<id>.zip`
//! link next to its SHA-256, so the page itself is the index.
//!
//! Downloading an image means accepting Google's factory-image terms, the
//! same as clicking "Acknowledge" on the page; the tool says so before it
//! starts.

use crate::remotezip::{self, RangeSource};
use anyhow::{anyhow, bail, ensure, Context, Result};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

pub const IMAGES_PAGE: &str = "https://developers.google.com/android/images";
const LINK_PREFIX: &str = "https://dl.google.com/dl/android/aosp/";
/// The cookie the page's "Acknowledge" button sets. Without it the page is
/// served with the terms wall only and no image links.
const TOS_ACK_COOKIE: &str = "devsite_wall_acks=nexus-image-tos";

/// Tensor-era Pixel codenames, so `--device "pixel 9 pro xl"` works too.
pub const DEVICES: &[(&str, &str)] = &[
    ("oriole", "Pixel 6"),
    ("raven", "Pixel 6 Pro"),
    ("bluejay", "Pixel 6a"),
    ("panther", "Pixel 7"),
    ("cheetah", "Pixel 7 Pro"),
    ("lynx", "Pixel 7a"),
    ("felix", "Pixel Fold"),
    ("tangorpro", "Pixel Tablet"),
    ("shiba", "Pixel 8"),
    ("husky", "Pixel 8 Pro"),
    ("akita", "Pixel 8a"),
    ("tokay", "Pixel 9"),
    ("caiman", "Pixel 9 Pro"),
    ("komodo", "Pixel 9 Pro XL"),
    ("comet", "Pixel 9 Pro Fold"),
    ("tegu", "Pixel 9a"),
    ("frankel", "Pixel 10"),
    ("blazer", "Pixel 10 Pro"),
    ("mustang", "Pixel 10 Pro XL"),
    ("rango", "Pixel 10 Pro Fold"),
];

/// Turn "komodo", "Pixel 9 Pro XL" or "pixel-9-pro-xl" into a codename.
pub fn codename(input: &str) -> Result<String> {
    let norm = |s: &str| {
        s.to_ascii_lowercase()
            .chars()
            .filter(|c| c.is_ascii_alphanumeric())
            .collect::<String>()
    };
    let want = norm(input);
    if DEVICES.iter().any(|(c, _)| *c == want) {
        return Ok(want);
    }
    if let Some((c, _)) = DEVICES.iter().find(|(_, n)| norm(n) == want) {
        return Ok(c.to_string());
    }
    // Pre-Tensor Pixels still have factory images, and naming one should get
    // `boot` to say why it can't help rather than "unknown device".
    if let Some(m) = crate::devices::QUALCOMM_MODELS
        .iter()
        .find(|m| m.codename == want || norm(m.name) == want)
    {
        return Ok(m.codename.to_string());
    }
    if input.chars().all(|c| c.is_ascii_alphanumeric()) && !input.is_empty() {
        // An unlisted codename; let the page decide whether it exists.
        return Ok(input.to_ascii_lowercase());
    }
    bail!(
        "unknown device '{input}'. Known: {}",
        DEVICES
            .iter()
            .map(|(c, n)| format!("{c} ({n})"))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Image {
    pub device: String,
    /// e.g. `cp3a.260905.009`
    pub build: String,
    pub url: String,
    pub sha256: Option<String>,
    /// The row's description, e.g. `17.0.0 (CP3A.260905.009, Sep 2026)`.
    pub description: String,
}

impl Image {
    /// An image named only by its URL, when the listing page is bypassed.
    pub fn from_url(device: &str, url: &str) -> Result<Image> {
        let file = url.rsplit('/').next().unwrap_or(url);
        ensure!(file.ends_with(".zip"), "{url} does not point at a .zip");
        let build = file
            .strip_prefix(&format!("{device}-"))
            .and_then(|r| r.split("-factory-").next())
            .unwrap_or("unknown")
            .to_string();
        Ok(Image {
            device: device.to_string(),
            build,
            url: url.to_string(),
            sha256: None,
            description: format!("direct URL {file}"),
        })
    }

    pub fn file_name(&self) -> &str {
        self.url.rsplit('/').next().unwrap_or(&self.url)
    }

    /// Sort key: the YYMMDD in the middle of the build id, then its suffix,
    /// so newer builds compare greater regardless of the leading tag.
    fn build_key(&self) -> (u32, u32) {
        let mut parts = self.build.split('.');
        let _tag = parts.next();
        let date = parts.next().and_then(|d| d.parse().ok()).unwrap_or(0);
        let num = parts.next().and_then(|n| n.parse().ok()).unwrap_or(0);
        (date, num)
    }
}

fn strip_tags(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_tag = false;
    for c in s.chars() {
        match c {
            '<' => in_tag = true,
            '>' => in_tag = false,
            _ if !in_tag => out.push(c),
            _ => {}
        }
    }
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Pull every factory-image link for `device` out of the page HTML.
pub fn parse_images(html: &str, device: &str) -> Vec<Image> {
    let needle = format!("{LINK_PREFIX}{device}-");
    let mut found = Vec::new();
    let mut from = 0usize;
    while let Some(rel) = html[from..].find(&needle) {
        let start = from + rel;
        let Some(end_rel) = html[start..].find('"') else {
            break;
        };
        let url = &html[start..start + end_rel];
        from = start + end_rel;
        if !url.ends_with(".zip") || !url.contains("-factory-") {
            continue;
        }
        let file = url.rsplit('/').next().unwrap_or(url);
        let rest = &file[device.len() + 1..];
        let Some(build) = rest.split("-factory-").next() else {
            continue;
        };

        // The SHA-256 is the first 64-hex-digit run after the link.
        let window = &html[from..(from + 4000).min(html.len())];
        let sha256 = window
            .split(|c: char| !c.is_ascii_hexdigit())
            .find(|t| t.len() == 64)
            .map(|t| t.to_ascii_lowercase());

        // The description is the text of the row this link sits in.
        let row_start = html[..start].rfind("<tr").unwrap_or(start);
        let description = strip_tags(&html[row_start..start])
            .trim_end_matches("Link")
            .trim()
            .trim_end_matches("Flash")
            .trim()
            .to_string();

        found.push(Image {
            device: device.to_string(),
            build: build.to_string(),
            url: url.to_string(),
            sha256,
            description,
        });
    }
    found
}

/// The newest image for a device, skipping carrier-specific rows unless
/// nothing else exists.
pub fn pick_latest<'a>(images: &'a [Image], build: Option<&str>) -> Option<&'a Image> {
    if let Some(b) = build {
        let b = b.to_ascii_lowercase();
        return images.iter().rev().find(|i| i.build == b);
    }
    let is_carrier = |i: &Image| {
        let d = i.description.to_ascii_lowercase();
        [
            "verizon", "t-mobile", "at&t", "telstra", "softbank", "kddi", "docomo", "emea", "jp",
            "tw",
        ]
        .iter()
        .any(|c| d.contains(&format!("({c}")) || d.contains(&format!(", {c}")))
    };
    let generic: Vec<&Image> = images.iter().filter(|i| !is_carrier(i)).collect();
    let pool = if generic.is_empty() {
        images.iter().collect()
    } else {
        generic
    };
    pool.into_iter()
        .enumerate()
        .max_by_key(|(idx, i)| (i.build_key(), *idx))
        .map(|(_, i)| i)
}

pub(crate) fn agent() -> ureq::Agent {
    ureq::Agent::config_builder()
        .http_status_as_error(false)
        .user_agent(concat!("pixel-restore/", env!("CARGO_PKG_VERSION")))
        .build()
        .into()
}

/// Download the listing page and return the images for `device`.
pub fn list_images(device: &str) -> Result<Vec<Image>> {
    eprintln!("reading {IMAGES_PAGE} ...");
    let mut resp = agent()
        .get(IMAGES_PAGE)
        .header("Cookie", TOS_ACK_COOKIE)
        .call()
        .context("fetching the factory image listing")?;
    ensure!(
        resp.status() == 200,
        "listing page returned HTTP {}",
        resp.status()
    );
    let html = resp
        .body_mut()
        .with_config()
        .limit(256 * 1024 * 1024)
        .read_to_string()
        .context("reading the factory image listing")?;
    let images = parse_images(&html, device);
    if images.is_empty() {
        bail!("no factory images for '{device}' on {IMAGES_PAGE}; check the codename");
    }
    Ok(images)
}

/// Byte-range reads over HTTP, for pulling one file out of the ZIP.
pub struct HttpRange {
    agent: ureq::Agent,
    url: String,
    len: u64,
    pub bytes_read: u64,
}

impl HttpRange {
    pub fn open(url: &str) -> Result<HttpRange> {
        let agent = agent();
        let resp = agent
            .head(url)
            .call()
            .with_context(|| format!("HEAD {url}"))?;
        ensure!(
            resp.status() == 200,
            "{url} returned HTTP {}",
            resp.status()
        );
        let len: u64 = resp
            .headers()
            .get("content-length")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok())
            .context("server did not report the file size")?;
        let ranges_ok = resp
            .headers()
            .get("accept-ranges")
            .and_then(|v| v.to_str().ok())
            .map(|v| v.contains("bytes"))
            .unwrap_or(true); // dl.google.com honours Range even when it omits this
        ensure!(ranges_ok, "server refuses byte-range requests");
        Ok(HttpRange {
            agent,
            url: url.to_string(),
            len,
            bytes_read: 0,
        })
    }
}

impl RangeSource for HttpRange {
    fn len(&self) -> u64 {
        self.len
    }
    fn read_range(&mut self, start: u64, end: u64) -> Result<Vec<u8>> {
        if start == end {
            return Ok(Vec::new());
        }
        let mut resp = self
            .agent
            .get(&self.url)
            .header("Range", &format!("bytes={start}-{}", end - 1))
            .call()
            .with_context(|| format!("GET range {start}-{end} of {}", self.url))?;
        ensure!(
            resp.status() == 206,
            "server ignored the Range header (HTTP {})",
            resp.status()
        );
        let want = end - start;
        let body = resp
            .body_mut()
            .with_config()
            .limit(want + 1)
            .read_to_vec()
            .context("reading range body")?;
        ensure!(
            body.len() as u64 == want,
            "short range read: got {} of {want} bytes",
            body.len()
        );
        self.bytes_read += want;
        Ok(body)
    }
}

/// Download only `bootloader-*.img` from a factory ZIP into `out_dir`.
pub fn fetch_bootloader(image: &Image, out_dir: &Path) -> Result<PathBuf> {
    fetch_member(image, out_dir, "bootloader")
}

/// Download only `radio-*.img` (the modem firmware) from a factory ZIP.
pub fn fetch_radio(image: &Image, out_dir: &Path) -> Result<PathBuf> {
    fetch_member(image, out_dir, "radio")
}

/// Download the top-level `<prefix>*.img` from a factory ZIP into `out_dir`,
/// reading only the ZIP's directory and that one member.
fn fetch_member(image: &Image, out_dir: &Path, prefix: &str) -> Result<PathBuf> {
    let mut src = HttpRange::open(&image.url)?;
    eprintln!(
        "{} is {:.2} GB; reading just its directory and {prefix} image ...",
        image.file_name(),
        src.len() as f64 / 1e9
    );
    let members = remotezip::list(&mut src)?;
    let member = remotezip::find_image(&members, prefix).ok_or_else(|| {
        anyhow!(
            "no {prefix}-*.img inside {} (members: {})",
            image.file_name(),
            members
                .iter()
                .map(|m| m.name.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        )
    })?;
    let data = remotezip::extract(&mut src, member)?;
    std::fs::create_dir_all(out_dir)?;
    let path = out_dir.join(member.base_name());
    std::fs::write(&path, &data).with_context(|| format!("writing {}", path.display()))?;
    eprintln!(
        "wrote {} ({} bytes, {} bytes transferred)",
        path.display(),
        data.len(),
        src.bytes_read
    );
    Ok(path)
}

/// Download the whole factory ZIP into `out_dir`, resuming a partial file if
/// one is there, and verify the SHA-256 the listing page gives.
pub fn fetch_full(image: &Image, out_dir: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(out_dir)?;
    let path = out_dir.join(image.file_name());
    let agent = agent();

    let head = agent.head(&image.url).call().context("HEAD on image")?;
    ensure!(
        head.status() == 200,
        "{} returned HTTP {}",
        image.url,
        head.status()
    );
    let total: u64 = head
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok())
        .context("server did not report the file size")?;

    let mut hasher = Sha256::new();
    let mut have = 0u64;
    if path.is_file() {
        let existing = std::fs::read(&path)?;
        if existing.len() as u64 > total {
            bail!(
                "{} is larger than the server's copy; delete it and retry",
                path.display()
            );
        }
        hasher.update(&existing);
        have = existing.len() as u64;
        if have > 0 && have < total {
            eprintln!(
                "resuming {} at {:.1}%",
                path.display(),
                have as f64 * 100.0 / total as f64
            );
        }
    }

    if have < total {
        let mut req = agent.get(&image.url);
        if have > 0 {
            req = req.header("Range", &format!("bytes={have}-"));
        }
        let mut resp = req.call().context("starting download")?;
        let expected = if have > 0 { 206 } else { 200 };
        if have > 0 && resp.status() == 200 {
            // Server ignored the range; start over.
            hasher = Sha256::new();
            have = 0;
        } else {
            ensure!(
                resp.status() == expected,
                "download returned HTTP {}",
                resp.status()
            );
        }
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(have == 0)
            .open(&path)?;
        if have > 0 {
            use std::io::Seek;
            file.seek(std::io::SeekFrom::Start(have))?;
        }
        let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
        let mut buf = vec![0u8; 1 << 20];
        let started = Instant::now();
        let mut last_pct = u64::MAX;
        loop {
            let n = reader.read(&mut buf).context("download stream")?;
            if n == 0 {
                break;
            }
            file.write_all(&buf[..n])?;
            hasher.update(&buf[..n]);
            have += n as u64;
            let pct = have * 100 / total;
            if pct != last_pct {
                last_pct = pct;
                let secs = started.elapsed().as_secs_f64().max(0.001);
                eprint!(
                    "\r{} {:3}%  {:.2}/{:.2} GB  {:.1} MB/s   ",
                    image.file_name(),
                    pct,
                    have as f64 / 1e9,
                    total as f64 / 1e9,
                    (have as f64 / 1e6) / secs
                );
            }
        }
        eprintln!();
        file.flush()?;
    }
    ensure!(
        have == total,
        "download stopped at {have} of {total} bytes; run again to resume"
    );

    let digest = format!("{:x}", hasher.finalize());
    match &image.sha256 {
        Some(want) if *want == digest => eprintln!("SHA-256 verified: {digest}"),
        Some(want) => bail!(
            "SHA-256 mismatch for {}: got {digest}, Google lists {want}. Delete it and retry.",
            path.display()
        ),
        None => eprintln!("SHA-256 {digest} (the listing had no checksum to compare)"),
    }
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    const PAGE: &str = r#"
<h2 id="komodo">"komodo" for Pixel 9 Pro XL</h2>
<table><tr><th>Version</th><th>Flash</th><th>Download</th><th>SHA-256 Checksum</th></tr>
<tr id="komodoap3a.241005.015"><td>15.0.0 (AP3A.241005.015, Oct 2024)</td><td><a href="https://flash.android.com/build/x">Flash</a></td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-ap3a.241005.015-factory-12345678.zip">Link</a></td>
<td>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</td></tr>
<tr id="komodocp3a.260905.009"><td>17.0.0 (CP3A.260905.009, Sep 2026)</td><td><a href="https://flash.android.com/build/y">Flash</a></td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-cp3a.260905.009-factory-99eb621a.zip">Link</a></td>
<td>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</td></tr>
<tr id="komodocp3a.260905.009.v"><td>17.0.0 (CP3A.260905.009.V1, Sep 2026, Verizon)</td><td><a href="https://flash.android.com/build/z">Flash</a></td>
<td><a href="https://dl.google.com/dl/android/aosp/komodo-cp3a.260905.009.v1-factory-deadbeef.zip">Link</a></td>
<td>cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc</td></tr>
</table>
<h2 id="comet">"comet" for Pixel 9 Pro Fold</h2>
<tr><td>17.0.0 (CP3A.260905.009, Sep 2026)</td><td></td><td><a href="https://dl.google.com/dl/android/aosp/comet-cp3a.260905.009-factory-00000001.zip">Link</a></td><td>dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd</td></tr>
"#;

    #[test]
    fn parses_rows_for_one_device() {
        let imgs = parse_images(PAGE, "komodo");
        assert_eq!(imgs.len(), 3);
        assert_eq!(imgs[0].build, "ap3a.241005.015");
        assert_eq!(imgs[0].sha256.as_deref(), Some("a".repeat(64).as_str()));
        assert_eq!(imgs[0].description, "15.0.0 (AP3A.241005.015, Oct 2024)");
        assert_eq!(
            imgs[1].file_name(),
            "komodo-cp3a.260905.009-factory-99eb621a.zip"
        );
        assert!(imgs[2].description.contains("Verizon"));
        assert_eq!(parse_images(PAGE, "comet").len(), 1);
        assert!(parse_images(PAGE, "shiba").is_empty());
    }

    #[test]
    fn picks_newest_generic_build() {
        let imgs = parse_images(PAGE, "komodo");
        let latest = pick_latest(&imgs, None).unwrap();
        assert_eq!(latest.build, "cp3a.260905.009");
        assert_eq!(latest.sha256.as_deref(), Some("b".repeat(64).as_str()));
        let pinned = pick_latest(&imgs, Some("AP3A.241005.015")).unwrap();
        assert_eq!(pinned.build, "ap3a.241005.015");
        assert!(pick_latest(&imgs, Some("nope")).is_none());
    }

    #[test]
    fn resolves_device_names() {
        assert_eq!(codename("komodo").unwrap(), "komodo");
        assert_eq!(codename("Pixel 9 Pro XL").unwrap(), "komodo");
        assert_eq!(codename("pixel-9-pro-xl").unwrap(), "komodo");
        assert_eq!(codename("newthing").unwrap(), "newthing");
        assert_eq!(codename("pixel 9 pro xl!").unwrap(), "komodo");
        assert!(codename("pixel 99 ultra?").is_err());
    }
}
