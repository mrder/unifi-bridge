"""
Telnet-based CLI driver for the D-Link DGS-1250 series (tested against DGS-1250-28X,
firmware Build 1.00.040).

Python 3.13 removed the stdlib `telnetlib` module, so this talks to the switch over a
plain TCP socket and strips the terminal control sequences the switch CLI sends back
(cursor moves, backspace-redraw on login) rather than doing real telnet IAC negotiation.
That works because the D-Link CLI here doesn't require telnet option negotiation to
reach a usable prompt -- confirmed by hand against the real device.

Command syntax is taken from the official "DGS-1250 Series CLI Reference Guide v2.03".
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass


_ANSI_ESCAPE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")
_BACKSPACE_RUN = re.compile(rb"\x08+")


@dataclass
class PortStatus:
    port: str
    status: str
    vlan: str
    duplex: str
    speed: str
    type: str


class DLinkCLIError(Exception):
    pass


class DLinkCLI:
    def __init__(self, host: str, username: str = "admin", password: str = "admin",
                 port: int = 23, timeout: float = 8.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None

    # -- low level -----------------------------------------------------

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._read_until("Username:")
        self._send(self.username)
        self._read_until("Password:")
        self._send(self.password)
        banner = self._read_available(wait=1.0)
        if "#" not in banner:
            raise DLinkCLIError(f"Login failed, unexpected response: {banner!r}")
        # Disable pagination ("--More--" prompts) for this session so multi-page
        # output (e.g. `show vlan` with many entries) doesn't stall the reader.
        self.command("terminal length 0")

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "DLinkCLI":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _send(self, line: str) -> None:
        assert self._sock is not None, "not connected"
        self._sock.sendall(line.encode("ascii") + b"\r\n")

    def _read_available(self, wait: float = 0.6) -> str:
        assert self._sock is not None, "not connected"
        time.sleep(wait)
        self._sock.setblocking(False)
        chunks = []
        try:
            while True:
                data = self._sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except BlockingIOError:
            pass
        finally:
            self._sock.setblocking(True)
        raw = b"".join(chunks)
        raw = _ANSI_ESCAPE.sub(b"", raw)
        raw = _BACKSPACE_RUN.sub(b"", raw)
        return raw.decode("ascii", errors="ignore")

    def _read_until(self, marker: str, timeout: float = 5.0) -> str:
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            buf += self._read_available(wait=0.2)
            if marker in buf:
                return buf
        raise TimeoutError(f"Timed out waiting for {marker!r}; got so far: {buf!r}")

    def command(self, cmd: str, wait: float = 0.7) -> str:
        """Send one line and return whatever came back (echo included, caller can ignore)."""
        self._send(cmd)
        return self._read_available(wait=wait)

    # -- read-only queries ----------------------------------------------

    def show_vlan_raw(self) -> str:
        return self.command("show vlan", wait=1.0)

    def show_interfaces_status(self) -> list[PortStatus]:
        raw = self.command("show interfaces status", wait=1.0)
        return self._parse_interfaces_status(raw)

    @staticmethod
    def _parse_interfaces_status(raw: str) -> list[PortStatus]:
        results: list[PortStatus] = []
        for line in raw.splitlines():
            m = re.match(
                r"\s*(eth\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$",
                line,
            )
            if m:
                results.append(PortStatus(*m.groups()))
        return results

    def show_interfaces_utilization_raw(self) -> str:
        return self.command("show interfaces utilization", wait=1.0)

    def show_version_raw(self) -> str:
        return self.command("show version", wait=1.0)

    # -- config-changing calls (untested against production traffic --
    #    review before running against a switch carrying live traffic!) --

    def create_vlan(self, vlan_id: int, name: str | None = None) -> None:
        self.command("configure terminal")
        self.command(f"vlan {vlan_id}")
        if name:
            self.command(f"name {name}")
        self.command("exit")
        self.command("exit")

    def set_access_port(self, interface_id: str, vlan_id: int) -> None:
        self.command("configure terminal")
        self.command(f"interface {interface_id}")
        self.command("switchport mode access")
        self.command(f"switchport access vlan {vlan_id}")
        self.command("exit")
        self.command("exit")

    def set_trunk_port(self, interface_id: str, native_vlan: int, allowed_vlans: str) -> None:
        """allowed_vlans e.g. '10,20,30-40' per CLI guide syntax."""
        self.command("configure terminal")
        self.command(f"interface {interface_id}")
        self.command("switchport mode trunk")
        self.command(f"switchport trunk native vlan {native_vlan}")
        self.command(f"switchport trunk allowed vlan add {allowed_vlans}")
        self.command("exit")
        self.command("exit")

    def save(self) -> str:
        self._send("copy running-config startup-config")
        time.sleep(0.4)
        self._send("y")
        return self._read_available(wait=2.0)
