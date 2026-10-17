//! Boot the read-enabled fastbootd built by `fastbootd-read/` from RAM: one
//! `fastboot boot fastbootd-boot.img`, then watch what the phone comes back
//! as. Nothing is flashed, and a reboot returns the phone to what is
//! installed. The same flow as `fastbootd-read/scripts/boot-fastbootd.sh`.

use crate::fastboot;
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
            "the phone is in fastbootd (userspace), but `fastboot boot` is a bootloader \
             command. Run `fastboot -s {} reboot bootloader`, then run this again.",
            phone.serial
        );
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
            bail!(
                "the phone came back in the bootloader, so the image did not stay up (it may \
                 have crashed and reset). Nothing was written."
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
