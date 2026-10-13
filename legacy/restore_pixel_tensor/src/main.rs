extern crate argparse;
extern crate hex;
extern crate std;
extern crate serialport;

use argparse::{Store, ArgumentParser};
use regex::Regex;
use std::path::Path;
use std::fs::File;
use std::result::Result;
use xmodem::Xmodem;
use std::i64;
use std::io::Read;

const XMODEM_BAUD_EBM: u32 = 9600;

struct BootloaderCommand {
    requested_payload: String,
    direction: String,
    base_address: i64
}

fn parse_bootloader_command(command: &str) -> Result<BootloaderCommand, &str> {
    let bl_request_regex: Regex = Regex::new(r"(?m)^eub:(req):([0-9A-F]+):(\w+)$").unwrap();

    let groups = bl_request_regex.captures(command);

    match groups {
        Some(g) => {
            let result =                 BootloaderCommand {
                direction: g[1].to_string(),
                base_address: i64::from_str_radix(&g[2], 16).expect("Failed to parse base address"),
                requested_payload: g[3].to_string(),
            };
            Ok(result)
        },
        None => {
            Err("Unable to parse the line")
        }
    }
}

fn main() {
    let mut firmware = "".to_string();
    let mut device = "".to_string();
    {
        let mut parser = ArgumentParser::new();
        parser.refer(&mut firmware).add_option(&["-f", "--firmware"], Store, "The directory of the boot firmware");
        parser.refer(&mut device).add_option(&["-d", "--device"], Store, "The UART device path");
        parser.parse_args_or_exit();
    }

    let firmware_path = Path::new(&firmware);

    let mut port = serialport::new(device, XMODEM_BAUD_EBM).open().expect("Port must open");

    let mut input_string: String = "".to_string();
    port.read_to_string(&mut input_string).expect("Failed to read");

    let bootloader_command = parse_bootloader_command(&input_string).unwrap();

    if bootloader_command.direction == "req" {
        let file_path = firmware_path.join(Path::new(&format!("{}.bin", &bootloader_command.requested_payload)));

        let mut file = File::open(file_path).expect("must open");

        let mut xmodem_connection = Xmodem::new();
        xmodem_connection.send(&mut port, &mut file).expect("TODO: panic message");
    }
}