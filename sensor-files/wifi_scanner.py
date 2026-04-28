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
from datetime import datetime

import requests
from gps import gps, WATCH_ENABLE
from scapy.all import sniff, RadioTap, Dot11, Dot11Elt

DEFAULT_API_URL = os.environ.get("WAYFINDRS_API_URL", "https://wayfindrs.com")
DEFAULT_OUTPUT_DIR = os.environ.get("WAYFINDRS_OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "scans"))

GPS_FIX_TIMEOUT = 120

CHANNELS_24GHZ = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
CHANNELS_5GHZ = [
    36, 40, 44, 48, 52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128,
    132, 136, 140, 149, 153, 157, 161, 165,
]
ALL_CHANNELS = CHANNELS_24GHZ + CHANNELS_5GHZ
CHANNEL_HOP_INTERVAL = 0.15

_gpsd = None
_stop_event = threading.Event()


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


def get_token(api_url, email, password):
    resp = requests.post(
        f"{api_url}/api/mobile/login",
        json={"email": email, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def freq_to_channel(freq_mhz):
    if not freq_mhz:
        return None
    try:
        freq = int(freq_mhz)
    except Exception:
        return None
    if 2412 <= freq <= 2484:
        return 14 if freq == 2484 else 1 + (freq - 2412) // 5
    if 5000 <= freq <= 6000:
        return (freq - 5000) // 5
    return None


def extract_ssid(pkt):
    if not pkt.haslayer(Dot11Elt):
        return None
    elt = pkt.getlayer(Dot11Elt)
    while elt is not None:
        try:
            if elt.ID == 0:
                return elt.info.decode("utf-8", errors="ignore")
        except Exception:
            pass
        elt = elt.payload.getlayer(Dot11Elt)
    return None


def get_radiotap_freq_and_rssi(pkt):
    freq = rssi = None
    if pkt.haslayer(RadioTap):
        rt = pkt.getlayer(RadioTap)
        try:
            rssi = rt.dBm_AntSignal
        except Exception:
            pass
        try:
            ch = getattr(rt, "Channel", None)
            if ch:
                freq = int(ch[0]) if isinstance(ch, (tuple, list)) else int(ch)
        except Exception:
            pass
        if freq is None:
            for name in ("channel", "ChannelFrequency", "channel_freq", "freq"):
                try:
                    val = getattr(rt, name, None)
                    if val:
                        freq = int(val[0]) if isinstance(val, (tuple, list)) else int(val)
                        break
                except Exception:
                    continue
    return freq, rssi


def pack_values(values):
    result = b""
    for v in values:
        bval = str(v).encode()
        result += len(bval).to_bytes(4, "little") + bval
    return result.hex()


def save_entry_local(entry, output_dir):
    try:
        os.makedirs(output_dir, exist_ok=True)
        date_str = datetime.now().strftime("%d-%m-%Y-%H-%M")
        outfile = os.path.join(output_dir, f"wifi_{date_str}.json")
        with open(outfile, "a") as f:
            json.dump(entry, f)
            f.write("\n")
        print(f"Saved locally: {entry.get('mac_address')} @ {entry['timestamp']}")
    except Exception as e:
        print(f"Error writing local file: {e}")


def save_batch_local(items, output_dir):
    try:
        os.makedirs(output_dir, exist_ok=True)
        date_str = datetime.now().strftime("%d-%m-%Y-%H-%M")
        outfile = os.path.join(output_dir, f"wifi_{date_str}.json")
        with open(outfile, "a") as f:
            for item in items:
                json.dump(item, f)
                f.write("\n")
        print(f"Saved {len(items)} records locally: {outfile}")
    except Exception as e:
        print(f"Error writing local file: {e}")


BATCH_SIZE = 50
_batch_lock = threading.Lock()
_batch: list = []


def flush_batch(api_url, token, output_dir):
    with _batch_lock:
        if not _batch:
            return
        items = _batch.copy()
        _batch.clear()
    try:
        resp = requests.post(
            f"{api_url}/api/mobile/upload/bulk",
            json=items,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code == 401:
            print("Token expired — stopping. Re-authenticate and restart.")
            save_batch_local(items, output_dir)
            _stop_event.set()
            return
        if resp.ok:
            r = resp.json()
            print(f"Bulk upload: {r.get('accepted')} accepted, {r.get('rejected')} rejected")
        else:
            print(f"Upload failed {resp.status_code} — saving {len(items)} records locally.")
            save_batch_local(items, output_dir)
    except (requests.ConnectionError, requests.Timeout) as e:
        print(f"Network error — saving {len(items)} records locally: {e}")
        save_batch_local(items, output_dir)
    except Exception as e:
        print(f"Upload error — saving {len(items)} records locally: {e}")
        save_batch_local(items, output_dir)


_gps_skip_count = 0
_gps_skip_lock = threading.Lock()


def handle_packet(pkt, local_mode=False, api_url=None, token=None, output_dir=None):
    global _gps_skip_count
    if not pkt.haslayer(Dot11):
        return
    dot11 = pkt.getlayer(Dot11)
    if getattr(dot11, "type", None) != 0 or getattr(dot11, "subtype", None) != 4:
        return
    ssid = extract_ssid(pkt)
    if not ssid:
        return

    src = getattr(dot11, "addr2", None)
    dst = getattr(dot11, "addr1", None)
    bssid = getattr(dot11, "addr3", None)
    rp = pack_values([dst, bssid, ssid, 0, 4, "Management", "Probe Request"])
    freq_mhz, rssi = get_radiotap_freq_and_rssi(pkt)
    channel = freq_to_channel(freq_mhz)

    coords = get_gps_coords()
    if coords is None:
        with _gps_skip_lock:
            _gps_skip_count += 1
            count = _gps_skip_count
        if count % 10 == 1:
            print(f"GPS fix lost — skipping packets ({count} skipped so far).")
        return
    else:
        with _gps_skip_lock:
            if _gps_skip_count > 0:
                print(f"GPS fix restored (had skipped {_gps_skip_count} packets).")
                _gps_skip_count = 0

    lat, lon = coords
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "protocol": "WiFi",
        "channel": int(channel) if channel is not None else None,
        "mac_address": src,
        "address_type": "random" if src and src[1] in ["a", "e", "2", "6"] else "public",
        "latitude": lat,
        "longitude": lon,
        "rssi": int(rssi) if rssi is not None else None,
        "raw_payload": rp,
    }

    if local_mode:
        save_entry_local(record, output_dir)
    else:
        with _batch_lock:
            _batch.append(record)
            should_flush = len(_batch) >= BATCH_SIZE
        if should_flush:
            flush_batch(api_url, token, output_dir)


def set_iface_managed(iface, managed):
    """Tell NetworkManager (if present) to manage or unmanage the interface."""
    try:
        state = "yes" if managed else "no"
        result = subprocess.run(
            ["nmcli", "device", "set", iface, "managed", state],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print(f"[+] {iface} {'returned to' if managed else 'removed from'} NetworkManager.")
    except FileNotFoundError:
        pass  # nmcli not available on this system
    except Exception:
        pass


def ensure_monitor_mode(iface):
    set_iface_managed(iface, False)
    try:
        output = subprocess.check_output(["iwconfig", iface], stderr=subprocess.STDOUT).decode()
        if "Mode:Monitor" in output:
            print(f"[+] {iface} is already in monitor mode.")
            return iface
        print(f"[!] Setting {iface} to monitor mode...")
        subprocess.check_call(["ip", "link", "set", iface, "down"])
        subprocess.check_call(["iw", iface, "set", "monitor", "none"])
        subprocess.check_call(["ip", "link", "set", iface, "up"])
        print(f"[+] {iface} is now in monitor mode.")
        return iface
    except subprocess.CalledProcessError as e:
        print(f"Failed to set monitor mode for {iface}: {e}")
        sys.exit(1)


def channel_hopper(iface, interval=CHANNEL_HOP_INTERVAL):
    while True:
        for ch in ALL_CHANNELS:
            try:
                subprocess.check_call(["iw", iface, "set", "channel", str(ch)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                pass
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="WiFi probe request sniffer — uploads to WayFindrs API.")
    parser.add_argument("-i", "--iface", required=True, help="Wireless interface (e.g. wlan0)")
    parser.add_argument("-c", "--count", type=int, default=0,
                        help="Packets to capture (0 = infinite)")
    parser.add_argument("-l", "--local", action="store_true",
                        help="Save results to JSON instead of uploading")
    parser.add_argument("--api-url", default=DEFAULT_API_URL,
                        help="WayFindrs API base URL")
    parser.add_argument("--token", default=os.environ.get("WAYFINDRS_TOKEN"),
                        help="Bearer token (or set WAYFINDRS_TOKEN env var)")
    parser.add_argument("--email", help="Email address to log in and obtain a token")
    parser.add_argument("--password", help="Password to log in and obtain a token")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help="Directory for local JSON saves (default: data/scans/)")
    parser.add_argument("--gps-device", default=os.environ.get("WAYFINDRS_GPS_DEVICE", ""),
                        help="GPS serial device (e.g. /dev/ttyUSB0 or /dev/ttyACM0)")
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

    if not setup_gps(args.gps_device):
        sys.exit(1)

    GpsPoller().start()

    if not wait_for_gps_fix():
        sys.exit(1)

    iface = ensure_monitor_mode(args.iface)

    local_mode = args.local

    def shutdown(sig, frame):
        print("\nShutting down...")
        set_iface_managed(iface, True)
        if not local_mode:
            flush_batch(args.api_url, token, args.output_dir)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=channel_hopper, args=(iface,), daemon=True).start()
    print(f"[+] Listening on {iface} with channel hopping — probe requests only.")

    try:
        sniff(
            iface=iface,
            prn=lambda pkt: handle_packet(pkt, local_mode=local_mode,
                                          api_url=args.api_url, token=token,
                                          output_dir=args.output_dir),
            store=False,
            count=args.count,
            stop_filter=lambda _: _stop_event.is_set(),
        )
    finally:
        if _stop_event.is_set():
            print("Stopped due to token expiry.")
        set_iface_managed(iface, True)
        if not local_mode:
            flush_batch(args.api_url, token, args.output_dir)


if __name__ == "__main__":
    main()
