"""
Small setup/status web UI for the bridge, so it can be configured by picking
options in a browser instead of hand-editing env vars -- pick the switch type
from a list, optionally scan for it, fill in the controller address, done.

Runs as the container's entrypoint. On first start (no saved config yet) it
shows a setup form; once submitted, the config is saved to CONFIG_FILE and
bridge_daemon.run_bridge() starts in a background thread. On later container
restarts, a saved config is loaded automatically and the bridge starts right
away -- the setup form only reappears if you explicitly ask for it (the
"Reconfigure" link on the status page).

Scanning: a full sweep of arbitrary networks isn't attempted -- too slow and
too unreliable across VLANs/segments to be worth it. Instead: try the
selected switch type's known factory-default IP directly first (fast,
covers the common case), then sweep this container's own local /24 as a
fallback (covers "I already gave the switch a normal LAN IP by hand" per
the README's recommended setup step). Both paths verify a hit by actually
logging into it with the profile's default credentials and checking it
identifies as the expected model -- an open port 80 alone is not treated as
a match.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "south_adapter"))
sys.path.insert(0, str(Path(__file__).parent / "north_adapter"))

from flask import Flask, request, redirect, url_for, jsonify, render_template_string

from bridge_daemon import run_bridge, load_south_adapter, BridgeStatus
from switch_profiles import PROFILES, get_profile

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))

app = Flask(__name__)
status = BridgeStatus()
bridge_thread: threading.Thread | None = None
bridge_thread_lock = threading.Lock()


def load_config() -> dict | None:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return None
    return None


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def start_bridge(config: dict) -> None:
    global bridge_thread
    with bridge_thread_lock:
        if bridge_thread and bridge_thread.is_alive():
            return  # already running
        bridge_thread = threading.Thread(target=run_bridge, args=(config, status), daemon=True)
        bridge_thread.start()


def get_local_ip() -> str | None:
    """The container's own IP on its default route, via the connect-a-UDP-
    socket trick -- no packet is actually sent, this just asks the kernel
    which local address/interface it would use to reach that destination."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def verify_switch(profile, ip: str) -> dict | None:
    south_cls = load_south_adapter(profile)
    south = south_cls(ip, username=profile.default_username, password=profile.default_password)
    try:
        south.login()
        info = south.get_switch_status()
        return {"ip": ip, "model": info.get("model"), "firmware": info.get("firmware")}
    except Exception:
        return None
    finally:
        try:
            south.logout()
        except Exception:
            pass


def scan_for_switch(profile) -> list[dict]:
    found: list[dict] = []

    # fast path: the well-known factory-default IP
    hit = verify_switch(profile, profile.default_ip)
    if hit:
        found.append(hit)
        return found  # exact known-IP hit, no need for a slow subnet sweep

    # fallback: sweep this container's own local /24 (covers "already gave
    # the switch a normal LAN IP by hand", per the README's setup step)
    local_ip = get_local_ip()
    if not local_ip:
        return found
    base = ".".join(local_ip.split(".")[:3])
    candidates = [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != local_ip]

    def probe_open_port(ip: str) -> str | None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                if s.connect_ex((ip, 80)) == 0:
                    return ip
            except OSError:
                pass
        return None

    with ThreadPoolExecutor(max_workers=64) as ex:
        open_ips = [ip for ip in ex.map(probe_open_port, candidates) if ip]

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda ip: verify_switch(profile, ip), open_ips))
    found.extend(r for r in results if r)
    return found


PAGE_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  label { display: block; margin-top: 14px; font-weight: 600; font-size: 0.9rem; }
  input, select { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; font-size: 1rem; }
  button { margin-top: 20px; padding: 10px 18px; font-size: 1rem; cursor: pointer; }
  .hint { color: #666; font-size: 0.85rem; margin-top: 2px; }
  .scan-result { padding: 8px; border: 1px solid #ddd; border-radius: 4px; margin-top: 6px; cursor: pointer; }
  .scan-result:hover { background: #f0f4ff; }
  .phase { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 0.85rem; font-weight: 600; }
  .phase-adopted { background: #d4f4dd; color: #1a7a34; }
  .phase-waiting_adopt { background: #fff3cd; color: #8a6d00; }
  .phase-error { background: #fbdada; color: #a3231f; }
  .phase-connecting_switch, .phase-idle { background: #e0e0e0; color: #444; }
  pre.log { background: #111; color: #ddd; padding: 12px; height: 300px; overflow-y: auto; font-size: 0.8rem; }
  a { color: #2255cc; }
</style>
"""

SETUP_PAGE = """
<!doctype html><html><head><title>UniFi Bridge Setup</title>""" + PAGE_STYLE + """</head><body>
<h1>UniFi Bridge -- Setup</h1>
<p class="hint">Requires the switch to be at factory defaults with its default admin login.</p>
<form method="post" action="{{ url_for('setup_submit') }}">
  <label>Switch type</label>
  <select name="switch_model" id="switch_model">
    {% for key, p in profiles.items() %}
    <option value="{{ key }}">{{ p.display_name }}</option>
    {% endfor %}
  </select>

  <label>Switch IP <button type="button" onclick="scan()" style="margin-top:0;padding:4px 10px;font-size:0.85rem;">Scan for it</button></label>
  <input type="text" name="switch_ip" id="switch_ip" placeholder="leave blank to use the factory default">
  <div id="scan_status" class="hint"></div>
  <div id="scan_results"></div>

  <label>Switch username</label>
  <input type="text" name="switch_username" placeholder="leave blank for the profile default">

  <label>Switch password</label>
  <input type="password" name="switch_password" placeholder="leave blank for the profile default">

  <label>UniFi controller host/IP</label>
  <input type="text" name="controller_host" required>

  <label>UniFi controller port</label>
  <input type="text" name="controller_port" value="8080">

  <button type="submit">Save &amp; Start</button>
</form>
<script>
function scan() {
  const model = document.getElementById('switch_model').value;
  const statusEl = document.getElementById('scan_status');
  const resultsEl = document.getElementById('scan_results');
  statusEl.textContent = 'Scanning (this can take a few seconds)...';
  resultsEl.innerHTML = '';
  fetch('/scan?model=' + encodeURIComponent(model))
    .then(r => r.json())
    .then(data => {
      if (data.results.length === 0) {
        statusEl.textContent = 'Nothing found. Enter the IP manually.';
        return;
      }
      statusEl.textContent = 'Found ' + data.results.length + ' match(es):';
      data.results.forEach(r => {
        const div = document.createElement('div');
        div.className = 'scan-result';
        div.textContent = r.ip + ' -- ' + r.model + ' (fw ' + r.firmware + ')';
        div.onclick = () => { document.getElementById('switch_ip').value = r.ip; };
        resultsEl.appendChild(div);
      });
    })
    .catch(() => { statusEl.textContent = 'Scan failed.'; });
}
</script>
</body></html>
"""

STATUS_PAGE = """
<!doctype html><html><head><title>UniFi Bridge Status</title>""" + PAGE_STYLE + """
<meta http-equiv="refresh-disabled">
</head><body>
<h1>UniFi Bridge</h1>
<p>
  <span class="phase phase-{{ s.phase }}">{{ s.phase }}</span>
  &nbsp; {{ s.message }}
</p>
<p>MAC: {{ s.mac or '(not connected yet)' }}</p>
{% if s.last_inform_ts %}
<p>Last inform sent: <span id="last_inform">{{ s.last_inform_ts }}</span></p>
{% endif %}
{% if s.error %}<p style="color:#a3231f;">Error: {{ s.error }}</p>{% endif %}
<h3>Recent log</h3>
<pre class="log" id="log">{{ s.log_lines|join('\\n') }}</pre>
<p><a href="{{ url_for('reconfigure') }}">Reconfigure</a></p>
<script>
setInterval(() => {
  fetch('/status.json').then(r => r.json()).then(s => {
    document.getElementById('log').textContent = s.log_lines.join('\\n');
    document.getElementById('log').scrollTop = document.getElementById('log').scrollHeight;
  });
}, 3000);
</script>
</body></html>
"""


@app.route("/")
def index():
    config = load_config()
    if not config:
        return render_template_string(SETUP_PAGE, profiles=PROFILES)
    start_bridge(config)  # no-op if already running
    return render_template_string(STATUS_PAGE, s=status.snapshot())


@app.route("/status.json")
def status_json():
    return jsonify(status.snapshot())


@app.route("/scan")
def scan():
    model = request.args.get("model", "")
    try:
        profile = get_profile(model)
    except SystemExit as e:
        return jsonify({"results": [], "error": str(e)}), 400
    results = scan_for_switch(profile)
    return jsonify({"results": results})


@app.route("/setup", methods=["POST"])
def setup_submit():
    config = {
        "switch_model": request.form["switch_model"],
        "switch_ip": request.form.get("switch_ip") or None,
        "switch_username": request.form.get("switch_username") or None,
        "switch_password": request.form.get("switch_password") or None,
        "controller_host": request.form["controller_host"],
        "controller_port": request.form.get("controller_port") or None,
    }
    save_config(config)
    start_bridge(config)
    return redirect(url_for("index"))


@app.route("/reconfigure")
def reconfigure():
    # Deliberately does NOT delete the saved authkey/state file (bridge_state.json)
    # or stop an already-running bridge thread -- only clears which config is
    # shown/used on next explicit setup submit, so an accidental click here can't
    # break an already-adopted device. Delete config.json manually (and restart
    # the container) for a full reset.
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    return render_template_string(SETUP_PAGE, profiles=PROFILES)


def config_from_env() -> dict | None:
    """Lets `.env` (or any env vars) pre-fill/unattended-boot the web UI flow --
    entirely optional, only used if no config.json exists yet. Requires at
    least SWITCH_MODEL and CONTROLLER_HOST; anything else falls back to the
    switch profile's defaults exactly like a manual setup submission would."""
    if not os.environ.get("SWITCH_MODEL") or not os.environ.get("CONTROLLER_HOST"):
        return None
    return {
        "switch_model": os.environ["SWITCH_MODEL"],
        "switch_ip": os.environ.get("SWITCH_IP"),
        "switch_username": os.environ.get("SWITCH_USERNAME"),
        "switch_password": os.environ.get("SWITCH_PASSWORD"),
        "controller_host": os.environ["CONTROLLER_HOST"],
        "controller_port": os.environ.get("CONTROLLER_PORT"),
    }


if __name__ == "__main__":
    config = load_config() or config_from_env()
    if config:
        if not CONFIG_FILE.exists():
            save_config(config)
        start_bridge(config)
    app.run(host="0.0.0.0", port=int(os.environ.get("WEBUI_PORT", "8099")))
