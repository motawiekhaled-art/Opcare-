"""Register/bit decoding (spec section 15.1) and read planning (merging points into block reads)."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

WIDTH = {"bool": 1, "bit": 1, "int16": 1, "uint16": 1, "int32": 2, "uint32": 2, "float32": 2,
         "int64": 4, "uint64": 4, "float64": 4}
FMT = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f",
       "int64": ">q", "uint64": ">Q", "float64": ">d"}
BIT_TABLES = ("coil", "discrete_input")


def width(address: dict) -> int:
    enc = address["encoding"]
    return address.get("length", 1) if enc == "string" else WIDTH[enc]


def to_bytes(regs: list[int], word_order: str = "big", byte_order: str = "big") -> bytes:
    """ABCD (big/big), CDAB (word little), BADC (byte little), DCBA (both little) -> canonical big-endian."""
    if word_order == "little":
        regs = list(reversed(regs))
    out = bytearray()
    for r in regs:
        out += bytes((r >> 8, r & 0xFF)) if byte_order == "big" else bytes((r & 0xFF, r >> 8))
    return bytes(out)


def decode(address: dict, words: list[int]):
    """words = bits for coil/discrete_input, registers otherwise. Returns the raw value."""
    enc = address["encoding"]
    if address["table"] in BIT_TABLES:
        return bool(words[0])
    if enc == "bit":
        return bool((words[0] >> address["bit"]) & 1)
    if enc == "bool":
        return words[0] != 0
    raw = to_bytes(words, address.get("word_order", "big"), address.get("byte_order", "big"))
    if enc == "string":
        return raw.decode("latin-1").rstrip("\x00 ")
    value = struct.unpack(FMT[enc], raw)[0]
    if enc == "float32" and value == value and abs(value) != float("inf"):
        value = float(f"{value:.7g}")          # float32 carries ~7 significant digits; do not invent more
    return value


@dataclass
class Block:
    device: str
    unit_id: int
    table: str
    start: int
    count: int
    items: list = field(default_factory=list)     # (Point, offset, width)
    sub: list | None = None                       # replacement reads after an exception (see split())

    @property
    def split(self) -> bool:
        return self.sub is not None

    def make_split(self) -> None:
        """First into runs of back-to-back addresses; a run that is already contiguous into single points."""
        points = [p for p, _, _ in self.items]
        runs = plan(points, {self.device: self.unit_id}, max_regs=self.count, max_bits=self.count, max_gap=0)
        if len(runs) == 1:
            runs = [Block(self.device, self.unit_id, self.table, self.start + off, w, [(p, 0, w)])
                    for p, off, w in self.items]
        self.sub = runs

    @property
    def end(self) -> int:
        return self.start + self.count


def plan(points, units: dict[str, int], max_regs: int = 100, max_bits: int = 1000, max_gap: int = 10) -> list[Block]:
    """Merge points of the same device and table into as few reads as possible (spec 15.1)."""
    groups: dict[tuple[str, str], list] = {}
    for p in points:
        groups.setdefault((p.device, p.address["table"]), []).append(p)
    blocks: list[Block] = []
    for (device, table), pts in sorted(groups.items()):
        limit = max_bits if table in BIT_TABLES else max_regs
        cur: Block | None = None
        for p in sorted(pts, key=lambda x: (x.address["address"], width(x.address))):
            a, w = p.address["address"], width(p.address)
            if cur is not None and a - cur.end <= max_gap and max(cur.end, a + w) - cur.start <= limit:
                cur.count = max(cur.end, a + w) - cur.start
            else:
                cur = Block(device, units[device], table, a, w)
                blocks.append(cur)
            cur.items.append((p, a - cur.start, w))
    return blocks
