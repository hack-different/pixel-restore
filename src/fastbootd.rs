//! Boot the read-enabled fastbootd built by `fastbootd-read/` from RAM: one
//! `fastboot boot fastbootd-boot.img`, then watch what the phone comes back
//! as. Nothing is flashed, and a reboot returns the phone to what is
//! installed. The same flow as `fastbootd-read/scripts/boot-fastbootd.sh`.
//!
//! The bootloader boots the image's kernel with the phone's own
//! vendor_kernel_boot (device tree and kernel modules), so a kernel from a
//! different build than the phone runs panics on mismatched modules. By
//! default the built ramdisks are therefore booted with the kernel read out
//! of the phone's own boot partition. When an image drops back to the
//! bootloader, the bootloader's log (`fastboot oem dmesg`) is saved and its
//! reboot reason shown.

use crate::{bootimg, fastboot};
use anyhow::{bail, ensure, Context, Result};
use clap::Args;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

#[derive(Args)]
pub struct FastbootdArgs {
    /// The image to boot. Default: fastbootd-read/out/<product>/fastbootd-boot.img,
    /// as `fastbootd-read/docker/build.sh` leaves it.
    #[arg(long, short)]
    image: Option<PathBuf>,

    /// Fastboot serial of the phone. Not needed when only one is attached.
    #[arg(long, short)]
    serial: Option<String>,

    /// Seconds to wait for fastbootd to come up after the boot.
    #[arg(long, default_value_t = 180)]
    wait: u64,

    /// Run the checks, show what would be booted and stop.
    #[arg(long)]
    dry_run: bool,

    /// Do not ask before booting.
    #[arg(long, short)]
    yes: bool,

    /// The kernel to boot the built ramdisks with. `phone` (default): the
    /// one in the phone's boot_<slot>, read with `fastboot fetch`, so it
    /// matches the device tree and modules the bootloader loads from the
    /// phone; the image is rebuilt from vendor_boot.img and init_boot.img
    /// next to --image. `built`: --image exactly as it is, which only works
    /// when the phone runs the build it came from. Or a boot.img whose kernel
    /// to use instead (e.g. from the factory image the phone runs).
    #[arg(long, default_value = "phone", value_name = "phone|built|BOOT.IMG")]
    kernel: String,

    /// Only save and summarize the bootloader's log (`fastboot oem dmesg`),
    /// e.g. after an image dropped back to the bootloader. Read only.
    #[arg(long)]
    dmesg: bool,
}

const REBUILD: &str = "rebuilding the image around another kernel needs the build's \
     vendor_boot.img (and init_boot.img) next to --image; pass --kernel built to boot a \
     prebuilt image as it is";

/// The lines of `fastboot oem dmesg` worth showing: every reboot reason and
/// panic or rejection, plus the last errors, without the `(bootloader)` prefix.
fn summarize(log: &str) -> Vec<String> {
    let lines: Vec<&str> = log
        .lines()
        .map(|l| l.trim_start_matches("(bootloader)").trim())
        .filter(|l| !l.is_empty())
        .collect();
    let key = |l: &str| {
        let low = l.to_ascii_lowercase();
        low.contains("reboot reason")
            || low.contains("reboot mode")
            || low.contains("enter reason")
            || low.contains("reset reason")
            || low.contains("panic")
            || low.contains("rejected")
    };
    let err = |l: &str| l.contains("[E]") || l.contains("[C]");
    let mut out: Vec<String> = lines
        .iter()
        .filter(|l| key(l))
        .map(|l| l.to_string())
        .collect();
    let errors: Vec<&&str> = lines.iter().filter(|l| err(l) && !key(l)).collect();
    let skip = errors.len().saturating_sub(12);
    out.extend(errors.into_iter().skip(skip).map(|l| l.to_string()));
    out
}

/// How the last boot ended, from the bootloader log. Its final "Reboot
/// reason" and "Reboot mode" lines describe the reset that brought the phone
/// back (earlier ones, in the DSS block, can be from an older reset).
#[derive(Debug, PartialEq)]
enum Ending {
    /// Software asked for the bootloader: the kernel ran, and Android's init
    /// reboots to the bootloader when it hits a fatal error.
    InitFatal,
    Panic,
    Watchdog,
    Other(String),
}

fn ending(log: &str) -> Option<Ending> {
    let last = |key: &str| {
        log.lines()
            .rev()
            .find_map(|l| l.split_once(key).map(|(_, v)| v.trim().to_string()))
    };
    let reason = last("Reboot reason:")?;
    let mode = last("Reboot mode:").unwrap_or_default();
    let low = reason.to_ascii_lowercase();
    Some(if low.contains("panic") {
        Ending::Panic
    } else if low.contains("watchdog") {
        Ending::Watchdog
    } else if low.contains("s/w reboot") && mode.to_ascii_lowercase().contains("bootloader") {
        Ending::InitFatal
    } else {
        Ending::Other(reason)
    })
}

/// Save the bootloader's log to `dir` and print what matters in it.
fn save_dmesg(phone: &fastboot::Device, dir: &Path) -> Result<Option<Ending>> {
    let (_, log) = phone.oem(&["dmesg"])?;
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let path = dir.join(format!("abl-dmesg-{}-{secs}.txt", phone.serial));
    std::fs::write(&path, &log).with_context(|| format!("writing {}", path.display()))?;
    println!(
        "bootloader log (fastboot oem dmesg) saved to {}",
        path.display()
    );
    let shown = summarize(&log);
    if shown.is_empty() {
        println!("  no reboot reason or errors in it");
    }
    for l in shown {
        println!("  {l}");
    }
    Ok(ending(&log))
}

/// `out`: the kernel (and its os_version and command line) from
/// `kernel_from`, with the built vendor_boot ramdisk and then the built
/// init_boot ramdisk from `art`. The built vendor_kernel_boot is left out,
/// so the phone's modules, which match that kernel, are the ones loaded.
fn compose(kernel_from: &Path, art: &Path, out: &Path) -> Result<()> {
    let read = |p: &Path| std::fs::read(p).with_context(|| format!("reading {}", p.display()));
    let k = bootimg::parse_boot(&read(kernel_from)?)
        .with_context(|| format!("reading the kernel from {}", kernel_from.display()))?;
    ensure!(
        !k.kernel.is_empty(),
        "{} has no kernel",
        kernel_from.display()
    );
    let vb = art.join("vendor_boot.img");
    let mut ramdisk = bootimg::vendor_ramdisk(&read(&vb)?)
        .with_context(|| format!("reading the ramdisk from {}", vb.display()))?;
    let ib = art.join("init_boot.img");
    if ib.is_file() {
        ramdisk.extend(bootimg::parse_boot(&read(&ib)?)?.ramdisk);
    }
    let img = bootimg::build_v4(&k.kernel, &ramdisk, k.os_version, &k.cmdline)?;
    std::fs::write(out, img).with_context(|| format!("writing {}", out.display()))?;
    Ok(())
}

/// What the `fetch` probe says about the fastbootd that answered it.
#[derive(Debug, PartialEq)]
enum Fastbootd {
    /// The patched build: it refuses until reading is enabled on screen.
    ReadEnabled,
    /// A stock fastbootd's refusal.
    Stock,
    Unknown,
}

fn classify(reply: &str) -> Fastbootd {
    if reply.contains("until reading is enabled from the on-screen fastboot menu") {
        Fastbootd::ReadEnabled
    } else if reply.contains("only allowed on") || reply.contains("not allowed on user build") {
        Fastbootd::Stock
    } else {
        Fastbootd::Unknown
    }
}

/// `max-download-size` is hex (`0x10000000`) on Pixels, decimal on some others.
fn parse_size(v: &str) -> Option<u64> {
    match v.strip_prefix("0x").or_else(|| v.strip_prefix("0X")) {
        Some(hex) => u64::from_str_radix(hex, 16).ok(),
        None => v.parse().ok(),
    }
}

/// Where the build leaves the image for `product`, looked up from the
/// current directory (the repo root or `fastbootd-read/`) and then from the
/// checkout this binary was built in.
fn default_image(product: &str, roots: &[PathBuf]) -> Option<PathBuf> {
    let rel = Path::new("out").join(product).join("fastbootd-boot.img");
    roots
        .iter()
        .flat_map(|r| [r.join("fastbootd-read").join(&rel), r.join(&rel)])
        .find(|p| p.is_file())
}

/// The lunch target the build recorded next to the image (`komodo-cur-userdebug`).
fn built_for(image: &Path) -> Option<String> {
    let txt = std::fs::read_to_string(image.parent()?.join("lunch-target.txt")).ok()?;
    let t = txt.trim();
    (!t.is_empty()).then(|| t.to_string())
}

/// Whether `adb devices` lists this serial (false if adb is not installed).
fn in_adb(serial: &str) -> bool {
    let bin = std::env::var("ADB").unwrap_or_else(|_| "adb".to_string());
    Command::new(bin)
        .arg("devices")
        .output()
        .map(|o| {
            String::from_utf8_lossy(&o.stdout)
                .lines()
                .any(|l| l.split_whitespace().next() == Some(serial))
        })
        .unwrap_or(false)
}

pub fn run(a: FastbootdArgs) -> Result<()> {
    let phone = fastboot::Device::open(a.serial.as_deref())?;
    let var = |name: &str| -> Result<String> {
        Ok(phone.getvar(name)?.unwrap_or_else(|| "?".to_string()))
    };
    let product = var("product")?;
    let unlocked = var("unlocked")?;
    let userspace = var("is-userspace")?;
    let slot = var("current-slot")?;
    let bootloader = var("version-bootloader")?;
    let max_download = phone.getvar("max-download-size")?;
    println!(
        "{}: product {product}, bootloader {bootloader}, slot {slot}, unlocked {unlocked}",
        phone.serial
    );

    if userspace == "yes" {
        bail!(
            "the phone is in fastbootd (userspace), but `fastboot boot` and `oem dmesg` are \
             bootloader commands. Run `fastboot -s {} reboot bootloader`, then run this again.",
            phone.serial
        );
    }
    if a.dmesg {
        save_dmesg(&phone, &std::env::current_dir()?)?;
        return Ok(());
    }
    if unlocked != "yes" {
        bail!(
            "the bootloader is not unlocked (unlocked: {unlocked}). It refuses to boot an \
             unsigned image, and fastbootd refuses to read partitions, on a locked phone. \
             This tool does not unlock it: unlocking wipes user data and is your decision."
        );
    }

    let image = match &a.image {
        Some(p) => p.clone(),
        None => {
            let mut roots = vec![std::env::current_dir()?];
            roots.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")));
            default_image(&product, &roots).with_context(|| {
                format!(
                    "no fastbootd-read/out/{product}/fastbootd-boot.img here. Build it with \
                     `fastbootd-read/docker/build.sh`, or pass --image"
                )
            })?
        }
    };
    let art = image
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let image = match a.kernel.as_str() {
        "built" => image,
        "phone" => {
            ensure!(
                slot != "?",
                "the bootloader did not report its current slot; use --kernel built or \
                 --kernel <boot.img>"
            );
            let part = format!("boot_{slot}");
            let from = art.join(format!("phone-{part}-{}.img", phone.serial));
            println!("reading the phone's kernel: fastboot fetch {part} (read only)");
            let (ok, reply) = phone.fetch(&part, &from)?;
            if !ok {
                let reply = reply.split_whitespace().collect::<Vec<_>>().join(" ");
                bail!(
                    "the bootloader would not let us read {part} ({reply}). Pass --kernel with \
                     boot.img from the factory image the phone runs (bootloader {bootloader}), \
                     or --kernel built to boot the image as built"
                );
            }
            let out = art.join(format!("fastbootd-boot.{}-{slot}.img", phone.serial));
            compose(&from, &art, &out).context(REBUILD)?;
            println!(
                "  built ramdisks + the phone's {part} kernel -> {}",
                out.display()
            );
            out
        }
        path => {
            let out = art.join("fastbootd-boot.kernel-swap.img");
            compose(Path::new(path), &art, &out).context(REBUILD)?;
            println!(
                "  built ramdisks + the kernel of {path} -> {}",
                out.display()
            );
            out
        }
    };
    let size = std::fs::metadata(&image)
        .with_context(|| format!("reading {}", image.display()))?
        .len();
    let target = built_for(&image);
    if let Some(t) = &target {
        let built = t.split('-').next().unwrap_or(t);
        ensure!(
            product == "?" || built == product,
            "{} was built for {built}, but this phone is {product}",
            image.display()
        );
    }
    if let Some(max) = max_download.as_deref().and_then(parse_size) {
        ensure!(
            size <= max,
            "{} is {size} bytes; the bootloader accepts at most {max}",
            image.display()
        );
    }

    println!(
        "will run: fastboot -s {} boot {}",
        phone.serial,
        image.display()
    );
    println!(
        "  ({size} bytes, {}). RAM only: nothing is flashed, and rebooting the phone \
         returns it to what is installed.",
        target.as_deref().unwrap_or("unknown target")
    );
    if a.dry_run {
        println!("dry run: nothing sent");
        return Ok(());
    }
    if !a.yes && !crate::confirm("boot it?")? {
        bail!("not confirmed; nothing sent");
    }

    phone.boot(&image)?;

    // The bootloader drops off USB when it starts the image.
    println!("waiting for the bootloader to start the image");
    let started = Instant::now();
    while phone.present()? {
        if started.elapsed() > Duration::from_secs(30) {
            save_dmesg(&phone, &art)?;
            bail!(
                "the phone stayed in the bootloader; it did not start the image. Nothing was \
                 written. The bootloader screen may say why."
            );
        }
        std::thread::sleep(Duration::from_secs(1));
    }

    println!("waiting up to {}s for fastbootd", a.wait);
    let started = Instant::now();
    let mut hinted = false;
    loop {
        if started.elapsed() > Duration::from_secs(a.wait) {
            bail!(
                "the phone did not reach fastbootd within {}s. Check its screen:\n  \
                 - recovery menu: pick \"Enter fastboot\", then run `fastboot getvar \
                 is-userspace` (it should answer yes)\n  \
                 - stuck or blank: hold Power + Volume Down to return to the bootloader.\n\
                 Nothing was written either way.",
                a.wait
            );
        }
        std::thread::sleep(Duration::from_secs(2));
        if phone.present()? {
            std::thread::sleep(Duration::from_secs(2)); // let it settle before asking
            if phone.getvar("is-userspace")?.as_deref() == Some("yes") {
                break;
            }
            let how = match save_dmesg(&phone, &art)? {
                Some(Ending::InitFatal) => "the kernel booted, then software rebooted to the \
                     bootloader: Android's init does that on a fatal error, so init stopped \
                     before recovery or fastbootd started"
                    .to_string(),
                Some(Ending::Panic) | Some(Ending::Watchdog) if a.kernel == "built" => format!(
                    "the kernel crashed. The built kernel only matches the phone's device tree \
                     and modules when it runs the same build; try again without --kernel built \
                     to use the phone's own kernel (bootloader {bootloader})"
                ),
                Some(Ending::Panic) => "the kernel panicked".to_string(),
                Some(Ending::Watchdog) => "a watchdog reset the phone".to_string(),
                Some(Ending::Other(r)) => format!("the bootloader recorded: {r}"),
                None => "the bootloader log gives no reboot reason".to_string(),
            };
            bail!(
                "the phone came back in the bootloader; {how}. Nothing was written. The \
                 kernel's log of that boot is the LAST KMSG section of `adb bugreport` on the \
                 next Android boot (`fastboot reboot`)."
            );
        }
        if in_adb(&phone.serial) {
            bail!(
                "the phone booted Android normally, not into fastbootd: the bootloader starts \
                 a `fastboot boot` image as a normal boot, so the patched fastbootd cannot be \
                 reached this way. Nothing was written; `adb reboot bootloader` returns to the \
                 bootloader. The remaining route is flashing vendor_boot, which writes to the \
                 phone; that is your decision and this tool will not do it."
            );
        }
        if !hinted && started.elapsed() > Duration::from_secs(45) {
            hinted = true;
            println!(
                "  still waiting. If the phone shows the recovery menu, pick \"Enter fastboot\"."
            );
        }
    }

    // A fresh boot has reading disabled, so this fetch is refused before
    // anything is read; the refusal text says which fastbootd answered.
    let probe = std::env::temp_dir().join(format!("fastbootd-probe-{}", std::process::id()));
    let (_, reply) = phone.fetch("devinfo", &probe)?;
    let _ = std::fs::remove_file(&probe);
    let reply = reply.split_whitespace().collect::<Vec<_>>().join(" ");
    match classify(&reply) {
        Fastbootd::ReadEnabled => {
            println!("the read-enabled fastbootd is running on {}", phone.serial)
        }
        Fastbootd::Stock => bail!(
            "the phone is in fastbootd, but it is the STOCK one (it answered: {reply}). The \
             built ramdisk did not replace the phone's. Nothing was written."
        ),
        Fastbootd::Unknown => {
            println!("  warning: could not identify this fastbootd; fetch said: {reply}")
        }
    }

    println!(
        "\nNext, on the phone's screen: scroll to \"Enable reading (fetch any partition)\" \
         (or \"Enable all partitions\" to read super/userdata whole), read the warning and \
         pick \"Yes, enable reading\". Then:\n\n  \
         pixel-restore modem read-partitions --fastboot --serial {s} --out ~/pixel-partitions\n\n\
         or hash them on the phone without transferring them:\n\n  \
         fastbootd-read/scripts/hash-partitions.sh --serial {s}\n\n\
         When you are done, reboot the phone; the read-enabled fastbootd is gone.",
        s = phone.serial
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tells_the_fastbootds_apart() {
        // The patched refusal also says "only allowed on", so it must win.
        let patched = "FAILED (remote: 'Fetch is only allowed on [devinfo] until reading is \
                       enabled from the on-screen fastboot menu')";
        assert_eq!(classify(patched), Fastbootd::ReadEnabled);
        let stock = "FAILED (remote: 'Fetch is only allowed on unlocked devices')";
        assert_eq!(classify(stock), Fastbootd::Stock);
        assert_eq!(
            classify("FAILED (remote: 'fetch is not allowed on user build')"),
            Fastbootd::Stock
        );
        assert_eq!(
            classify("FAILED (remote: 'unknown command')"),
            Fastbootd::Unknown
        );
    }

    #[test]
    fn picks_the_reboot_reason_out_of_the_bootloader_log() {
        let log = "(bootloader) [   0.100] [I] [DSS] Reboot reason: 0xbaba - kernel_panic\n\
                   (bootloader) [   0.200] [I] [UFS] init ok\n\
                   (bootloader) [   0.300] [E] [GPT] failed to read GPT header\n\
                   OKAY [  0.010s]\n";
        assert_eq!(
            summarize(log),
            [
                "[   0.100] [I] [DSS] Reboot reason: 0xbaba - kernel_panic",
                "[   0.300] [E] [GPT] failed to read GPT header",
            ]
        );
        let many: String = (0..20).map(|i| format!("[E] err {i}\n")).collect();
        let shown = summarize(&many);
        assert_eq!(shown.len(), 12);
        assert_eq!(shown[0], "[E] err 8");
    }

    #[test]
    fn reads_how_the_last_boot_ended() {
        // An older DSS record first, then the reset that brought the phone back.
        let log = "[PRE] Reboot reason: 0xccca - Long Press Power Key Reboot\n\
                   [PRE] Reboot mode: 0x0 - Normal Boot\n\
                   Reboot Info:\n  Reboot reason: 0xcafe - User Reboot(S/W Reboot)\n\
                   \x20 Reboot mode: 0xfc - Bootloader\n";
        assert_eq!(ending(log), Some(Ending::InitFatal));
        assert_eq!(
            ending("Reboot reason: 0xbaba - Kernel PANIC\n"),
            Some(Ending::Panic)
        );
        assert_eq!(
            ending("Reboot reason: 0xccca - Long Press Power Key Reboot\n"),
            Some(Ending::Other("0xccca - Long Press Power Key Reboot".into()))
        );
        assert_eq!(ending("nothing here"), None);
    }

    #[test]
    fn swaps_in_another_kernel() {
        let dir = std::env::temp_dir().join(format!("fastbootd-swap-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let phone = bootimg::build_v4(b"PHONE-KERNEL", b"", 7, b"").unwrap();
        std::fs::write(dir.join("phone.img"), phone).unwrap();
        let mut vb = vec![0u8; 4096];
        vb[..8].copy_from_slice(b"VNDRBOOT");
        vb[8..12].copy_from_slice(&4u32.to_le_bytes());
        vb[12..16].copy_from_slice(&4096u32.to_le_bytes());
        vb[24..28].copy_from_slice(&3u32.to_le_bytes());
        vb[2096..2100].copy_from_slice(&2128u32.to_le_bytes());
        vb.extend_from_slice(b"VBR");
        std::fs::write(dir.join("vendor_boot.img"), vb).unwrap();
        let ib = bootimg::build_v4(b"", b"IBR", 0, b"").unwrap();
        std::fs::write(dir.join("init_boot.img"), ib).unwrap();
        let out = dir.join("out.img");
        compose(&dir.join("phone.img"), &dir, &out).unwrap();
        let b = bootimg::parse_boot(&std::fs::read(&out).unwrap()).unwrap();
        assert_eq!(b.kernel, b"PHONE-KERNEL");
        assert_eq!(b.ramdisk, b"VBRIBR");
        assert_eq!(b.os_version, 7);
        // Without the build's vendor_boot it refuses rather than guessing.
        std::fs::remove_file(dir.join("vendor_boot.img")).unwrap();
        assert!(compose(&dir.join("phone.img"), &dir, &out).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn parses_download_size() {
        assert_eq!(parse_size("0x10000000"), Some(0x1000_0000));
        assert_eq!(parse_size("268435456"), Some(268_435_456));
        assert_eq!(parse_size("?"), None);
    }

    #[test]
    fn finds_the_built_image() {
        let root = std::env::temp_dir().join(format!("fastbootd-test-{}", std::process::id()));
        let out = root.join("fastbootd-read/out/komodo");
        std::fs::create_dir_all(&out).unwrap();
        assert_eq!(default_image("komodo", &[root.clone()]), None);
        std::fs::write(out.join("fastbootd-boot.img"), b"x").unwrap();
        std::fs::write(out.join("lunch-target.txt"), "komodo-cur-userdebug\n").unwrap();
        let found = default_image("komodo", &[PathBuf::from("/nonexistent"), root.clone()]);
        assert_eq!(
            found.as_deref(),
            Some(out.join("fastbootd-boot.img").as_path())
        );
        // From inside fastbootd-read/ too.
        let inner = default_image("komodo", &[root.join("fastbootd-read")]);
        assert!(inner.is_some());
        assert_eq!(
            built_for(&found.unwrap()).as_deref(),
            Some("komodo-cur-userdebug")
        );
        assert_eq!(default_image("caiman", &[root.clone()]), None);
        std::fs::remove_dir_all(&root).unwrap();
    }
}
