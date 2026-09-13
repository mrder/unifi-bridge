#!/usr/bin/env python3
"""
Sends the adoption-confirming inform: encrypted with the NEW authkey the
controller handed us in its "setparam"/mgmt_cfg response, using AES-128-GCM
(not CBC) since the controller's mgmt_cfg said "use_aes_gcm=true".

Uses OpenSSL's EVP API directly via ctypes (libcrypto, already present
wherever the `openssl` CLI is installed -- nothing new to install) because the
plain `openssl enc` command cannot supply AAD or a non-standard 16-byte GCM
nonce, both of which this protocol requires (confirmed by reading the
controller's own crypto helper class): AAD = the 6-byte device MAC, nonce =
16 bytes (not the usual 12), tag = 16 bytes appended after the ciphertext.
Verified independently against the official NIST AES-GCM test vector and
cross-checked byte-for-byte against the Python `cryptography` package before
ever touching the real controller.

Usage:
    docker run --rm --network br0 -v /tmp/adopt_confirm.py:/t.py nicolaka/netshoot \\
        python3 /t.py --mac aa:bb:cc:dd:ee:20 --switch-ip 10.90.90.90 \\
        --controller 192.168.1.10 --sysid 60201 --model USXG24 --version 7.5.15 \\
        --authkey a1752b4826770fb0aa3378d3eaa01ec6
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import platform


def _load_libcrypto():
    if platform.system() == "Windows":
        for candidate in (r"C:\Program Files\Git\mingw64\bin\libcrypto-3-x64.dll",
                          "libcrypto-3-x64.dll", "libcrypto-1_1-x64.dll"):
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                continue
        raise OSError("libcrypto DLL not found")
    for name in ("libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OSError("libcrypto not found")


_lib = _load_libcrypto()
_lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
_lib.EVP_aes_128_gcm.restype = ctypes.c_void_p
_lib.EVP_EncryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_lib.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_lib.EVP_CIPHER_CTX_ctrl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_lib.EVP_EncryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_int]
_lib.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_int]
_lib.EVP_EncryptFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_lib.EVP_DecryptFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]

EVP_CTRL_GCM_SET_IVLEN = 0x9
EVP_CTRL_GCM_GET_TAG = 0x10
EVP_CTRL_GCM_SET_TAG = 0x11
TAG_LEN = 16


def gcm_encrypt(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    ctx = _lib.EVP_CIPHER_CTX_new()
    try:
        _lib.EVP_EncryptInit_ex(ctx, _lib.EVP_aes_128_gcm(), None, None, None)
        _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, len(nonce), None)
        _lib.EVP_EncryptInit_ex(ctx, None, None, key, nonce)
        outlen = ctypes.c_int(0)
        if aad:
            _lib.EVP_EncryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad))
        buf = ctypes.create_string_buffer(len(plaintext) + 16)
        total = 0
        _lib.EVP_EncryptUpdate(ctx, buf, ctypes.byref(outlen), plaintext, len(plaintext))
        total += outlen.value
        finlen = ctypes.c_int(0)
        _lib.EVP_EncryptFinal_ex(ctx, ctypes.byref(buf, total), ctypes.byref(finlen))
        total += finlen.value
        tag = ctypes.create_string_buffer(TAG_LEN)
        _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_GET_TAG, TAG_LEN, tag)
        return buf.raw[:total] + tag.raw
    finally:
        _lib.EVP_CIPHER_CTX_free(ctx)


def gcm_decrypt(key: bytes, nonce: bytes, aad: bytes, ciphertext_with_tag: bytes) -> bytes:
    ciphertext, tag = ciphertext_with_tag[:-TAG_LEN], ciphertext_with_tag[-TAG_LEN:]
    ctx = _lib.EVP_CIPHER_CTX_new()
    try:
        _lib.EVP_DecryptInit_ex(ctx, _lib.EVP_aes_128_gcm(), None, None, None)
        _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, len(nonce), None)
        _lib.EVP_DecryptInit_ex(ctx, None, None, key, nonce)
        outlen = ctypes.c_int(0)
        if aad:
            _lib.EVP_DecryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad))
        buf = ctypes.create_string_buffer(len(ciphertext) + 16)
        total = 0
        _lib.EVP_DecryptUpdate(ctx, buf, ctypes.byref(outlen), ciphertext, len(ciphertext))
        total += outlen.value
        _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_TAG, TAG_LEN, tag)
        finlen = ctypes.c_int(0)
        if _lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(buf, total), ctypes.byref(finlen)) != 1:
            raise ValueError("GCM auth failed")
        total += finlen.value
        return buf.raw[:total]
    finally:
        _lib.EVP_CIPHER_CTX_free(ctx)


MAGIC = b"TNBU"


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(o, 16) for o in mac.replace("-", ":").split(":"))


def encode_inform_gcm(mac: str, payload: dict, key: bytes) -> bytes:
    # AAD is the FULL 40-byte header (as transmitted), not just the MAC -- confirmed
    # by reading the controller's gbVbgGNOm.pOkagEOMjOTpAE() method, which serializes
    # magic+dataversion+hwaddr+flags+iv+dataversion+datalen (with an empty data
    # section) and passes THAT as AAD. GCM has no padding, so datalen (=len(plaintext)+16
    # for the appended tag) is known before encrypting, letting us build the real
    # header up front and use it as AAD.
    mac_bytes = mac_to_bytes(mac)
    nonce = os.urandom(16)
    plaintext = json.dumps(payload).encode("utf-8")
    data_len = len(plaintext) + TAG_LEN
    flags = 0x1 | 0x8  # encrypted (bit0) + GCM (bit3)
    header = (
        MAGIC + (1).to_bytes(4, "big") + mac_bytes
        + flags.to_bytes(2, "big") + nonce
        + (1).to_bytes(4, "big") + data_len.to_bytes(4, "big")
    )
    ciphertext = gcm_encrypt(key, nonce, header, plaintext)  # AAD = full header
    return header + ciphertext


def decode_inform_gcm(wire: bytes, key: bytes) -> dict:
    if wire[0:4] != MAGIC:
        raise ValueError(f"bad magic: {wire[0:4]!r}")
    header = wire[0:40]
    flags = int.from_bytes(wire[14:16], "big")
    nonce = wire[16:32]
    data_len = int.from_bytes(wire[36:40], "big")
    ciphertext = wire[40:40 + data_len]
    if not (flags & 0x1):
        return json.loads(ciphertext.decode("utf-8"))
    is_gcm = bool(flags & 0x8)
    if is_gcm:
        plaintext = gcm_decrypt(key, nonce, header, ciphertext)  # AAD = full header
    else:
        raise ValueError("response is CBC, not GCM -- unexpected for this script")
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
    ap.add_argument("--authkey", required=True, help="the new authkey from the controller's setparam/mgmt_cfg response (hex)")
    args = ap.parse_args()

    key = bytes.fromhex(args.authkey)
    if len(key) != 16:
        raise SystemExit(f"authkey must decode to 16 bytes, got {len(key)}")

    payload = {
        "sysid": args.sysid,
        "model": args.model,
        "version": args.version,
        "mac": args.mac,
        "ip": args.switch_ip,
        "uptime": 600,
        "inform_url": f"http://{args.controller}:{args.controller_port}/inform",
        "state": 1,  # 1 = CONNECTED/managed, per the AP-protocol docs (0 was "not yet adopted")
    }
    wire = encode_inform_gcm(args.mac, payload, key)

    conn = http.client.HTTPConnection(args.controller, args.controller_port, timeout=10)
    conn.request("POST", "/inform", body=wire, headers={"Content-Type": "application/x-binary"})
    resp = conn.getresponse()
    body = resp.read()
    print(f"inform -> HTTP {resp.status}, {len(body)} bytes")

    if not body:
        print("empty body (this can be a normal ack for a routine inform once adopted)")
        return

    try:
        decoded = decode_inform_gcm(body, key)
        print("decoded response payload:")
        print(json.dumps(decoded, indent=2))
    except Exception as e:
        print(f"could not decode response: {e}")
        print(f"raw body (first 300 bytes): {body[:300]!r}")


if __name__ == "__main__":
    main()
