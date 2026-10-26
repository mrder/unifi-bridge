"""
NetgearSwitchAdapter: implements the universal SwitchAdapter contract
(switch_adapter.py) for the NETGEAR JGS524Ev2, combining two sources:

  - Port/device status: NSDP, via the `python-netgear-switch-library`
    (Apache-2.0, github.com/mithro/python-netgear-switch-library). This
    model isn't in that library's own registry, but its NSDP reads are
    protocol-generic rather than per-model-coded, and were confirmed live
    against the real switch to work correctly (port link/speed/admin-enabled
    state all matched the switch's real, observed state). The closest
    registered model key ("gs305ep") is passed to satisfy the library's API
    even though port COUNT differs -- only used for backend selection, not
    for interpreting data.
  - VLAN lifecycle: our own netgear_http.py, reverse-engineered directly
    against this model since the library's registered HTTP login scheme
    does not match this firmware generation (confirmed live: it resets the
    connection). See that module's docstring for the full writeup.

Known, deliberate limitations (see TODO.md):
  - Per-port tagged VLAN assignment is NOT implemented. The 802.1Q config
    page covers VID create/delete only; where per-port tagging actually
    lives was not found during reconnaissance. `list_ports()` therefore
    always reports `tagged_vlans=[]`/`untagged_vlan=None`, and
    `set_port_vlans()` raises NotImplementedError rather than silently
    doing nothing.
  - `set_port_enabled()` (admin up/down) delegates to the library's NSDP
    write path, which is NOT independently live-tested against this exact
    switch/firmware (reads were verified live; a write test of an actual
    port toggle was judged not worth the risk beyond what was already
    explicitly approved for the VLAN test). Verify with a real port toggle
    once there's a maintenance window before relying on it in production.
  - `ensure_vlans()` refuses to run (raises NetgearAdapterError) if 802.1Q is
    currently disabled, rather than auto-enabling it. CONFIRMED LIVE:
    toggling 802.1Q in either direction deletes ALL existing VLAN
    configuration on this switch -- auto-enabling it as a side effect of a
    routine VLAN push would be a silent, surprising, destructive action.
    Enabling 802.1Q is left as a deliberate one-time manual step.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from netgear_http import NetgearHttp, NetgearHttpError  # noqa: E402
from switch_adapter import SwitchAdapter, SwitchInfo, VlanInfo, PortInfo  # noqa: E402

from netgear_switch.sync_api import SyncSwitch  # noqa: E402
from netgear_switch.registry import get_model  # noqa: E402


class NetgearAdapterError(Exception):
    pass


class NetgearSwitchAdapter(SwitchAdapter):
    # The closest registered model in python-netgear-switch-library -- used
    # only to pick the NSDP backend/port-count-agnostic read path, not
    # trusted for anything model-specific (the real model/port-count come
    # from the switch's own NSDP/HTTP responses, not this constant).
    _NSDP_MODEL_KEY = "gs305ep"

    def __init__(self, host: str, username: str = "admin", password: str = "admin"):
        self._host = host
        self._http = NetgearHttp(host, username=username, password=password)
        self._nsdp = SyncSwitch(get_model(self._NSDP_MODEL_KEY), host, nsdp_password=password)

    def login(self) -> None:
        self._http.login()

    def logout(self) -> None:
        self._http.logout()
        self._nsdp.close()

    def get_switch_info(self) -> SwitchInfo:
        dev = self._nsdp.nsdp_device()
        return SwitchInfo(
            model=dev.model or "JGS524Ev2",
            firmware=dev.firmware_version or "unknown",
            mac=dev.mac.replace("-", ":").lower() if dev.mac else "",
            serial=dev.serial_number,
        )

    def list_vlans(self) -> list[VlanInfo]:
        state = self._http.get_vlan_state()
        if not state.vlan_8021q_enabled:
            return [VlanInfo(vid=1, name="default")]
        return [VlanInfo(vid=vid, name=str(vid)) for vid in sorted(state.membership)]

    def ensure_vlans(self, vids: list[int]) -> None:
        state = self._http.get_vlan_state()
        if not state.vlan_8021q_enabled:
            raise NetgearAdapterError(
                "802.1Q VLAN is disabled on this switch. Enabling it deletes all "
                "existing VLAN config (confirmed switch behavior) -- enable it "
                "manually once via the web UI before pushing VLAN config here."
            )
        existing = set(state.membership)
        for vid in vids:
            if vid not in existing:
                self._http.add_vlan(vid)

    def list_ports(self) -> list[PortInfo]:
        # Confirmed live 2026-10-26: this switch's NSDP agent reports real
        # cumulative rx/tx byte counters even though it's an unregistered
        # model (per-packet counters come back empty -- bytes only).
        stats_by_port = {s.port: s for s in self._nsdp.get_stats()}
        ports = []
        for p in self._nsdp.get_ports():
            stats = stats_by_port.get(p.port)
            ports.append(PortInfo(
                port_idx=p.port,
                name=p.name or f"port{p.port}",
                enabled=p.admin_enabled,
                up=p.link_up,
                speed_mbps=p.speed_mbps,
                full_duplex=p.full_duplex,
                untagged_vlan=None,  # see module docstring -- not implemented
                tagged_vlans=[],
                rx_bytes=stats.rx_bytes if stats else None,
                tx_bytes=stats.tx_bytes if stats else None,
            ))
        return ports

    def set_port_enabled(self, port_idx: int, enabled: bool) -> None:
        """Delegates to the library's own NSDP write path. NOT independently
        live-tested against this switch (reads were verified live; a write
        test of a port toggle was judged not worth the risk on a switch in
        active use beyond what was already explicitly approved for the VLAN
        test) -- the library itself is well-tested in general, just not by us
        on this exact model/firmware combination. Verify once there's a
        maintenance window before relying on this in production."""
        self._nsdp.set_port_enabled(port_idx, enabled)

    def set_port_vlans(self, port_idx: int, untagged_vlan: int | None, tagged_vlans: list[int]) -> None:
        raise NotImplementedError(
            "per-port VLAN tagging is not yet implemented for this switch "
            "-- the config page that would do this was not located during "
            "reconnaissance; see netgear_adapter.py's module docstring"
        )

    def save_config(self) -> None:
        # No equivalent of DLinkWebUI's explicit "Save" step was found during
        # reconnaissance -- this switch's VLAN/802.1Q writes appeared to take
        # effect immediately (confirmed live: a fresh GET right after each
        # POST reflected the change) without a separate persist step. If a
        # reboot-persistence gap turns up later, implement it here.
        pass
