from inform_protocol import InformPacket, encode, decode, DEFAULT_KEY, InformProtocolError

packet = InformPacket(
    mac="aa:bb:cc:dd:ee:ff",  # any MAC works for this self-test
    payload={"model": "DGS-1250-28X", "state": 1, "uptime": 12345},
)

wire = encode(packet)
print(f"encoded {len(wire)} bytes, header={wire[:40].hex()}")

decoded = decode(wire)
assert decoded.mac == packet.mac, (decoded.mac, packet.mac)
assert decoded.payload == packet.payload, (decoded.payload, packet.payload)
print("round-trip OK:", decoded)

# wrong key must fail loudly, not silently return garbage
wrong_key = bytes(range(16))
try:
    decode(wire, key=wrong_key)
    raise SystemExit("expected InformProtocolError for wrong key, got none")
except InformProtocolError as e:
    print("wrong-key rejection OK:", e)

# deterministic IV round-trip, for reproducible test vectors
fixed_iv = bytes(range(16))
wire2 = encode(packet, iv=fixed_iv)
decoded2 = decode(wire2)
assert decoded2.payload == packet.payload
print("fixed-IV round-trip OK")

print("ALL OK")
