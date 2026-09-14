"""
Wraps the low-level `DLinkWebUI` driver (dlink_webui.py, unchanged, still fully
usable on its own for scripting/debugging) to implement the universal
`SwitchAdapter` contract (see switch_adapter.py at the repo root) -- this is
the class bridge_daemon.py actually talks to.

Known gap, carried over honestly rather than papered over: DLinkWebUI has no
confirmed way yet to READ a port's current tagged/untagged VLAN membership
back (see TODO.md "Bridge / south_adapter") -- `get_port_vlan_info()` only
confirms a port's VLAN *mode* (Hybrid/Access/Trunk), not which VLANs it
carries. So here: `list_ports()` reports `tagged_vlans=[]` /
`untagged_vlan=None` rather than guessing at an unconfirmed data format, and
`set_port_vlans()` is additive-only (it can't safely remove memberships it
can't first read back).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dlink_webui import DLinkWebUI  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from switch_adapter import SwitchAdapter, SwitchInfo, VlanInfo, PortInfo  # noqa: E402


class DLinkSwitchAdapter(SwitchAdapter):
    def __init__(self, host: str, username: str = "admin", password: str = "admin"):
        self._d = DLinkWebUI(host, username=username, password=password)

    def login(self) -> None:
        self._d.login()

    def logout(self) -> None:
        self._d.logout()

    def get_switch_info(self) -> SwitchInfo:
        s = self._d.get_switch_status()
        return SwitchInfo(
            model=s.get("model", "unknown"),
            firmware=s.get("firmware", "unknown"),
            mac=s["mac"].replace("-", ":").lower(),
            serial=s.get("serial"),
        )

    def list_vlans(self) -> list[VlanInfo]:
        return [VlanInfo(vid=int(v.vid), name=v.name) for v in self._d.list_vlans()]

    def ensure_vlans(self, vids: list[int]) -> None:
        existing = {int(v.vid) for v in self._d.list_vlans()}
        missing = [str(v) for v in vids if v not in existing]
        if missing:
            self._d.add_vlans(",".join(missing))

    def list_ports(self) -> list[PortInfo]:
        link_status = {p["port"]: p for p in self._d.get_port_status()}
        ports = []
        for port_name, vlan_mode, _ingress, _accept_frame, port_no in self._d.get_port_vlan_info():
            link = link_status.get(port_name, {})
            up = link.get("status") == "Connected"
            speed_raw = link.get("speed")
            ports.append(PortInfo(
                port_idx=int(port_no),
                name=port_name,
                enabled=True,  # no confirmed way yet to read admin enable/disable state -- see module docstring
                up=up,
                speed_mbps=_parse_speed_mbps(speed_raw) if up else None,
                full_duplex=(link.get("duplex") == "Full") if up else None,
                untagged_vlan=None,  # see module docstring -- not reliably readable yet
                tagged_vlans=[],
            ))
        ports.sort(key=lambda p: p.port_idx)
        return ports

    def set_port_enabled(self, port_idx: int, enabled: bool) -> None:
        self._d.set_port_state(port_idx, enabled=enabled)

    def set_port_vlans(self, port_idx: int, untagged_vlan: int | None, tagged_vlans: list[int]) -> None:
        """Additive only -- see module docstring for why a full reconciliation
        (removing memberships not in the requested set) isn't safe yet."""
        if untagged_vlan is not None:
            self._d.set_port_vlan_membership(port_idx, str(untagged_vlan), tagged=False)
        for vid in tagged_vlans:
            self._d.set_port_vlan_membership(port_idx, str(vid), tagged=True)

    def save_config(self) -> None:
        self._d.save_config()


def _parse_speed_mbps(speed_str: str | None) -> int | None:
    """DLinkWebUI's port-status speed field is a human string like '1000M' or
    '10G' -- normalize to a plain Mbps int for the universal PortInfo shape."""
    if not speed_str:
        return None
    s = speed_str.strip().upper()
    try:
        if s.endswith("G"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("M"):
            return int(float(s[:-1]))
        return int(s)
    except ValueError:
        return None
