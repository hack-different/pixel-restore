//! Recovery packs: bootloader stages signed with the "USB bootable" bit, which
//! the ROM insists on for BL1 and which retail factory images lack.
//!
//! The only public source is the tensor-usbdl release ZIP, whose `sources/`
//! tree holds one directory per bootloader (`gs201`, `zuma/shiba`,
//! `zuma/husky`). It is fetched once, checked against a pinned SHA-256 and
//! cached; nothing in it is executed, only its `.img` files are read.

use crate::devices::Model;
use anyhow::{bail, ensure, Context, Result};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::{Read, Seek};
use std::path::{Path, PathBuf};

pub const RELEASE_URL: &str =
    "https://github.com/JoshuaDoes/tensor-usbdl/releases/download/020/tensor-usbdl-v0.2.0.zip";
pub const RELEASE_FILE: &str = "tensor-usbdl-v0.2.0.zip";
pub const RELEASE_SHA256: &str = "15dbd6406c1c74c2cd7104388ab745f3605d026ede6fc6c61c754ddb30b194b2";

/// Where the release ZIP is cached: `$PIXEL_RESTORE_CACHE`, else the
/// platform cache directory (`~/Library/Caches` on macOS, `$XDG_CACHE_HOME`
/// or `~/.cache` elsewhere) under `pixel-restore/`.
pub fn cache_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("PIXEL_RESTORE_CACHE") {
        return PathBuf::from(dir);
    }
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let base = if cfg!(target_os = "macos") {
        home.join("Library/Caches")
    } else if let Some(xdg) = std::env::var_os("XDG_CACHE_HOME") {
        PathBuf::from(xdg)
    } else {
        home.join(".cache")
    };
    base.join("pixel-restore")
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut f = std::fs::File::open(path)?;
    let mut h = Sha256::new();
    std::io::copy(&mut f, &mut h)?;
    Ok(format!("{:x}", h.finalize()))
}

/// The cached release ZIP, downloading it if it is missing or damaged.
pub fn release_zip() -> Result<PathBuf> {
    let dir = cache_dir();
    let path = dir.join(RELEASE_FILE);
    if path.is_file() && sha256_file(&path)? == RELEASE_SHA256 {
        return Ok(path);
    }
    std::fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
    eprintln!("downloading recovery packs from {RELEASE_URL} ...");
    let mut resp = crate::fetch::agent()
        .get(RELEASE_URL)
        .call()
        .context("downloading the tensor-usbdl release")?;
    ensure!(
        resp.status() == 200,
        "{RELEASE_URL} returned HTTP {}",
        resp.status()
    );
    let bytes = resp
        .body_mut()
        .with_config()
        .limit(256 * 1024 * 1024)
        .read_to_vec()
        .context("reading the tensor-usbdl release")?;
    let digest = format!("{:x}", Sha256::digest(&bytes));
    ensure!(
        digest == RELEASE_SHA256,
        "tensor-usbdl release SHA-256 is {digest}, expected {RELEASE_SHA256}; refusing to use it"
    );
    let tmp = path.with_extension("zip.part");
    std::fs::write(&tmp, &bytes)?;
    std::fs::rename(&tmp, &path)?;
    eprintln!("cached {}", path.display());
    Ok(path)
}

/// Read every `<name>.img` directly under `sources/<pack>/` in a release ZIP
/// (or an unpacked release directory, or a pack directory itself).
pub fn load(source: &Path, pack: &str) -> Result<HashMap<String, Vec<u8>>> {
    let mut files = HashMap::new();
    if source.is_dir() {
        let dir = [source.join("sources").join(pack), source.join(pack)]
            .into_iter()
            .find(|d| d.is_dir())
            .unwrap_or_else(|| source.to_path_buf());
        for entry in
            std::fs::read_dir(&dir).with_context(|| format!("reading {}", dir.display()))?
        {
            let path = entry?.path();
            if let (Some(stem), Some("img")) = (
                path.file_stem().and_then(|s| s.to_str()),
                path.extension().and_then(|s| s.to_str()),
            ) {
                files.insert(stem.to_ascii_lowercase(), std::fs::read(&path)?);
            }
        }
    } else {
        let file =
            std::fs::File::open(source).with_context(|| format!("opening {}", source.display()))?;
        files = load_zip(file, pack)?;
    }
    if files.is_empty() {
        bail!("no {pack} recovery pack in {}", source.display());
    }
    Ok(files)
}

fn load_zip<R: Read + Seek>(reader: R, pack: &str) -> Result<HashMap<String, Vec<u8>>> {
    let mut zip = zip::ZipArchive::new(reader).context("reading the recovery pack ZIP")?;
    let prefix = format!("sources/{pack}/");
    let mut files = HashMap::new();
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i)?;
        let Some(rest) = entry.name().strip_prefix(&prefix) else {
            continue;
        };
        let Some(stem) = rest.strip_suffix(".img") else {
            continue;
        };
        if stem.contains('/') {
            continue;
        }
        let stem = stem.to_ascii_lowercase();
        let mut data = Vec::with_capacity(entry.size() as usize);
        entry.read_to_end(&mut data)?;
        files.insert(stem, data);
    }
    Ok(files)
}

/// Load the pack for `model`, from `source` if given (a release ZIP, an
/// unpacked release, or a pack directory) or else the cached release.
pub fn for_model(
    model: &Model,
    source: Option<&Path>,
) -> Result<(String, HashMap<String, Vec<u8>>)> {
    let Some(pack) = model.pack else {
        bail!(
            "no recovery pack exists for the {} ({}, {}). Its ROM only accepts a BL1 \
             signed with the USB-boot bit and none has been published, so retail \
             images will be refused with \"bl1 header fail\".",
            model.name,
            model.codename,
            model.soc.name
        );
    };
    let path = match source {
        Some(p) => p.to_path_buf(),
        None => release_zip()?,
    };
    Ok((pack.to_string(), load(&path, pack)?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Write};

    #[test]
    fn loads_only_the_requested_pack() {
        let mut buf = Cursor::new(Vec::new());
        {
            let mut z = zip::ZipWriter::new(&mut buf);
            let opts = zip::write::SimpleFileOptions::default();
            for (name, body) in [
                ("sources/zuma/husky/bl1.img", b"H1".as_slice()),
                ("sources/zuma/husky/abl.img", b"HA"),
                ("sources/zuma/shiba/bl1.img", b"S1"),
                ("sources/gs201/bl1.img", b"G1"),
                ("tensor-usbdl", b"binary"),
            ] {
                z.start_file(name, opts).unwrap();
                z.write_all(body).unwrap();
            }
            z.finish().unwrap();
        }
        buf.set_position(0);
        let files = load_zip(buf, "zuma/husky").unwrap();
        assert_eq!(files.len(), 2);
        assert_eq!(files["bl1"], b"H1");
        assert_eq!(files["abl"], b"HA");
    }
}
