#!/usr/bin/env python3
import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import binascii
from datetime import datetime

import requests
from bluepy import btle
from gps import gps, WATCH_ENABLE

DEFAULT_API_URL = os.environ.get("WAYFINDRS_API_URL", "https://wayfindrs.com")
DEFAULT_OUTPUT_DIR = os.environ.get("WAYFINDRS_OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "scans"))

GPS_FIX_TIMEOUT = 120

_gpsd = None


class TokenExpiredError(Exception):
    pass


def get_token(api_url, email, password):
    resp = requests.post(
        f"{api_url}/api/mobile/login",
        json={"email": email, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def start_session(api_url, token):
    resp = requests.post(
        f"{api_url}/api/mobile/session/start",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    print("Session started.")


def end_session(api_url, token):
    try:
        requests.post(
            f"{api_url}/api/mobile/session/end",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        print("Session ended.")
    except Exception as e:
        print(f"Warning: could not end session: {e}")


def get_gps_coords():
    try:
        if _gpsd and _gpsd.fix and _gpsd.fix.latitude and _gpsd.fix.longitude:
            lat, lon = _gpsd.fix.latitude, _gpsd.fix.longitude
            if lat != 0.0 and lon != 0.0 and not math.isnan(lat) and not math.isnan(lon):
                return lat, lon
    except Exception:
        pass
    return None


def _gps_status_str():
    try:
        if _gpsd is None:
            return "gpsd not connected"
        fix = _gpsd.fix
        mode = getattr(fix, "mode", 0)
        mode_str = {0: "unknown", 1: "no fix", 2: "2D fix", 3: "3D fix"}.get(mode, str(mode))
        sats = getattr(_gpsd, "satellites", []) or []
        used = sum(1 for s in sats if getattr(s, "used", False))
        ss_list = [getattr(s, "ss", 0) or 0 for s in sats if getattr(s, "used", False)]
        best_ss = max(ss_list) if ss_list else 0
        return f"mode={mode_str}, {len(sats)} sats visible, {used} used, best signal={best_ss:.0f}dBHz"
    except Exception:
        return "status unavailable"


def wait_for_gps_fix():
    print(f"Waiting for GPS fix (timeout: {GPS_FIX_TIMEOUT}s)...")
    for elapsed in range(GPS_FIX_TIMEOUT):
        if get_gps_coords() is not None:
            lat, lon = get_gps_coords()
            print(f"GPS fix acquired: {lat:.6f}, {lon:.6f}")
            return True
        if elapsed > 0 and elapsed % 15 == 0:
            print(f"  still waiting… ({elapsed}s) — {_gps_status_str()}")
        time.sleep(1)
    print(f"ERROR: No GPS fix within {GPS_FIX_TIMEOUT}s — aborting.")
    return False


def save_batch_local(records, output_dir):
    try:
        os.makedirs(output_dir, exist_ok=True)
        date_str = datetime.now().strftime("%d-%m-%Y-%H-%M")
        outfile = os.path.join(output_dir, f"ble_{date_str}.json")
        with open(outfile, "a") as f:
            for el in records:
                json.dump(el, f)
                f.write("\n")
        print(f"Saved {len(records)} records locally: {outfile}")
    except Exception as e:
        print(f"Error writing local file: {e}")


def scan_and_upload(api_url, token, duration=10, local_mode=False, iface=0, output_dir=None):
    scanner = btle.Scanner(iface=iface)
    devices = scanner.scan(duration)

    upload_data = []
    gps_skipped = 0
    for dev in devices:
        try:
            raw_payload = binascii.hexlify(dev.rawData).decode()
        except Exception as e:
            print(f"Payload encode error: {e}")
            continue

        coords = get_gps_coords()
        if coords is None:
            gps_skipped += 1
            continue
        lat, lon = coords
        upload_data.append({
            "timestamp": datetime.utcnow().isoformat(),
            "protocol": "BLE",
            "mac_address": dev.addr,
            "address_type": dev.addrType,
            "rssi": dev.rssi,
            "raw_payload": raw_payload,
            "latitude": lat,
            "longitude": lon,
        })

    if gps_skipped:
        print(f"GPS fix lost — skipped {gps_skipped} device(s).")

    if not upload_data:
        print("No devices found.")
        return

    if local_mode:
        save_batch_local(upload_data, output_dir)
    else:
        try:
            resp = requests.post(
                f"{api_url}/api/mobile/upload/bulk",
                json=upload_data,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 401:
                raise TokenExpiredError("Token expired — re-authenticate and restart.")
            if resp.ok:
                r = resp.json()
                print(f"Bulk upload: {r.get('accepted')} accepted, {r.get('rejected')} rejected")
            else:
                print(f"Upload failed {resp.status_code} — saving {len(upload_data)} records locally.")
                save_batch_local(upload_data, output_dir)
        except TokenExpiredError:
            raise
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"Network error — saving {len(upload_data)} records locally: {e}")
            save_batch_local(upload_data, output_dir)
        except Exception as e:
            print(f"Upload error — saving {len(upload_data)} records locally: {e}")
            save_batch_local(upload_data, output_dir)


class GpsPoller(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        global _gpsd
        for attempt in range(20):
            try:
                _gpsd = gps(mode=WATCH_ENABLE)
                break
            except Exception:
                time.sleep(0.5)
        if _gpsd is None:
            print("ERROR: Could not connect to gpsd after retries.")
            return
        while True:
            try:
                _gpsd.next()
            except Exception:
                break


def ensure_hci_up(iface_idx):
    """Bring the HCI device up if it is currently down."""
    iface = f"hci{iface_idx}"
    try:
        result = subprocess.run(["hciconfig", iface, "up"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print(f"{iface} is up.")
        else:
            print(f"Warning: could not bring {iface} up: {result.stderr.strip()}")
    except Exception as e:
        print(f"Warning: hciconfig {iface} up failed: {e}")


def _kill_stale_gpsd():
    import glob
    for comm_file in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm_file) as f:
                if f.read().strip() == "gpsd":
                    pid = int(comm_file.split("/")[2])
                    os.kill(pid, signal.SIGTERM)
                    print(f"Killed stale gpsd (PID {pid}).")
        except Exception:
            pass
    time.sleep(0.5)


def setup_gps(device):
    import socket as _socket
    try:
        s = _socket.create_connection(("localhost", 2947), timeout=2)
        s.close()
        print("GPSD already running — reusing.")
        return True
    except Exception:
        pass

    _kill_stale_gpsd()

    if not os.path.exists(device):
        print(f"ERROR: GPS device {device} not found.")
        return False
    print(f"Setting up GPSD on {device}...")
    result = subprocess.run(
        ["gpsd", "-n", device, "-F", "/var/run/gpsd.sock"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("GPSD setup successful.")
        return True
    print(f"ERROR: GPSD failed to start: {result.stderr.strip()}")
    return False


def main():
    parser = argparse.ArgumentParser(description="BLE scanner — uploads to WayFindrs API.")
    parser.add_argument("-l", "--local", action="store_true",
                        help="Save results to JSON instead of uploading")
    parser.add_argument("-d", "--duration", type=int, default=10,
                        help="Scan duration in seconds (default: 10)")
    parser.add_argument("--iface", default=os.environ.get("WAYFINDRS_BLE_IFACE", "hci0"),
                        help="Bluetooth interface to use (default: hci0)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help="Directory for local JSON saves (default: data/scans/)")
    parser.add_argument("--gps-device", default=os.environ.get("WAYFINDRS_GPS_DEVICE", ""),
                        help="GPS serial device (e.g. /dev/ttyUSB0 or /dev/ttyACM0)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL,
                        help="WayFindrs API base URL")
    parser.add_argument("--token", default=os.environ.get("WAYFINDRS_TOKEN"),
                        help="Bearer token (or set WAYFINDRS_TOKEN env var)")
    parser.add_argument("--email", help="Email address to log in and obtain a token")
    parser.add_argument("--password", help="Password to log in and obtain a token")
    parser.add_argument("--skip-session", action="store_true",
                        help="Skip session start/end (session managed externally, e.g. by the manager)")
    args = parser.parse_args()

    token = args.token
    if not token and args.email and args.password:
        print(f"Logging in as {args.email}...")
        token = get_token(args.api_url, args.email, args.password)
        print("Token obtained.")

    if not args.local and not token:
        parser.error(
            "Authentication required. Provide --token, set WAYFINDRS_TOKEN, "
            "or supply --email and --password."
        )

    if not args.gps_device:
        parser.error("--gps-device is required. GPS coordinates are mandatory for all scans.")

    iface_idx = int(args.iface.lstrip("hci")) if args.iface.startswith("hci") else int(args.iface)
    ensure_hci_up(iface_idx)

    if not setup_gps(args.gps_device):
        sys.exit(1)

    GpsPoller().start()

    if not wait_for_gps_fix():
        sys.exit(1)

    local_mode = args.local
    session_started = False

    if not local_mode and not args.skip_session:
        try:
            start_session(args.api_url, token)
            session_started = True
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"Cannot reach API ({e}) — switching to local mode for this session.")
            print(f"Scans will be saved to {args.output_dir} and can be uploaded later via the manager.")
            local_mode = True
        except Exception as e:
            print(f"Session start failed ({e}) — switching to local mode for this session.")
            local_mode = True

    def shutdown(sig, frame):
        print("\nShutting down...")
        if session_started and not local_mode:
            end_session(args.api_url, token)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            scan_and_upload(args.api_url, token, duration=args.duration, local_mode=local_mode, iface=iface_idx, output_dir=args.output_dir)
            time.sleep(1)
    except TokenExpiredError as e:
        print(f"\n{e}")
        if session_started:
            end_session(args.api_url, token)
        sys.exit(1)
    finally:
        if session_started and not local_mode:
            end_session(args.api_url, token)


if __name__ == "__main__":
    main()
