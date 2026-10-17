mod dnw;
mod eub;
mod fastboot;
mod fbpk;
mod fetch;
mod remotezip;
mod stages;

use anyhow::{anyhow, bail, ensure, Context, Result};
use clap::{Args, Parser, Subcommand};
use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

/// Boot a Tensor Pixel out of "Pixel ROM Recovery" mode into fastboot.
///
/// When a Pixel 6 or newer cannot find a valid bootloader it enumerates over
/// USB as a serial device named "Pixel ROM Recovery" and asks the host to
/// feed it bootloader stages one at a time. This tool answers those requests
/// straight from the bootloader.img inside the factory image for your model.
/// Once ABL is running the phone comes up in fastboot and you can flash the
/// bootloader permanently with `fastboot flash bootloader ...` / flash-all.
#[derive(Parser)]
#[command(version, about, long_about)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Look for a Pixel in ROM-recovery mode and print its serial port.
    Detect {
        /// Also list every other serial port, to help spot the phone when
        /// its USB ids are unexpected.
        #[arg(long)]
        all: bool,
        /// Keep scanning until a device appears.
        #[arg(long, short)]
        wait: bool,
    },
    /// Show what is inside a bootloader.img (or factory ZIP), optionally
    /// extracting every partition image.
    Unpack {
        /// bootloader-*.img, or the factory image ZIP containing one.
        image: PathBuf,
        /// Write each partition to this directory as <name>.img.
        #[arg(long, short)]
        out: Option<PathBuf>,
    },
    /// Feed bootloader stages to the phone until it reaches fastboot.
    Boot(BootArgs),
    /// Find the newest factory image for a device on Google's download page
    /// and fetch it. By default only bootloader-*.img is pulled out of the
    /// ZIP (a few MB via byte-range requests); --full downloads the whole
    /// image for flash-all. Downloading means accepting Google's
    /// factory-image terms, as on the page itself.
    Fetch {
        /// Codename or model, e.g. `komodo` or "Pixel 9 Pro XL".
        device: String,
        /// Pick a specific build id (e.g. cp3a.260905.009) instead of the newest.
        #[arg(long, short)]
        build: Option<String>,
        /// Download the whole factory ZIP, not just the bootloader.
        #[arg(long)]
        full: bool,
        /// Just list what is available.
        #[arg(long, short)]
        list: bool,
        /// Skip the listing page and fetch from this factory ZIP URL directly
        /// (a mirror, or a build the page no longer shows).
        #[arg(long, conflicts_with_all = ["build", "list"])]
        url: Option<String>,
        /// Directory to save into.
        #[arg(long, short, default_value = ".")]
        out: PathBuf,
    },
    /// Repair a phone's modem from fastboot: reflash the radio image that
    /// matches its bootloader, and optionally write back the phone's own
    /// backups of its modem partitions (efs, persist ...). For a blank or
    /// "unknown" IMEI, or no baseband, after a flash.
    RepairModem(RepairModemArgs),
}

#[derive(Args)]
struct RepairModemArgs {
    /// radio-*.img, or the factory ZIP containing one. Use the same build as
    /// the bootloader on the phone.
    #[arg(long, short)]
    image: Option<PathBuf>,

    /// Instead of --image, fetch radio-*.img for this device (codename or
    /// model name) from Google, a few tens of MB via range requests.
    #[arg(long, conflicts_with = "image")]
    device: Option<String>,

    /// With --device, the build id to take the radio from (e.g.
    /// cp3a.260905.009). Default: the newest generic build.
    #[arg(long, short, requires = "device")]
    build: Option<String>,

    /// Write back a backup of one of this phone's modem partitions:
    /// `--restore efs=efs.img`. Repeatable. Only for images taken from this
    /// same phone; the IMEI lives in them.
    #[arg(long, value_name = "PARTITION=FILE")]
    restore: Vec<String>,

    /// Fastboot serial of the phone. Not needed when only one is attached.
    #[arg(long, short)]
    serial: Option<String>,

    /// Where to save a radio image pulled out of a ZIP.
    #[arg(long, short, default_value = ".")]
    out: PathBuf,

    /// Show what would be flashed and stop.
    #[arg(long)]
    dry_run: bool,

    /// Do not ask before flashing.
    #[arg(long, short)]
    yes: bool,

    /// Boot Android when done, instead of staying in the bootloader.
    #[arg(long)]
    reboot: bool,
}

#[derive(Args)]
struct BootArgs {
    /// bootloader-*.img from the factory image, or the factory ZIP itself.
    /// Must match the phone's exact model and be at least as new as what it
    /// last ran.
    #[arg(long, short)]
    image: Option<PathBuf>,

    /// Instead of --image, fetch the newest bootloader for this device
    /// (codename or model name) from Google and use it. Saved next to the
    /// tool so the next run can pass it with --image.
    #[arg(long, conflicts_with = "image")]
    device: Option<String>,

    /// Directory of loose images (bl1.img, pbl.img, abl.img ...) to use
    /// instead of, or on top of, --image.
    #[arg(long, short)]
    dir: Option<PathBuf>,

    /// Serve one partition from a specific file: `--stage abl=/path/abl.img`.
    /// Repeatable. Wins over --dir and --image.
    #[arg(long, value_name = "NAME=FILE")]
    stage: Vec<String>,

    /// Tell the tool which partition an unfamiliar ROM request refers to:
    /// `--map FOO=gsa`. Repeatable.
    #[arg(long, value_name = "REQUEST=PARTITION")]
    map: Vec<String>,

    /// Serial port to use instead of auto-detecting (e.g. /dev/cu.usbmodem1234).
    #[arg(long, short)]
    port: Option<String>,

    /// Keep scanning until the phone shows up, then boot it.
    #[arg(long, short)]
    wait: bool,

    /// How the ROM wants EPBL: `split` (header now, body on EPBB; Pixel 8+)
    /// or `full` (Pixel 6/7). Default: guess from the ROM's serial.
    #[arg(long, value_parser = ["split", "full"])]
    epbl: Option<String>,

    /// Frame checksum: `ffff` (what tensor-usbdl sends and the ROM accepts),
    /// `sum16` (classic DNW byte sum), or a 4-digit hex constant.
    #[arg(long, default_value = "ffff")]
    checksum: String,

    /// Send the DNW STOP command when connecting.
    #[arg(long)]
    stop: bool,

    /// Treat this many seconds of silence as the end of a phase.
    #[arg(long, default_value_t = 10)]
    idle_timeout: u64,

    /// After a phase ends, wait this many seconds for the phone to show up
    /// again as a ROM device and continue. BL2 re-enumerates USB on Pixel 8
    /// before asking for GSA1, so the sequence spans two connections.
    #[arg(long, default_value_t = 15)]
    reconnect_wait: u64,

    /// Print every line the ROM sends.
    #[arg(long, short)]
    verbose: bool,
}

fn main() {
    if let Err(e) = run() {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Detect { all, wait } => detect(all, wait),
        Cmd::Unpack { image, out } => unpack(&image, out.as_deref()),
        Cmd::Boot(args) => boot(args),
        Cmd::Fetch {
            device,
            build,
            full,
            list,
            url,
            out,
        } => fetch_cmd(&device, build.as_deref(), full, list, url.as_deref(), &out),
        Cmd::RepairModem(args) => repair_modem(args),
    }
}

fn fetch_cmd(
    device: &str,
    build: Option<&str>,
    full: bool,
    list: bool,
    url: Option<&str>,
    out: &std::path::Path,
) -> Result<()> {
    let code = fetch::codename(device)?;
    if let Some(url) = url {
        let image = fetch::Image::from_url(&code, url)?;
        let path = if full {
            fetch::fetch_full(&image, out)?
        } else {
            fetch::fetch_bootloader(&image, out)?
        };
        println!("{}", path.display());
        return Ok(());
    }
    let images = fetch::list_images(&code)?;
    if list {
        for i in &images {
            println!(
                "{:<22} {:<48} {}",
                i.build,
                i.description,
                i.sha256.as_deref().unwrap_or("(no checksum)")
            );
        }
        return Ok(());
    }
    let image = fetch::pick_latest(&images, build).ok_or_else(|| {
        anyhow!(
            "no build '{}' for {code}; use --list to see them",
            build.unwrap_or("?")
        )
    })?;
    println!("{code}: {} -> {}", image.description, image.file_name());
    println!(
        "(downloading accepts Google's factory-image terms, as on {})",
        fetch::IMAGES_PAGE
    );
    let path = if full {
        fetch::fetch_full(image, out)?
    } else {
        fetch::fetch_bootloader(image, out)?
    };
    println!("{}", path.display());
    Ok(())
}

fn detect(all: bool, wait: bool) -> Result<()> {
    let mut polls = 0u32;
    loop {
        let devices = eub::find_devices()?;
        if !devices.is_empty() {
            for d in &devices {
                println!(
                    "Pixel ROM Recovery on {}  serial={}  product={}",
                    d.port,
                    d.serial.as_deref().unwrap_or("?"),
                    d.product.as_deref().unwrap_or("?")
                );
            }
            return Ok(());
        }
        if all {
            let ports = eub::all_ports()?;
            if ports.is_empty() {
                println!("no serial ports at all");
            }
            for p in ports {
                match p.port_type {
                    serialport::SerialPortType::UsbPort(u) => println!(
                        "  {}  usb {:04x}:{:04x}  {} {}",
                        p.port_name,
                        u.vid,
                        u.pid,
                        u.manufacturer.unwrap_or_default(),
                        u.product.unwrap_or_default()
                    ),
                    other => println!("  {}  {:?}", p.port_name, other),
                }
            }
        }
        if !wait {
            bail!(
                "no Pixel in ROM-recovery mode (USB {:04x}:{:04x}) found. \
                 Unplug the phone, hold Power + Volume Up + Volume Down, plug it in \
                 and keep holding for about 15 seconds.",
                eub::ROM_VID,
                eub::ROM_PID
            );
        }
        if polls.is_multiple_of(20) {
            eprintln!("waiting for the phone to appear in ROM-recovery mode ...");
        }
        polls += 1;
        std::thread::sleep(Duration::from_millis(500));
    }
}

fn unpack(image: &std::path::Path, out: Option<&std::path::Path>) -> Result<()> {
    let pack = fbpk::load(image)?;
    println!(
        "FBPK v{}  platform={}  version={}  slot_type={}  align={}  total={} bytes",
        pack.version,
        pack.platform,
        pack.pack_version.trim(),
        pack.slot_type,
        pack.data_align,
        pack.total_size
    );
    println!(
        "{:<20} {:<10} {:<12} {:>12} {:>12}  {:<8} {:<9} {:<5} flags",
        "name", "type", "product", "offset", "size", "slotted", "crc", "tag"
    );
    for e in &pack.entries {
        let crc = if e.kind == fbpk::ENTRY_PARTITION_TABLE {
            "-".to_string()
        } else if pack.verify_crc(e) {
            "ok".to_string()
        } else {
            "MISMATCH".to_string()
        };
        let (tag, flags) = match stages::header_info(pack.entry_data(e)) {
            Some(info) if e.kind != fbpk::ENTRY_PARTITION_TABLE => (
                stages::header_tag(pack.entry_data(e)).unwrap_or_else(|| "-".into()),
                format!("{:#x}", info.flags),
            ),
            _ => ("-".into(), "-".into()),
        };
        println!(
            "{:<20} {:<10} {:<12} {:>12} {:>12}  {:<8} {:<9} {:<5} {}",
            e.name,
            e.kind_name(),
            e.product,
            e.offset,
            e.size,
            if e.slotted { "yes" } else { "no" },
            crc,
            tag,
            flags
        );
    }
    if let Some(dir) = out {
        std::fs::create_dir_all(dir)?;
        for e in pack.data_entries() {
            let path = dir.join(format!("{}.img", e.name));
            std::fs::write(&path, pack.entry_data(e))
                .with_context(|| format!("writing {}", path.display()))?;
            println!("wrote {}", path.display());
        }
    }
    Ok(())
}

/// After a phase ends, poll for the phone to reappear as a ROM device. With a
/// fixed --port, reappearing means that path exists again.
fn wait_for_rom(fixed_port: Option<&str>, wait: Duration) -> Result<Option<String>> {
    let started = std::time::Instant::now();
    // Give the old connection a moment to go away before looking for the new one.
    std::thread::sleep(Duration::from_millis(750));
    while started.elapsed() < wait {
        match fixed_port {
            Some(p) => {
                if std::path::Path::new(p).exists() {
                    return Ok(Some(p.to_string()));
                }
            }
            None => {
                if let Some(d) = eub::find_devices()?.into_iter().next() {
                    return Ok(Some(d.port));
                }
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Ok(None)
}

fn parse_kv(items: &[String], what: &str) -> Result<HashMap<String, String>> {
    items
        .iter()
        .map(|s| {
            let (k, v) = s
                .split_once('=')
                .ok_or_else(|| anyhow!("--{what} expects KEY=VALUE, got '{s}'"))?;
            Ok((k.trim().to_string(), v.trim().to_string()))
        })
        .collect()
}

fn parse_checksum(s: &str) -> Result<dnw::Checksum> {
    match s.to_ascii_lowercase().as_str() {
        "sum16" | "sum" => Ok(dnw::Checksum::Sum16),
        hex => {
            let hex = hex.trim_start_matches("0x");
            let v = u16::from_str_radix(hex, 16).map_err(|_| {
                anyhow!("--checksum must be sum16 or a 16-bit hex value, got '{s}'")
            })?;
            Ok(dnw::Checksum::Fixed(v))
        }
    }
}

const LOOSE_IMAGE_NAMES: &[&str] = &[
    "bl1", "pbl", "bl2", "abl", "bl31", "tzsw", "ldfw", "gsa_bl1", "gsa", "gsaf", "gcf", "dpm",
];

fn boot(args: BootArgs) -> Result<()> {
    if args.image.is_none() && args.device.is_none() && args.dir.is_none() && args.stage.is_empty()
    {
        bail!("give me the bootloader with --image <bootloader-*.img or factory.zip> or --device <codename>");
    }

    let image_path = match (&args.image, &args.device) {
        (Some(p), _) => Some(p.clone()),
        (None, Some(dev)) => {
            let code = fetch::codename(dev)?;
            let images = fetch::list_images(&code)?;
            let image = fetch::pick_latest(&images, None)
                .ok_or_else(|| anyhow!("no factory images listed for {code}"))?;
            println!("{code}: {}", image.description);
            Some(fetch::fetch_bootloader(image, std::path::Path::new("."))?)
        }
        (None, None) => None,
    };
    let pack = match &image_path {
        Some(p) => Some(fbpk::load(p)?),
        None => None,
    };
    if let Some(pack) = &pack {
        println!(
            "bootloader.img: platform {} version {} ({} partitions)",
            pack.platform,
            pack.pack_version.trim(),
            pack.data_entries().count()
        );
        let bad: Vec<_> = pack
            .data_entries()
            .filter(|e| !pack.verify_crc(e))
            .map(|e| e.name.clone())
            .collect();
        if !bad.is_empty() {
            bail!(
                "CRC mismatch in {}; the download is corrupt",
                bad.join(", ")
            );
        }
        if pack.find("bl1").is_none() {
            bail!("this bootloader.img has no bl1 partition; the ROM asks for that first");
        }
    }

    let mut files = HashMap::new();
    if let Some(dir) = &args.dir {
        for name in LOOSE_IMAGE_NAMES {
            let path = dir.join(format!("{name}.img"));
            if path.is_file() {
                files.insert(name.to_string(), std::fs::read(&path)?);
                println!("loose image: {}", path.display());
            }
        }
        if files.is_empty() {
            bail!(
                "{} holds none of {}",
                dir.display(),
                LOOSE_IMAGE_NAMES.join(".img, ")
            );
        }
    }
    for (name, path) in parse_kv(&args.stage, "stage")? {
        files.insert(
            name.to_ascii_lowercase(),
            std::fs::read(&path).with_context(|| format!("reading {path}"))?,
        );
    }
    let remap = parse_kv(&args.map, "map")?
        .into_iter()
        .map(|(k, v)| (k.to_ascii_uppercase(), v.to_ascii_lowercase()))
        .collect();

    let sources = stages::Sources { pack, files, remap };
    if let Some(info) = sources.partition("bl1").and_then(stages::header_info) {
        println!(
            "bl1 header: magic {:#010x}, body {} bytes, flags {:#010x}",
            info.magic, info.body_len, info.flags
        );
        if info.flags & 0x1 == 0 {
            println!(
                "  note: bit 0 of the flags word (\"USB bootable\") is clear. The ROM only \
                 accepts a BL1 signed with it set; retail factory images are not, and the \
                 known-good recovery packs carry 0x211 here. Expect \"bl1 header fail\"."
            );
        }
    }
    let mut opts = eub::BootOptions {
        checksum: parse_checksum(&args.checksum)?,
        generation: args.epbl.as_deref().map(|e| match e {
            "full" => stages::Generation::Legacy,
            _ => stages::Generation::Split,
        }),
        send_stop: args.stop,
        verbose: args.verbose,
        idle_timeout: Duration::from_secs(args.idle_timeout),
        startup_timeout: Duration::from_secs(args.idle_timeout),
    };

    let port_name = match &args.port {
        Some(p) => p.clone(),
        None => loop {
            let devices = eub::find_devices()?;
            if let Some(d) = devices.first() {
                if devices.len() > 1 {
                    eprintln!(
                        "more than one ROM device; using {} (pick with --port)",
                        d.port
                    );
                }
                println!(
                    "found Pixel ROM Recovery on {} (serial {})",
                    d.port,
                    d.serial.as_deref().unwrap_or("?")
                );
                break d.port.clone();
            }
            if !args.wait {
                bail!(
                    "no Pixel in ROM-recovery mode found. Hold Power + Volume Up + \
                     Volume Down, plug in USB and keep holding ~15 s, or pass --wait."
                );
            }
            std::thread::sleep(Duration::from_millis(500));
        },
    };

    // The hand-off happens in phases: an early stage (BL2 on Pixel 8) takes
    // over USB, re-enumerates, and carries on asking for stages on a fresh
    // connection. Keep serving until the phone stops coming back as a ROM.
    let mut all_stages: Vec<String> = Vec::new();
    let mut port_name = port_name;
    let mut phase = 1;
    let clean_exit = loop {
        let mut port = eub::open(&port_name)?;
        let report = eub::boot(port.as_mut(), &sources, &opts)?;
        drop(port);

        if report.silent {
            if phase == 1 {
                bail!(
                    "no message from the ROM for {:?}. Is the phone still in ROM-recovery mode?",
                    opts.startup_timeout
                );
            }
            // The device came back but asked for nothing more: the previous
            // phase was the last one.
            println!("no further requests on {port_name}");
            break true;
        }
        all_stages.extend(report.stages_sent);
        if !report.clean_exit {
            break false;
        }
        match wait_for_rom(
            args.port.as_deref(),
            Duration::from_secs(args.reconnect_wait),
        )? {
            Some(next) => {
                phase += 1;
                println!("phone is back as a ROM device on {next}; continuing (phase {phase})");
                port_name = next;
                opts.startup_timeout = Duration::from_secs(args.reconnect_wait);
            }
            None => break true,
        }
    };

    println!();
    if all_stages.is_empty() {
        bail!("the ROM never asked for a stage");
    }
    println!("sent {} stages: {}", all_stages.len(), all_stages.join(" "));
    if clean_exit {
        println!(
            "The phone should now be in fastboot. Check with `fastboot devices`, then make it stick:\n\
             \n  fastboot getvar battery-voltage   # want > 4200 mV before flashing\n  \
             fastboot flash bootloader <bootloader-*.img>\n  \
             fastboot reboot-bootloader\n  \
             ./flash-all.sh                    # from the factory image"
        );
    }
    Ok(())
}

/// Partitions `repair-modem --restore` will write. They hold the modem's
/// calibration and identity (IMEI) data, which a factory image never carries,
/// so the only valid source is a backup of the same phone.
const MODEM_PARTITIONS: &[&str] = &["efs", "efs_backup", "modem_userdata", "persist"];

/// The device codename in a factory file name such as
/// `radio-komodo-g5400c-....img` or `komodo-cp3a....-factory-....zip`.
fn codename_in(file: &str) -> Option<&str> {
    let rest = file
        .strip_prefix("radio-")
        .or_else(|| file.strip_prefix("bootloader-"))
        .unwrap_or(file);
    rest.split('-').next().filter(|c| !c.is_empty())
}

/// The baseband version a radio image carries, from its file name:
/// `radio-komodo-g5400c-250909-250916-b-14110587.img` gives
/// `g5400c-250909-250916-b-14110587`, which is how `getvar version-baseband`
/// spells it (modulo case).
fn radio_version(file: &str) -> Option<String> {
    let rest = file.strip_prefix("radio-")?.strip_suffix(".img")?;
    let (_, version) = rest.split_once('-')?;
    Some(version.to_ascii_lowercase())
}

/// The bootloader version in `bootloader-komodo-ripcurrentpro-17.0-1234.img`.
fn bootloader_version(file: &str) -> Option<String> {
    let rest = file.strip_prefix("bootloader-")?.strip_suffix(".img")?;
    let (_, version) = rest.split_once('-')?;
    Some(version.to_ascii_lowercase())
}

/// The radio image to flash, plus the bootloader file name from the same
/// build when known (to check the pair matches the phone).
fn radio_source(args: &RepairModemArgs) -> Result<Option<(PathBuf, Option<String>)>> {
    if let Some(dev) = &args.device {
        let code = fetch::codename(dev)?;
        let images = fetch::list_images(&code)?;
        let image = fetch::pick_latest(&images, args.build.as_deref()).ok_or_else(|| {
            anyhow!(
                "no build '{}' for {code}; see `fetch {code} --list`",
                args.build.as_deref().unwrap_or("?")
            )
        })?;
        println!("{code}: {}", image.description);
        println!(
            "(downloading accepts Google's factory-image terms, as on {})",
            fetch::IMAGES_PAGE
        );
        let path = fetch::fetch_radio(image, &args.out)?;
        // The listing has no bootloader name; the build id is what matters.
        return Ok(Some((path, None)));
    }
    let Some(path) = &args.image else {
        return Ok(None);
    };
    let is_zip = path
        .extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("zip"));
    if !is_zip {
        return Ok(Some((path.clone(), None)));
    }
    let mut src = remotezip::FileRange::open(path)?;
    let members = remotezip::list(&mut src)?;
    let radio = remotezip::find_image(&members, "radio").ok_or_else(|| {
        anyhow!(
            "no radio-*.img in {}; is it a factory image ZIP (not an OTA)?",
            path.display()
        )
    })?;
    let bootloader =
        remotezip::find_image(&members, "bootloader").map(|m| m.base_name().to_string());
    let data = remotezip::extract(&mut src, radio)?;
    std::fs::create_dir_all(&args.out)?;
    let out = args.out.join(radio.base_name());
    std::fs::write(&out, &data).with_context(|| format!("writing {}", out.display()))?;
    println!("extracted {} ({} bytes)", out.display(), data.len());
    Ok(Some((out, bootloader)))
}

fn file_name(p: &std::path::Path) -> String {
    p.file_name()
        .map(|f| f.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn confirm(prompt: &str) -> Result<bool> {
    use std::io::Write;
    print!("{prompt} [type yes] ");
    std::io::stdout().flush()?;
    let mut line = String::new();
    std::io::stdin().read_line(&mut line)?;
    Ok(line.trim().eq_ignore_ascii_case("yes"))
}

fn repair_modem(args: RepairModemArgs) -> Result<()> {
    let mut restores = Vec::new();
    for (part, file) in parse_kv(&args.restore, "restore")? {
        let part = part.to_ascii_lowercase();
        if !MODEM_PARTITIONS.contains(&part.as_str()) {
            bail!(
                "--restore only writes the modem partitions ({}), not '{part}'",
                MODEM_PARTITIONS.join(", ")
            );
        }
        let path = PathBuf::from(&file);
        let len = std::fs::metadata(&path)
            .with_context(|| format!("reading {file}"))?
            .len();
        ensure!(len > 0, "{file} is empty");
        restores.push((part, path));
    }
    restores.sort();
    if args.image.is_none() && args.device.is_none() && restores.is_empty() {
        bail!(
            "nothing to do: give the radio with --image <radio-*.img or factory.zip> or \
             --device <codename>, and/or a backup with --restore efs=<file>"
        );
    }

    // Talk to the phone before downloading anything, so a missing or
    // ambiguous device fails fast.
    let phone = fastboot::Device::open(args.serial.as_deref())?;
    let var = |name: &str| -> Result<String> {
        Ok(phone.getvar(name)?.unwrap_or_else(|| "?".to_string()))
    };
    let product = var("product")?;
    let unlocked = var("unlocked")?;
    let bootloader = var("version-bootloader")?;
    let baseband = var("version-baseband")?;
    println!(
        "phone {}: product {product}, bootloader {bootloader}",
        phone.serial
    );
    println!("  baseband now: {baseband}");
    println!("  unlocked: {unlocked}");

    let radio = radio_source(&args)?;
    if let Some((path, zip_bootloader)) = &radio {
        let name = file_name(path);
        if let Some(code) = codename_in(&name) {
            if name.starts_with("radio-") && product != "?" && !code.eq_ignore_ascii_case(&product)
            {
                bail!("{name} is for {code}, but this phone is {product}");
            }
        }
        let want = radio_version(&name);
        println!(
            "radio to flash: {name}{}",
            want.as_deref()
                .map(|v| format!(" (baseband {v})"))
                .unwrap_or_default()
        );
        if want.is_some() && want.as_deref() == Some(baseband.to_ascii_lowercase().as_str()) {
            println!(
                "  note: the phone already reports this baseband. Reflashing still rewrites \
                 the modem partition, which fixes a corrupted one."
            );
        }
        if let Some(bl) = zip_bootloader.as_deref().and_then(bootloader_version) {
            if bootloader != "?" && !bl.eq_ignore_ascii_case(&bootloader) {
                println!(
                    "  warning: this build's bootloader is {bl} but the phone runs {bootloader}. \
                     A radio from a different build than the bootloader is the usual cause of \
                     a missing baseband/IMEI; flash the bootloader from the same build \
                     (or use the factory image that matches the phone)."
                );
            }
        }
    }
    for (part, path) in &restores {
        println!("restore {part} from {}", path.display());
    }
    if !restores.is_empty() {
        println!(
            "  these must be backups of THIS phone ({}); another phone's efs/persist would \
             give it the wrong identity and calibration.",
            phone.serial
        );
    }
    if unlocked == "no" {
        bail!(
            "the bootloader is locked, so fastboot will refuse to flash the modem. Unlocking \
             (`fastboot flashing unlock`) wipes user data; if you go that way, back up first."
        );
    }
    if args.dry_run {
        println!("dry run: nothing flashed");
        return Ok(());
    }
    if !args.yes && !confirm(&format!("flash this to {}?", phone.serial))? {
        bail!("not confirmed; nothing flashed");
    }

    if let Some((path, _)) = &radio {
        println!("fastboot flash radio {}", path.display());
        phone.flash("radio", path)?;
        // The bootloader writes the modem image on its next start, so
        // reboot into it before touching anything else.
        println!("fastboot reboot-bootloader");
        phone.reboot_bootloader()?;
        wait_for_fastboot(&phone.serial, Duration::from_secs(60))?;
    }
    for (part, path) in &restores {
        println!("fastboot flash {part} {}", path.display());
        phone.flash(part, path)?;
    }

    let after = var("version-baseband")?;
    println!("baseband after: {after}");
    if after == "?" || after.is_empty() {
        println!(
            "  the bootloader still reports no baseband; the radio may not match the \
             bootloader. Flash both from the same factory image."
        );
    }
    if args.reboot {
        phone.reboot()?;
        println!("rebooting; check the IMEI with *#06# or Settings > About phone");
    } else {
        println!(
            "done. `fastboot reboot`, then check the IMEI with *#06# or Settings > About phone"
        );
    }
    Ok(())
}

fn wait_for_fastboot(serial: &str, wait: Duration) -> Result<()> {
    let started = std::time::Instant::now();
    std::thread::sleep(Duration::from_secs(2));
    while started.elapsed() < wait {
        if fastboot::devices()?.iter().any(|s| s == serial) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    bail!("{serial} did not come back to fastboot within {wait:?}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_versions_from_file_names() {
        let radio = "radio-komodo-g5400c-250909-250916-b-14110587.img";
        assert_eq!(codename_in(radio), Some("komodo"));
        assert_eq!(
            radio_version(radio).as_deref(),
            Some("g5400c-250909-250916-b-14110587")
        );
        let bl = "bootloader-komodo-ripcurrentpro-17.0-14129999.img";
        assert_eq!(codename_in(bl), Some("komodo"));
        assert_eq!(
            bootloader_version(bl).as_deref(),
            Some("ripcurrentpro-17.0-14129999")
        );
        assert_eq!(
            codename_in("komodo-cp3a.260905.009-factory-99eb621a.zip"),
            Some("komodo")
        );
        assert_eq!(radio_version("modem.img"), None);
    }
}
