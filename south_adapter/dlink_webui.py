"""
HTTP client for the D-Link DGS-1250 web management UI.

Works on the switch's CURRENT firmware (1.00.040) with no firmware upgrade needed --
unlike the CLI (south_adapter/dlink_cli.py), which on this firmware only exposes a
handful of bootstrap commands (see README). The web UI already implements full VLAN
management on this firmware, we just talk to its own HTTP endpoints directly instead
of clicking through a browser.

Reverse-engineered live against a real DGS-1250-28X (2026-09-08) by reading the
served page source (view-source, not guesswork) for:
  - www/login.html            -> login form + the login_action() JS (password hashing)
  - datastore/login.js        -> the per-session `sequence` nonce and `remote` flag
  - www/iss/366_vlan.html     -> the VLAN table's form fields and JS handlers
  - datastore/366_vlan.js     -> the VLAN list read format

Login scheme (remote == '0' branch only -- that's what this switch serves):
  The plaintext password never leaves the client. Instead three fields are derived
  from a fresh per-page-load nonce ("sequence") and posted alongside it:
    sequence0 = sequence (unchanged)
    sequence3 = base64( sha256( base64(md5(password))    + sequence ) )
    sequence2 = base64( sha256( base64(sha1(password))   + sequence ) )
    sequence1 = base64( sha256( base64(sha256(password)) + sequence ) )
  The <input name="pwd"> is disabled before submit, so it's excluded from the POST
  entirely -- only pn + sequence0..3 are sent.

Session scheme:
  There is NO session cookie. POST /form/Login responds 303 with
  `Location: /html/acctReplyMsg.html?RpWebID=<token>` -- that token is the entire
  session credential and must be appended as a `RpWebID` query parameter on every
  subsequent request (datastore reads AND form POSTs alike). Skip it and you get a
  plain 403 even though the login itself "succeeded". No need to actually fetch
  acctReplyMsg.html -- it 403s regardless, the token from the Location header is
  already usable on its own.

VLAN write scheme:
  POST ../../form/VLAN_Apply (relative to www/iss/), fields:
    action   0 = add VLAN(s) from vid_list (e.g. "3" or "2-5")
             1 = delete VLAN(s) from vid_list
             2 = rename VLAN edit_vid to edit_name
             3 = delete single VLAN del_vid
    vid_list / edit_vid / edit_name / del_vid as needed per action

VLAN read: GET datastore/366_vlan.js?vid=1&findtype=0&index=0&pagesize=<n>
  Returns `var vlan_info = [[vid,name,tagged_ports,untagged_ports,vlan_type,is_default,description], ...];`
  as a literal JS array assignment (not JSON) -- see parse_vlan_info().

NOT YET covered: per-port tagged/untagged VLAN membership. That's a separate page
("VLAN Schnittstelle" in the menu, i.e. VLAN Interface) with its own form endpoint --
same pattern, just not reverse-engineered yet. Do that next before wiring up a real
south-adapter port-config call.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import requests


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


def _sha256_b64(text: str) -> str:
    return _b64(hashlib.sha256(text.encode()).digest())


class DLinkWebUIError(Exception):
    pass


@dataclass
class VlanEntry:
    vid: str
    name: str
    tagged_ports: str
    untagged_ports: str
    vlan_type: str
    is_default: str
    description: str


class DLinkWebUI:
    def __init__(self, host: str, username: str = "admin", password: str = "admin"):
        self.base = f"http://{host}"
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.rpwebid: str | None = None

    def _p(self, params: dict | None = None) -> dict:
        """Merge the RpWebID session token into a query-param dict."""
        if self.rpwebid is None:
            raise DLinkWebUIError("not logged in (no RpWebID) -- call login() first")
        merged = dict(params or {})
        merged["RpWebID"] = self.rpwebid
        return merged

    def login(self, retries: int = 4, retry_delay: float = 4.0) -> None:
        """The switch allows exactly one admin session. The slot doesn't always
        free up immediately after a previous client disconnects (observed delay:
        a few seconds, cause unclear -- possibly an internal timeout rather than
        an explicit release), so a login attempt right after a prior session ended
        can spuriously fail. Retry a few times before giving up for real."""
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                self._login_once()
                return
            except DLinkWebUIError as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(retry_delay)
        raise last_error

    def _login_once(self) -> None:
        # Free a stale session left over from a previous run/crash before trying
        # to log in ourselves.
        try:
            self.session.post(f"{self.base}/form/Logout", timeout=5)
        except requests.RequestException:
            pass

        login_page_url = f"{self.base}/www/login.html"
        r = self.session.get(login_page_url, timeout=8)
        r.raise_for_status()

        seq_resp = self.session.get(
            f"{self.base}/datastore/login.js",
            headers={"Referer": login_page_url},
            timeout=8,
        )
        seq_resp.raise_for_status()
        m = re.search(r"var sequence='([^']*)';", seq_resp.text)
        remote_m = re.search(r"var remote='([^']*)';", seq_resp.text)
        if not m or not remote_m:
            raise DLinkWebUIError(f"Could not parse login.js: {seq_resp.text!r}")
        sequence = m.group(1)
        remote = remote_m.group(1)
        if remote != "0":
            raise DLinkWebUIError(
                f"remote={remote!r} uses the AES-encrypted login branch, not implemented here"
            )

        md5_b64 = _b64(hashlib.md5(self.password.encode()).digest())
        sha1_b64 = _b64(hashlib.sha1(self.password.encode()).digest())
        sha256_b64 = _b64(hashlib.sha256(self.password.encode()).digest())

        sequence0 = sequence
        sequence1 = _sha256_b64(sha256_b64 + sequence)
        sequence2 = _sha256_b64(sha1_b64 + sequence)
        sequence3 = _sha256_b64(md5_b64 + sequence)

        resp = self.session.post(
            f"{self.base}/form/Login",
            data={
                "pn": self.username,
                "sequence0": sequence0,
                "sequence1": sequence1,
                "sequence2": sequence2,
                "sequence3": sequence3,
            },
            headers={"Referer": login_page_url},
            timeout=8,
            allow_redirects=False,
        )
        location = resp.headers.get("Location", "")
        rpwebid = parse_qs(urlparse(location).query).get("RpWebID", [None])[0]
        if resp.status_code != 303 or not rpwebid:
            raise DLinkWebUIError(
                f"Login failed (status {resp.status_code}, Location={location!r}) "
                "-- wrong credentials, or the switch is on a firmware with a "
                "different login flow than the one this was reverse-engineered against."
            )
        self.rpwebid = rpwebid

    def _vlan_datastore(self, vid: int = 1, findtype: int = 0, index: int = 0, pagesize: int = 50) -> str:
        r = self.session.get(
            f"{self.base}/datastore/366_vlan.js",
            params=self._p({"vid": vid, "findtype": findtype, "index": index, "pagesize": pagesize}),
            headers={"Referer": f"{self.base}/www/iss/366_vlan.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        return r.text

    def list_vlans(self, pagesize: int = 50) -> list[VlanEntry]:
        text = self._vlan_datastore(pagesize=pagesize)
        return self.parse_vlan_info(text)

    @staticmethod
    def parse_vlan_info(text: str) -> list[VlanEntry]:
        m = re.search(r"var vlan_info\s*=\s*(\[[\s\S]*?\]);", text)
        if not m:
            raise DLinkWebUIError(f"Unexpected vlan datastore response: {text!r}")
        rows = re.findall(r"\[([^\[\]]*)\]", m.group(1))
        entries = []
        for row in rows:
            fields = [f.strip().strip("'") for f in row.split(",")]
            fields += [""] * (7 - len(fields))
            entries.append(VlanEntry(*fields[:7]))
        return entries

    def _vlan_apply(self, **fields) -> requests.Response:
        payload = {"action": "", "edit_vid": "", "del_vid": "", "edit_name": "", "vid_list": ""}
        payload.update({k: str(v) for k, v in fields.items()})
        r = self.session.post(
            f"{self.base}/form/VLAN_Apply",
            params=self._p(),
            data=payload,
            headers={"Referer": f"{self.base}/www/iss/366_vlan.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        return r

    def get_switch_status(self) -> dict:
        """Model, firmware, hardware rev, MAC, serial -- from datastore/switch.js."""
        r = self.session.get(
            f"{self.base}/datastore/switch.js",
            params=self._p(),
            headers={"Referer": f"{self.base}/www/main.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        m = re.search(r"var Switch_Status\s*=\s*(\[[^\]]*\]);", r.text)
        if not m:
            raise DLinkWebUIError(f"Unexpected switch.js response: {r.text!r}")
        fields = [f.strip().strip("'") for f in re.findall(r"'([^']*)'", m.group(1))]
        keys = ["model", "firmware", "hw_rev", "mac", "kernel", "serial", "unknown1", "unknown2", "boot_version"]
        return dict(zip(keys, fields))

    def set_port_settings(
        self,
        from_port: int,
        to_port: int | None = None,
        unit: int = 1,
        state: bool = True,
        duplex: int = 0,
        speed: int = 0,
        mdix: int = 3,
        flow_control: bool = False,
        auto_downgrade: bool = False,
        medium_type: int = 0,
        description: str | None = None,
    ) -> None:
        """Configure a port or port range. Reverse-engineered from 887_port_settings.html
        -> POST /form/interface_switch_port_apply.

        NOTE: this form applies ALL fields at once to the whole range, not just the
        one you care about -- there's no "leave everything else as-is" option in the
        underlying endpoint itself. The defaults here (duplex/speed/mdix=Auto, flow
        control off, auto-downgrade off, RJ45) match this switch's factory defaults,
        so calling this with just `state=` is safe on an unmodified port, but will
        silently reset any other custom setting on that port back to these defaults.
        If that matters, read `get_port_vlan_info()`-equivalent current settings
        first and pass them through explicitly (no such read method exists yet for
        speed/duplex/mdix specifically -- only link status via `get_port_status()`,
        which shows negotiated values, not configured ones).

        duplex: 0=Auto, 1=Half, 2=Full
        speed: 0=Auto, 1=10M, 2=100M, 3=1000M, 4=10G, 6=1000M Master, 7=1000M Slave
        mdix: 1=Cross, 2=Normal, 3=Auto
        medium_type: 0=RJ45, 1=SFP (only matters on combo ports)
        """
        if to_port is None:
            to_port = from_port
        payload = {
            "Descflag": "1" if description is not None else "0",
            "unit": unit,
            "fPort": from_port,
            "tPort": to_port,
            "medium_type": medium_type,
            "state": 3 if state else 2,
            "mdix": mdix,
            "autoDowngrade": 3 if auto_downgrade else 2,
            "flowControl": 3 if flow_control else 2,
            "duplex": duplex,
            "speed": speed,
            "spd10Flag": 1,
            "spd100Flag": 1,
            "spd1KFlag": 1,
        }
        if description is not None:
            payload["description"] = description
        r = self.session.post(
            f"{self.base}/form/interface_switch_port_apply",
            params=self._p(),
            data=payload,
            headers={"Referer": f"{self.base}/www/iss/887_port_settings.html?RpWebID={self.rpwebid}"},
            timeout=10,
        )
        r.raise_for_status()

    def set_port_state(self, from_port: int, to_port: int | None = None, enabled: bool = True, unit: int = 1) -> None:
        """Convenience wrapper: enable/disable a port (or range) without touching
        speed/duplex/etc -- reads the port's current negotiated settings first via
        `get_port_status()`... actually doesn't, see `set_port_settings()` docstring:
        this still resets speed/duplex/mdix/flow-control to factory defaults, same
        caveat applies. Fine for a freshly reset port; not a safe no-op on a tuned one.
        """
        self.set_port_settings(from_port, to_port, unit=unit, state=enabled)

    def get_port_utilization(self, unit: int = 1, from_port: int = 1, to_port: int = 28) -> list[dict]:
        """TX/RX packets-per-second and utilization % per port -- lighter-weight
        than full byte/error counters but matches what `show interfaces utilization`
        gives over CLI. Source: 965_port_utilization.html -> datastore/965_utilization.js
        (query string is "unit,fromPort,toPort,showAll" positionally, not named params)."""
        r = self.session.get(
            f"{self.base}/datastore/965_utilization.js",
            params=self._p({f"{unit},{from_port},{to_port},1": ""}),
            headers={"Referer": f"{self.base}/www/iss/965_port_utilization.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        m = re.search(r"var Port_Utilization\s*=\s*(\[[\s\S]*?\]);", r.text)
        if not m:
            raise DLinkWebUIError(f"Unexpected utilization datastore response: {r.text!r}")
        rows = re.findall(r"\[([^\[\]]*)\]", m.group(1))
        keys = ["port", "tx_pps", "rx_pps", "utilization_pct"]
        return [dict(zip(keys, [f.strip().strip("'") for f in row.split(",")])) for row in rows]

    def get_port_status(self, unit: int = 1) -> list[dict]:
        """Live link status per port: connected/not, MAC, VLAN, flow control, duplex,
        speed, media type. Source: datastore/887_port_status.js (backs the "Port
        Status" monitoring page)."""
        r = self.session.get(
            f"{self.base}/datastore/887_port_status.js",
            params=self._p({str(unit): ""}),
            headers={"Referer": f"{self.base}/www/iss/887_port_status.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        m = re.search(r"var port_staus\s*=\s*(\[[\s\S]*?\]);", r.text)
        if not m:
            raise DLinkWebUIError(f"Unexpected port_status datastore response: {r.text!r}")
        rows = re.findall(r"\[([^\[\]]*)\]", m.group(1))
        keys = ["port", "status", "mac", "vlan", "flow_send", "flow_receive", "duplex", "speed", "media_type"]
        return [dict(zip(keys, [f.strip().strip("'") for f in row.split(",")])) for row in rows]

    def get_port_vlan_info(self, unit: int = 1) -> list[tuple[str, str, str, str, str]]:
        """Returns (port, vlan_mode, ingress_checking, acceptable_frame, port_no) per port."""
        r = self.session.get(
            f"{self.base}/datastore/366_vlan_interface.js",
            params=self._p({str(unit): ""}),
            headers={"Referer": f"{self.base}/www/iss/366_vlan_interface.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        m = re.search(r"var vlan_interface\s*=\s*(\[[\s\S]*?\]);", r.text)
        if not m:
            raise DLinkWebUIError(f"Unexpected vlan_interface datastore response: {r.text!r}")
        rows = re.findall(r"\[([^\[\]]*)\]", m.group(1))
        return [tuple(f.strip().strip("'") for f in row.split(",")) for row in rows]

    def add_vlans(self, vid_list: str) -> None:
        """vid_list e.g. '999' or '3' or '2-5' (same syntax as the web UI field)."""
        self._vlan_apply(action=0, vid_list=vid_list)

    def delete_vlans(self, vid_list: str) -> None:
        self._vlan_apply(action=1, vid_list=vid_list)

    def rename_vlan(self, vid: int, name: str) -> None:
        self._vlan_apply(action=2, edit_vid=vid, edit_name=name)

    def delete_vlan(self, vid: int) -> None:
        self._vlan_apply(action=3, del_vid=vid)

    def set_port_vlan_membership(
        self,
        port_no: int,
        vid_list: str,
        tagged: bool,
        unit: int = 1,
        remove: bool = False,
    ) -> None:
        """Add or remove a port as a tagged/untagged member of the given VLAN(s).

        `port_no` is the plain port index (1 for eth1/0/1, 2 for eth1/0/2, ...),
        NOT the "eth1/0/N" string -- that's what the switch's own edit page calls
        `edit_port` / `port_no`, taken from datastore/366_vlan_interface.js.

        Only the port's VLAN mode is left as "Hybrid" (2) here -- that's the
        factory-default mode on every port on this switch and is the closest
        match to a UniFi-style "this port carries these tagged/untagged VLANs"
        model. Access/Trunk/private VLAN modes use a different `vlan_action`
        option set entirely (see README) and aren't wired up here.
        """
        payload = {
            "edit_unit": unit,
            "edit_port": port_no,
            "h_nativevlan": 0,
            "clone_flag": 0,
            "vlan_mode": 2,  # Hybrid
            "acceptable_frame": 1,  # Admit All
            "ingress_checking": 1,  # Enabled
            "vlan_action": 1 if remove else 0,  # 1=Remove, 0=Add
            "add_mode": 1 if tagged else 0,  # 1=Tagged, 0=Untagged
            "vid_list": vid_list,
        }
        r = self.session.post(
            f"{self.base}/form/VLAN_Interface_Apply",
            params=self._p(),
            data=payload,
            headers={"Referer": f"{self.base}/www/iss/366_vlan_interface_edit.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()

    def set_lldp_enabled(self, enabled: bool = True) -> None:
        """Global LLDP on/off -- disabled by factory default on this switch. Needed
        for the switch to show up at all in a neighboring UniFi device's topology
        view via LLDP, independent of anything on the UniFi-protocol side.
        Reverse-engineered from 366_lldp_global.html -> POST /form/LLDP_Global_Apply.
        Forward/trap/MED-trap states are submitted alongside (same form) and left
        matching the switch's own defaults (all off) rather than exposed here."""
        v = "1" if enabled else "0"
        r = self.session.post(
            f"{self.base}/form/LLDP_Global_Apply",
            params=self._p(),
            data={"lldp_state": v, "forward_state": "0", "trap_state": "0", "med_state": "0"},
            headers={"Referer": f"{self.base}/www/iss/366_lldp_global.html?RpWebID={self.rpwebid}"},
            timeout=10,
        )
        r.raise_for_status()

    def get_lldp_neighbors(self, unit: int = 1) -> list[dict]:
        """Discovered LLDP neighbors per port (chassis ID, port ID/description,
        system name). Empty until `set_lldp_enabled(True)` has been on for at least
        one LLDP transmit interval (default 30s) so neighbors have had a chance to
        be heard. Source: datastore/366_lldp_remote_brief.js."""
        r = self.session.get(
            f"{self.base}/datastore/366_lldp_remote_brief.js",
            params=self._p({str(unit): ""}),
            headers={"Referer": f"{self.base}/www/iss/366_lldp_remote.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        m = re.search(r"=\s*(\[[\s\S]*?\]);", r.text)
        if not m:
            return []
        rows = re.findall(r"\[([^\[\]]*)\]", m.group(1))
        return [[f.strip().strip("'") for f in row.split(",")] for row in rows]

    def save_config(self) -> None:
        """Persist the running config to startup-config (`copy running-config
        startup-config` in CLI terms). Without this, VLAN/port changes made through
        this client are lost on the next reboot -- same as if you'd made them in the
        web UI and never clicked the toolbar "Save" button."""
        r = self.session.post(
            f"{self.base}/form/Save_Apply",
            params=self._p(),
            data={"selfile_name": "0"},
            headers={"Referer": f"{self.base}/www/iss/369_save.html?RpWebID={self.rpwebid}"},
            timeout=10,
        )
        r.raise_for_status()

    def logout(self) -> None:
        """Free the single admin session slot. Safe to call even if not logged in."""
        try:
            self.session.post(f"{self.base}/form/Logout", timeout=5)
        except requests.RequestException:
            pass
        self.rpwebid = None

    def __enter__(self) -> "DLinkWebUI":
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        self.logout()

    def get(self, path: str, extra_params: dict | None = None) -> requests.Response:
        """Raw authenticated GET, for pages/datastores not wrapped by a dedicated method."""
        r = self.session.get(
            f"{self.base}/{path.lstrip('/')}",
            params=self._p(extra_params),
            headers={"Referer": f"{self.base}/www/main.html?RpWebID={self.rpwebid}"},
            timeout=8,
        )
        r.raise_for_status()
        return r
