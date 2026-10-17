//! Pull a single member out of a (possibly multi-gigabyte, ZIP64) archive
//! without downloading the whole thing. Only the end-of-central-directory
//! record, the central directory and the one member's bytes are read, each
//! through a byte-range read, so grabbing the few-megabyte `bootloader-*.img`
//! out of a 4 GB factory ZIP costs a few megabytes of transfer.

use anyhow::{bail, ensure, Context, Result};
use std::io::Read;

/// Anything that can serve byte ranges of a fixed-length blob: an HTTP URL
/// with `Range:` support, or an in-memory buffer in tests.
pub trait RangeSource {
    fn len(&self) -> u64;
    /// Read `[start, end)`; must return exactly `end - start` bytes.
    fn read_range(&mut self, start: u64, end: u64) -> Result<Vec<u8>>;
}

#[derive(Debug, Clone)]
pub struct Member {
    pub name: String,
    pub method: u16,
    pub crc32: u32,
    pub compressed_size: u64,
    pub uncompressed_size: u64,
    pub local_header_offset: u64,
}

impl Member {
    /// The file name without any directory, e.g. `radio-komodo-g5400c.img`.
    pub fn base_name(&self) -> &str {
        self.name.rsplit('/').next().unwrap_or(&self.name)
    }
}

/// The `<prefix>-*.img` member, as factory ZIPs name `bootloader-*.img` and
/// `radio-*.img`.
pub fn find_image<'a>(members: &'a [Member], prefix: &str) -> Option<&'a Member> {
    members.iter().find(|m| {
        let base = m.base_name();
        base.starts_with(prefix) && base.ends_with(".img")
    })
}

/// Byte ranges of a file on disk, so a local 4 GB factory ZIP is not read
/// into memory to get one image out of it.
pub struct FileRange {
    file: std::fs::File,
    len: u64,
}

impl FileRange {
    pub fn open(path: &std::path::Path) -> Result<FileRange> {
        let file =
            std::fs::File::open(path).with_context(|| format!("opening {}", path.display()))?;
        let len = file.metadata()?.len();
        Ok(FileRange { file, len })
    }
}

impl RangeSource for FileRange {
    fn len(&self) -> u64 {
        self.len
    }
    fn read_range(&mut self, start: u64, end: u64) -> Result<Vec<u8>> {
        use std::io::{Seek, SeekFrom};
        ensure!(
            end <= self.len && start <= end,
            "range {start}..{end} out of bounds"
        );
        self.file.seek(SeekFrom::Start(start))?;
        let mut buf = vec![0; (end - start) as usize];
        self.file.read_exact(&mut buf)?;
        Ok(buf)
    }
}

const EOCD_SIG: u32 = 0x0605_4b50;
const EOCD64_LOCATOR_SIG: u32 = 0x0706_4b50;
const EOCD64_SIG: u32 = 0x0606_4b50;
const CENTRAL_SIG: u32 = 0x0201_4b50;
const LOCAL_SIG: u32 = 0x0403_4b50;

fn u16_at(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes(b[o..o + 2].try_into().unwrap())
}
fn u32_at(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn u64_at(b: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

/// List the archive's members by reading only its directory.
pub fn list(src: &mut dyn RangeSource) -> Result<Vec<Member>> {
    let len = src.len();
    ensure!(len >= 22, "archive is only {len} bytes");
    // EOCD is 22 bytes plus a comment of up to 64 KiB; fetch the tail once.
    let tail_len = len.min(22 + 65_535 + 20);
    let tail_start = len - tail_len;
    let tail = src.read_range(tail_start, len)?;
    let eocd_rel = (0..=tail.len() - 22)
        .rev()
        .find(|&i| u32_at(&tail, i) == EOCD_SIG)
        .context("no end-of-central-directory record; not a ZIP file?")?;
    let eocd = &tail[eocd_rel..];
    let mut entries = u16_at(eocd, 10) as u64;
    let mut cd_size = u32_at(eocd, 12) as u64;
    let mut cd_offset = u32_at(eocd, 16) as u64;

    let needs_zip64 = entries == 0xffff || cd_size == 0xffff_ffff || cd_offset == 0xffff_ffff;
    if needs_zip64 {
        ensure!(eocd_rel >= 20, "ZIP64 archive without a locator record");
        let loc = &tail[eocd_rel - 20..eocd_rel];
        ensure!(
            u32_at(loc, 0) == EOCD64_LOCATOR_SIG,
            "bad ZIP64 locator signature"
        );
        let eocd64_off = u64_at(loc, 8);
        let rec = if eocd64_off >= tail_start {
            tail[(eocd64_off - tail_start) as usize..].to_vec()
        } else {
            src.read_range(eocd64_off, eocd64_off + 56)?
        };
        ensure!(
            rec.len() >= 56 && u32_at(&rec, 0) == EOCD64_SIG,
            "bad ZIP64 EOCD signature"
        );
        entries = u64_at(&rec, 32);
        cd_size = u64_at(&rec, 40);
        cd_offset = u64_at(&rec, 48);
    }

    ensure!(
        cd_offset + cd_size <= len,
        "central directory lies past end of file"
    );
    let cd = if cd_offset >= tail_start {
        let s = (cd_offset - tail_start) as usize;
        tail[s..s + cd_size as usize].to_vec()
    } else {
        src.read_range(cd_offset, cd_offset + cd_size)?
    };

    let mut members = Vec::with_capacity(entries.min(10_000) as usize);
    let mut pos = 0usize;
    for _ in 0..entries {
        ensure!(pos + 46 <= cd.len(), "truncated central directory");
        ensure!(
            u32_at(&cd, pos) == CENTRAL_SIG,
            "bad central directory entry signature"
        );
        let method = u16_at(&cd, pos + 10);
        let crc32 = u32_at(&cd, pos + 16);
        let mut compressed = u32_at(&cd, pos + 20) as u64;
        let mut uncompressed = u32_at(&cd, pos + 24) as u64;
        let name_len = u16_at(&cd, pos + 28) as usize;
        let extra_len = u16_at(&cd, pos + 30) as usize;
        let comment_len = u16_at(&cd, pos + 32) as usize;
        let mut local_off = u32_at(&cd, pos + 42) as u64;
        let name_start = pos + 46;
        let extra_start = name_start + name_len;
        let end = extra_start + extra_len + comment_len;
        ensure!(end <= cd.len(), "truncated central directory entry");
        let name = String::from_utf8_lossy(&cd[name_start..extra_start]).into_owned();

        // ZIP64 extended information: only the fields that overflowed are
        // present, in this fixed order.
        let extra = &cd[extra_start..extra_start + extra_len];
        let mut e = 0usize;
        while e + 4 <= extra.len() {
            let id = u16_at(extra, e);
            let sz = u16_at(extra, e + 2) as usize;
            let body = &extra[e + 4..(e + 4 + sz).min(extra.len())];
            if id == 0x0001 {
                let mut b = 0usize;
                if uncompressed == 0xffff_ffff && b + 8 <= body.len() {
                    uncompressed = u64_at(body, b);
                    b += 8;
                }
                if compressed == 0xffff_ffff && b + 8 <= body.len() {
                    compressed = u64_at(body, b);
                    b += 8;
                }
                if local_off == 0xffff_ffff && b + 8 <= body.len() {
                    local_off = u64_at(body, b);
                }
            }
            e += 4 + sz;
        }

        members.push(Member {
            name,
            method,
            crc32,
            compressed_size: compressed,
            uncompressed_size: uncompressed,
            local_header_offset: local_off,
        });
        pos = end;
    }
    Ok(members)
}

/// Fetch and decompress one member, verifying its CRC32.
pub fn extract(src: &mut dyn RangeSource, m: &Member) -> Result<Vec<u8>> {
    let lh = src.read_range(m.local_header_offset, m.local_header_offset + 30)?;
    ensure!(
        u32_at(&lh, 0) == LOCAL_SIG,
        "bad local header signature for '{}'",
        m.name
    );
    let name_len = u16_at(&lh, 26) as u64;
    let extra_len = u16_at(&lh, 28) as u64;
    let data_start = m.local_header_offset + 30 + name_len + extra_len;
    let raw = src.read_range(data_start, data_start + m.compressed_size)?;
    let data = match m.method {
        0 => raw,
        8 => {
            let mut out = Vec::with_capacity(m.uncompressed_size as usize);
            flate2::read::DeflateDecoder::new(&raw[..])
                .read_to_end(&mut out)
                .with_context(|| format!("inflating '{}'", m.name))?;
            out
        }
        other => bail!("'{}' uses unsupported compression method {other}", m.name),
    };
    ensure!(
        data.len() as u64 == m.uncompressed_size,
        "'{}' inflated to {} bytes, expected {}",
        m.name,
        data.len(),
        m.uncompressed_size
    );
    ensure!(
        crc32fast::hash(&data) == m.crc32,
        "CRC mismatch on '{}': the download is corrupt",
        m.name
    );
    Ok(data)
}

/// An in-memory archive, for tests.
#[cfg(test)]
pub struct Bytes<'a>(pub &'a [u8]);

#[cfg(test)]
impl RangeSource for Bytes<'_> {
    fn len(&self) -> u64 {
        self.0.len() as u64
    }
    fn read_range(&mut self, start: u64, end: u64) -> Result<Vec<u8>> {
        ensure!(
            end <= self.len() && start <= end,
            "range {start}..{end} out of bounds"
        );
        Ok(self.0[start as usize..end as usize].to_vec())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn make_zip(zip64: bool) -> Vec<u8> {
        let mut buf = std::io::Cursor::new(Vec::new());
        let mut w = zip::ZipWriter::new(&mut buf);
        let stored = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored)
            .large_file(zip64);
        let deflated = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated)
            .large_file(zip64);
        w.start_file("komodo-x/flash-all.sh", deflated).unwrap();
        w.write_all(b"#!/bin/sh\necho hi\n").unwrap();
        w.start_file("komodo-x/bootloader-komodo-test.img", stored)
            .unwrap();
        w.write_all(&[7u8; 5000]).unwrap();
        w.start_file("komodo-x/radio.img", deflated).unwrap();
        w.write_all(&vec![1u8; 20_000]).unwrap();
        w.set_comment("factory");
        w.finish().unwrap();
        buf.into_inner()
    }

    #[test]
    fn lists_and_extracts_from_plain_zip() {
        let z = make_zip(false);
        let mut src = Bytes(&z);
        let members = list(&mut src).unwrap();
        assert_eq!(members.len(), 3);
        let bl = members
            .iter()
            .find(|m| m.name.ends_with("bootloader-komodo-test.img"))
            .unwrap();
        assert_eq!(bl.method, 0);
        assert_eq!(bl.uncompressed_size, 5000);
        assert_eq!(extract(&mut src, bl).unwrap(), vec![7u8; 5000]);
        let radio = members
            .iter()
            .find(|m| m.name.ends_with("radio.img"))
            .unwrap();
        assert_eq!(radio.method, 8);
        assert_eq!(extract(&mut src, radio).unwrap(), vec![1u8; 20_000]);
    }

    #[test]
    fn handles_zip64_records() {
        let z = make_zip(true);
        let mut src = Bytes(&z);
        let members = list(&mut src).unwrap();
        let bl = members
            .iter()
            .find(|m| m.name.contains("bootloader"))
            .unwrap();
        assert_eq!(extract(&mut src, bl).unwrap().len(), 5000);
    }

    #[test]
    fn rejects_non_zip() {
        let junk = vec![0u8; 100];
        assert!(list(&mut Bytes(&junk)).is_err());
    }
}
