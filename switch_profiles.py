"""
Registry of supported switches: for each one, which real-but-older UniFi switch
model to impersonate (so the controller's legacy L2 discovery/inform code accepts
it -- see README "Why an older UniFi model" for how this was found), and enough
defaults that a factory-reset switch of that type can be adopted with just its
type selected, no further manual lookup required.

Add a new SwitchProfile here to support another switch model. Nothing else in
the bridge needs to change as long as the new switch has its own south-adapter
class implementing the `SwitchAdapter` contract (see switch_adapter.py at the
repo root, and south_adapter/dlink_adapter.py for the reference
implementation).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SwitchProfile:
    key: str  # value for the SWITCH_MODEL env var
    display_name: str
    south_adapter: str  # dotted path "module:ClassName" under south_adapter/,
    # implementing switch_adapter.SwitchAdapter
    default_ip: str  # factory-default management IP for a freshly reset switch
    default_username: str
    default_password: str

    # -- the UniFi identity we present to the controller --
    fake_sysid: int
    fake_model: str  # e.g. "USXG24" -- must be one of the "classic" (pre-Enterprise)
    # switch platforms; newer models (the "USWEDxx" Enterprise-XG family and later)
    # are rejected by the legacy L2 discovery listener with "unknown model", found
    # empirically by testing several real platform codes against a live controller
    # (see README "Why an older UniFi model").
    fake_version: str  # a real, currently-shipping firmware version for fake_model
    fake_port_count: int


PROFILES: dict[str, SwitchProfile] = {
    "dlink-dgs1250-28x": SwitchProfile(
        key="dlink-dgs1250-28x",
        display_name="D-Link DGS-1250-28X",
        south_adapter="dlink_adapter:DLinkSwitchAdapter",
        default_ip="10.90.90.90",
        default_username="admin",
        default_password="admin",
        fake_sysid=60201,
        fake_model="USXG24",
        fake_version="7.5.15",
        fake_port_count=28,
    ),
}


def get_profile(key: str) -> SwitchProfile:
    try:
        return PROFILES[key]
    except KeyError:
        available = ", ".join(sorted(PROFILES))
        raise SystemExit(f"Unknown SWITCH_MODEL '{key}'. Available: {available}")
