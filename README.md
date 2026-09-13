# unifi-bridge

**Adopt a switch UniFi never sold you.**

This bridges a third-party (non-Ubiquiti) network switch into an existing UniFi
Network Application, so it shows up, gets adopted, and is *actually managed* --
VLANs, port config, live status -- from the same UniFi app as your real APs and
gateway, instead of living as a separate island with its own login and its own
mental model. Started as a project for a single used D-Link DGS-1250-28X
(Realtek RTL930x, 24x1G + 4x10G SFP+, no PoE); the architecture is built to add
more switch models without touching the core protocol code -- see
`switch_profiles.py`.

Comes with a small setup web UI (pick your switch type, optionally auto-scan
for it, point it at your controller) as well as a plain env-var/no-UI mode --
see "Deployment" below.

## Disclaimer

This is unofficial, community reverse-engineering work -- **not affiliated
with, endorsed by, or supported by Ubiquiti Inc. or D-Link.** "UniFi" is a
trademark of Ubiquiti Inc. Provided as-is, no warranty (see `LICENSE`); this
talks to your network infrastructure, so read what it does before running it
against anything you care about. Built by decompiling and testing against
Ubiquiti's own, legitimately-obtained UniFi Network Application software for
interoperability purposes (see "How we got ground truth" below for exactly
what was read and why) -- the same category of work as any third-party client
or plugin reverse-engineered against a vendor's official app.

## What it actually does today

Makes a used DGS-1250-28X (Realtek RTL930x, 24x1G + 4x10G SFP+, no PoE, currently
on firmware 1.00.040 / 2019) show up, get adopted, and be managed inside an
existing UniFi Network Application -- instead of being a separate island with its
own login.

**Status: the core loop works end-to-end against a real controller** -- discovery,
first contact, adoption (with the real AES-GCM key exchange), and controller config
push have all been confirmed live. What remains is broadening how much of a
pushed config actually gets translated into switch changes (currently: VLAN
creation + logging of anything else) -- see "Known limitations".

## Reality check

There is no existing project, firmware, or vendor feature that does this. The
closest prior art (wvengen/unifi-controllable-switch, porting the inform protocol
onto Ubiquiti's *own* TOUGHswitch hardware) only ever reached **read-only status
reporting** -- it never accepted config pushed from the controller. Nobody has done
this for third-party (non-Ubiquiti-silicon) switch hardware before. This project
has now gone further than that prior art: it completes the full adoption handshake
and receives real config pushes from a live controller.

The big unknowns here were resolved by decompiling the real UniFi Network
Application (Java, obfuscated but not encrypted) and testing directly against a
live controller -- not by guessing from blog posts. See "How we got ground truth"
below for specifics.

## Architecture

```
UniFi Controller  <--- inform + discovery --->  [north_adapter]  <--->  [bridge_daemon]  <--->  [south_adapter]  <--- HTTP --->  D-Link switch
```

- **south_adapter**: talks to the D-Link switch via its own web UI's HTTP
  endpoints. Complete and live-tested.
- **north_adapter**: speaks the UniFi discovery + inform protocol (envelope,
  AES-CBC for the default-key handshake, AES-GCM for everything after adoption).
  Complete and confirmed against a live controller, including the full adoption
  handshake.
- **bridge_daemon.py**: the persistent process that ties the two together --
  handles discovery, waits for a human to click "Adopt", performs the key
  exchange, then loops forever reporting real switch state and applying pushed
  config.

## Why an older UniFi model is impersonated, not the closest capability match

The closest real UniFi switch to this D-Link's port layout (24x1G + 4x10G SFP+)
is `USWED72` (real product: UniFi Switch Enterprise-XG-24), and its `sysid`
(60786) and full device-catalog entry were confirmed straight from the
controller's own decompiled source. Using it seemed like the obvious choice.

It doesn't work. Live-tested against a real controller with debug logging
enabled, `USWED72` (and its sibling `USWED76`) get rejected by the legacy L2
discovery listener (UDP 10001) with `unknown model`, even though the model is a
perfectly valid entry in the controller's own device catalog:
```
<discover> DEBUG api      - Unknown model: USWED72
<discover> DEBUG discover - ignore [...] unknown model : USWED72 / 7.5.15
```
Meanwhile older "classic" platforms (`US24`, `US48`, and -- the best port-layout
match among the ones that work -- `USXG24`, a real 24x1G+4x10G-SFP+ product) got
past discovery immediately:
```
<discover> DEBUG discover - from [...](l2, USXG24, 7.5.15): ... unsupported=false (default)
<discover> INFO  event    - [event] Switch[...] was discovered and waiting for adoption
```
Working theory: the legacy L2 broadcast discovery protocol simply isn't
implemented/enabled for newer "Enterprise XG" hardware in this controller
version, which likely uses a different discovery mechanism for real hardware of
that generation. This is a load-bearing, empirically-found constraint, not a
preference -- `switch_profiles.py` uses `USXG24` (sysid 60201) for exactly this
reason.

## Deployment

### Prerequisites

- The switch must be at **factory defaults** with its **default admin login**
  (`admin`/`admin`) -- this bridge takes over the switch's identity from first
  contact. If it's already been configured, either factory-reset it or set
  `SWITCH_USERNAME`/`SWITCH_PASSWORD`/`SWITCH_IP` in `.env` to match its
  current state instead.
- **Network reachability, both directions** -- this is the part most likely to
  need manual attention on your specific setup:
  - The container needs to reach the **UniFi controller** (typically your main
    LAN/macvlan network -- confirmed working via a macvlan network like `br0`).
  - The container needs to reach the **switch's management IP**. A
    factory-reset D-Link boots to `10.90.90.90` (a `10.0.0.0/8` address), which
    is very likely NOT on the same subnet as your controller's LAN. Our own
    testing only ever reached that address from a laptop directly wired to the
    switch -- reachability from the actual Docker host has not been verified
    end-to-end. Before relying on this container, do ONE of:
    1. **(Simplest)** Wire a laptop directly to the switch once, log into its
       web UI at `10.90.90.90` with the default login, and change its
       management IP to a normal address on your main LAN. Then set
       `SWITCH_IP` in `.env` to that address. No reset needed for this step.
    2. Physically connect the switch's management port to the same LAN segment
       your Docker host's macvlan-mapped NIC is on, and add a static route on
       the host for `10.90.90.0/24` via that interface.
- A human needs to click **"Adopt"** on the device once it shows up as Pending
  in the controller UI -- this cannot be (and, for a first adoption, should not
  be) automated away. Confirmed from the controller's own source: the very
  first `setparam`/`mgmt_cfg` response (which hands the device its real key)
  only comes after that click.

### Running it

```bash
git clone <this-repo-url> unifi-bridge && cd unifi-bridge
cp .env.example .env   # compose needs this file to exist; leave everything in
                        # it commented out if you'd rather configure via the
                        # web UI below instead of env vars
docker compose up -d --build
```

Then open the setup web UI: `docker inspect unifi-bridge` (or
`docker network inspect br0`) to find the container's macvlan IP (Docker's
usual `localhost:PORT` publishing doesn't apply on macvlan -- the container
has its own real LAN IP), then browse to `http://<that-ip>:8099`. Pick the
switch type from the dropdown, optionally click "Scan for it" to auto-fill the
IP (see "How scanning works" below), enter the controller address, and hit
"Save & Start". The status page then shows live phase (waiting for switch
login / waiting for the "Adopt" click / adopted) and a tailing log.

Prefer plain env vars with no web UI at all? Run `bridge_daemon.py` directly
instead of the default `webui.py` entrypoint (see `.env.example` for every
setting) -- both read/write the same `/data/bridge_state.json` and
`switch_profiles.py` registry, so either path works identically underneath.

`switch_profiles.py` is the registry of supported switches -- add an entry
there (pointing at a south-adapter class with the same interface as
`DLinkWebUI`) to support another switch model; it shows up in the web UI's
dropdown automatically.

The adoption authkey is persisted to `/data/bridge_state.json` and the web
UI's saved setup to `/data/config.json` (both bind-mounted to `./data` by the
compose file), so a container restart resumes automatically without
re-adopting the device or re-doing setup.

#### How scanning works

Clicking "Scan for it" does NOT sweep arbitrary networks -- too slow and too
unreliable across VLANs/segments to be worth it, and it would give a false
impression of "fully automatic" when the network prerequisite above still has
to be true regardless. Instead it: (1) tries the selected switch type's
known factory-default IP directly first, and (2) if that doesn't answer,
sweeps the container's own local /24 as a fallback -- covering the "I already
gave the switch a normal LAN IP by hand" case from the prerequisite section
above. Either way, a hit is only reported if the scanner actually logs into it
with the profile's default credentials and it identifies as the expected
switch model -- a host merely answering on port 80 is never treated as a
match.

### Known limitations

- **Config-push translation is partial, on purpose rather than by oversight.**
  A real controller pushes a large, switch-specific config blob on every
  provisioning cycle (VLANs, per-port settings, STP, ACLs, and more, depending
  on what capabilities the device claims). `bridge_daemon.py`'s
  `apply_pushed_config()` currently handles VLAN creation (`vlansToDeploy`) and
  logs everything else it sees under `portsToDeploy` instead of silently
  dropping it -- so gaps are visible in the logs, not hidden. Extend that
  function as real pushed-config shapes are observed; a live-adopted device is
  needed to see what the controller actually sends for this specific fake
  model, which wasn't available for iteration beyond the first full push (see
  "How we got ground truth" for what was captured so far).
- `cmd` responses from the controller (`reboot`, `upgrade`, `setdefault`) are
  logged but **never executed automatically** -- these are destructive against
  a real physical switch and were deliberately left as a manual/future
  decision rather than wired up blind.
- PoE settings pushed by the controller are logged and skipped -- this switch
  has no PoE hardware.
- Per-port tagged/untagged VLAN membership from `south_adapter` is available
  (`set_port_vlan_membership()`) but not yet wired into `apply_pushed_config()`
  -- the specific JSON shape the controller uses to express per-port VLAN
  assignment for this fake model hasn't been observed yet.

## south_adapter -- status: complete for VLANs/ports, live-tested

Two implementations exist; only one is currently usable (see why below).

### `dlink_webui.py` -- the switch's own web UI, reverse-engineered -- **this is the one that works**

The stock web UI has full switch management on the *current* firmware (that's
normal for these switches -- CLI came later, GUI was always primary). Instead of
waiting on a firmware upgrade, this drives the GUI's own HTTP endpoints directly,
reverse-engineered by reading the actual served page source (never guessed).

**Session/auth** (the trickiest part, fully solved):
- No session cookie. `POST /form/Login` needs `pn` (username) + three derived
  fields computed from a fresh per-page-load nonce (`sequence`, from
  `/datastore/login.js`) and the password -- the plaintext password never leaves
  the client:
  ```
  sequence1 = base64(sha256(base64(sha256(password)) + sequence))
  sequence2 = base64(sha256(base64(sha1(password))   + sequence))
  sequence3 = base64(sha256(base64(md5(password))    + sequence))
  ```
- The response is a 303 redirect to `.../acctReplyMsg.html?RpWebID=<token>`. That
  `RpWebID` token is the entire session credential and must be appended as a query
  param on **every** subsequent request or you get a 403.
- **Only one admin session at a time.** A browser tab left open blocks script
  logins. The session slot also doesn't always free up instantly after a client
  disconnects (a few seconds' delay, cause unclear) -- `login()` retries
  automatically (4x, 4s apart) to absorb this; bump `retries=` if you still hit it.

**What's implemented and live-tested** (`test_webui.py` runs all of this against
the real switch end to end):
- VLAN CRUD: `list_vlans()`, `add_vlans()`, `rename_vlan()`, `delete_vlan()` -- via
  `POST /form/VLAN_Apply` (action 0=add/1=delete/2=rename/3=delete-one) and
  `GET /datastore/366_vlan.js` (JS array literal, not JSON).
- Per-port VLAN tagging: `set_port_vlan_membership()` -- via
  `POST /form/VLAN_Interface_Apply`. Only the Hybrid-mode add/remove path is wired
  up (that's every port's factory-default mode); Access/Trunk/private-VLAN modes
  use the same endpoint with different `vlan_action` values, documented in the
  method's docstring, but aren't exposed as separate methods yet.
- Port link status (read): `get_port_status()` -- connected/not, MAC, VLAN, flow
  control, duplex, speed, media type, from `datastore/887_port_status.js`.
- Port utilization (read): `get_port_utilization()` -- TX/RX packets-per-second +
  utilization %, from `datastore/965_utilization.js`.
- Port config (write): `set_port_settings()` / `set_port_state()` -- state
  (enable/disable), duplex, speed, MDIX, flow control, auto-downgrade, medium type,
  description, via `POST /form/interface_switch_port_apply`. Note: this endpoint
  applies *all* fields at once to the whole port range -- there's no partial-update
  option server-side, so calling it resets any field you don't explicitly pass back
  to this switch's factory defaults. Fine for a fresh port, not a safe no-op on a
  hand-tuned one.
- LLDP: `set_lldp_enabled()` (disabled by factory default -- needed for the switch
  to appear in any neighboring device's topology view) and `get_lldp_neighbors()`
  (via `datastore/366_lldp_remote_brief.js`; returns raw rows, column meaning not
  further decoded since nothing LLDP-capable was attached to test against).
- `get_switch_status()` -- model/firmware/hw-rev/MAC/serial from `datastore/switch.js`.
- `save_config()` -- `POST /form/Save_Apply`, `selfile_name=0` (startup-config).
  **Without this, every change above is lost on next reboot** -- same as making
  changes in the browser and never clicking the toolbar "Save" button.

Live-tested end to end 2026-09-11 (`test_webui.py`): VLAN create/rename/delete,
port-tag a VLAN and verify via re-read, disable/re-enable a port and verify via
re-read, enable LLDP, save config. All passed against the real switch.

### `dlink_cli.py` -- Telnet CLI (unusable on this switch's firmware)

The switch's current firmware (1.00.040, 2019) has a CLI so minimal it doesn't even
have a `vlan` command -- confirmed live via the CLI's own `?` help. D-Link's "full
feature command line" only exists from firmware 2.0x onwards, and the mandatory
stepping-stone firmware needed to get there (v2.00.013) isn't hosted anywhere
public anymore (checked support.dlink.com, ftp.dlink.ru, archive.org). Since
`dlink_webui.py` covers everything needed without a firmware change, this path was
dropped rather than pursued further. Kept in case the firmware gap ever gets solved.

## north_adapter -- status: complete, confirmed live against a real controller

### How we got ground truth

The UniFi Network Application is a Java/Spring Boot app, obfuscated (short garbage
class/method names) but **not encrypted or compiled to native code** -- unlike the
actual UniFi *device* firmware, which turned out to be genuinely encrypted (checked:
downloaded the real USWED72 switch firmware from Ubiquiti's own public firmware API,
`fw-update.ubnt.com`; its payload sections have the gzip "encrypted" flag set and
don't decompress -- that avenue is closed). The controller side, however, decompiles
cleanly with a standard tool (CFR). We downloaded the current release directly from
Ubiquiti's own update API (same one their official Docker images use internally --
`https://fw-update.ubnt.com/api/firmware-latest?filter=eq~~product~~unifi-controller...`)
and decompiled the relevant classes out of `UniFi/lib/internal/internal-dependencies.jar`.
This is standard interoperability reverse-engineering on software the user is
legitimately running -- not extracting or redistributing anything, just reading it
to build a compatible client, the same category of work as the AP-protocol writeups
this project already leaned on.

Classes actually read (all in the decompiled, obfuscated source -- class names below
are the real (obfuscated) ones so they're greppable if revisited):
- `com.ubnt.net.InformServlet` -- the `/inform` HTTP endpoint. Confirmed the wire
  envelope byte-for-byte and the default-key/unknown-device handling.
- `com.ubnt.service.devmgr.l.DXufmCC` -- the actual per-inform business logic:
  device model lookup, the default-key-only-when-ADOPTING gate, and the
  `setparam`/`mgmt_cfg` response that hands a device its real authkey.
- `com.ubnt.i.g.a.dChnXOlwHH` -- the full real device model enum (name, chip
  vendor, `sysid` int, port count) for every UniFi device ever sold. This is
  where `USWED72`, `USXG24`, `US24`, `US48` and their real `sysid`s came from.
- `com.a.a.a.UWKqI` / `com.a.a.a.ixsXr` -- the L2 discovery packet builder and
  parser. Reading the parser caught a real bug in the first attempt: TLV type
  0x0C (12) is the actual "platform" field; the first attempt used type 0x15
  (21), which the parser treats as an ESSID-style AirMax-legacy filter field,
  never as "platform".
- `com.a.a.hEiVLGSwaf` -- the actual AES crypto helper (CBC and GCM). This is
  where the AES-GCM parameters were confirmed: a 16-byte nonce (not the usual
  12), and AAD = the full 40-byte header as transmitted (not just the MAC, as
  first assumed -- that mistake produced a `400` GCM-authentication failure on
  the first live attempt, fixed once this class was actually read).

### Inform envelope

```
offset  size   field
0-3     u32    magic "TNBU" (0x54424E55 / 1414414933, checked against literal
               int constant in InformServlet)
4-7     u32    packet version (1)
8-13    6B     device MAC address
14-15   u16    flags: bit0=encrypted, bit0+bit3=AES-GCM (bit3 alone is invalid),
                       bit1=zlib compressed, bit2=snappy compressed
16-31   16B    IV (CBC) / nonce (GCM, non-standard 16 bytes not 12)
32-35   u32    payload version
36-39   u32    payload length (for GCM: plaintext length + 16-byte tag)
40+     bytes  payload: AES-CBC+PKCS7 (default key, pre-adoption) or AES-GCM
               (real authkey, post-adoption; AAD = the full 40-byte header)
```
All integers big-endian. HTTP transport: `POST http://<controller>:8080/inform`,
`Content-Type: application/x-binary`. Implemented in
`north_adapter/inform_protocol.py`: `encode()`/`decode()`, both CBC and GCM.
Self-tested (`test_inform_protocol.py`) and confirmed live against a real
controller for both the pre-adoption (CBC, default key) and post-adoption (GCM,
real key) paths.

### Discovery packet

`north_adapter/discovery_protocol.py`: UDP broadcast to `255.255.255.255:10001`
and multicast to `233.89.188.1:10001`. TLV format `type(1) + length(2, BE) +
value`, with a 4-byte header (`version, command, length_hi, length_lo`). Full
field list and the type-12 platform-field fix are documented in the module's own
docstring. Confirmed live: the controller's discovery listener parses these
packets correctly and transitions a matching device to "discovered and waiting
for adoption" -- for the right (older/classic) model strings, see "Why an older
UniFi model" above.

### The adoption handshake -- confirmed live end to end

1. Send discovery (broadcast + multicast) with the fake identity, then a
   first-contact inform (`state: 0`, default key, CBC). While the device is only
   "Pending" (not yet clicked "Adopt" in the UI), the controller responds `404`
   ("Unknown Device") -- confirmed from source and from a live controller's debug
   log (`Inform for Unknown Device[...]`) to be the *expected* response at this
   stage, not a failure.
2. Once a human clicks "Adopt", the *next* default-key inform gets back
   `_type: setparam` with an `mgmt_cfg` field -- a newline-separated
   `key=value` blob containing the real `authkey` (hex, 16 bytes) the device
   must use for all future informs, plus `use_aes_gcm=true`, `mgmt_url`,
   `stun_url`, `cfgversion`, etc.
3. All subsequent informs must use that authkey with **AES-GCM**, not CBC --
   confirmed live (a CBC-encrypted inform under the new key was never tested
   since the source already made clear GCM is required once
   `use_aes_gcm=true`; a wrong-AAD GCM attempt was tried first and correctly
   rejected with `400`, confirming the controller *does* verify the GCM tag).
4. The controller then pushes real state-transition + config: observed live as
   `INFORM_ERROR->PROVISIONING` followed by a `setparam` containing
   `cfgversion`, `system_cfg` (real VLAN/DHCP/VPN network configs from the
   actual site), and per-port `portsToDeploy` entries -- full proof the
   controller treats the fake device as a fully real, manageable switch.
5. If the device stops informing (e.g. the process restarts without persisting
   state), the controller demotes it `PROVISIONING->UNKNOWN` after about two
   minutes -- this is just the normal offline-detection timeout, not an
   adoption failure; `bridge_daemon.py` avoids this by informing continuously
   and persisting the authkey across restarts.

## Serial console / U-Boot access -- done, gives real recovery capability

Documented in `serial_console/uboot_access.py` (the module docstring has the full
story). Short version: a "warm" `reboot` plus flooding the **`&`** character (not
Ctrl+C, which looked promising but turned out to be a dead end -- see the module
docstring for how that was proven) drops you into a real U-Boot prompt (`RTL9300#`)
reliably. Confirmed real facts gathered there:
- CPU is **MIPS**, not ARM -- matches OpenWrt's realtek target.
- Flash layout: 1M uboot, 512K env, 512K sys, remainder as a UBI "fs" partition
  holding a small boot kernel (`uImage`) plus the two dual-boot application images
  (`/switchfs/Image1`, `Image2`, ~8.2MB each) and saved configs.
- Real TFTP capability (`tftpboot`, `bootp`) independent of the application layer --
  this is the recovery path if a future firmware flash ever goes wrong.
- U-Boot does **not** auto-continue after you drop to its prompt -- must explicitly
  `run bootcmd` (or power-cycle) to resume, or the switch just sits there.

Not used for anything beyond reconnaissance -- no flash writes attempted. Firmware
embedding (running the translator on the switch itself) remains a distinct,
deliberately-deferred track from the external-bridge approach documented above; this
access just means it's no longer blocked on recovery capability if it's picked up
later.

## Files

- `south_adapter/dlink_webui.py` -- Web UI HTTP client (complete, see above)
- `south_adapter/dlink_cli.py` -- Telnet CLI driver (unusable on current firmware)
- `south_adapter/test_webui.py` -- full regression test, safe to rerun (creates/
  cleans up throwaway VLANs 998/999, toggles port eth1/0/2 -- **don't repoint that
  at whatever port your management PC is plugged into**)
- `south_adapter/test_read.py` -- read-only CLI smoke test
- `north_adapter/inform_protocol.py` -- inform envelope encode/decode (CBC + GCM)
- `north_adapter/discovery_protocol.py` -- L2 discovery packet encode
- `north_adapter/test_inform_protocol.py` -- envelope round-trip self-test
- `switch_profiles.py` -- registry of supported switches and their fake UniFi
  identity; add an entry here to support another switch model
- `bridge_daemon.py` -- the persistent process: adoption + ongoing inform loop +
  config-push application. `run_bridge(config, status)` is the reusable entry
  point (env-var-only `main()` is a thin wrapper around it)
- `webui.py` -- setup/status web UI (the default container entrypoint); runs
  `run_bridge()` in a background thread once configured via the form
- `Dockerfile` / `docker-compose.yml` / `.env.example` -- container deployment
- `serial_console/uboot_access.py` -- serial/U-Boot access tool + full recipe writeup
- `standalone_adopt_test.py`, `multi_model_test.py`, `adopt_handshake_test.py`,
  `adopt_confirm.py` -- the one-shot test scripts used to reach and verify each
  stage of the adoption handshake live, kept for future debugging/re-verification
  (e.g. after a controller upgrade). `gcm_ctypes.py` is a dependency-free
  (no `cryptography` package needed) AES-GCM implementation via ctypes+libcrypto,
  used by these test scripts when run inside minimal containers; `bridge_daemon.py`
  itself uses the regular `cryptography` package instead since it has its own
  Docker image where adding that dependency is unremarkable.

## Next steps

1. **Observe a real, sustained provisioning cycle.** So far a full config push
   has been seen exactly once (one-shot test, then the process exited). Running
   `bridge_daemon.py` for real against an adopted device will surface what
   *else* the controller sends over time (per-port VLAN assignment shape,
   STP/spanning-tree settings, firmware-update `cmd`s, etc.) -- extend
   `apply_pushed_config()` as those are observed.
2. Wire per-port VLAN assignment from pushed config into
   `set_port_vlan_membership()` once its real JSON shape is observed (see
   "Known limitations").
3. Decide deliberately, when it comes up, whether to ever act on `cmd`
   (reboot/upgrade/setdefault) -- currently intentionally inert.
4. Firmware embedding (running the translator on the switch itself): still a
   distinct, deferred track. Not worth pursuing now that the external-bridge
   approach is proven to work.
