"""
UniFi "inform" packet envelope -- encode/decode only, no networking here.

Wire format (all integers big-endian), per two independent public reverse-engineering
write-ups (fxkr/unifi-protocol-reverse-engineering, jrjparks unofficial UniFi guide) --
NOT verified yet against real captured traffic, see README:

  offset  size  field
  0-3     u32   magic b"TNBU"
  4-7     u32   packet version (1)
  8-13    6B    device MAC address (raw bytes, not the "aa:bb:.." string)
  14-15   u16   flags: bit0=encrypted, bit1=zlib compressed, bit2=snappy compressed,
                       bit0+bit3 together = AES-GCM instead of the bit0-only AES-CBC
  16-31   16B   IV
  32-35   u32   payload version (1)
  36-39   u32   payload length
  40+     bytes payload (compressed, then encrypted; JSON once decrypted+decompressed)

Default (pre-adoption) key is MD5(b"ubnt") used directly as a 16-byte AES-128 key --
that's the one implemented here. A real adopted device's actual key lives in that
device's own /etc/persistent/cfg/mgmt (mgmt.authkey); this module takes any 16-byte
key, so swapping in a real one later is just a constructor argument.

Both AES-CBC (flags bit0 set, bit3 clear -- used for the default-key, pre-adoption
handshake) and AES-GCM (bit0+bit3 -- required by the controller for every inform
once a device has been adopted and issued a real authkey) are implemented.

GCM specifics, confirmed by reading the controller's own crypto helper class
(com.a.a.hEiVLGSwaf, decompiled from a real UniFi Network Application build) rather
than guessed from public write-ups, which all describe the CBC-only pre-adoption
path and don't cover this:
  - nonce is the same 16-byte field used as the IV in the CBC framing (NOT the usual
    12-byte GCM nonce)
  - AAD is the full 40-byte header AS TRANSMITTED (with the real payload length
    already filled in), not just the MAC -- GCM has no padding so the ciphertext
    length is known before encrypting, making this buildable up front
  - tag is 16 bytes, appended after the ciphertext (both on the wire and in what
    `cryptography`'s AESGCM.encrypt()/.decrypt() produce/expect)

Compression is NOT implemented (flag bit1 always 0 on encode; decode raises if it
sees a compressed packet). Real devices support sending it uncompressed -- the
`compressed` flag is something the sender chooses, not something the receiver
demands -- so this is a deliberate scope cut, not a protocol violation. Add zlib
decompress/compress here if a real controller ever proves to require it.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TNBU"
PACKET_VERSION = 1
PAYLOAD_VERSION = 1

FLAG_ENCRYPTED = 0x1
FLAG_ZLIB = 0x2
FLAG_SNAPPY = 0x4
FLAG_GCM = 0x8

DEFAULT_KEY = hashlib.md5(b"ubnt").digest()  # ba86f2bbe107c7c57eb5f2690775c712

HEADER_LEN = 40


class InformProtocolError(Exception):
    pass


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(o, 16) for o in mac.replace("-", ":").split(":"))


def mac_to_str(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


@dataclass
class InformPacket:
    mac: str
    payload: dict
    packet_version: int = PACKET_VERSION
    payload_version: int = PAYLOAD_VERSION


def _pack_header(mac: bytes, flags: int, iv: bytes, payload_len: int) -> bytes:
    assert len(mac) == 6
    assert len(iv) == 16
    return (
        MAGIC
        + struct.pack(">I", PACKET_VERSION)
        + mac
        + struct.pack(">H", flags)
        + iv
        + struct.pack(">I", PAYLOAD_VERSION)
        + struct.pack(">I", payload_len)
    )


def _unpack_header(data: bytes) -> tuple[int, bytes, int, bytes, int, int]:
    if len(data) < HEADER_LEN:
        raise InformProtocolError(f"packet too short for header: {len(data)} bytes")
    magic = data[0:4]
    if magic != MAGIC:
        raise InformProtocolError(f"bad magic {magic!r}, expected {MAGIC!r}")
    packet_version = struct.unpack(">I", data[4:8])[0]
    mac = data[8:14]
    flags = struct.unpack(">H", data[14:16])[0]
    iv = data[16:32]
    payload_version = struct.unpack(">I", data[32:36])[0]
    payload_len = struct.unpack(">I", data[36:40])[0]
    return packet_version, mac, flags, iv, payload_version, payload_len


def encode(packet: InformPacket, key: bytes = DEFAULT_KEY, iv: bytes | None = None, gcm: bool = False) -> bytes:
    """Build a wire-format inform packet. `iv` is exposed only for deterministic tests --
    real callers should leave it None (os.urandom(16) is used). Set `gcm=True` for any
    inform sent with a real (post-adoption) authkey -- the controller requires it and
    will reject a CBC-encrypted inform under a non-default key as a "downgrade"."""
    if iv is None:
        import os

        iv = os.urandom(16)
    if len(key) != 16:
        raise InformProtocolError(f"key must be 16 bytes for AES-128, got {len(key)}")

    plaintext = json.dumps(packet.payload).encode("utf-8")
    mac_bytes = mac_to_bytes(packet.mac)

    if gcm:
        # AAD = the full header, so it must be built with the real (already-known,
        # since GCM adds no padding) payload length before encrypting.
        payload_len = len(plaintext) + 16  # +16 for the appended GCM tag
        flags = FLAG_ENCRYPTED | FLAG_GCM
        header = _pack_header(mac_bytes, flags, iv, payload_len)
        ciphertext = AESGCM(key).encrypt(iv, plaintext, header)
        return header + ciphertext

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    flags = FLAG_ENCRYPTED
    header = _pack_header(mac_bytes, flags, iv, len(ciphertext))
    return header + ciphertext


def decode(data: bytes, key: bytes = DEFAULT_KEY) -> InformPacket:
    packet_version, mac, flags, iv, payload_version, payload_len = _unpack_header(data)

    if not (flags & FLAG_ENCRYPTED):
        raise InformProtocolError("unencrypted packets are not implemented")
    if flags & (FLAG_ZLIB | FLAG_SNAPPY):
        raise InformProtocolError("compressed packets are not implemented")

    ciphertext = data[HEADER_LEN : HEADER_LEN + payload_len]
    if len(ciphertext) != payload_len:
        raise InformProtocolError(
            f"declared payload length {payload_len} but only {len(ciphertext)} bytes present"
        )

    if flags & FLAG_GCM:
        header = data[0:HEADER_LEN]
        try:
            plaintext = AESGCM(key).decrypt(iv, ciphertext, header)
        except Exception as e:
            raise InformProtocolError(
                "AES-GCM authentication failed -- almost always means the key is wrong"
            ) from e
    else:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        try:
            plaintext = unpadder.update(padded) + unpadder.finalize()
        except ValueError as e:
            raise InformProtocolError(
                "PKCS7 unpad failed -- almost always means the key is wrong "
                "(default key only works for an unadopted device)"
            ) from e

    payload = json.loads(plaintext.decode("utf-8"))
    return InformPacket(
        mac=mac_to_str(mac),
        payload=payload,
        packet_version=packet_version,
        payload_version=payload_version,
    )
