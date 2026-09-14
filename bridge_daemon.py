"""
Persistent bridge process: keeps a non-UniFi switch adopted and managed inside a
UniFi Network Application by speaking the controller's own discovery+inform
protocol on the switch's behalf, translating pushed config into real calls
against the switch's own management API (south_adapter), and reporting the
switch's real port/VLAN state back on every inform.

REQUIREMENTS (read before running):
  - The switch must be at FACTORY DEFAULTS with its default admin login. This
    bridge takes over the switch's identity from first contact -- if it has
    already been configured/renamed/repasseworded, either reset it or set
    SWITCH_USERNAME/SWITCH_PASSWORD/SWITCH_IP to match its current state.
  - The switch's management interface must be reachable from wherever this
    container runs (same L2 segment/VLAN, or routed) -- a factory-default
    switch is usually NOT on your main LAN (this D-Link's default is the
    10.90.90.0/8 range), so this container likely needs its own network
    attached to whatever the switch's management port is physically on.
  - The UniFi controller must be reachable too (typically the LAN the
    controller and your other UniFi devices already live on).
  - After this container starts and shows the switch as "Pending Adoption" in
    the controller UI, a human must click "Adopt" there -- this bridge cannot
    (and, for a first adoption, should not) do that step itself.

Why an older UniFi model: this switch is impersonated as a real, older UniFi
switch platform (see switch_profiles.py) rather than the newest/closest
capability match, because testing against a real controller showed the legacy
L2 discovery listener (UDP 10001) flatly rejects newer "Enterprise XG" switch
platforms with "unknown model" even though they're valid entries in the
controller's own device catalog -- only older platforms got past discovery at
all. This is a hard constraint discovered empirically, not a preference.

Env vars:
  SWITCH_MODEL        required, key into switch_profiles.PROFILES
  SWITCH_IP           optional, default: the profile's factory-default IP
  SWITCH_USERNAME     optional, default: the profile's default username
  SWITCH_PASSWORD     optional, default: the profile's default password
  CONTROLLER_HOST     required
  CONTROLLER_PORT     optional, default 8080
  BROADCAST_ADDR      optional, default 255.255.255.255
  INFORM_INTERVAL     optional, default 30 (seconds, once adopted)
  DISCOVERY_INTERVAL  optional, default 10 (seconds, while waiting to be adopted)
  STATE_FILE          optional, default /data/bridge_state.json (persists the
                       authkey issued at adoption so a container restart
                       doesn't require re-adopting the device in the UI)
"""

from __future__ import annotations

import collections
import importlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "south_adapter"))
sys.path.insert(0, str(Path(__file__).parent / "north_adapter"))

from inform_protocol import InformPacket, encode, decode, DEFAULT_KEY, InformProtocolError  # noqa: E402
from discovery_protocol import build_discovery_packet, send_discovery_broadcast, send_discovery_multicast  # noqa: E402
from switch_profiles import get_profile, SwitchProfile  # noqa: E402
from switch_adapter import SwitchAdapter  # noqa: E402

import requests  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")


class BridgeStatus:
    """Thread-safe, shared status object so webui.py can show live phase/log
    info for a run_bridge() happening in a background thread, without the core
    bridge logic needing to know a web UI exists."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "idle"  # idle -> connecting_switch -> waiting_adopt -> adopted -> error
        self.message = ""
        self.mac: str | None = None
        self.last_inform_ts: float | None = None
        self.error: str | None = None
        self.log_lines: collections.deque[str] = collections.deque(maxlen=200)

    def set(self, phase: str, message: str = "") -> None:
        with self._lock:
            self.phase = phase
            self.message = message

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "message": self.message,
                "mac": self.mac,
                "last_inform_ts": self.last_inform_ts,
                "error": self.error,
                "log_lines": list(self.log_lines),
            }


class _StatusLogHandler(logging.Handler):
    def __init__(self, status: BridgeStatus) -> None:
        super().__init__()
        self.status = status

    def emit(self, record: logging.LogRecord) -> None:
        self.status.log_lines.append(self.format(record))


def load_south_adapter(profile: SwitchProfile):
    module_name, class_name = profile.south_adapter.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log.warning("state file %s unreadable, starting fresh", path)
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def parse_mgmt_cfg(text: str) -> dict[str, str]:
    """mgmt_cfg is newline-separated key=value pairs, e.g. 'authkey=...\\ncfgversion=...'."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def build_report_payload(south: SwitchAdapter, profile: SwitchProfile, mac: str, ip: str, uptime: int,
                          state: int, cfgversion: str | None) -> dict:
    """Talks ONLY to the universal SwitchAdapter interface (switch_adapter.py) --
    never to a specific vendor's raw data shapes. That's the actual point of
    that interface: this function works unchanged for any switch model with a
    conforming south adapter, not just the D-Link this was written against."""
    payload = {
        "sysid": profile.fake_sysid,
        "model": profile.fake_model,
        "version": profile.fake_version,
        "mac": mac,
        "ip": ip,
        "uptime": uptime,
        "state": state,
    }
    if cfgversion:
        payload["cfgversion"] = cfgversion
    try:
        payload["vlan_table"] = [{"vid": v.vid, "name": v.name} for v in south.list_vlans()]
    except Exception as e:
        log.warning("could not read VLANs from switch: %s", e)
    try:
        port_table = [
            {
                "port_idx": p.port_idx,
                "name": p.name,
                "enabled": p.enabled,
                "up": p.up,
                "speed": p.speed_mbps,
                "full_duplex": p.full_duplex,
                "untagged_vlan": p.untagged_vlan,
                "tagged_vlans": p.tagged_vlans,
            }
            for p in south.list_ports()
        ]
        payload["port_table"] = port_table
    except Exception as e:
        log.warning("could not read port status from switch: %s", e)
    return payload


def apply_pushed_config(south: SwitchAdapter, cfg: dict) -> None:
    """Best-effort translation of controller-pushed config into real south-adapter
    calls, via the universal SwitchAdapter interface only. Currently covers: VLAN
    creation (vlansToDeploy) and per-port enable/disable when the controller
    includes it. PoE settings are logged and skipped -- this switch has no PoE
    hardware. Anything else unrecognized is logged, never silently dropped, so
    gaps are visible instead of hidden."""
    vlans_to_deploy = cfg.get("vlansToDeploy")
    if vlans_to_deploy:
        try:
            vids = [int(v) for v in vlans_to_deploy]
            log.info("ensuring VLANs pushed by controller exist: %s", vids)
            south.ensure_vlans(vids)
        except Exception as e:
            log.error("failed to apply pushed VLANs %s: %s", vlans_to_deploy, e)

    ports_to_deploy = cfg.get("portsToDeploy")
    if ports_to_deploy:
        for port_cfg in ports_to_deploy:
            handled = {"port_idx", "poe_mode"}
            if "poe_mode" in port_cfg:
                log.debug("port %s: ignoring poe_mode=%s (no PoE hardware on this switch)",
                          port_cfg.get("port_idx"), port_cfg["poe_mode"])
            if "enabled" in port_cfg and "port_idx" in port_cfg:
                handled.add("enabled")
                try:
                    south.set_port_enabled(int(port_cfg["port_idx"]), bool(port_cfg["enabled"]))
                except Exception as e:
                    log.error("failed to set port %s enabled=%s: %s",
                              port_cfg.get("port_idx"), port_cfg.get("enabled"), e)
            unhandled = {k: v for k, v in port_cfg.items() if k not in handled}
            if unhandled:
                log.warning("port %s: unhandled pushed fields (not applied): %s",
                            port_cfg.get("port_idx"), unhandled)


def run_pre_adoption_loop(south, profile: SwitchProfile, mac: str, switch_ip: str,
                           controller_host: str, controller_port: int, broadcast_addr: str,
                           discovery_interval: int) -> dict:
    """Sends discovery + default-key informs until the controller responds with a
    setparam/mgmt_cfg (i.e. until a human clicks "Adopt" in the controller UI).
    Returns the parsed mgmt_cfg dict once that happens."""
    packet = build_discovery_packet(
        mac=mac, ip=switch_ip, uptime_seconds=120,
        fw_build=f"{profile.fake_model}.rtl930x.v{profile.fake_version}.build",
        short_version=profile.fake_version, model_code=profile.fake_model,
    )
    inform_url = f"http://{controller_host}:{controller_port}/inform"

    while True:
        try:
            send_discovery_broadcast(packet, broadcast_addr=broadcast_addr)
            send_discovery_multicast(packet)
        except Exception as e:
            log.warning("discovery send failed (continuing): %s", e)

        payload = {
            "sysid": profile.fake_sysid,
            "model": profile.fake_model,
            "version": profile.fake_version,
            "mac": mac,
            "ip": switch_ip,
            "uptime": 120,
            "inform_url": inform_url,
            "state": 0,
        }
        wire = encode(InformPacket(mac=mac, payload=payload), key=DEFAULT_KEY)
        try:
            resp = requests.post(inform_url, data=wire,
                                  headers={"Content-Type": "application/x-binary"}, timeout=10)
        except Exception as e:
            log.warning("inform POST failed (will retry): %s", e)
            time.sleep(discovery_interval)
            continue

        if resp.status_code == 200 and resp.content:
            try:
                decoded = decode(resp.content, key=DEFAULT_KEY)
            except InformProtocolError as e:
                log.warning("got a response but couldn't decode it: %s", e)
                time.sleep(discovery_interval)
                continue
            if decoded.payload.get("_type") == "setparam" and "mgmt_cfg" in decoded.payload:
                log.info("adoption command received -- device has been adopted in the UI")
                return parse_mgmt_cfg(decoded.payload["mgmt_cfg"])
            log.info("unexpected response while waiting for adoption: %s", decoded.payload.get("_type"))
        else:
            log.info("not adopted yet (HTTP %s) -- waiting for 'Adopt' click in the UI, "
                      "device should show as Pending in Devices", resp.status_code)

        time.sleep(discovery_interval)


def run_adopted_loop(south, profile: SwitchProfile, mac: str, switch_ip: str,
                      controller_host: str, controller_port: int,
                      authkey: bytes, cfgversion: str | None,
                      inform_interval: int, state_path: Path, state: dict,
                      status: BridgeStatus | None = None) -> None:
    inform_url = f"http://{controller_host}:{controller_port}/inform"
    started = time.time()

    while True:
        uptime = int(time.time() - started)
        payload = build_report_payload(south, profile, mac, switch_ip, uptime, state=1, cfgversion=cfgversion)
        payload["inform_url"] = inform_url

        wire = encode(InformPacket(mac=mac, payload=payload), key=authkey, gcm=True)
        try:
            resp = requests.post(inform_url, data=wire,
                                  headers={"Content-Type": "application/x-binary"}, timeout=10)
            if status:
                status.last_inform_ts = time.time()
        except Exception as e:
            log.warning("inform POST failed (will retry next cycle): %s", e)
            time.sleep(inform_interval)
            continue

        if resp.status_code == 200 and resp.content:
            try:
                decoded = decode(resp.content, key=authkey)
            except InformProtocolError as e:
                log.warning("could not decode inform response: %s", e)
                time.sleep(inform_interval)
                continue

            rtype = decoded.payload.get("_type")
            if rtype == "setparam":
                new_cfgversion = decoded.payload.get("cfgversion")
                if new_cfgversion:
                    cfgversion = new_cfgversion
                    state["cfgversion"] = cfgversion
                    save_state(state_path, state)
                mgmt_cfg_raw = decoded.payload.get("mgmt_cfg")
                if mgmt_cfg_raw:
                    mgmt_cfg = parse_mgmt_cfg(mgmt_cfg_raw)
                    if mgmt_cfg.get("authkey") and bytes.fromhex(mgmt_cfg["authkey"]) != authkey:
                        log.info("controller issued a new authkey, switching to it")
                        authkey = bytes.fromhex(mgmt_cfg["authkey"])
                        state["authkey"] = mgmt_cfg["authkey"]
                        save_state(state_path, state)
                system_cfg = decoded.payload.get("system_cfg")
                if system_cfg:
                    try:
                        apply_pushed_config(south, json.loads(system_cfg) if isinstance(system_cfg, str) else system_cfg)
                    except Exception as e:
                        log.error("failed applying pushed config: %s", e)
                log.info("applied setparam (cfgversion=%s)", cfgversion)
            elif rtype == "cmd":
                cmd = decoded.payload.get("cmd")
                log.warning("controller sent cmd=%s -- NOT executing automatically "
                            "(reboot/upgrade/setdefault are destructive; wire this up "
                            "deliberately if you actually want it)", cmd)
            elif rtype:
                log.info("received response type=%s: %s", rtype, decoded.payload)
        elif resp.status_code == 404:
            log.warning("controller says Unknown Device again -- it may have been "
                        "forgotten/removed in the UI; will keep informing")

        time.sleep(inform_interval)


def run_bridge(config: dict, status: BridgeStatus | None = None) -> None:
    """Runs the full bridge forever (adoption wait, then the ongoing inform
    loop). Designed to be called either directly (see main() below, for
    plain env-var/no-web-UI operation) or from webui.py in a background
    thread, passing a BridgeStatus so the web UI can show live progress.

    `config` keys (all required except where noted):
      switch_model, switch_ip, switch_username, switch_password,
      controller_host, controller_port, broadcast_addr (default
      "255.255.255.255"), inform_interval (default 30),
      discovery_interval (default 10), state_file (default
      "/data/bridge_state.json")
    """
    if status:
        log.addHandler(_StatusLogHandler(status))

    profile = get_profile(config["switch_model"])
    switch_ip = config.get("switch_ip") or profile.default_ip
    username = config.get("switch_username") or profile.default_username
    password = config.get("switch_password") or profile.default_password
    controller_host = config["controller_host"]
    controller_port = int(config.get("controller_port") or 8080)
    broadcast_addr = config.get("broadcast_addr") or "255.255.255.255"
    inform_interval = int(config.get("inform_interval") or 30)
    discovery_interval = int(config.get("discovery_interval") or 10)
    state_path = Path(config.get("state_file") or "/data/bridge_state.json")

    log.info("=" * 70)
    log.info("UniFi bridge starting for %s", profile.display_name)
    log.info("REQUIRES: switch at factory defaults with its default admin login.")
    log.info("Switch: %s@%s   Controller: %s:%s", username, switch_ip, controller_host, controller_port)
    log.info("=" * 70)

    if status:
        status.set("connecting_switch", f"Logging into {switch_ip}...")

    south_cls = load_south_adapter(profile)
    south = south_cls(switch_ip, username=username, password=password)
    try:
        south.login()
    except Exception as e:
        if status:
            status.error = str(e)
            status.set("error", f"Could not log into switch at {switch_ip}: {e}")
        raise
    switch_info = south.get_switch_info()
    mac = switch_info.mac
    log.info("connected to real switch: %s fw=%s mac=%s",
              switch_info.model, switch_info.firmware, mac)
    if status:
        status.mac = mac

    state = load_state(state_path)
    authkey_hex = state.get("authkey")
    cfgversion = state.get("cfgversion")

    try:
        if not authkey_hex:
            log.info("no saved adoption -- waiting for 'Adopt' click in the controller UI")
            if status:
                status.set("waiting_adopt", "Device is broadcasting; click 'Adopt' in the controller UI")
            mgmt_cfg = run_pre_adoption_loop(
                south, profile, mac, switch_ip, controller_host, controller_port,
                broadcast_addr, discovery_interval,
            )
            authkey_hex = mgmt_cfg.get("authkey")
            if not authkey_hex:
                raise SystemExit(f"adoption response had no authkey: {mgmt_cfg}")
            cfgversion = mgmt_cfg.get("cfgversion")
            state["authkey"] = authkey_hex
            state["cfgversion"] = cfgversion
            state["mac"] = mac
            save_state(state_path, state)
            log.info("adopted successfully, authkey saved to %s", state_path)
        else:
            log.info("resuming with previously saved authkey from %s", state_path)

        if status:
            status.set("adopted", "Adopted -- reporting switch state to the controller")

        authkey = bytes.fromhex(authkey_hex)
        run_adopted_loop(
            south, profile, mac, switch_ip, controller_host, controller_port,
            authkey, cfgversion, inform_interval, state_path, state, status=status,
        )
    finally:
        try:
            south.logout()
        except Exception:
            pass


def main() -> None:
    """Plain env-var entrypoint, no web UI -- see README/.env.example."""
    config = {
        "switch_model": os.environ["SWITCH_MODEL"],
        "switch_ip": os.environ.get("SWITCH_IP"),
        "switch_username": os.environ.get("SWITCH_USERNAME"),
        "switch_password": os.environ.get("SWITCH_PASSWORD"),
        "controller_host": os.environ["CONTROLLER_HOST"],
        "controller_port": os.environ.get("CONTROLLER_PORT"),
        "broadcast_addr": os.environ.get("BROADCAST_ADDR"),
        "inform_interval": os.environ.get("INFORM_INTERVAL"),
        "discovery_interval": os.environ.get("DISCOVERY_INTERVAL"),
        "state_file": os.environ.get("STATE_FILE"),
    }
    run_bridge(config)


if __name__ == "__main__":
    main()
