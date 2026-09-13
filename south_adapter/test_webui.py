"""Full regression test for dlink_webui.py against a real switch.

Touches port eth1/0/2 for the enable/disable test (chosen because it's not the
port this test runs over -- don't repoint PORT_TO_TOGGLE at whatever port your
management PC is plugged into, or you'll cut your own connection). Creates and
cleans up throwaway VLANs 998/999. Safe to rerun, but only while nobody else has
the web UI open (single-session limit, see dlink_webui.py docstring).
"""

import sys
import time

from dlink_webui import DLinkWebUI

HOST = sys.argv[1] if len(sys.argv) > 1 else "10.90.90.90"
PORT_TO_TOGGLE = 2


def with_retry(fn, retries=5, delay=4.0):
    """The switch's single-session slot doesn't always free up instantly between
    script runs (see DLinkWebUI.login()'s own internal retry, which handles the
    common case) -- this wraps a whole test function for the rarer case where even
    that isn't enough."""
    last = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            print(f"  (retry {i}: {e})")
            time.sleep(delay)
    raise last


def run():
    with DLinkWebUI(HOST) as ui:
        print("Login OK")

        print("=== switch status ===")
        status = ui.get_switch_status()
        print(status)
        assert status["mac"], "no MAC returned"

        print("\n=== VLANs before ===")
        for v in ui.list_vlans():
            print(v)

        print("Creating test VLAN 998...")
        ui.add_vlans("998")
        vlans = ui.list_vlans()
        assert any(v.vid == "998" for v in vlans), "VLAN 998 was not created!"

        print("Renaming VLAN 998...")
        ui.rename_vlan(998, "test-bridge")
        vlans = ui.list_vlans()
        renamed = [v for v in vlans if v.vid == "998"]
        assert renamed and renamed[0].name == "test-bridge", "rename didn't take effect"

        print("Deleting test VLAN 998...")
        ui.delete_vlan(998)
        vlans = ui.list_vlans()
        assert not any(v.vid == "998" for v in vlans), "cleanup failed, VLAN 998 still present!"

        print("\nTesting per-port VLAN tagging (port 1 <-> VLAN 999)...")
        ui.add_vlans("999")
        ui.set_port_vlan_membership(port_no=1, vid_list="999", tagged=True)
        vlans = ui.list_vlans()
        v999 = [v for v in vlans if v.vid == "999"]
        assert v999 and v999[0].tagged_ports == "1/0/1", "port tagging didn't take effect"

        print("Removing tag + deleting VLAN 999...")
        ui.set_port_vlan_membership(port_no=1, vid_list="999", tagged=True, remove=True)
        ui.delete_vlan(999)
        vlans = ui.list_vlans()
        assert not any(v.vid == "999" for v in vlans), "cleanup failed, VLAN 999 still present!"

        print("\n=== port status (link/speed/duplex) ===")
        pstatus = ui.get_port_status()
        assert len(pstatus) == 28, f"expected 28 ports, got {len(pstatus)}"
        for p in pstatus[:3]:
            print(p)

        print("\n=== port utilization ===")
        putil = ui.get_port_utilization()
        assert len(putil) == 28
        print(putil[0])

        print(f"\nTesting port state control on eth1/0/{PORT_TO_TOGGLE} (disable/enable)...")
        ui.set_port_state(PORT_TO_TOGGLE, enabled=False)
        s = [p for p in ui.get_port_status() if p["port"] == f"eth1/0/{PORT_TO_TOGGLE}"][0]
        assert s["status"] == "Disabled", f"port didn't disable: {s}"
        ui.set_port_state(PORT_TO_TOGGLE, enabled=True)
        s = [p for p in ui.get_port_status() if p["port"] == f"eth1/0/{PORT_TO_TOGGLE}"][0]
        assert s["status"] != "Disabled", f"port didn't re-enable: {s}"
        print("port state control OK")

        print("\nEnabling LLDP + reading neighbors...")
        ui.set_lldp_enabled(True)
        neighbors = ui.get_lldp_neighbors()
        print(f"neighbors: {neighbors} (empty is expected/fine if nothing LLDP-capable is attached)")

        print("\nSaving config...")
        ui.save_config()
        print("save_config() completed without error")

        print("\nALL OK")


with_retry(run)
