#!/usr/bin/env python3
import glob
import json
import os
import subprocess
import threading

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", os.path.join(os.path.dirname(__file__), "..", "sensor-files"))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(os.path.dirname(__file__), "..", "data", "config.json"))

DEFAULT_OUTPUT_DIR = os.environ.get("WAYFINDRS_OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "scans"))

DEFAULT_CONFIG = {
    "api_url": "https://wayfindrs.com",
    "token": None,
    "ble_duration": 10,
    "ble_iface": "hci0",
    "wifi_iface": "wlan0",
    "wifi_count": 0,
    "output_dir": None,
    "gps_device": None,
}

_processes = {
    "ble":  {"proc": None, "output": [], "lock": threading.Lock()},
    "wifi": {"proc": None, "output": [], "lock": threading.Lock()},
}

def resolve_output_dir(cfg):
    return cfg.get("output_dir") or DEFAULT_OUTPUT_DIR


def _usb_product_name(sysfs_dev_path):
    """Walk up the sysfs device path to find the USB product name string."""
    try:
        path = os.path.realpath(sysfs_dev_path)
        while path and path != '/':
            p = os.path.join(path, 'product')
            if os.path.exists(p):
                with open(p) as f:
                    return f.read().strip()
            path = os.path.dirname(path)
    except Exception:
        pass
    return None


def _has_default_route(iface):
    """Return True if the interface has a default route (i.e. internet-facing)."""
    try:
        r = subprocess.run(
            ['ip', 'route', 'show', 'default', 'dev', iface],
            capture_output=True, text=True, timeout=2,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _stream(slot, proc):
    for line in iter(proc.stdout.readline, ""):
        with slot["lock"]:
            slot["output"].append(line.rstrip())
            if len(slot["output"]) > 150:
                slot["output"].pop(0)
    with slot["lock"]:
        if slot["proc"] is proc:
            slot["proc"] = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()
    return jsonify({k: v for k, v in cfg.items() if k != "token"} | {"has_token": bool(cfg.get("token"))})


@app.route("/api/config", methods=["POST"])
def update_config():
    cfg = load_config()
    for key in ("api_url", "ble_iface", "ble_duration", "wifi_iface", "wifi_count", "output_dir", "gps_device"):
        if key in request.json:
            cfg[key] = request.json[key]
    save_config(cfg)
    return jsonify({"status": "ok"})


@app.route("/api/token", methods=["POST"])
def set_token():
    data = request.json or {}
    cfg = load_config()
    api_url = data.get("api_url") or cfg["api_url"]

    if data.get("token"):
        cfg["token"] = data["token"]
        cfg["api_url"] = api_url
        save_config(cfg)
        return jsonify({"status": "ok", "message": "Token saved."})

    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"status": "error", "message": "Provide a token or email + password."}), 400

    try:
        resp = requests.post(
            f"{api_url}/api/mobile/login",
            json={"email": email, "password": password},
            timeout=10,
        )
        resp.raise_for_status()
        cfg["token"] = resp.json()["access_token"]
        cfg["api_url"] = api_url
        save_config(cfg)
        return jsonify({"status": "ok", "message": "Token obtained and saved."})
    except requests.RequestException as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        return jsonify({"status": "error", "message": msg}), 400


@app.route("/api/token", methods=["DELETE"])
def clear_token():
    cfg = load_config()
    cfg["token"] = None
    save_config(cfg)
    return jsonify({"status": "ok", "message": "Token cleared."})


@app.route("/api/ble/interfaces", methods=["GET"])
def ble_interfaces():
    try:
        names = sorted(
            i for i in os.listdir("/sys/class/bluetooth") if i.startswith("hci")
        )
    except Exception:
        names = []

    interfaces = []
    for iface in names:
        info = {"name": iface}
        product = _usb_product_name(f"/sys/class/bluetooth/{iface}/device")
        if product:
            info["device_name"] = product
        try:
            dev_path = os.path.realpath(f"/sys/class/bluetooth/{iface}/device")
            info["bus"] = "USB" if "/usb" in dev_path else "built-in"
        except Exception:
            pass
        interfaces.append(info)
    return jsonify({"interfaces": interfaces})


@app.route("/api/wifi/interfaces", methods=["GET"])
def wifi_interfaces():
    try:
        names = sorted(
            i for i in os.listdir("/sys/class/net")
            if os.path.exists(f"/sys/class/net/{i}/wireless")
        )
    except Exception:
        names = []

    interfaces = []
    for iface in names:
        info = {"name": iface}
        product = _usb_product_name(f"/sys/class/net/{iface}/device")
        if product:
            info["device_name"] = product
        info["has_internet"] = _has_default_route(iface)
        interfaces.append(info)
    return jsonify({"interfaces": interfaces})


@app.route("/api/gps/devices", methods=["GET"])
def gps_devices():
    candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return jsonify({"devices": candidates, "detected": len(candidates) > 0})


@app.route("/api/connectivity", methods=["GET"])
def check_connectivity():
    cfg = load_config()
    api_url = cfg.get("api_url", "https://wayfindrs.com")
    try:
        resp = requests.head(api_url, timeout=5)
        return jsonify({"reachable": True, "status_code": resp.status_code})
    except (requests.ConnectionError, requests.Timeout):
        return jsonify({"reachable": False, "reason": "Cannot reach API — scans will fall back to local storage automatically."})
    except Exception as e:
        return jsonify({"reachable": False, "reason": str(e)})


@app.route("/api/me", methods=["GET"])
def get_me():
    cfg = load_config()
    token = cfg.get("token")
    if not token:
        return jsonify({"status": "error", "message": "No token configured."}), 401
    try:
        resp = requests.get(
            f"{cfg['api_url']}/api/mobile/user/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        return jsonify({"status": "error", "message": msg}), 502


def _build_scanner_cmd(scanner, cfg, local_mode=False):
    output_dir = resolve_output_dir(cfg)
    if scanner == "ble":
        cmd = [
            "python3", os.path.join(SCRIPTS_DIR, "ble_scanner.py"),
            "--duration", str(cfg.get("ble_duration", 10)),
            "--iface", cfg.get("ble_iface", "hci0"),
            "--output-dir", output_dir,
            "--gps-device", cfg["gps_device"],
        ]
    else:
        cmd = [
            "python3", os.path.join(SCRIPTS_DIR, "wifi_scanner.py"),
            "--iface", cfg.get("wifi_iface", "wlan0"),
            "--count", str(cfg.get("wifi_count", 0)),
            "--output-dir", output_dir,
            "--gps-device", cfg["gps_device"],
        ]
    if local_mode:
        cmd.append("--local")
    return cmd


def _launch_scanner(scanner, cfg):
    """Start a single scanner subprocess. Returns (ok, message)."""
    slot = _processes[scanner]
    with slot["lock"]:
        if slot["proc"] and slot["proc"].poll() is None:
            return False, f"{scanner.upper()} scanner is already running."

    token = cfg.get("token")
    api_url = cfg["api_url"]
    env = {**os.environ, "WAYFINDRS_TOKEN": token or "", "WAYFINDRS_API_URL": api_url, "PYTHONUNBUFFERED": "1"}
    cmd = _build_scanner_cmd(scanner, cfg, local_mode=False)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        with slot["lock"]:
            slot["proc"] = proc
            slot["output"] = [f"[manager] {scanner.upper()} scanner started (PID {proc.pid})"]
        threading.Thread(target=_stream, args=(slot, proc), daemon=True).start()
        return True, f"{scanner.upper()} scanner started."
    except Exception as e:
        return False, str(e)


@app.route("/api/scanner/<scanner>/start", methods=["POST"])
def start_scanner(scanner):
    if scanner not in _processes:
        return jsonify({"status": "error", "message": "Unknown scanner."}), 404

    cfg = load_config()
    if not cfg.get("token"):
        return jsonify({"status": "error", "message": "No token configured. Authenticate first."}), 400
    if not cfg.get("gps_device"):
        return jsonify({"status": "error", "message": "GPS device is required. Configure it in the Config tab."}), 400

    ok, message = _launch_scanner(scanner, cfg)
    return jsonify({"status": "ok" if ok else "error", "message": message}), (200 if ok else 400)


@app.route("/api/scanner/both/start", methods=["POST"])
def start_both_scanners():
    cfg = load_config()
    if not cfg.get("token"):
        return jsonify({"status": "error", "message": "No token configured. Authenticate first."}), 400
    if not cfg.get("gps_device"):
        return jsonify({"status": "error", "message": "GPS device is required. Configure it in the Config tab."}), 400

    results = {}
    for scanner in ("ble", "wifi"):
        ok, message = _launch_scanner(scanner, cfg)
        results[scanner] = {"ok": ok, "message": message}
    return jsonify({"status": "ok", "results": results})


@app.route("/api/scanner/<scanner>/stop", methods=["POST"])
def stop_scanner(scanner):
    if scanner not in _processes:
        return jsonify({"status": "error", "message": "Unknown scanner."}), 404

    slot = _processes[scanner]
    with slot["lock"]:
        proc = slot["proc"]
        if not proc or proc.poll() is not None:
            return jsonify({"status": "error", "message": f"{scanner.upper()} scanner is not running."})
        proc.terminate()
        slot["output"].append(f"[manager] {scanner.upper()} scanner stopping...")

    return jsonify({"status": "ok", "message": f"{scanner.upper()} scanner stopping."})


@app.route("/api/scanner/both/stop", methods=["POST"])
def stop_both_scanners():
    results = {}
    for scanner in ("ble", "wifi"):
        slot = _processes[scanner]
        with slot["lock"]:
            proc = slot["proc"]
            if proc and proc.poll() is None:
                proc.terminate()
                slot["output"].append(f"[manager] {scanner.upper()} scanner stopping...")
                results[scanner] = "stopping"
            else:
                results[scanner] = "was not running"
    return jsonify({"status": "ok", "results": results})


@app.route("/api/scanner/<scanner>/status", methods=["GET"])
def scanner_status(scanner):
    if scanner not in _processes:
        return jsonify({"status": "error", "message": "Unknown scanner."}), 404

    slot = _processes[scanner]
    with slot["lock"]:
        running = slot["proc"] is not None and slot["proc"].poll() is None
        output = list(slot["output"][-30:])

    return jsonify({"running": running, "output": output})


@app.route("/api/scans", methods=["GET"])
def list_scans():
    cfg = load_config()
    output_dir = resolve_output_dir(cfg)
    try:
        files = sorted(f for f in os.listdir(output_dir) if f.endswith(".json"))
    except FileNotFoundError:
        files = []
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    result = []
    for fname in files:
        fpath = os.path.join(output_dir, fname)
        try:
            size_kb = round(os.path.getsize(fpath) / 1024, 1)
            with open(fpath) as f:
                records = sum(1 for line in f if line.strip())
        except Exception:
            size_kb = 0
            records = 0
        result.append({"filename": fname, "records": records, "size_kb": size_kb})

    return jsonify({"files": result})


@app.route("/api/scans/<filename>/upload", methods=["POST"])
def upload_scan(filename):
    if filename != os.path.basename(filename) or ".." in filename:
        return jsonify({"status": "error", "message": "Invalid filename."}), 400

    cfg = load_config()
    token = cfg.get("token")
    if not token:
        return jsonify({"status": "error", "message": "No token configured."}), 401

    output_dir = resolve_output_dir(cfg)
    fpath = os.path.join(output_dir, filename)
    if not os.path.isfile(fpath):
        return jsonify({"status": "error", "message": "File not found."}), 404

    try:
        with open(fpath) as f:
            records = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to read file: {e}"}), 500

    api_url = cfg["api_url"]
    CHUNK = 50
    accepted = rejected = skipped = batches = 0

    for i in range(0, len(records), CHUNK):
        batch = records[i:i + CHUNK]
        try:
            resp = requests.post(
                f"{api_url}/api/mobile/upload/bulk",
                json=batch,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.ok:
                r = resp.json()
                accepted += r.get("accepted", 0)
                rejected += r.get("rejected", 0)
            else:
                skipped += len(batch)
        except Exception:
            skipped += len(batch)
        batches += 1

    return jsonify({
        "status": "ok",
        "total": len(records),
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "batches": batches,
    })


@app.route("/api/scans/<filename>", methods=["DELETE"])
def delete_scan(filename):
    if filename != os.path.basename(filename) or ".." in filename:
        return jsonify({"status": "error", "message": "Invalid filename."}), 400

    cfg = load_config()
    output_dir = resolve_output_dir(cfg)
    fpath = os.path.join(output_dir, filename)
    if not os.path.isfile(fpath):
        return jsonify({"status": "error", "message": "File not found."}), 404

    try:
        os.remove(fpath)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    # Persist any keys missing from an older config file
    save_config(load_config())
    app.run(host="0.0.0.0", port=8080, debug=False)
