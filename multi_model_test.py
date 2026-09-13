#!/usr/bin/env python3
"""
Runs several adopt-test attempts back to back, each pretending to be a DIFFERENT
UniFi switch model, each under its own fake MAC suffix so the resulting log lines
in the controller's server.log can be told apart unambiguously afterwards.

WHY: the first clean test (real model "USWED72", the closest hardware match to
our D-Link) got rejected during L2 discovery itself with:
    <discover> DEBUG api    - Unknown model: USWED72
    <discover> DEBUG discover - ignore [...] unknown model : USWED72 / 7.5.15
even though "USWED72" IS a real, valid model name in the controller's own device
catalog (confirmed by reading the decompiled device-model enum). Working theory:
USWED72 is a member of Ubiquiti's newer "Enterprise XG" switch family, which may
simply not support/use the legacy L2 broadcast discovery protocol (UDP 10001,
the one this whole project's discovery packet is built for) -- newer devices may
adopt via a different mechanism entirely. Older, "classic" switch models almost
certainly DO support this legacy path since it's been in UniFi since early on.

This script tries the original model plus three alternates (all confirmed to be
real, currently-supported platforms per Ubiquiti's public fw-update API) to see
which ones the controller's discovery listener accepts vs rejects as "unknown
model". Whichever one gets furthest is the one to build the rest of the bridge
around -- if only the classic models work, that's a real, load-bearing
constraint: this D-Link switch must be impersonated as an older-generation
UniFi switch, not as the newest Enterprise-XG-24 despite it being the closest
capability match.

Usage (unchanged from standalone_adopt_test.py, run on the Unraid host or via
netshoot on the br0 macvlan network):
    docker run --rm --network br0 -v /tmp/multi_model_test.py:/t.py nicolaka/netshoot \\
        python3 /t.py --controller 192.168.1.10

After running, check the controller log for each MAC suffix, e.g.:
    docker exec unifi-controller sh -c \\
        "grep -n -A3 -B1 'aa:bb:cc:dd:ee:1[0-3]' /config/logs/server.log"

Each candidate's own MAC (last octet 0x10, 0x11, 0x12, 0x13) makes it trivial to
tell which model got which result without needing to match timestamps.
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

MAGIC = b"TNBU"
DEFAULT_KEY = hashlib.md5(b"ubnt").digest()


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(o, 16) for o in mac.replace("-", ":").split(":"))


def aes_128_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
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
    flags = 0x1
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
    body += _tlv(0x0C, model_code.encode("latin1"))
    body += _tlv(0x16, short_version.encode("latin1"))
    body += _tlv(0x15, model_code.encode("latin1"))
    body += _tlv(0x17, bytes([1]))
    header = bytes([2, 6, len(body) // 256, len(body) % 256])
    return header + body


# (mac last octet, sysid, model, version, description) -- all versions confirmed
# via Ubiquiti's public fw-update API to be real, currently-shipping firmware for
# that platform (queried 2026-09-13).
CANDIDATES = [
    ("10", 60786, "USWED72", "7.5.15", "USW-EnterpriseXG-24 (original pick, 10G, Realtek) -- known to fail discovery"),
    ("11", 60790, "USWED76", "7.5.15", "Same Enterprise-XG family, smaller 10-port sibling"),
    ("12", 60208, "US24",    "7.5.15", "Classic UniFi Switch 24 (Gen1, Broadcom, Gigabit-only)"),
    ("13", 60256, "US48",    "7.5.15", "Classic UniFi Switch 48 (Gen1, Broadcom, Gigabit-only)"),
]

BASE_MAC_PREFIX = "aa:bb:cc:dd:ee"


def run_one(mac_suffix: str, sysid: int, model: str, version: str, desc: str,
            switch_ip: str, controller: str, controller_port: int, broadcast: str) -> None:
    mac = f"{BASE_MAC_PREFIX}:{mac_suffix}"
    print(f"\n=== {model} (sysid={sysid}, v{version}) mac={mac} -- {desc} ===")

    packet = build_discovery_packet(
        mac=mac, ip=switch_ip, uptime_seconds=120,
        fw_build=f"{model}.rtl930x.v{version}.260908.0000",
        short_version=version, model_code=model,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, 10001))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        s.sendto(packet, ("233.89.188.1", 10001))
    print(f"sent discovery broadcast+multicast for {model}")

    time.sleep(2)

    payload = {
        "sysid": sysid,
        "model": model,
        "version": version,
        "mac": mac,
        "ip": switch_ip,
        "uptime": 120,
        "inform_url": f"http://{controller}:{controller_port}/inform",
        "state": 0,
    }
    wire = encode_inform(mac, payload)

    conn = http.client.HTTPConnection(controller, controller_port, timeout=10)
    conn.request("POST", "/inform", body=wire, headers={"Content-Type": "application/x-binary"})
    resp = conn.getresponse()
    body = resp.read()
    print(f"inform -> HTTP {resp.status}, body {len(body)} bytes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--switch-ip", default="10.90.90.90")
    ap.add_argument("--controller", default="192.168.1.10")
    ap.add_argument("--controller-port", type=int, default=8080)
    ap.add_argument("--broadcast", default="255.255.255.255")
    ap.add_argument("--gap", type=float, default=3.0, help="seconds to wait between candidates")
    args = ap.parse_args()

    for mac_suffix, sysid, model, version, desc in CANDIDATES:
        run_one(mac_suffix, sysid, model, version, desc,
                args.switch_ip, args.controller, args.controller_port, args.broadcast)
        time.sleep(args.gap)

    print("\nDone. Now check the controller log per candidate, e.g.:")
    print("  docker exec unifi-controller sh -c \"grep -n -A3 -B1 'aa:bb:cc:dd:ee:1[0-3]' /config/logs/server.log\"")


if __name__ == "__main__":
    main()
