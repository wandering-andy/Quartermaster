"""Headless wardrive runner for Linux hosts."""

import threading
from functools import partial
from pathlib import Path
import sys
import time

from . import config, faults, hardware, lora, uploads
from .engine import DEFAULT_ROOT, Wardrive
from .gnss import GPS


def _duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def dashboard(status, hat_state):
    gps = status.get("gps") or {}
    if gps.get("valid"):
        fix = "FIX %.6f, %.6f" % (gps["latitude"], gps["longitude"])
        fix += " | %s | age %.1fs" % (gps.get("source", "GPS"), gps.get("age_seconds") or 0)
        if gps.get("hdop") is not None:
            fix += " | HDOP %.1f" % gps["hdop"]
    else:
        fix = "NO FIX | WiGLE rows wait for a fresh GPS fix"
        if gps.get("status") == "stale":
            fix = "STALE FIX | WiGLE rows wait for a fresh GPS fix"

    lines = [
        "Quartermaster | Raspberry Pi Zero 2 W | HEADLESS WARDRIVE",
        "Runtime %s | State %s" % (_duration(status.get("elapsed")), str(status.get("state", "unknown")).upper()),
        "",
        "CAPTURED",
        "  Wi-Fi         %6d" % status.get("wifi", 0),
        "  Bluetooth     %6d" % status.get("ble", 0),
        "  Aircraft      %6s" % (status.get("aircraft") if status.get("aircraft") is not None else "off"),
        "  Mesh nodes    %6s" % (status.get("mesh") if status.get("mesh") is not None else "waiting"),
        "  Log rows      %6d" % status.get("rows", 0),
        "  Sightings     %6d" % status.get("sightings", 0),
        "  Skipped (GPS) %6d" % status.get("skipped_no_fix", 0),
        "",
        "GPS",
        "  " + fix,
        "",
        "RADIOS",
    ]
    adapters = status.get("adapters") or []
    if adapters:
        for adapter in adapters:
            label = adapter.get("label") or adapter.get("interface", "Wi-Fi")
            detail = "%s | %s sweeps" % (adapter.get("interface", "radio"), adapter.get("sweeps", 0))
            if adapter.get("method"):
                detail += " | " + str(adapter["method"])
            if adapter.get("error"):
                detail += " | " + str(adapter["error"])
            lines.append("  %s: %s" % (label, detail))
    else:
        lines.append("  Wi-Fi: no adapter status reported")
    if status.get("ble_error"):
        lines.append("  Bluetooth: " + str(status["ble_error"]))

    mesh_status = status.get("lora")
    if mesh_status:
        lines.append("  LoRa: %s | %s packets | %s" % (
            str(mesh_status.get("state", "unknown")).upper(), mesh_status.get("packets", 0),
            mesh_status.get("detail") or mesh_status.get("reason") or ""))
    else:
        lines.append("  LoRa HAT: %s" % hat_state)

    if status.get("failed"):
        lines.extend(("", "ERROR", "  " + str(status["failed"])))
    elif status.get("error"):
        lines.extend(("", "NOTICE", "  " + str(status["error"])))
    upload = status.get("upload")
    if upload is not None:
        lines.extend(("", "UPLOADS"))
        lines.append("  %d keys | %d pending | automatic %s" % (
            upload.get("keys", 0), upload.get("waiting", 0), "on" if upload.get("auto") else "off"))
        current = upload.get("current")
        if current:
            lines.append("  Sending %s to %s" % (current[0], current[1]))
    paths = [status.get(key) for key in ("log", "mesh_log", "aircraft_log") if status.get(key)]
    if paths:
        lines.extend(("", "OUTPUT", *["  " + str(path) for path in paths]))
    lines.extend(("", "Press Ctrl-C to stop and finalize logs."))
    return "\n".join(lines)


def run(stop_event, root=DEFAULT_ROOT, cfg=None, gps_factory=GPS, drive_factory=Wardrive,
    hat_factory=lora.WaveshareHat, uploader_factory=uploads.Uploader, output=print, stdout=None, terminal=None,
    refresh=1.0, plain_interval=15.0, on_started=None, status_sink=None):
    root = Path(root)
    cfg = config.load() if cfg is None else dict(cfg)
    faults.path = root / "errors.log"
    ports = partial(hardware.gps_candidates, cardputer=False, pi_uart=True)
    gps = gps_factory(cfg.get("gps_source", "auto"), candidates=ports, rails=None, power=False)
    hat = hat_factory()
    hat.start(cfg.get("lora", "auto") is not False)
    gps_stopped = threading.Event()
    gps_thread = threading.Thread(target=gps.run, args=(gps_stopped,), daemon=True, name="gps")
    drive = drive_factory(cfg, gps, root=root, nodes=None, cap=hat, display_profile="headless")
    uploader = None
    stdout = stdout or sys.stdout
    terminal = stdout.isatty() if terminal is None else terminal
    last_plain = 0.0
    try:
        gps_thread.start()
        try:
            drive.start()
        except (OSError, RuntimeError) as exc:
            stop_event.set()
            reason = str(exc).strip() or type(exc).__name__
            output("Cannot start wardrive: %s" % reason)
            return 1
        uploader = uploader_factory(root, cfg, active=drive.active_log)
        uploader.start()
        if on_started is not None:
            on_started()
        while not stop_event.is_set():
            status = drive.status()
            status["upload"] = uploader.status()
            if status_sink is not None:
                try:
                    status_sink.update(status, hat.state)
                except Exception as exc:
                    # A failing screen must never stop the wardrive.
                    output("Status display stopped: %s" % exc)
                    status_sink.close()
                    status_sink = None
            if terminal:
                stdout.write("\x1b[H\x1b[2J" + dashboard(status, hat.state) + "\n")
                stdout.flush()
            elif time.monotonic() - last_plain >= plain_interval:
                output(dashboard(status, hat.state))
                last_plain = time.monotonic()
            stop_event.wait(refresh)
    finally:
        stop_event.set()
        if status_sink is not None:
            status_sink.close()
        try:
            drive.stop()
        finally:
            try:
                if uploader is not None:
                    try:
                        if cfg.get("auto_upload", False):
                            uploader.run_pass()
                    finally:
                        uploader.stop()
            finally:
                gps_stopped.set()
                gps_thread.join(timeout=3)

    output("Wardrive stopped: %d Wi-Fi/Bluetooth rows logged." % drive.rows)
    paths = list(drive.log_paths)
    paths.extend(path for path in (drive.aircraft_path, drive.mesh_path) if path is not None)
    for path in paths:
        output("Saved %s" % path)
    if drive.failed:
        output(drive.failed)
    return 0