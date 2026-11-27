"""Entry point for the headless Linux wardriving application."""

import argparse
from contextlib import ExitStack
from functools import partial
import json
import os
import signal
import sys
import tempfile
import threading


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quartermaster", description="Quartermaster wardriver")
    parser.add_argument("--display", choices=("cardputer", "st7735-128", "st7789-240", "st7789-320x240"), default=None,
                        help="open the CardputerZero framebuffer or a Waveshare LCD HAT with its controls; "
                             "without it, run the headless Linux collector")
    parser.add_argument("--epaper", choices=("2in13-v4",), default=None,
                        help="show the headless collector's counts on a Waveshare 2.13inch e-Paper HAT (V4)")
    parser.add_argument("--oled", choices=("ssd1309",), default=None,
                        help="show the headless collector's counts on a 128x64 I2C OLED (address 0x3C)")
    parser.add_argument("--apply-settings", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--autostart", action="store_true",
                        help="start the wardrive as soon as the LCD UI opens, without a button press")
    parser.add_argument("--no-splash", action="store_true", help="skip the boot animation")
    parser.add_argument("--probe", action="store_true", help="print the adapters and GPS ports found, then exit")
    parser.add_argument("--headless-wardrive", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dashboard", action="store_true",
                        help="run in the foreground with live counts; Ctrl-C stops and saves logs")
    parser.add_argument("--stop", action="store_true", help="stop the background wardrive and finalize logs")
    parser.add_argument("--status", action="store_true", help="show whether a background wardrive is running")
    parser.add_argument("--_background-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--configure-uploads", action="store_true", help="securely enter or update upload keys")
    parser.add_argument("--upload", action="store_true", help="upload finished logs now using saved keys")
    parser.add_argument("--upload-status", action="store_true", help="show the finished-log upload queue without sending")
    parser.add_argument("--export", metavar="DIRECTORY", help="copy finished logs to an external directory")
    parser.add_argument("--show-config", action="store_true", help="show settings; secret values are redacted")
    parser.add_argument("--set", dest="settings", nargs=2, action="append", metavar=("KEY", "VALUE"),
                        help="set a validated scalar setting, for example --set adsb off")
    parser.add_argument("--cardputer-ui", action="store_true", help="same as --display cardputer")
    parser.add_argument("--version", action="store_true", help="print the version, then exit")
    args = parser.parse_args(argv)
    os.umask(0o077)

    from . import __version__, background, config, hardware, lora
    from .engine import DEFAULT_ROOT
    if args.version:
        print(__version__)
        return 0
    display = args.display or ("cardputer" if args.cardputer_ui else None)
    if (args.epaper or args.oled) and display:
        parser.error("--epaper and --oled work with the headless collector, not with --display")
    if args.epaper and args.oled:
        parser.error("choose one of --epaper and --oled")
    headless = display is None
    cardputer = display == "cardputer"
    gps_ports = partial(hardware.gps_candidates, cardputer=cardputer, **({"pi_uart": True} if headless else {}))
    if args.apply_settings:
        from . import bootconf

        applied, problems = bootconf.apply(args.apply_settings)
        for problem in problems:
            print("quartermaster.txt: %s" % problem, file=sys.stderr)
        print("quartermaster.txt applied: %s" % (", ".join(applied) or "nothing"))
        return 0
    if args.configure_uploads:
        return configure_uploads()
    if args.show_config:
        return show_config(config)
    if args.settings:
        return set_config(config, args.settings)
    if args.upload_status:
        return background.run_if_idle(DEFAULT_ROOT, lambda: upload_status(DEFAULT_ROOT),
                                      "Stop the wardrive before checking the upload queue.")
    if args.upload:
        return background.run_if_idle(DEFAULT_ROOT, lambda: upload_finished(DEFAULT_ROOT),
                                      "Stop the wardrive before uploading finished logs.")
    if args.export:
        return background.run_if_idle(DEFAULT_ROOT, lambda: export_finished(DEFAULT_ROOT, args.export),
                                      "Stop the wardrive before exporting finalized logs.")
    if args.stop:
        return background.stop(DEFAULT_ROOT)
    if args.status:
        return background.status(DEFAULT_ROOT)
    if args.probe:
        from . import adsb, ble

        print("display  %s" % (display or "headless"))
        for adapter in hardware.wifi_adapters():
            print("wifi  %(interface)s  %(label)s  bands=%(bands)s  usb=%(usb_id)s  driver=%(driver)s" % adapter)
        for adapter in hardware.stalled_usb_adapters():
            title, detail = hardware.stall_words(hardware.explain(adapter))
            print("wifi  (not starting)  %s  usb=%s  port=%s  %s" % (title, adapter["usb_id"], adapter["port"], detail))
        for port in gps_ports():
            print("gps   %(path)s  %(label)s" % port)
        if headless:
            dongles = hardware.rtl_sdr_dongles()
            decoder = adsb.find_decoder()
            print("adsb  %d RTL-SDR device(s); decoder %s" %
                  (len(dongles), decoder or "rtl_adsb/readsb not found"))
            print("ble   %s" % ble.probe())
        if cardputer:
            cap = lora.Cap()
            cap.start()
            cap.wait(10)
            print("lora  Cap LoRa-1262: %s" % ("found" if cap.found else lora.WORDS.get(cap.state, cap.state)))
        elif headless:
            hat = lora.WaveshareHat()
            hat.start()
            print("lora  Waveshare SX1262 HAT: %s" % ("SPI device present" if hat.found else "not found"))
        return 0

    if args._background_worker:
        from . import headless

        lock_fd = background.worker_root(DEFAULT_ROOT)
        if lock_fd is None:
            return 2
        stopped = threading.Event()
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

        def stop_worker(*_):
            stopped.set()

        for sig in signals:
            signal.signal(sig, stop_worker)
        try:
            return headless.run(stopped, on_started=background.worker_ready, **status_options(args))
        finally:
            background.finish_worker(DEFAULT_ROOT, lock_fd)

    if args.headless_wardrive or args.dashboard:
        from . import headless

        lock_fd = background.acquire_foreground(DEFAULT_ROOT)
        if lock_fd is None:
            return 1
        stopped = threading.Event()
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        previous = {sig: signal.getsignal(sig) for sig in signals}

        def stop(*_):
            stopped.set()

        try:
            for sig in signals:
                signal.signal(sig, stop)
            return headless.run(stopped, **status_options(args))
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            background.release_foreground(DEFAULT_ROOT, lock_fd)

    if headless:
        return background.start(DEFAULT_ROOT, **({"extra": status_flag(args)} if status_flag(args) else {}))

    from . import faults, updates_check, wigle
    from .gnss import GPS
    from .power import Rails
    from .ui.app import App, HardwareWatch
    from .ui.display import FramebufferDisplay
    from .ui.keys import EvdevInput

    faults.path = DEFAULT_ROOT / "errors.log"
    cfg = config.load()
    if not cardputer and cfg.get("gps_source") == "/dev/ttyS0":
        cfg["gps_source"] = "auto"  # A setting carried over from a Cardputer's Cap / Grove connector.
    with ExitStack() as cleanup:
        try:
            if cardputer:
                display = FramebufferDisplay()
                cleanup.callback(display.close)
                keyboard = EvdevInput()
            else:
                # lgpio opens a notification FIFO relative to its working directory at import.
                # Keep the pipe private, then return to the caller's directory before opening logs.
                gpio_dir = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="quartermaster-gpio-"))
                with ExitStack() as startup:
                    startup.callback(os.chdir, os.getcwd())
                    os.chdir(gpio_dir)
                    if "LG_WD" in os.environ:
                        startup.callback(os.environ.__setitem__, "LG_WD", os.environ.pop("LG_WD"))
                    from .ui.hat import HatDisplay, HatInput
                    display = HatDisplay(args.display)
                    cleanup.callback(display.close)
                    keyboard = HatInput(buttons=False) if args.display == "st7789-320x240" else HatInput()
            cleanup.callback(keyboard.close)
        except (OSError, ImportError) as exc:
            print("Cannot open %s display or controls: %s" % (args.display, exc), file=sys.stderr)
            return 2

        rails = Rails() if cardputer else Rails(())
        cleanup.callback(rails.restore_all)
        gps = GPS(cfg.get("gps_source", "auto"), candidates=gps_ports, rails=rails, power=cfg.get("gps_power", True))
        stopped = threading.Event()
        worker = threading.Thread(target=gps.run, args=(stopped,), daemon=True, name="gps")
        worker.start()
        cleanup.callback(worker.join, 3)
        cleanup.callback(stopped.set)
        app = App(display, keyboard, gps, cfg, root=DEFAULT_ROOT, splash=not args.no_splash,
                  hw=HardwareWatch(gps_ports=gps_ports), cap=lora.Cap(rails, cardputer=cardputer),
                  updates=updates_check.check if cardputer else updates_check.check_pi)
        wigle.prune_empty(app.logs_dir)
        if args.autostart:
            app.start_wardrive(ask=False)

        def stop(*_):
            app.running = False
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, stop)
        app.run()
    return 0


def configure_uploads():
    from getpass import getpass

    from . import accounts

    values = {}
    labels = {field: label for field, label, _ in accounts.FIELDS}
    print("Leave a key blank to keep its current value.")
    for field, _, _ in accounts.FIELDS:
        prompt = labels[field] + ": "
        value = input(prompt) if field == "wigle_name" else getpass(prompt)
        if value.strip():
            values[field] = value
    saved = accounts.update(values)
    ready = accounts.ready(saved)
    print("Upload keys saved securely. %d upload service(s) configured." % len(ready))
    return 0


def status_flag(args):
    """The flag pair that selects a status screen for the background worker, or ()."""
    if args.epaper:
        return ("--epaper", args.epaper)
    return ("--oled", args.oled) if args.oled else ()


def status_options(args):
    """Keyword arguments for headless.run; empty without a status screen."""
    try:
        if args.epaper:
            from .ui.epaper import EpaperDisplay, EpaperStatus

            return {"status_sink": EpaperStatus(EpaperDisplay())}
        if args.oled:
            from .ui.oled import OledDisplay, OledStatus

            return {"status_sink": OledStatus(OledDisplay())}
    except (OSError, ImportError) as exc:
        # Collecting matters more than the screen, so carry on without it.
        print("Cannot open the status display: %s" % exc, file=sys.stderr)
    return {}


def show_config(config_module):
    cfg = config_module.load(config_module.PATH)
    if cfg.get("cluster_key"):
        cfg["cluster_key"] = "[configured]"
    print(json.dumps(cfg, indent=2, sort_keys=True))
    return 0


def set_config(config_module, assignments):
    cfg = config_module.load()
    settings = {key: (choices or []) for key, _, choices, _ in config_module.SETTINGS}
    updates = {}
    for key, raw in assignments:
        choices = settings.get(key)
        if not choices:
            print("Unknown or non-scalar setting: %s" % key, file=sys.stderr)
            return 2
        match = next(((value, label) for value, label in choices
                      if raw.casefold() in (str(value).casefold(), label.casefold())), None)
        if match is None:
            allowed = ", ".join("%s (%s)" % (value, label) for value, label in choices)
            print("Invalid value for %s. Choose: %s" % (key, allowed), file=sys.stderr)
            return 2
        updates[key] = match[0]
    cfg.update(updates)
    if "adsb" in updates:
        cfg["adsb_set"] = True
    if "country" in updates:
        cfg["country_set"] = True
    config_module.save(cfg, config_module.PATH)
    print("Updated %s: %s" % (config_module.PATH, ", ".join(sorted(updates))))
    return 0


def upload_status(root):
    from . import config, uploads

    status = uploads.Uploader(root, config.load()).status()
    print("Upload queue: %d finished file(s), %d pending upload(s); automatic %s." %
          (status["files"], status["waiting"], "on" if status["auto"] else "off"))
    for service, detail in status["services"].items():
        if detail["configured"] or detail["waiting"] or detail["sent"] or detail["refused"]:
            print("%s: %d pending, %d sent, %d refused%s" %
                  (detail["name"], detail["waiting"], detail["sent"], detail["refused"],
                   " (key configured)" if detail["configured"] else ""))
        else:
            print("%s: no key configured" % detail["name"])
    return 0


def upload_finished(root):
    from . import accounts, config, uploads

    cfg = config.load()
    saved = accounts.load()
    if not accounts.ready(saved):
        print("No upload keys are configured. Run `quartermaster --configure-uploads` first.")
        return 2
    uploader = uploads.Uploader(root, cfg)
    retry = uploader.run_pass()
    status = uploader.status()
    print("Checked %d finished log file(s); %d upload(s) remain pending." %
          (status["files"], status["waiting"]))
    for detail in status["services"].values():
        if detail["last"]:
            print("%s: %s" % (detail["name"], detail["last"]))
    return 1 if retry else 0


def export_finished(root, destination):
    from . import export_logs

    try:
        copied = export_logs.export(root, destination)
    except (OSError, ValueError) as exc:
        print("Could not export logs: %s" % (getattr(exc, "strerror", None) or exc))
        return 1
    print("Exported %d finished log file(s) to %s." % (len(copied), destination))
    if not copied:
        print("No finished log files were found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
