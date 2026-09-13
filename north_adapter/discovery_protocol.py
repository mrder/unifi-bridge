"""
UBNT L2 discovery packet (the broadcast that makes a fresh device show up as
"pending adoption" in the UniFi app) -- encode only.

Reverse-engineered from the controller's own bundled reference implementation
(com.a.a.a.UWKqI -- the packet builder -- and com.ubnt.net.ZuidRsTtQxRYeBk, a
literal test main() in the controller jar that builds and broadcasts one). This
is the older, well-known "ubnt-discover" TLV protocol (predates the HTTP inform
protocol and still runs alongside it) -- several independent public
reimplementations of it already exist, our version here matches the controller's
own packet builder byte-for-byte rather than a third party's guess.

Wire format:
  offset 0    u8   version (2 in the "extended"/TLV style used here)
  offset 1    u8   command/type (6 in the reference sample -- meaning not confirmed,
                   copied as-is since it's what the controller's own code sends)
  offset 2-3  u16  body length, big-endian, NOT including this 4-byte header
  offset 4+   TLV* type(1) + length(2, big-endian) + value(length bytes)

If version==2, the builder automatically prepends two TLVs before any caller-added
ones: type 0x12 (a locally-incrementing sequence number, 4-byte BE int -- appears
to just need to be *a* number, not a specific one) and type 0x13 (raw 6-byte MAC,
duplicating the type 0x01 field below -- also copied as-is from the reference).

TLV types used by the reference sample (values confirmed working against real
Ubiquiti gear per the controller's own bundled code; semantics inferred from
public "ubnt-discover" protocol writeups, not from controller source comments
since obfuscation strips those):
  0x01  6 bytes   MAC address
  0x02  10 bytes  MAC address (6) + IPv4 address (4)
  0x03  string    firmware "build" identifier (e.g. "BZ.ar7240.v3.1.0.15.150311.1401")
  0x0A  4 bytes   uptime in seconds, big-endian
  0x15  string    short model/platform code (e.g. "BZ2")
  0x16  string    short version string (e.g. "3.1.0")
  0x17  1 byte    flag, 1 in the reference sample (meaning unconfirmed --
                  possibly "not yet adopted" / "using default credentials")

Sent as a UDP broadcast to 255.255.255.255:10001 (the controller also listens on
multicast 233.89.188.1:10001, not implemented here -- broadcast is simpler and is
what the controller's own sample does).

UNVERIFIED: we have no real UniFi switch to confirm the model/version strings a
real USW sends, or whether switches include additional TLV types beyond what this
AP-era reference sample shows (e.g. a numeric board/sysid field, matching the
`sysid` int used by the HTTP inform path). This is the actual open question the
live test against a real controller is meant to answer.
"""

from __future__ import annotations

import socket

DISCOVERY_PORT = 10001
BROADCAST_ADDR = "255.255.255.255"
MULTICAST_ADDR = "233.89.188.1"  # confirmed from controller source (com.a.a.a.cZldsW):
# the discovery listener binds BOTH a broadcast/unicast channel per interface AND
# joins this multicast group on the same port. Which one(s) are actually active in
# a given deployment isn't knowable from outside, so send to both.


def _tlv(tlv_type: int, value: bytes) -> bytes:
    if len(value) > 0xFFFF:
        raise ValueError("TLV value too long")
    return bytes([tlv_type, len(value) // 256, len(value) % 256]) + value


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(o, 16) for o in mac.replace("-", ":").split(":"))


def ip_to_bytes(ip: str) -> bytes:
    return bytes(int(o) for o in ip.split("."))


def build_discovery_packet(
    mac: str,
    ip: str,
    uptime_seconds: int,
    fw_build: str,
    short_version: str,
    model_code: str,
    sequence: int = 500,
    version: int = 2,
    command: int = 6,
) -> bytes:
    mac_bytes = mac_to_bytes(mac)
    ip_bytes = ip_to_bytes(ip)

    body = b""
    if version == 2:
        body += _tlv(0x12, sequence.to_bytes(4, "big"))
        body += _tlv(0x13, mac_bytes)
    body += _tlv(0x02, mac_bytes + ip_bytes)
    body += _tlv(0x01, mac_bytes)
    body += _tlv(0x0A, uptime_seconds.to_bytes(4, "big"))
    body += _tlv(0x03, fw_build.encode("latin1"))
    body += _tlv(0x0C, model_code.encode("latin1"))  # "platform" -- confirmed from the
    # controller's own parser (com.a.a.a.ixsXr): case 12 is explicitly logged as
    # "platform: '{}'". Our first attempt used 0x15 instead (copied uncritically from
    # the AP-era reference sample), which the parser actually treats as an ESSID-like
    # field used only to filter out legacy AirMax devices -- never seen as "platform"
    # anywhere in the parser. This was a real bug in the first attempt, not just an
    # unconfirmed guess.
    body += _tlv(0x16, short_version.encode("latin1"))
    body += _tlv(0x15, model_code.encode("latin1"))
    body += _tlv(0x17, bytes([1]))

    header = bytes([version, command, len(body) // 256, len(body) % 256])
    return header + body


def send_discovery_broadcast(packet: bytes, broadcast_addr: str = BROADCAST_ADDR, port: int = DISCOVERY_PORT,
                              bind_ip: str | None = None) -> None:
    """`broadcast_addr` defaults to the global 255.255.255.255, but on a machine
    with multiple active network interfaces (e.g. Wi-Fi to the controller's LAN
    *and* Ethernet to an isolated switch subnet, as in this project) the OS's
    choice of outbound interface for that address isn't guaranteed. Pass the
    controller subnet's actual directed broadcast address (e.g. 192.168.1.255)
    to make sure it goes out the right interface.

    `bind_ip`: force the packet out a specific local interface (e.g. the wired
    Ethernet address instead of Wi-Fi) by binding the socket to it before
    sending. Confirmed necessary in practice: a tcpdump on the controller host
    showed zero packets arriving when this was sent from a Wi-Fi-connected
    laptop, consistent with the router doing client/AP isolation or blocking
    broadcast-forwarding from wireless clients -- normal unicast (the /inform
    HTTP POST) reached the same host fine over the same Wi-Fi link, so this is
    specifically a broadcast/multicast forwarding gap, not a general
    connectivity problem."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if bind_ip:
            sock.bind((bind_ip, 0))
        sock.sendto(packet, (broadcast_addr, port))


def send_discovery_multicast(packet: bytes, ttl: int = 4, port: int = DISCOVERY_PORT,
                              bind_ip: str | None = None) -> None:
    """Send to the controller's multicast group instead of/in addition to broadcast.
    `bind_ip` picks the outbound interface for the multicast packet the same way
    as `send_discovery_broadcast` -- see that function's docstring for why this
    matters in practice, not just in theory."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        if bind_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
            sock.bind((bind_ip, 0))
        sock.sendto(packet, (MULTICAST_ADDR, port))
