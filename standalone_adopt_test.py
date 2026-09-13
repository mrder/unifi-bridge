#!/usr/bin/env python3
"""
Self-contained adopt-test script -- run this DIRECTLY on the Unraid host (or in a
container with --net host), not from the Windows dev machine. No dependencies
beyond the Python standard library.

Why: sending our discovery broadcast/multicast from the Windows laptop produced
zero packets on the wire even for a trivial, library-independent test send
(verified with a local Wireshark capture on the sending machine itself, both
Ethernet and Wi-Fi) -- something about that specific Windows machine is silently
swallowing outbound UDP broadcast/multicast before it ever reaches the network
card. Rather than debug a Windows networking quirk unrelated to the actual
project, this runs the exact same logic from Linux, on the same host as the
controller, sidestepping the question entirely.

Usage:
    python3 standalone_adopt_test.py --mac aa:bb:cc:dd:ee:00 --switch-ip 10.90.90.90 \\
        --controller 127.0.0.1

Or via a throwaway container (no local Python install needed), from the Unraid
host shell:
    docker run --rm --net host -v /path/to/this/file.py:/t.py python:3-slim \\
        python3 /t.py --mac aa:bb:cc:dd:ee:00 --switch-ip 10.90.90.90 --controller 127.0.0.1
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import subprocess
import time

# ---- inform envelope (see north_adapter/inform_protocol.py for the full,
# tested version -- this trimmed copy shells out to the `openssl` CLI for AES
# instead of the `cryptography` package, since that's what's actually available
# in the netshoot image this runs in without installing anything new) ----

MAGIC = b"TNBU"
DEFAULT_KEY = hashlib.md5(b"ubnt").digest()


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(o, 16) for o in mac.replace("-", ":").split(":"))


def aes_128_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    """Shells out to `openssl enc`. Its default PKCS7 padding for `enc` matches
    what the controller expects (confirmed against controller source -- see
    north_adapter/inform_protocol.py's docstring), so no extra padding flags needed."""
    result = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex(), "-nosalt"],
        input=plaintext,
        capture_output=True,
        check=True,
    )
    return result.stdout


def encode_inform(mac: str, payload: dict, key: bytes = DEFAULT_KEY) -> bytes:
    iv = os.urandom(16)
    plaintext = json.dumps(payload).encode("utf-8")
    ciphertext = aes_128_cbc_encrypt(plaintext, key, iv)
    flags = 0x1  # encrypted, CBC, uncompressed
    header = (
        MAGIC
        + (1).to_bytes(4, "big")
        + mac_to_bytes(mac)
        + flags.to_bytes(2, "big")
        + iv
        + (1).to_bytes(4, "big")
        + len(ciphertext).to_bytes(4, "big")
    )
    return header + ciphertext


# ---- discovery packet (see north_adapter/discovery_protocol.py for the full,
# documented version -- same trimmed-inline approach) ----

def _tlv(tlv_type: int, value: bytes) -> bytes:
    return bytes([tlv_type, len(value) // 256, len(value) % 256]) + value


def build_discovery_packet(mac: str, ip: str, uptime_seconds: int, fw_build: str,
                            short_version: str, model_code: str, sequence: int = 500) -> bytes:
    mac_bytes = mac_to_bytes(mac)
    ip_bytes = bytes(int(o) for o in ip.split("."))
    body = b""
    body += _tlv(0x12, sequence.to_bytes(4, "big"))
    body += _tlv(0x13, mac_bytes)
    body += _tlv(0x02, mac_bytes + ip_bytes)
    body += _tlv(0x01, mac_bytes)
    body += _tlv(0x0A, uptime_seconds.to_bytes(4, "big"))
    body += _tlv(0x03, fw_build.encode("latin1"))
    body += _tlv(0x0C, model_code.encode("latin1"))  # platform -- the type-12 fix
    body += _tlv(0x16, short_version.encode("latin1"))
    body += _tlv(0x15, model_code.encode("latin1"))
    body += _tlv(0x17, bytes([1]))
    header = bytes([2, 6, len(body) // 256, len(body) % 256])
    return header + body


# Real, currently-released firmware version for USWED72 per Ubiquiti's public
# fw-update.ui.com API (queried directly, see scratchpad/uswed72_latest.json) --
# previous attempts used a guessed "6.6.77", which is a full major version below
# the real v7.x line and very likely why DXufmCC's version-compatibility check
# ("unable to manage dev with model[...]") rejected every prior test regardless
# of transport/encryption correctness.
FAKE_SYSID = 60786
FAKE_MODEL = "USWED72"
FAKE_VERSION = "7.5.15"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", required=True, help="the D-Link switch's real MAC, e.g. aa:bb:cc:dd:ee:00")
    ap.add_argument("--switch-ip", required=True, help="the D-Link switch's IP, e.g. 10.90.90.90")
    ap.add_argument("--controller", default="127.0.0.1", help="controller host (default: 127.0.0.1, since this runs on the same host)")
    ap.add_argument("--controller-port", type=int, default=8080)
    ap.add_argument("--broadcast", default="255.255.255.255")
    ap.add_argument("--sysid", type=int, default=FAKE_SYSID, help="device model sysid to claim (default: USWED72's real sysid, 60786)")
    ap.add_argument("--model", default=FAKE_MODEL, help="device model string to claim (default: USWED72)")
    ap.add_argument("--version", default=FAKE_VERSION, help="firmware version string to claim (default: the real current USWED72 release, 7.5.15)")
    args = ap.parse_args()

    packet = build_discovery_packet(
        mac=args.mac, ip=args.switch_ip, uptime_seconds=120,
        fw_build=f"{args.model}.rtl930x.v{args.version}.260908.0000",
        short_version=args.version, model_code=args.model,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (args.broadcast, 10001))
        print(f"sent discovery broadcast to {args.broadcast}:10001 ({len(packet)} bytes)")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        s.sendto(packet, ("233.89.188.1", 10001))
        print("sent discovery multicast to 233.89.188.1:10001")

    time.sleep(2)

    payload = {
        "sysid": args.sysid,
        "model": args.model,
        "version": args.version,
        "mac": args.mac,
        "ip": args.switch_ip,
        "uptime": 120,
        "inform_url": f"http://{args.controller}:{args.controller_port}/inform",
        "state": 0,
    }
    wire = encode_inform(args.mac, payload)

    conn = http.client.HTTPConnection(args.controller, args.controller_port, timeout=10)
    conn.request("POST", "/inform", body=wire, headers={"Content-Type": "application/x-binary"})
    resp = conn.getresponse()
    body = resp.read()
    print(f"first-contact inform -> {args.controller}:{args.controller_port}: HTTP {resp.status}")
    print(f"response headers: {dict(resp.getheaders())}")
    print(f"response body ({len(body)} bytes): {body[:200]!r}")


if __name__ == "__main__":
    main()
