# Raspberry Pi Zero 2 W Porting Guide

This guide is for agents and contributors creating a Raspberry Pi Zero 2 W version of Quartermaster from this repository. The goal is to adapt the Linux application and its hardware integration, not to rewrite its data pipeline.

## Compatibility Goal

The Pi version must produce the same logs and use the same configuration as the existing application. Treat existing serializers, persisted files, tests, and upload behavior as compatibility contracts.

- Keep log directory layout, filenames, encodings, headers, column order, field meanings, timestamps, units, row-selection rules, and gzip/JSONL representation unchanged.
- Keep existing settings keys, defaults, accepted values, validation, and behavior unchanged. Existing settings files must continue to load without losing unknown or currently unsupported values when the Pi app saves settings.
- Keep account and upload-ledger formats, data-root behavior, file permissions, rotation limits, and crash-recovery journals compatible.
- Do not add a Pi-only log format or silently translate existing settings. If a hardware setting has no Pi implementation, retain its stored value and report the feature as unavailable. Any intentional migration must be explicit, reversible where practical, and tested against existing files.
- Platform identity in the WiGLE pre-header is the one likely device-specific detail. Preserve its record structure, keys, and order. If changing a value that currently identifies CardputerZero, make that a conscious metadata-only change; do not alter the WiGLE CSV schema or observation semantics.

Before changing a writer or a persisted-data path, read its existing tests. The code and tests named below are the source of truth; this guide is not a replacement schema specification.

## Repository Workflow

For a separately maintained Pi target, create a fork and work on a descriptively named branch such as `pi-zero-2w`. Keep the fork's `main` branch aligned with upstream and do Pi-specific work on the feature branch. This makes upstream updates and review of upstream-compatible fixes easier. If the maintainers intend to support the Pi target in the upstream project, coordinate before choosing a branch directly in the upstream repository.

Keep platform-independent fixes separable from Pi-only changes. Avoid rewriting history or force-pushing shared branches. Record the upstream commit used as the port's baseline and periodically merge or rebase from upstream according to the fork's collaboration policy.

## Target Assumptions

Select and document the Raspberry Pi OS release and 32-bit or 64-bit image before implementation. Kernel drivers, firmware packages, BlueZ, NetworkManager, Python packages, and device permissions vary by image.

The initial Pi Zero 2 W MVP is headless Linux collection: start a basic wardrive, collect Wi-Fi/Bluetooth, GPS, and the selected Waveshare SX1262 XXXM LoRaWAN/GNSS HAT data, and save logs using the existing formats. Do not make a display, keyboard, audio, battery monitoring, or GPIO-controlled power a prerequisite. `quartermaster/setup_server.py` handles setup and log workflows, but is not a complete application UI.

Treat every USB adapter as a candidate, not as supported merely because it works on the Cardputer. Verify its kernel driver, firmware, userspace tools, permissions, RF behavior, and power draw on the chosen Pi image. Use a powered USB hub if the combined adapters exceed the Pi's available USB power.

Running `quartermaster` starts collection in the background and returns; `python3 -m quartermaster` does the same from a source checkout. Use `quartermaster --status` to check it, `quartermaster --stop` to stop/finalize it, and inspect `~/quartermaster/background.log` for snapshots. Use `quartermaster --dashboard` for a foreground diagnostics screen with cumulative Wi-Fi, Bluetooth, aircraft, mesh, row, and sighting counts, GPS freshness, adapter errors, HAT status, and upload state. Ctrl-C stops and finalizes logs. Copy completed artifacts to a mounted drive with `quartermaster --export DIRECTORY`; it preserves `logs/` and `recon/` directories and skips partial journals. `--headless-wardrive` remains a compatibility alias for foreground mode. The runner can receive mesh packets on the Waveshare SX1262 HAT and can include ADS-B when enabled and an RTL-SDR is available. GNSS uses the Pi primary UART candidate and must be confirmed on-device. ESP32 nodes and Recon UI remain outside the MVP. WiGLE observations still require a fresh GPS fix.

Upload behavior keeps the upstream default: automatic uploads are enabled when valid service keys are configured. Run `quartermaster --configure-uploads` to enter keys through hidden prompts; credentials are saved in `~/.config/quartermaster/accounts.json` with private permissions. Run `quartermaster --upload` to send completed logs explicitly, or `quartermaster --upload-status` to inspect the queue without sending. Use `quartermaster --set auto_upload false` to opt out. `--show-config` displays settings with the cluster key redacted; `--set KEY VALUE` changes a validated scalar setting. Existing saved preferences remain in effect.

Stop the wardrive before `--export` or manual `--upload`; both commands refuse to run while a collector holds the process lock, ensuring only finalized logs are copied or sent.

### Pi UART GPS

The Waveshare HAT includes an L76K GNSS module. The Pi Zero 2 W primary UART is available on the 40-pin header at physical pin 8 (BCM GPIO14 / TXD) and physical pin 10 (BCM GPIO15 / RXD); a UART receiver's TX goes to Pi RX and RX to Pi TX. Use a common ground and 3.3 V UART signaling. Enable the hardware UART and disable the serial login console in Raspberry Pi OS. The app reads `/dev/serial0`; it does not bit-bang GPIO. Existing GPS discovery checks this primary UART, and `gps_source` can explicitly select `/dev/serial0`. Confirm the HAT's GNSS-to-UART routing for the exact board revision before hardware acceptance.

### SX1262 LoRa HAT

The target HAT is the 868/915 MHz version for US use. Existing US mesh profiles use the 902-928 MHz region; set the application country to `US`. The attached Waveshare receiver example maps SPI bus 0 / CE0, RESET BCM GPIO18 (physical pin 12), BUSY GPIO20 (pin 38), DIO1/IRQ GPIO16 (pin 36), and TXEN GPIO6 (pin 31); RXEN is unused. Standard Pi Zero 2 W SPI0 pins are MOSI physical pin 19 (BCM GPIO10), MISO pin 21 (BCM GPIO9), and SCLK pin 23 (BCM GPIO11). Waveshare's text says SX1262 DIO2 controls the RF switch. The port is receive-only and drives TXEN high for receive; Cardputer GPIO/CE pins are not reused.

## Porting Sequence

1. **Establish a baseline.** Record the upstream commit, target OS/image, architecture, expected UI, adapter list, and required features. Run the existing tests before edits and keep the output as the comparison baseline.
2. **Separate startup from hardware.** The default startup in `quartermaster/__main__.py` runs the Pi terminal dashboard and headless collector. Keep it independent from the opt-in legacy `--cardputer-ui` path and do not initialize Cardputer-only display, power, or LoRa devices on the Pi path.
3. **Retain the data pipeline.** Reuse `quartermaster/engine.py` and the existing serializers unless target testing proves a specific interface must change. Keep collection, tracking, row selection, logging, journal recovery, and upload code independent from display and GPIO choices.
4. **Validate Linux adapters individually.** Test the existing Wi-Fi and Bluetooth paths in `quartermaster/hardware.py`, `quartermaster/wifi.py`, and `quartermaster/ble.py`. Verify GPS via USB serial or gpsd using `quartermaster/gnss.py`. RTL-SDR and ESP32 serial nodes may be retained after testing their programs, drivers, permissions, and power. Do not claim arbitrary adapters are supported.
5. **Replace platform-specific hardware.** Keep the MVP terminal-based and independent of the Cardputer UI. Use the Pi's Linux Wi-Fi/Bluetooth interfaces and validate HAT GNSS on its UART. The Waveshare SX1262 transport uses the supplied receiver example's SPI0 CE0 and RESET/BUSY/DIO1 assignments, and reuses the existing receive and mesh packet logic from `quartermaster/lora.py` and `quartermaster/meshpackets.py`; it omits the Cardputer switched-rail and antenna-mux assumptions. Validate on the actual HAT before claiming hardware support.
6. **Adapt distribution.** Use the PEP 517 package in `pyproject.toml` to build a platform-independent wheel (`python -m build --wheel`). Install with pip in a virtual environment and expose the `quartermaster` command. Keep Linux drivers and services as documented OS prerequisites rather than package-manager-specific dependencies. The legacy `tools/build_deb.py`, `packaging/postinst`, and APPLaunch metadata are not part of the Pi MVP.
7. **Preserve user data on upgrade.** Keep existing data and config paths by default. If a Pi install needs a different service account or path, provide a one-time migration that preserves ownership, permissions, and contents, and test upgrades from an existing Quartermaster data directory.

## Data and Configuration Contracts

These are the primary compatibility surfaces. Preserve their current exact schemas and behavior; use the serializers and regression tests as the definitive details.

| Data | Current implementation | Compatibility checks |
| --- | --- | --- |
| Wi-Fi/Bluetooth WiGLE CSV and its hidden write-ahead journal | `quartermaster/wigle.py`: `COLUMNS`, `pre_header`, `WigleWriter`, `compact` | `tests/test_core.py::test_wigle_file_is_spec_exact`; preserve the `WigleWifi-1.6` pre-header structure, 14-column header, row types, UTC/fix requirements, and compaction behavior. Change device identity values to Pi Zero 2 W while retaining the same metadata keys and record order. |
| Wardrive summary CSV | `quartermaster/engine.py`: `SUMMARY_COLUMNS`, `_summary_row` | `tests/test_engine.py`; preserve all 19 columns and row meaning. |
| Aircraft gzip JSON and CSV summary | `quartermaster/adsb.py`: `Sky.document`, `Plane.entry`, `SUMMARY_COLUMNS`, `write` | `tests/test_adsb.py`; preserve dump1090/readsb-shaped JSON and all 22 summary columns. |
| Mesh gzip JSON and CSV summary | `quartermaster/mesh.py`: `Mesh.document`, `Node.entry`, `SUMMARY_COLUMNS`, `write` | `tests/test_lora.py`; preserve `format: "quartermaster-mesh-1"` and all 24 summary columns. |
| Recon and Drive Alert JSON Lines | `quartermaster/recon_engine.py`, `quartermaster/drive_watch.py` | `tests/test_recon.py`, `tests/test_drive_watch.py`; preserve one JSON object per line, record field meaning, filenames, and the rule that Drive Alert logs are not uploaded. |
| Settings JSON | `quartermaster/config.py`: `PATH`, `DEFAULTS`, `load`, `save` | `tests/test_ui.py`, `tests/test_nodes.py`; preserve defaults, accepted values, validation, and existing user choices. |
| Credentials JSON and upload ledger | `quartermaster/accounts.py`, `quartermaster/uploads.py`: `FIELDS`, `Ledger` | `tests/test_ui.py`, `tests/test_uploads.py`; preserve field names, secure permissions, resumable delivery state, and duplicate-send behavior. |

The default data directory is `QUARTERMASTER_HOME` or `~/quartermaster`; config and credentials use `QUARTERMASTER_CONFIG` and `QUARTERMASTER_ACCOUNTS` or their existing `~/.config/quartermaster/` paths. Check the source for exact behavior before changing any path. The current log pipeline uses hidden `.part` journals, startup recovery, and 8 MiB WiGLE log rotation; these operational details are part of compatibility, not just implementation internals.

When a code change touches a contract, add or retain tests that:

- Compare exact CSV headers and representative row values, including the WiGLE pre-header structure.
- Parse decompressed aircraft and mesh JSON and assert required keys, format markers, and summary columns.
- Parse every JSONL line and assert required fields and stable field meaning.
- Load and save representative existing settings, credentials, and upload ledgers without losing values or changing their shape.
- Exercise interrupted-write recovery, truncated journals, name collisions, log rotation, and upload resumption after restart where relevant.
- Verify output with the existing readers/uploaders or compatibility fixtures, not only with the new implementation's own parser.

Do not weaken or delete an existing compatibility assertion to make the port pass. If a change is required, first establish whether the difference is platform metadata, an implementation bug, or a proposed format break; document the decision and add a migration or compatibility test before changing behavior.

## Host Checks

Use the repository's existing checks as a baseline. Install the test/build dependencies, then run:

```sh
python3 -m pip install .[test,performance] build
python3 -m pyflakes quartermaster tests tools
python3 -m pytest -q tests
python3 tools/render_screens.py --out build/screenshots
```

CI builds a universal wheel with `python -m build --wheel` and smoke-tests the installed `quartermaster --version` command. Linux radio drivers and services remain OS-level prerequisites and must be validated on the target system.

### Optional systemd user service

`systemd/quartermaster.service` runs the collector as the logged-in user from `~/.local/opt/quartermaster/venv`. From the repository root, install and enable it with:

```sh
mkdir -p ~/.local/opt/quartermaster
python3 -m venv --system-site-packages ~/.local/opt/quartermaster/venv
~/.local/opt/quartermaster/venv/bin/pip install '.[pi-hat]'
mkdir -p ~/.config/systemd/user
cp systemd/quartermaster.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now quartermaster.service
```

This explicitly starts wardriving immediately and at subsequent logins. `systemctl --user stop quartermaster` sends SIGINT to finalize active logs; use `systemctl --user status quartermaster` to check state and `journalctl --user -u quartermaster -f` to view periodic dashboard snapshots. On Raspberry Pi OS, install `python3-dbus` from the OS package manager and create this venv with `python3 -m venv --system-site-packages ~/.local/opt/quartermaster/venv`, so the BLE scanner can import the system D-Bus binding. The unit is unprivileged: grant its user the required SPI, GPIO, Wi-Fi, Bluetooth, and serial-device access using the target OS's normal device groups or udev rules. Do not solve device access by running the service as root. User services normally start with login; enabling lingering is an optional administrator action if it must survive logout or start before login.

## Pi Hardware Acceptance

Run this checklist on the actual Pi Zero 2 W and chosen Raspberry Pi OS image. Host tests cannot validate kernel drivers, RF availability, device permissions, display behavior, or USB power.

- App starts and exits cleanly using the documented launcher or service; restart and shutdown do not leave scanners or child processes behind.
- The selected display and keyboard work, or the documented headless interface provides the complete intended workflow.
- Each listed adapter is discovered, reports useful errors when absent or denied, and operates with the documented least-privilege permissions.
- GPS fix freshness and timestamps are valid; WiGLE rows are written only under the existing validity rules.
- A representative session produces the existing filenames, schemas, headers, row semantics, compression, and upload behavior. Compare outputs with checked-in compatibility fixtures or the upstream version using equivalent input.
- Existing settings, credentials, and upload state remain usable after upgrade; saving settings does not erase unsupported Cardputer options.
- Stop, forced termination, and reboot recover journals without corrupting completed logs or duplicating uploads.
- Sustained operation stays within acceptable CPU and memory use; attached adapters remain stable under USB power load.

Document each tested OS image, adapter model/chip, required firmware/package, permissions, and test result. Support claims should name the tested combination rather than generalizing from the connector type.