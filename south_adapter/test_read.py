"""Read-only smoke test against a real DGS-1250 switch. Safe to run any time --
touches no configuration, only `show` commands."""

import sys

from dlink_cli import DLinkCLI

HOST = sys.argv[1] if len(sys.argv) > 1 else "10.90.90.90"

with DLinkCLI(HOST) as cli:
    print("=== show version ===")
    print(cli.show_version_raw())

    print("=== show vlan ===")
    print(cli.show_vlan_raw())

    print("=== show interfaces status (parsed) ===")
    for p in cli.show_interfaces_status():
        print(p)
