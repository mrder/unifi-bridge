# TODO

Living list of open work. Move finished items to CHANGELOG.md under
`[Unreleased]` (or a new version section) instead of just deleting them here.

## Before the first real release (`main`, 0.x.0)

- [ ] Run the actual `docker build` / `docker compose up` flow end-to-end on
  real Unraid hardware -- so far only the underlying Python logic has been
  tested directly, never the containerized build.
- [ ] Verify the web UI's network scan (`webui.py: scan_for_switch()`) against
  a real factory-reset switch from a clean container start, not just unit-level.
- [ ] Fix the git commit identity (currently a placeholder,
  `unifi-bridge dev <noreply@example.com>`) to the real GitHub account before
  treating history as final.

## Bridge / south_adapter

- [ ] `DLinkWebUI` has no confirmed way to read a port's current
  tagged/untagged VLAN membership back (`get_port_vlan_info()` only confirms
  VLAN *mode*, not membership) -- needed so `DLinkSwitchAdapter.list_ports()`
  can report real `tagged_vlans`/`untagged_vlan` and so
  `set_port_vlans()` can stop being additive-only (see switch_adapter.py /
  dlink_adapter.py docstrings). Needs a real adopted device running for a
  while to also observe the controller's actual per-port VLAN push shape.
- [ ] Decide deliberately whether to ever act on pushed `cmd`s
  (reboot/upgrade/setdefault) -- currently intentionally inert
  (`bridge_daemon.apply_pushed_config` / `run_adopted_loop`).
- [ ] `dlink_webui.py`'s `set_port_settings()` applies all fields at once
  (no partial update server-side) -- worth a safety check/guard before
  calling it from an automated config-push path, not just from manual tests.

## NETGEAR JGS524Ev2 (south_adapter/netgear_http.py, netgear_adapter.py)

- [ ] Find where per-port 802.1Q tagged/untagged VLAN assignment actually
  lives in this switch's web UI (the "Advanced VLAN Configuration" page only
  does VID create/delete + a membership summary, confirmed by reading its
  full rendering JS) -- likely under "802.1Q > Einfach" (Basic), not yet
  explored. Needed before `set_port_vlans()` can be implemented.
- [ ] Live-verify `set_port_enabled()` (currently delegates to the
  `python-netgear-switch-library` NSDP write path, unverified against this
  exact switch/firmware) with an actual port toggle during a maintenance
  window.
- [ ] Verify `default_ip`/`default_password` in the `netgear-jgs524ev2`
  profile against a genuinely factory-reset unit -- currently NETGEAR's
  commonly-documented defaults, not live-tested (see switch_profiles.py).
- [ ] No logout endpoint was found for this switch's web UI (unlike
  `DLinkWebUI`'s `/form/Logout`) -- `NetgearHttp.logout()` only drops the
  session cookie client-side; confirm whether that's actually sufficient to
  free up the switch's single-admin-session slot promptly, or whether
  sessions linger server-side until timeout.

## North adapter / protocol

- [ ] Observe a real, sustained provisioning cycle (current understanding of
  `system_cfg`/`portsToDeploy` comes from exactly one captured push) to see
  what else a controller sends over time (STP, ACLs, firmware-update cmds).
- [ ] `discovery_protocol.py`: TLV field semantics beyond what's used today
  (0x01/0x02/0x03/0x0A/0x0C/0x15/0x16/0x17) are still unconfirmed for a
  switch specifically (all confirmed fields came from an AP-era reference
  sample plus the discovery *parser*, not a real switch's own packet).

## Web UI

- [ ] `/reconfigure` currently only clears `config.json`, not
  `bridge_state.json` (the saved authkey) -- fine for changing controller/IP
  settings, but there's no UI path yet to fully "forget" a device and
  re-adopt from scratch without shelling into the container.
- [ ] No auth on the web UI itself -- fine on a private macvlan LAN, would
  need real auth before ever being exposed more broadly.

## Adding a new switch vendor/model

- [ ] Write a new south-adapter class implementing the `SwitchAdapter` ABC
  (`switch_adapter.py`) -- `south_adapter/dlink_adapter.py` (wraps one
  hand-written low-level driver) and `south_adapter/netgear_adapter.py`
  (combines a hand-written driver for what an existing library doesn't cover
  with that library for what it does) are both reference implementations;
  pick whichever pattern fits whether a usable library already exists for
  the vendor.
- [ ] Add a `SwitchProfile` entry in `switch_profiles.py` pointing at it, with
  a real, currently-supported "classic" UniFi model/sysid (check against a
  live controller's discovery log first -- see README "Why an older UniFi
  model" for why the newest-looking match can silently fail).
- [ ] It should then appear in the web UI's dropdown automatically -- no
  other code changes needed for the setup flow itself.

## Process

- [ ] Cut `main` (0.x.0) from `master` once the "before the first real
  release" items above are done, and tag/publish an actual GitHub Release so
  the web UI's update check has something to find.
