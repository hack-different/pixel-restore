//! Just enough of the Android boot image formats (header v3/v4 boot and
//! init_boot, v3/v4 vendor_boot) to take the kernel from one image and the
//! ramdisks from others, and write them out as a header-v4 boot image the
//! way `mkbootimg --header_version 4` does.

use anyhow::{ensure, Result};

const BOOT_MAGIC: &[u8; 8] = b"ANDROID!";
const VENDOR_MAGIC: &[u8; 8] = b"VNDRBOOT";
/// Boot images v3 and later use a fixed 4 KiB page.
const PAGE: usize = 4096;
const V4_HEADER_SIZE: u32 = 1584;
const CMDLINE_OFFSET: usize = 44;
const CMDLINE_SIZE: usize = 1536;

fn u32_at(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}

fn align(n: usize) -> usize {
    n.div_ceil(PAGE) * PAGE
}

fn slice(b: &[u8], start: usize, len: usize, what: &str) -> Result<Vec<u8>> {
    ensure!(
        start.checked_add(len).is_some_and(|end| end <= b.len()),
        "{what} runs past the end of the image ({start}+{len} > {})",
        b.len()
    );
    Ok(b[start..start + len].to_vec())
}

/// The parts of a v3/v4 boot (or init_boot) image.
pub struct Boot {
    pub kernel: Vec<u8>,
    pub ramdisk: Vec<u8>,
    pub os_version: u32,
    pub cmdline: Vec<u8>,
}

pub fn parse_boot(b: &[u8]) -> Result<Boot> {
    ensure!(
        b.len() >= PAGE && &b[..8] == BOOT_MAGIC,
        "not an Android boot image"
    );
    let version = u32_at(b, 40);
    ensure!(
        version == 3 || version == 4,
        "boot image header v{version}; only v3 and v4 (Pixel 6 and later) are handled"
    );
    let kernel_size = u32_at(b, 8) as usize;
    let ramdisk_size = u32_at(b, 12) as usize;
    let kernel = slice(b, PAGE, kernel_size, "kernel")?;
    let ramdisk = slice(b, PAGE + align(kernel_size), ramdisk_size, "ramdisk")?;
    let raw = &b[CMDLINE_OFFSET..CMDLINE_OFFSET + CMDLINE_SIZE];
    let cmdline = raw[..raw.iter().position(|&c| c == 0).unwrap_or(raw.len())].to_vec();
    Ok(Boot {
        kernel,
        ramdisk,
        os_version: u32_at(b, 16),
        cmdline,
    })
}

/// Every vendor ramdisk of a v3/v4 vendor_boot image, all fragments in
/// table order (they are stored back to back).
pub fn vendor_ramdisk(b: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        b.len() >= 2128 && &b[..8] == VENDOR_MAGIC,
        "not a vendor_boot image"
    );
    let version = u32_at(b, 8);
    ensure!(
        version == 3 || version == 4,
        "vendor_boot header v{version}; only v3 and v4 are handled"
    );
    let page = u32_at(b, 12) as usize;
    ensure!(page.is_power_of_two(), "vendor_boot page size {page}");
    let size = u32_at(b, 24) as usize;
    let header = u32_at(b, 2096) as usize;
    let start = header.div_ceil(page) * page;
    slice(b, start, size, "vendor ramdisk")
}

/// A header-v4 boot image holding `kernel` and `ramdisk`, unsigned.
pub fn build_v4(kernel: &[u8], ramdisk: &[u8], os_version: u32, cmdline: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        cmdline.len() < CMDLINE_SIZE,
        "kernel command line is {} bytes; at most {} fit",
        cmdline.len(),
        CMDLINE_SIZE - 1
    );
    let mut out = vec![0u8; PAGE];
    out[..8].copy_from_slice(BOOT_MAGIC);
    out[8..12].copy_from_slice(&(kernel.len() as u32).to_le_bytes());
    out[12..16].copy_from_slice(&(ramdisk.len() as u32).to_le_bytes());
    out[16..20].copy_from_slice(&os_version.to_le_bytes());
    out[20..24].copy_from_slice(&V4_HEADER_SIZE.to_le_bytes());
    out[40..44].copy_from_slice(&4u32.to_le_bytes());
    out[CMDLINE_OFFSET..CMDLINE_OFFSET + cmdline.len()].copy_from_slice(cmdline);
    // signature_size at 1580 stays 0.
    for part in [kernel, ramdisk] {
        out.extend_from_slice(part);
        out.resize(align(out.len()), 0);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vendor_boot(ramdisk: &[u8]) -> Vec<u8> {
        let mut b = vec![0u8; PAGE];
        b[..8].copy_from_slice(VENDOR_MAGIC);
        b[8..12].copy_from_slice(&4u32.to_le_bytes());
        b[12..16].copy_from_slice(&(PAGE as u32).to_le_bytes());
        b[24..28].copy_from_slice(&(ramdisk.len() as u32).to_le_bytes());
        b[2096..2100].copy_from_slice(&2128u32.to_le_bytes());
        b.extend_from_slice(ramdisk);
        b.resize(align(b.len()), 0);
        b
    }

    #[test]
    fn round_trips_a_v4_boot_image() {
        let kernel = vec![0xaa; 5000];
        let ramdisk = vec![0xbb; 100];
        let img = build_v4(&kernel, &ramdisk, 0x1234, b"console=ttySAC0").unwrap();
        assert_eq!(img.len(), PAGE + 2 * PAGE + PAGE);
        assert_eq!(u32_at(&img, 20), V4_HEADER_SIZE);
        let b = parse_boot(&img).unwrap();
        assert_eq!(b.kernel, kernel);
        assert_eq!(b.ramdisk, ramdisk);
        assert_eq!(b.os_version, 0x1234);
        assert_eq!(b.cmdline, b"console=ttySAC0");
    }

    #[test]
    fn reads_the_vendor_ramdisk() {
        let vb = vendor_boot(b"cpio-bytes");
        assert_eq!(vendor_ramdisk(&vb).unwrap(), b"cpio-bytes");
        assert!(vendor_ramdisk(b"ANDROID!").is_err());
    }

    #[test]
    fn rejects_truncated_and_foreign_images() {
        let mut img = build_v4(&[1; 10], &[2; 10], 0, b"").unwrap();
        img.truncate(PAGE + 5);
        assert!(parse_boot(&img).is_err());
        assert!(parse_boot(&vendor_boot(b"x")).is_err());
        let mut v2 = build_v4(&[1; 10], &[], 0, b"").unwrap();
        v2[40..44].copy_from_slice(&2u32.to_le_bytes());
        assert!(parse_boot(&v2).is_err());
    }
}
