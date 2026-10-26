"""
Raw HTTP client for the NETGEAR JGS524Ev2's web UI ("Smart Managed Plus"
switch line). Covers VLAN lifecycle only -- port status/device info comes from
NSDP instead (see netgear_adapter.py), which this model also speaks and which
needed no reverse-engineering (nsdp_read.py in the `python-netgear-switch-
library` dependency already covers it generically).

Reverse-engineered live against a real JGS524Ev2 (firmware 2.6.0.48,
2026-10-26) by reading the actual served page source -- never guessed. The
existing `python-netgear-switch-library` (github.com/mithro/python-netgear-
switch-library) does NOT have this model registered; its closest analog
(gs305ep)'s HTTP login scheme does not match this one (confirmed live: the
switch resets the TCP connection on that scheme's login path).

Login scheme:
  POST /login.htm, fields: submitId=pwdLogin, password=<hash>, submitEnd=
  `password` is NOT the plaintext -- the real password never leaves the
  client. It's an HMAC-MD5 over a padded message, read straight out of the
  served page's own CalcMD5PW() JS:
    key      = "YOU_CAN_NOT_PASS"  (literal, hardcoded in every page's JS)
    message  = (password + "\\0") repeated to fill exactly 2048 bytes
               (last repeat truncated by "\\0" padding to hit 2048 exactly)
    password field = hex(HMAC-MD5(key, message))
  On success the switch sets an HttpOnly `SID` cookie (invisible to page JS,
  which is why `document.cookie` looked empty during manual testing --
  `requests.Session()` handles it correctly, it was never actually missing).
  Session is single-admin: logging in from here kicks any other active
  session (confirmed live, including the human operator's own browser tab
  and vice versa) -- there is no concurrent access.

VLAN (802.1Q) lifecycle, on config/dot1based_advanced_vlan_conf.htm:
  This page is VID create/delete + a membership *summary* only -- it does
  NOT expose per-port tagged/untagged assignment (confirmed by reading its
  full rendering JS: the per-VLAN row only ever prints a port's NUMBER if
  it's a member, never a distinguishable tagged/untagged state, and there is
  no per-port input control on this page at all). Where that assignment
  actually lives is UNKNOWN -- see TODO.md.
  - Enable/disable 802.1Q switch-wide: POST same URL, fields submitId=
    vlanDot1VidCfg, secureRand=<nonce from the page>, changeType=1,
    confirmOK=1, aDot1VLAN=Enable|Disable, submitEnd=. CONFIRMED LIVE this
    deletes all VLAN config when toggled (the switch's own confirmation
    dialog says so, and it's consistent with the page's own JS) -- the
    caller should expect this, not be surprised by it.
  - Add a VLAN: fields submitId=vlanDot1VidCfg, secureRand=<nonce>,
    changeType=0, confirmOK=0, addVid=<1-4094>, submitEnd=. CONFIRMED LIVE: a
    freshly created VLAN starts with EVERY port excluded (not auto-added to
    any port) -- the resulting `vlanMem` entry for a brand new VID 4000 was
    literally `4000?` followed by 24 empty fields.
  - Delete a VLAN: fields submitId=vlanDot1VidCfg, secureRand=<nonce>,
    changeType=0, confirmOK=1, delVid=<vid>, submitEnd=. VLAN 1 cannot be
    deleted (the switch's own JS refuses it client-side; not verified
    server-side since there was no reason to try against the real switch).
  - `secureRand` is a per-page-load nonce embedded in a `var secureRand = '...'`
    JS literal on every GET of this page -- must be read fresh and echoed
    back on each POST, it is not stable across requests.

The `vlanMem`/`pvid` state variables embedded in the page (also JS literals,
not a JSON API) use the format `VID?Port1?Port2?...?Port24` (multiple VLANs
comma-separated): membership value per port is `'U'` (untagged member) or
empty string (excluded). `'T'` (tagged) is inferred by elimination from the
UI's own sprite set (PG_Top_Tagged/Untagged/Normal images referenced
elsewhere in this UI) but NOT live-confirmed -- no live-observed VLAN had a
tagged port, since this switch's test VLAN was created but never assigned to
any port before cleanup. Treat 'T' as a reasonable inference, not a
confirmed fact, until observed directly.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

import requests

MD5_KEY = "YOU_CAN_NOT_PASS"
PASSWORD_MESSAGE_LEN = 2048


class NetgearHttpError(Exception):
    pass


def _calc_password_hash(password: str) -> str:
    """Replicates CalcMD5PW() from the switch's own served JS byte-for-byte
    (verified by running the real JS in a browser against this switch and
    comparing hashes for the same password -- they matched exactly)."""
    space = "\0"
    count = 0
    parts: list[str] = []
    while count <= (PASSWORD_MESSAGE_LEN - (len(password) + 1)):
        parts.append(password)
        parts.append(space)
        count += len(password) + 1
    message = "".join(parts)
    message += space * (PASSWORD_MESSAGE_LEN - len(message))
    assert len(message) == PASSWORD_MESSAGE_LEN, len(message)
    return hmac.new(MD5_KEY.encode("latin1"), message.encode("latin1"), hashlib.md5).hexdigest()


@dataclass
class VlanState:
    vlan_8021q_enabled: bool
    # vid -> {port_idx: 'U' | 'T'} (only member ports present; absent == excluded)
    membership: dict[int, dict[int, str]]


class NetgearHttp:
    VLAN_PATH = "config/dot1based_advanced_vlan_conf.htm"

    def __init__(self, host: str, username: str = "admin", password: str = "admin"):
        # username is accepted for interface symmetry with other south
        # adapters, but this switch's login has no username field at all --
        # a single shared admin password is the whole auth model.
        self.base = f"http://{host}"
        self.password = password
        self.session = requests.Session()
        self._logged_in = False

    def login(self) -> None:
        self.session.get(f"{self.base}/login.htm", timeout=10)
        pw_hash = _calc_password_hash(self.password)
        r = self.session.post(
            f"{self.base}/login.htm",
            data={"submitId": "pwdLogin", "password": pw_hash, "submitEnd": ""},
            headers={"Referer": f"{self.base}/login.htm"},
            timeout=10,
        )
        r.raise_for_status()
        if "SID" not in self.session.cookies.get_dict():
            raise NetgearHttpError("login did not yield a session cookie -- wrong password?")
        self._logged_in = True

    def logout(self) -> None:
        # No dedicated logout endpoint found during reconnaissance (unlike
        # DLinkWebUI's /form/Logout) -- dropping the session cookie client-side
        # is the best available option. The switch's single-admin-session
        # limit will naturally free up on its own session timeout.
        self.session.cookies.clear()
        self._logged_in = False

    def _require_login(self) -> None:
        if not self._logged_in:
            raise NetgearHttpError("not logged in -- call login() first")

    def _fetch_vlan_page(self) -> str:
        """Confirmed live 2026-10-26: this switch's single-admin-session limit
        can silently invalidate an already-logged-in session from elsewhere
        (another login, even a brief read-only one, kicks the old session) --
        the server doesn't error on the next request, it just serves the
        LOGIN page back instead of the requested one. Detect that (no
        `vlanType` marker present) and transparently re-login once rather
        than let a caller's regex crash on an unexpected page."""
        self._require_login()
        r = self.session.get(f"{self.base}/{self.VLAN_PATH}", timeout=10)
        r.raise_for_status()
        if "vlanType" not in r.text:
            self.login()
            r = self.session.get(f"{self.base}/{self.VLAN_PATH}", timeout=10)
            r.raise_for_status()
            if "vlanType" not in r.text:
                raise NetgearHttpError(
                    "VLAN page didn't return expected content even after re-login "
                    "-- possibly another session is actively competing for this "
                    "switch's single admin-session slot"
                )
        return r.text

    def get_vlan_state(self) -> VlanState:
        text = self._fetch_vlan_page()
        vlan_type = int(re.search(r"vlanType = (\d+)", text).group(1))
        vlan_mem_raw = re.search(r"var vlanMem = '([^']*)'", text).group(1)
        membership: dict[int, dict[int, str]] = {}
        if vlan_mem_raw:
            for entry in vlan_mem_raw.split(","):
                fields = entry.split("?")
                vid = int(fields[0])
                ports = {i: v for i, v in enumerate(fields[1:], start=1) if v}
                membership[vid] = ports
        return VlanState(vlan_8021q_enabled=(vlan_type == 4), membership=membership)

    def _post_vlan_form(self, extra: dict[str, str]) -> None:
        self._require_login()
        text = self._fetch_vlan_page()
        rand = re.search(r"secureRand = '([^']*)'", text).group(1)
        data = {
            "submitId": "vlanDot1VidCfg",
            "secureRand": rand,
            "changeType": "0",
            "confirmOK": "0",
            "submitEnd": "",
        }
        data.update(extra)
        r = self.session.post(
            f"{self.base}/{self.VLAN_PATH}",
            data=data,
            headers={"Referer": f"{self.base}/{self.VLAN_PATH}"},
            timeout=10,
        )
        r.raise_for_status()

    def set_8021q_enabled(self, enabled: bool) -> None:
        """WARNING, confirmed live: toggling this (in either direction)
        deletes all existing VLAN configuration -- that is this switch's own
        documented behavior, not a bug in this client."""
        self._post_vlan_form({
            "changeType": "1",
            "confirmOK": "1",
            "aDot1VLAN": "Enable" if enabled else "Disable",
        })

    def add_vlan(self, vid: int) -> None:
        if not (1 <= vid <= 4094):
            raise NetgearHttpError(f"VLAN id must be 1-4094, got {vid}")
        self._post_vlan_form({"addVid": str(vid)})

    def delete_vlan(self, vid: int) -> None:
        if vid == 1:
            raise NetgearHttpError("VLAN 1 cannot be deleted (switch-enforced)")
        self._post_vlan_form({"confirmOK": "1", "delVid": str(vid)})
