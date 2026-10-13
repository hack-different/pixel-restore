//! Talking to a Pixel in ROM-recovery mode over its serial port: finding the
//! port, and running the request/upload loop until the ROM stops asking for
//! stages (which is when ABL takes over and the phone shows fastboot).

use crate::devices::{self, Soc};
use crate::dnw::{self, Checksum, LineReader, Message};
use crate::stages::{self, Generation, Sources};
use anyhow::{bail, Context, Result};
use serialport::{SerialPortInfo, SerialPortType};
use std::io::{Read, Write};
use std::time::{Duration, Instant};

/// Google's USB vendor id and the product id the Tensor boot ROM enumerates
/// with. macOS shows it as "Pixel ROM Recovery" in System Information.
pub const ROM_VID: u16 = 0x18d1;
pub const ROM_PID: u16 = 0x4f00;

pub const BAUD: u32 = 115_200;
const READ_TIMEOUT: Duration = Duration::from_millis(200);
/// tensor-usbdl writes in 10 KiB blocks and flushes after each; the ROM has no
/// flow control, so pacing writes this way is what keeps it from dropping data.
const WRITE_BLOCK: usize = 10_240;
/// The ROM repeats its request and `C` about every 0.8 s while it waits, so
/// a connection that has said nothing for this long really is silent and a
/// newline nudge cannot land inside an upload the ROM is expecting.
const NUDGE_AFTER: Duration = Duration::from_millis(1500);

#[derive(Debug, Clone)]
pub struct RomDevice {
    pub port: String,
    pub serial: Option<String>,
    pub product: Option<String>,
}

/// Every serial port that belongs to a Pixel in ROM-recovery mode.
pub fn find_devices() -> Result<Vec<RomDevice>> {
    let ports = serialport::available_ports().context("listing serial ports")?;
    let mut found: Vec<RomDevice> = ports
        .into_iter()
        .filter_map(|p| match &p.port_type {
            SerialPortType::UsbPort(usb) if usb.vid == ROM_VID && usb.pid == ROM_PID => {
                Some(RomDevice {
                    port: p.port_name.clone(),
                    serial: usb.serial_number.clone(),
                    product: usb.product.clone(),
                })
            }
            _ => None,
        })
        .collect();
    // macOS exposes each CDC device twice, as /dev/tty.* and /dev/cu.*. The
    // cu (call-up) node is the one meant for initiating a connection.
    if found.iter().any(|d| d.port.starts_with("/dev/cu.")) {
        found.retain(|d| !d.port.starts_with("/dev/tty."));
    }
    Ok(found)
}

/// Serial ports that are *not* the ROM, for the `detect --all` listing.
pub fn all_ports() -> Result<Vec<SerialPortInfo>> {
    serialport::available_ports().context("listing serial ports")
}

/// What the boot loop needs from a port: bytes in, bytes out. Reads time out
/// with `ErrorKind::TimedOut` after about `READ_TIMEOUT`.
pub trait Link: Read + Write + Send {}
impl<T: Read + Write + Send + ?Sized> Link for T {}

pub fn open(port: &str) -> Result<Box<dyn Link>> {
    match serialport::new(port, BAUD).timeout(READ_TIMEOUT).open() {
        Ok(p) => Ok(Box::new(p)),
        // macOS: serialport always sets the speed with the IOSSIOSPEED ioctl,
        // which a pseudo-terminal (tools/fake_rom.py) rejects with ENOTTY.
        Err(e)
            if e.to_string().contains("Not a typewriter") || e.to_string().contains("ENOTTY") =>
        {
            Ok(Box::new(PtyLink::open(port)?))
        }
        Err(e) => Err(e).with_context(|| format!("opening {port}")),
    }
}

/// A pseudo-terminal opened as a plain non-blocking file, for testing
/// against the fake ROM where serialport cannot configure it.
struct PtyLink(std::fs::File);

impl PtyLink {
    fn open(port: &str) -> Result<PtyLink> {
        use std::os::unix::fs::OpenOptionsExt;
        #[cfg(target_os = "linux")]
        const O_NONBLOCK: i32 = 0o4000;
        #[cfg(not(target_os = "linux"))]
        const O_NONBLOCK: i32 = 0x0004;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(O_NONBLOCK)
            .open(port)
            .with_context(|| format!("opening {port}"))?;
        Ok(PtyLink(file))
    }
}

impl Read for PtyLink {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let deadline = Instant::now() + READ_TIMEOUT;
        loop {
            match self.0.read(buf) {
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    if Instant::now() > deadline {
                        return Err(std::io::ErrorKind::TimedOut.into());
                    }
                    std::thread::sleep(Duration::from_millis(5));
                }
                other => return other,
            }
        }
    }
}

impl Write for PtyLink {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        loop {
            match self.0.write(buf) {
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1))
                }
                other => return other,
            }
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.0.flush()
    }
}

/// Options for one boot attempt.
pub struct BootOptions {
    pub checksum: Checksum,
    /// Force the generation instead of guessing from the ROM's serial.
    pub generation: Option<Generation>,
    /// Refuse to send anything if the ROM reports a different SoC: the user
    /// named a model and the ROM says it is something else.
    pub expected_soc: Option<Soc>,
    /// Send the DNW STOP command as soon as the port is open.
    pub send_stop: bool,
    /// Print every raw line from the ROM.
    pub verbose: bool,
    /// Give up if the ROM says nothing for this long before the first stage.
    pub idle_timeout: Duration,
    /// After a stage has been sent, this much silence ends the session so the
    /// caller can look for the phone again. When BL2 takes over it
    /// re-enumerates and carries on asking for stages on a new connection,
    /// and on macOS the old file descriptor just goes quiet.
    pub settle: Duration,
}

/// What the phone has told us so far, carried across reconnects.
#[derive(Debug, Default)]
pub struct Session {
    pub serial: Option<String>,
    pub soc: Option<Soc>,
    pub stages_sent: Vec<String>,
}

/// Why a session on one connection ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ending {
    /// The port went away (the phone re-enumerated or left ROM mode).
    Disconnected,
    /// The phone stopped talking after a stage.
    Quiet,
}

fn note_serial(session: &mut Session, serial: &str, opts: &BootOptions) -> Result<()> {
    if serial.is_empty() || session.serial.as_deref() == Some(serial) {
        return Ok(());
    }
    let soc = devices::soc_for_serial(serial);
    if session.serial.is_none() {
        match soc {
            Some(s) => println!(
                "ROM chip id {} = {} {} ({})",
                &serial[1..5],
                s.tensor,
                s.name,
                devices::family(s)
            ),
            None => println!("ROM serial {serial}: unknown chip, assuming Pixel 8 style"),
        }
    }
    if let (Some(want), Some(got)) = (opts.expected_soc, soc) {
        if want != got {
            bail!(
                "this phone's ROM is {} {} ({}), not {} {} as the chosen device implies. \
                 Nothing was sent. Check the model and pass the right --device.",
                got.tensor,
                got.name,
                devices::family(got),
                want.tensor,
                want.name
            );
        }
    }
    session.serial = Some(serial.to_string());
    session.soc = soc;
    Ok(())
}

/// Listen for the ROM's first request and return its serial, without
/// sending it anything but a newline nudge if it is silent.
pub fn probe(port: &mut dyn Link, timeout: Duration) -> Result<Option<String>> {
    let mut lines = LineReader::default();
    let mut buf = [0u8; 4096];
    let opened = Instant::now();
    let mut nudged = false;
    let mut heard = false;
    while opened.elapsed() < timeout {
        match port.read(&mut buf) {
            Ok(n) => {
                lines.push(&buf[..n]);
                heard |= n > 0;
            }
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(e) if is_disconnect(&e) => return Ok(None),
            Err(e) => return Err(e).context("reading from ROM"),
        }
        while let Some(line) = lines.next_line() {
            match Message::parse(&line) {
                Some(Message::Booting { serial }) | Some(Message::Request { serial, .. })
                    if !serial.is_empty() =>
                {
                    return Ok(Some(serial))
                }
                _ => {}
            }
        }
        if !nudged && !heard && opened.elapsed() > NUDGE_AFTER {
            nudged = true;
            port.write_all(b"\n").ok();
            port.flush().ok();
        }
    }
    Ok(None)
}

/// Drive the boot ROM on one connection: answer every `eub:req` with the
/// right slice of the right image until it goes away or goes quiet.
pub fn boot(
    port: &mut dyn Link,
    sources: &Sources,
    opts: &BootOptions,
    session: &mut Session,
) -> Result<Ending> {
    let mut lines = LineReader::default();
    let mut pending: Option<String> = None;
    let mut last_activity = Instant::now();
    let mut sent_here = false;
    let mut buf = [0u8; 4096];
    let generation = |session: &Session| {
        opts.generation.unwrap_or_else(|| {
            session
                .soc
                .map(|s| s.generation)
                .unwrap_or(Generation::Split)
        })
    };

    // A newline nudges a silent ROM into (re)sending its current request. It
    // is only sent if nothing at all has arrived on this connection: a stray
    // newline arriving after `C` would become the first byte of the upload.
    let opened = Instant::now();
    let mut nudged = false;
    let mut heard = false;
    if opts.send_stop {
        println!("sending DNW STOP");
        port.write_all(&dnw::stop_frame())?;
        port.flush().ok();
    }

    loop {
        match port.read(&mut buf) {
            Ok(0) => {}
            Ok(n) => {
                lines.push(&buf[..n]);
                last_activity = Instant::now();
                heard |= n > 0;
            }
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(e) if is_disconnect(&e) => {
                if opts.verbose {
                    println!("  port closed by device ({e})");
                }
                return Ok(Ending::Disconnected);
            }
            Err(e) => return Err(e).context("reading from ROM"),
        }

        if !nudged && !heard && opened.elapsed() > NUDGE_AFTER {
            nudged = true;
            port.write_all(b"\n").context("nudging the ROM")?;
            port.flush().ok();
        }

        let quiet = last_activity.elapsed();
        if sent_here && pending.is_none() && quiet > opts.settle {
            return Ok(Ending::Quiet);
        }
        if quiet > opts.idle_timeout {
            if !session.stages_sent.is_empty() {
                return Ok(Ending::Quiet);
            }
            let partial = String::from_utf8_lossy(lines.pending())
                .escape_debug()
                .to_string();
            bail!(
                "no message from the ROM for {:?} (pending request: {:?}, unparsed bytes: \"{}\"). \
                 Is the phone still in ROM-recovery mode?",
                opts.idle_timeout,
                pending,
                partial
            );
        }

        while let Some(line) = lines.next_line() {
            if opts.verbose {
                println!("  < {}", String::from_utf8_lossy(&line).escape_debug());
            }
            let Some(msg) = Message::parse(&line) else {
                continue;
            };
            match msg {
                Message::Booting { serial } => note_serial(session, &serial, opts)?,
                Message::Request { serial, stage } => {
                    note_serial(session, &serial, opts)?;
                    if pending.as_deref() == Some(stage.as_str()) {
                        continue; // the ROM repeats its request until served
                    }
                    println!("ROM requests {stage}");
                    pending = Some(stage);
                }
                Message::ClearToSend => {
                    let Some(stage) = pending.take() else {
                        if opts.verbose {
                            println!("  (clear-to-send with nothing pending)");
                        }
                        continue;
                    };
                    let (payload, desc) = sources.payload_for(&stage, generation(session))?;
                    print!("  sending {desc} ... ");
                    std::io::stdout().flush().ok();
                    let started = Instant::now();
                    send(port, &payload, opts.checksum)
                        .with_context(|| format!("uploading {stage}"))?;
                    println!("done in {:.1?}", started.elapsed());
                    session.stages_sent.push(stage);
                    sent_here = true;
                    last_activity = Instant::now();
                }
                Message::Ack { stage } => {
                    if opts.verbose {
                        println!("  ROM acknowledged {stage}");
                    }
                }
                Message::Nak { stage } => {
                    bail!("ROM refused {stage}. Wrong device's image, or an older build than the phone's anti-rollback level?");
                }
                Message::HeaderFail { stage } => {
                    let usb_bit = stage.eq_ignore_ascii_case("bl1")
                        && sources
                            .partition("bl1")
                            .and_then(stages::header_info)
                            .is_some_and(|h| h.flags & 1 == 0);
                    if usb_bit {
                        bail!(
                            "ROM rejected the bl1 header. This BL1 is a retail one (USB-boot bit \
                             clear), which the ROM refuses over USB. Use a recovery pack: \
                             `--device <codename>` picks one, see `pixel-restore models`."
                        );
                    }
                    bail!(
                        "ROM rejected the {stage} header: the image is for a different SoC, \
                         is missing the USB-boot bit, or is older than the anti-rollback \
                         version burned into the phone."
                    );
                }
                Message::BootFailure { trace } => {
                    let after = session
                        .stages_sent
                        .last()
                        .map(|s| format!(" after {s}"))
                        .unwrap_or_default();
                    let mut text = format!("boot ROM reported a failure{after}:");
                    for t in &trace {
                        text.push_str("\n  > ");
                        text.push_str(t);
                    }
                    bail!(text);
                }
                Message::Other(text) => {
                    if !opts.verbose {
                        println!("  ROM: {}", text.escape_debug());
                    }
                }
            }
        }
    }
}

fn send(port: &mut dyn Link, payload: &[u8], checksum: Checksum) -> Result<()> {
    let frame = dnw::upload_frame(payload, checksum);
    for chunk in frame.chunks(WRITE_BLOCK) {
        port.write_all(chunk)?;
        port.flush()?;
    }
    Ok(())
}

fn is_disconnect(e: &std::io::Error) -> bool {
    use std::io::ErrorKind::*;
    matches!(
        e.kind(),
        BrokenPipe | NotConnected | ConnectionAborted | ConnectionReset | NotFound
    ) || e.raw_os_error() == Some(6) // ENXIO: device vanished (macOS/Linux)
        || e.raw_os_error() == Some(5) // EIO on a gone tty
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::io::{self, Cursor};

    struct FakeRom {
        incoming: Cursor<Vec<u8>>,
        outgoing: Vec<u8>,
    }
    impl Read for FakeRom {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let n = self.incoming.read(buf)?;
            if n == 0 {
                Err(io::ErrorKind::BrokenPipe.into())
            } else {
                Ok(n)
            }
        }
    }
    impl Write for FakeRom {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            self.outgoing.extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    fn options() -> BootOptions {
        BootOptions {
            checksum: Checksum::default(),
            generation: None,
            expected_soc: None,
            send_stop: false,
            verbose: false,
            idle_timeout: Duration::from_secs(1),
            settle: Duration::from_secs(1),
        }
    }
    #[test]
    fn legacy_request_payload_is_sent_unchanged_in_a_dnw_frame() {
        // Lowercase request, a serial too large for the prototype's i64 parser,
        // a pre-split body, and a bare clear-to-send byte.
        let mut port = FakeRom {
            incoming: Cursor::new(b"eub:req:09875001abcdef0123456789:ablb\r\nC".to_vec()),
            outgoing: Vec::new(),
        };
        let payload = vec![0x5a; stages::HEADER_LEN + 23];
        let sources = Sources {
            pack: None,
            files: HashMap::new(),
            remap: HashMap::new(),
            request_files: Some(HashMap::from([("ABLB".into(), payload.clone())])),
        };
        let mut session = Session::default();
        assert_eq!(
            boot(&mut port, &sources, &options(), &mut session).unwrap(),
            Ending::Disconnected
        );
        let frame = &port.outgoing;
        assert_eq!(&frame[..4], b"\x1bDNW");
        assert_eq!(
            u32::from_le_bytes(frame[4..8].try_into().unwrap()) as usize,
            payload.len() + 10
        );
        assert_eq!(&frame[8..frame.len() - 2], payload);
        assert_eq!(&frame[frame.len() - 2..], &[255, 255]);
        assert_eq!(session.stages_sent, ["ABLB"]);
    }
    #[test]
    fn a_missing_request_file_stops_before_upload() {
        let mut port = FakeRom {
            incoming: Cursor::new(b"eub:req:09875001abcdef0123456789:BL1\r\nC".to_vec()),
            outgoing: Vec::new(),
        };
        let sources = Sources {
            pack: None,
            files: HashMap::new(),
            remap: HashMap::new(),
            request_files: Some(HashMap::new()),
        };
        let mut session = Session::default();
        assert!(boot(&mut port, &sources, &options(), &mut session)
            .unwrap_err()
            .to_string()
            .contains("no exact payload"));
        assert!(port.outgoing.is_empty());
        assert!(session.stages_sent.is_empty());
    }
}
