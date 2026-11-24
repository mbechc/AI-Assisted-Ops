"""Minimal MaxMind DB (MMDB v2) writer for IPv4 networks.

Used by tests, the mock Catalyst Center (to produce a sample database matching its
estate) and by operators who want to turn a CSV of subnet->location into an .mmdb.
"""
from __future__ import annotations

import ipaddress
import struct
import time

_SEP = b"\x00" * 16
_META = b"\xab\xcd\xefMaxMind.com"


def _ctrl(type_id: int, size: int) -> bytes:
    if size < 29:
        sbits, ext = size, b""
    elif size < 285:
        sbits, ext = 29, bytes([size - 29])
    elif size < 65821:
        sbits, ext = 30, struct.pack(">H", size - 285)
    else:
        sbits, ext = 31, struct.pack(">I", size - 65821)[1:]
    if type_id <= 7:
        return bytes([(type_id << 5) | sbits]) + ext
    return bytes([sbits, type_id - 7]) + ext


def _uint(type_id: int, v: int) -> bytes:
    b = v.to_bytes((v.bit_length() + 7) // 8, "big") if v else b""
    return _ctrl(type_id, len(b)) + b


def encode(v) -> bytes:
    if isinstance(v, bool):
        return _ctrl(14, 1 if v else 0)
    if isinstance(v, str):
        b = v.encode()
        return _ctrl(2, len(b)) + b
    if isinstance(v, float):
        return _ctrl(3, 8) + struct.pack(">d", v)
    if isinstance(v, int):
        if v < 0:
            return _ctrl(8, 4) + struct.pack(">i", v)
        return _uint(6 if v < 2**32 else 9, v)
    if isinstance(v, dict):
        return _ctrl(7, len(v)) + b"".join(encode(str(k)) + encode(x) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return _ctrl(11, len(v)) + b"".join(encode(x) for x in v)
    raise TypeError(f"cannot encode {type(v)}")


def write_mmdb(path: str, networks: list[tuple[str, dict]], database_type: str = "Steward-Location",
               description: str = "Steward location database") -> None:
    """networks: [("10.1.2.0/24", {"location": "CPH-B03"}), ...]. More specific prefixes win."""
    data = bytearray()
    offsets: dict[bytes, int] = {}
    tree: list[list] = [[None, None]]  # entry: None | ("n", idx) | ("d", offset)
    for cidr, record in sorted(networks, key=lambda x: ipaddress.ip_network(x[0], strict=False).prefixlen):
        net = ipaddress.ip_network(cidr, strict=False)
        if net.version != 4:
            raise ValueError("only IPv4 networks are supported")
        blob = encode(record)
        if blob not in offsets:
            offsets[blob] = len(data)
            data += blob
        bits = int(net.network_address)
        node = 0
        for depth in range(net.prefixlen):
            bit = (bits >> (31 - depth)) & 1
            if depth == net.prefixlen - 1:
                tree[node][bit] = ("d", offsets[blob])
                break
            nxt = tree[node][bit]
            if nxt is None or nxt[0] == "d":
                tree.append([nxt, nxt])
                tree[node][bit] = ("n", len(tree) - 1)
            node = tree[node][bit][1]
    count = len(tree)

    def rec(e) -> int:
        if e is None:
            return count
        return e[1] if e[0] == "n" else count + 16 + e[1]

    out = bytearray()
    for left, right in tree:
        out += rec(left).to_bytes(3, "big") + rec(right).to_bytes(3, "big")
    out += _SEP + data + _META
    meta = [
        ("node_count", _uint(6, count)), ("record_size", _uint(5, 24)), ("ip_version", _uint(5, 4)),
        ("database_type", encode(database_type)), ("languages", encode(["en"])),
        ("binary_format_major_version", _uint(5, 2)), ("binary_format_minor_version", _uint(5, 0)),
        ("build_epoch", _uint(9, int(time.time()))), ("description", encode({"en": description})),
    ]
    out += _ctrl(7, len(meta)) + b"".join(encode(k) + v for k, v in meta)
    with open(path, "wb") as fh:
        fh.write(out)
