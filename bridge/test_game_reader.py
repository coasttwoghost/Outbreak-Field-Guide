"""Checks game_reader against the DLL, using runs you have already recorded.

Every sessions/rec-*.jsonl.gz line holds what the DLL said (players, enemies) and,
beside it, the raw memory those values came from (the four player records and the
enemy roster). This feeds that raw memory to our reader and compares, field by
field. It needs no game and no DLL.

    py test_game_reader.py            all recordings
    py test_game_reader.py --max 200  at most 200 lines per file (quick)

What it can check: player HP, room, character; enemy nameID, HP, room. What it
can't (the recorder does not save that memory): game info, items, positions,
virus. Those are left to shadow mode on a live run.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import game_reader                                           # noqa: E402

# Where the recorder took each block from: VirusEstimator.LAYOUT in bridge.py.
REC = {2: (0x47BD30, 0x1100, 0x760630), 1: (0x476DD0, 0x10E0, 0x71A030)}


class RowRam:
    """A Ram that only knows the blocks one recorder line saved."""

    def __init__(self, row: dict):
        base, stride, roster = REC[row["file"]]
        self.blocks = []
        for i, b in enumerate(row.get("rec") or []):
            if b:
                self.blocks.append((base + i * stride, zlib.decompress(base64.b64decode(b))))
        if row.get("roster"):
            self.blocks.append((roster, zlib.decompress(base64.b64decode(row["roster"]))))

    def read(self, addr, size):
        for a, b in self.blocks:
            if a <= addr and addr + size <= a + len(b):
                return b[addr - a: addr - a + size]
            if a <= addr < a + len(b):                       # a longer read than was saved
                return (b[addr - a:] + bytes(size))[:size]
        return None

    def u8(self, a):
        b = self.read(a, 1)
        return b[0] if b else None

    def u16(self, a):
        b = self.read(a, 2)
        return int.from_bytes(b, "little") if b else None

    def u32(self, a):
        b = self.read(a, 4)
        return int.from_bytes(b, "little") if b else None


def check(path: Path, limit: int) -> tuple[int, dict]:
    lines, bad = 0, {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f):
            if limit and n >= limit:
                break
            row = json.loads(line)
            if row.get("file") not in REC:
                continue
            r = game_reader.GameReader(RowRam(row))
            L = game_reader.LAYOUT[row["file"]]
            ours_p = {p["slot"]: p for p in r._players(row["file"], L, [])}
            ours_e = {e["slot"]: e for e in r._enemies(L)}
            lines += 1
            for slot, name, hp, _virus, room, *_ in row.get("players") or []:
                o = ours_p.get(slot)
                if o is None:
                    bad["player missing"] = bad.get("player missing", 0) + 1
                    continue
                for k, want, got in (("HP", hp, o["HP"]), ("roomID", room, o["roomID"]),
                                     ("name", name, o["type"])):
                    if want != got:
                        bad[f"player.{k}"] = bad.get(f"player.{k}", 0) + 1
            # The roster count byte limits what was saved; rows past it are not compared.
            for slot, nid, hp, room in row.get("enemies") or []:
                o = ours_e.get(slot)
                if o is None:
                    bad["enemy missing"] = bad.get("enemy missing", 0) + 1
                    continue
                for k, want, got in (("nameID", nid, o["nameID"]), ("HP", hp, o["HP"]),
                                     ("roomID", room, o["roomID"])):
                    if want != got:
                        bad[f"enemy.{k}"] = bad.get(f"enemy.{k}", 0) + 1
    return lines, bad


class FakeRam:
    """32 MB of zeroes with chosen values poked in, for the self-test."""

    def __init__(self):
        self.mem = bytearray(0x2000000)

    def put(self, addr, fmt, v):
        import struct
        struct.pack_into(fmt, self.mem, addr, v)

    def read(self, addr, size):
        return bytes(self.mem[addr:addr + size])

    def u8(self, a):
        return self.mem[a]

    def u16(self, a):
        return int.from_bytes(self.mem[a:a + 2], "little")

    def u32(self, a):
        return int.from_bytes(self.mem[a:a + 4], "little")


def selftest() -> int:
    """Decode a hand-built File 2 memory image and check every field lands.

    This proves the reader's plumbing (offsets wired to the right keys, filters,
    the room-item indirection), not the addresses themselves: those are checked
    against the DLL, by the recordings above and by shadow mode.
    """
    r = FakeRam()
    L = game_reader.LAYOUT[2]
    P = L["p"]
    r.put(0x23DFD3, "<B", 0x53)                          # File 2
    r.put(L["scenario"], "<H", 15)
    r.put(L["difficulty"], "<B", 3)
    r.put(L["frames"], "<I", 5000)
    r.put(L["itemrandom"], "<B", 2)
    r.put(L["puzzlerandom"], "<B", 1)
    base = L["player"] + L["pstride"]                    # slot 2
    r.put(base + 0, "<B", 1)
    r.put(base + 1, "<B", 1)
    r.put(base + P["hp"], "<H", 1500)
    r.put(base + P["maxhp"], "<H", 2000)
    r.put(base + P["ctype"], "<B", 5)                    # Alyssa
    r.put(base + P["virus"], "<i", 250)
    r.put(L["virusmax"] + 4 * 5, "<i", 1000)
    r.put(base + 0x38, "<f", 1234.5)
    r.put(base + 0x1E2, "<H", 11)
    r.put(base + P["inventory"], "<B", 3)                # holds pickup #3
    pk = L["pickups"] + 2 * 60                           # pickup #3
    r.put(pk + 1, "<B", 11)
    r.put(pk + 4, "<B", 1)
    r.put(pk + 6, "<H", 316)
    r.put(pk + 36, "<H", 1)
    ptr = 0x500000
    r.put(L["roomitem_ptr"], "<I", ptr)
    slot = ptr + 0x40 - 192 * 32 // 2 + 4 * 192          # room slot 5
    r.put(slot, "<B", 3)
    r.put(slot + 10, "<H", 0xFFFF)
    row = L["roster"] + 0x60                             # roster slot 2
    r.put(row + 2, "<B", 33)
    r.put(row + 0x1C, "<H", 900)
    r.put(row + 0x1E, "<H", 1200)
    r.put(row + 0x22, "<B", 7)

    s = game_reader.GameReader(r, {33: "Stalker"}).snap()
    g, p = s["game"], s["players"][0]
    want = [
        (s["state"], "in-game"), (g["currentFile"], 2), (g["scenario"], "desperate times"),
        (g["difficulty"], "very hard"), (g["frames"], 5000), (g["itemrandom"], 2),
        (g["puzzlerandom"], 1), (len(s["players"]), 1), (p["slot"], 2), (p["HP"], 1500),
        (p["type"], "Alyssa"), (round(p["virus"], 3), 25.0), (p["positionX"], 1234.5),
        (p["roomID"], 11), (p["status"], "OK"), (p["inventory"][0]["type"], 316),
        (p["inventory"][1]["id"], -1), (len(s["items"]), 1), (s["items"][0]["roomid"], 11),
        (len(s["roomItems"]), 1), (s["roomItems"][0]["slot"], 5),
        (s["roomItems"][0]["id"], 3), (len(s["enemies"]), 1), (s["enemies"][0]["slot"], 2),
        (s["enemies"][0]["name"], "Stalker"), (s["enemies"][0]["roomID"], 7),
    ]
    bad = [(i, got, exp) for i, (got, exp) in enumerate(want) if got != exp]
    r.put(0x23DFD3, "<B", 0)
    if game_reader.GameReader(r).snap()["state"] != "not-outbreak":
        bad.append(("not-outbreak", None, None))
    print(f"  selftest: {len(want) + 1 - len(bad)}/{len(want) + 1} {'ok' if not bad else bad}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--selftest", action="store_true",
                    help="no recordings needed: decode a hand-built memory image")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    files = sorted((HERE / "sessions").glob("rec-*.jsonl.gz"))
    if not files:
        print("no recordings in sessions/")
        return 0
    total, worst = 0, 0
    for p in files:
        n, bad = check(p, args.max)
        total += n
        worst = max(worst, sum(bad.values()))
        print(f"  {p.name:34} {n:6} lines  {'OK' if not bad else bad}")
    print(f"\n  {total} lines compared, {'all match' if not worst else 'MISMATCHES above'}")
    return 1 if worst else 0


if __name__ == "__main__":
    raise SystemExit(main())
