"""
The universal contract between the translator (bridge_daemon.py, which speaks
to the UniFi controller via north_adapter) and any south adapter (which
speaks to a specific vendor's real switch hardware).

This is the actual fix for something that was wrong before: bridge_daemon.py
used to unpack D-Link-specific tuple shapes directly (e.g. iterating
`get_port_vlan_info()`'s raw `(port, vlan_mode, ingress, accept_frame, port_no)`
tuples). That meant the "translator" was only nominally vendor-agnostic --
swapping in a different switch's south adapter would have required editing
bridge_daemon.py itself, not just writing a new adapter class.

Now: bridge_daemon.py talks ONLY to the `SwitchAdapter` interface below, using
ONLY the dataclasses below as data shapes. A south adapter's job is to
translate its vendor's native API into these -- see
`south_adapter/dlink_adapter.py` for the reference implementation, which wraps
the (unchanged, still fully usable on its own) low-level `DLinkWebUI` driver.
Adding a new switch vendor never requires touching bridge_daemon.py -- only
writing a new class here's contract. See TODO.md "Adding a new switch
vendor/model".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SwitchInfo:
    model: str
    firmware: str
    mac: str
    serial: str | None = None


@dataclass
class VlanInfo:
    vid: int
    name: str


@dataclass
class PortInfo:
    port_idx: int
    name: str
    enabled: bool  # administratively enabled/disabled
    up: bool  # link actually up right now
    speed_mbps: int | None  # negotiated speed; None if down/unknown
    full_duplex: bool | None
    untagged_vlan: int | None  # native/PVID, if any
    tagged_vlans: list[int] = field(default_factory=list)
    # Cumulative counters (not instantaneous rates) -- matches how UniFi's own
    # device agents report traffic (confirmed against unpoller/unifi's Go
    # structs as a schema reference): the controller's own UI computes
    # throughput graphs from the delta between successive informs, so sending
    # a point-in-time rate here would be the wrong shape even if we had one.
    # None where a south adapter can't read them (not every backend exposes
    # this, e.g. D-Link's web UI currently only exposes %/pps utilization,
    # not cumulative bytes -- see TODO.md).
    rx_bytes: int | None = None
    tx_bytes: int | None = None


class SwitchAdapter(ABC):
    """Every south adapter must implement this. Use as a context manager
    (`with adapter:`) or call login()/logout() directly -- both are supported,
    matching how DLinkWebUI already worked before this contract existed."""

    @abstractmethod
    def login(self) -> None: ...

    @abstractmethod
    def logout(self) -> None: ...

    @abstractmethod
    def get_switch_info(self) -> SwitchInfo: ...

    @abstractmethod
    def list_vlans(self) -> list[VlanInfo]: ...

    @abstractmethod
    def ensure_vlans(self, vids: list[int]) -> None:
        """Create any of these VLANs that don't already exist. A no-op for
        ones that already do -- callers don't need to check first."""
        ...

    @abstractmethod
    def list_ports(self) -> list[PortInfo]: ...

    @abstractmethod
    def set_port_enabled(self, port_idx: int, enabled: bool) -> None: ...

    @abstractmethod
    def set_port_vlans(self, port_idx: int, untagged_vlan: int | None, tagged_vlans: list[int]) -> None: ...

    @abstractmethod
    def save_config(self) -> None:
        """Persist any changes made above so they survive a switch reboot."""
        ...

    def __enter__(self) -> "SwitchAdapter":
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        self.logout()
