#!/usr/bin/env python3
"""
Second-stage adoption test: sends another inform for an already-"Adopt"-clicked
pending device and DECODES the response instead of just printing raw bytes.

Context: after clicking "Use"/"Adopt" in the UniFi UI on a pending device, the
controller marks it as adopting and, on the device's *next* inform, replies with
a "setparam" command carrying "mgmt_cfg" -- this is the actual adoption payload
(new auth key + config). A real device would switch to using that new key for
all future informs. This script sends one more inform (still encrypted with the
default key, since our fake device hasn't "accepted" anything yet) and decrypts
whatever comes back with the default key so we can see the mgmt_cfg content.

Usage:
    docker run --rm --network br0 -v /tmp/adopt_handshake_test.py:/t.py nicolaka/netshoot \\
        python3 /t.py --mac aa:bb:cc:dd:ee:20 --switch-ip 10.90.90.90 \\
        --controller 192.168.1.10 --sysid 60201 --model USXG24 --version 7.5.15
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import subprocess

MAGIC = b"TNBU"
DEFAULT_KEY = hashlib.md5(b"ubnt").digest()


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(o, 16) for o in mac.replace("-", ":").split(":"))


def aes_128_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    result = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex(), "-nosalt"],
        input=plaintext, capture_output=True, check=True,
    )
    return result.stdout


def aes_128_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    result = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex(), "-nosalt"],
        input=ciphertext, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"decrypt failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def encode_inform(mac: str, payload: dict, key: bytes = DEFAULT_KEY) -> bytes:
    iv = os.urandom(16)
    plaintext = json.dumps(payload).encode("utf-8")
    ciphertext = aes_128_cbc_encrypt(plaintext, key, iv)
    flags = 0x1
    header = (
        MAGIC + (1).to_bytes(4, "big") + mac_to_bytes(mac)
        + flags.to_bytes(2, "big") + iv
        + (1).to_bytes(4, "big") + len(ciphertext).to_bytes(4, "big")
    )
    return header + ciphertext


def decode_inform(wire: bytes, key: bytes = DEFAULT_KEY) -> dict:
    if wire[0:4] != MAGIC:
        raise ValueError(f"bad magic: {wire[0:4]!r}")
    mac = wire[8:14]
    flags = int.from_bytes(wire[14:16], "big")
    iv = wire[16:32]
    data_len = int.from_bytes(wire[36:40], "big")
    ciphertext = wire[40:40 + data_len]
    encrypted = flags & 0x1
    if not encrypted:
        return json.loads(ciphertext.decode("utf-8"))
    # openssl's `-d` already strips PKCS7 padding by default -- no manual unpad needed
    plaintext = aes_128_cbc_decrypt(ciphertext, key, iv)
    return json.loads(plaintext.decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", required=True)
    ap.add_argument("--switch-ip", required=True)
    ap.add_argument("--controller", default="192.168.1.10")
    ap.add_argument("--controller-port", type=int, default=8080)
    ap.add_argument("--sysid", type=int, required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--version", required=True)
    args = ap.parse_args()

    payload = {
        "sysid": args.sysid,
        "model": args.model,
        "version": args.version,
        "mac": args.mac,
        "ip": args.switch_ip,
        "uptime": 300,
        "inform_url": f"http://{args.controller}:{args.controller_port}/inform",
        "state": 0,
    }
    wire = encode_inform(args.mac, payload)

    conn = http.client.HTTPConnection(args.controller, args.controller_port, timeout=10)
    conn.request("POST", "/inform", body=wire, headers={"Content-Type": "application/x-binary"})
    resp = conn.getresponse()
    body = resp.read()
    print(f"inform -> HTTP {resp.status}, {len(body)} bytes")

    if not body:
        print("empty body -- nothing to decode (still 404/not-yet-adopting? check the UI status)")
        return

    try:
        decoded = decode_inform(body, key=DEFAULT_KEY)
        print("decoded response payload:")
        print(json.dumps(decoded, indent=2))
    except Exception as e:
        print(f"could not decode with default key: {e}")
        print(f"raw body (first 300 bytes): {body[:300]!r}")


if __name__ == "__main__":
    main()
