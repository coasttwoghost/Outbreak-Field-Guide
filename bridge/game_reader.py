"""Our own reader of RE Outbreak's game state, so the bridge needs no third-party DLL.

It fills the same snapshot the tracker DLL used to (see LUA_PRELUDE / BRIDGE_SNAP in
bridge.py): same keys, same units, same `state` values. Nothing downstream changes:
the guide, the overlays, the recorder and watch_run read it as before.

Read-only. It goes through the bridge's `Ram`, which opens PCSX2 with query and read
rights only and has no write call. Every read can fail; a failed read gives None or
leaves a field out, and never stops the snapshot.

Where the addresses come from
-----------------------------
They are facts about the game's memory, in PS2 address space. Many were first
mapped by the Outbreak Tracker project (Fothsid, killme, phoe-nix, madgamer98), and
we credit them for that. Several are confirmed independently in this repo:

    player record base and stride, HP offset ... VirusEstimator.LAYOUT (disc)
    enemy roster base, 0x60 rows, count byte ... VirusEstimator.LAYOUT (disc)
    the record's +0x38/+0x40 position .......... FastPositions, live

Everything else is checked against the DLL by shadow mode (`bridge.py --shadow`)
before this reader is trusted alone. Until a field has matched on a real run, treat
it as unconfirmed. The tally lives in _docs/bridge-standalone-plan.md.
"""
from __future__ import annotations

import struct

# Two bytes that tell the discs apart: the 'S' of each executable's boot string.
FILE_PROBE = ((1, 0x2321B3), (2, 0x23DFD3))
SIG = 0x53

# Per-file layout. Offsets inside a player record are relative to its base.
LAYOUT = {
    1: dict(
        player=0x476DD0, pstride=0x10E0,
        scenario=0x3065AA, frames=0x48BF78, difficulty=0x48C01A,
        playernum=0x23BE14, hoststatus=0x62E230,
        itemrandom=0x23BB20, itemrandom2=0x23BBA1, puzzlerandom=0x23BBA0,
        virusmax=0x6E6C70, dead_inv=0x48BDE2,
        pickups=0x39680C, roomitem_ptr=0x23BD90, roomitem_stride=196,
        roster=0x71A030,
        p=dict(hp=0x544, maxhp=0x546, ctype=0xB98, status=0xBA0, virus=0xBAC,
               nameid=0xBB4, antivirus=0xBB6, speed=0xBC0, power=0xBC4,
               equipped=0xC7C, inventory=0xC84, bleed=0xC5A),
        file2_only=False,
    ),
    2: dict(
        player=0x47BD30, pstride=0x1100,
        scenario=0x3137BA, frames=0x4912B8, difficulty=0x49135A,
        playernum=0x24AFCC, hoststatus=0x62DDF0,
        itemrandom=0x24AD30, itemrandom2=0x24ADB1, puzzlerandom=0x24ADB0,
        virusmax=0x728500, dead_inv=0x491146,
        pickups=0x397B7C, roomitem_ptr=0x24AF48, roomitem_stride=192,
        roster=0x760630,
        p=dict(hp=0x540, maxhp=0x542, ctype=0xBB0, status=0xBB8, virus=0xBC4,
               nameid=0xBC8, antivirus=0xBCA, speed=0xBD8, power=0xBDC,
               equipped=0xC8C, inventory=0xC94, bleed=0xC6A),
        file2_only=True,
    ),
}
# File 2 scenario fields. The tracker reads these on File 1 too, where they are
# whatever happens to live there; nothing uses them outside File 2's levels.
F2_EXTRA = dict(gasflag=(0x48FFFC, "<I"), gasrandom=(0x24ADB0, "<B"),
                fighttime2=(0x49124E, "<H"), killedzombies=(0x491268, "<B"))
F2_COIN = 0x491188          # four bytes, two apart, summed

P_COMMON = dict(enabled=0x00, ingame=0x01, x=0x38, y=0x40, size=0xB4, room=0x1E2)

PICKUPS, PICKUP_SIZE = 255, 60
ROSTER_ROWS, ROSTER_SIZE = 80, 0x60
ROOM_SLOTS = 32

# The strings the products match on. Kept exactly as the DLL wrote them, so a guide
# or overlay comparing against "decisions,decisions" or "very hard" keeps working.
SCENARIOS = {1: "outbreak", 2: "hellfire", 28: "the hive", 35: "below freezing point",
             41: "decisions,decisions", 40: "wild things", 10: "underbelly",
             15: "desperate times", 6: "end of the road", 26: "flashback",
             20: "training ground", 29: "elimination 1", 30: "elimination 2",
             27: "elimination 3", 21: "showdown 1", 22: "showdown 2", 23: "showdown 3"}
DIFFICULTIES = {0: "easy", 1: "normal", 2: "hard", 3: "very hard"}
CHARACTERS = ("Kevin", "Mark", "Jim", "George", "David", "Alyssa", "Yoko", "Cindy")


def status_text(file: int, s: int) -> str:
    """The player status byte as the tracker named it. The two files differ above 3."""
    if s in (0, 1, 2, 3):
        return ("OK", "Poison", "Bleed", "Poison+Bleed")[s]
    if file == 1:
        if s >= 0x80:
            return "Zombie"
        if 0x18 <= s <= 0x1C:
            return "Down"
        if s > 0x1C:
            return "Dead"
        if s == 0x04:
            return "Cleared"
        return "unknown"
    if 0x30 <= s <= 0x34:
        return "Zombie"
    if 0x08 <= s < 0x0C:
        return "Down"
    if s == 0x0C:
        return "Down+Gas"
    if s > 0x0C:
        return "Dead"
    if s == 0x04:
        return "Gas"
    if s == 0x06:
        return "Gas+Bleed"
    return "unknown"


def _u(fmt: str, b: bytes | None, off: int = 0):
    if b is None or off + struct.calcsize(fmt) > len(b):
        return None
    return struct.unpack_from(fmt, b, off)[0]


def _f(v: float | None):
    """A float the JSON encoder can carry: NaN and infinities become None."""
    if v is None or v != v or v in (float("inf"), float("-inf")):
        return None
    return v


EMPTY_ITEM = {"id": -1, "type": 0, "count": 0, "pick": 0, "present": 0}


class GameReader:
    """Same interface as the bridge's `Vm`: init() and snap()."""

    def __init__(self, ram, enemy_names: dict[int, str] | None = None):
        self.ram = ram
        self.names = enemy_names or {}

    # -- the Vm interface ----------------------------------------------------

    def init(self) -> bool:
        return self.ram.read(0, 4) is not None

    def snap(self) -> dict:
        ram = self.ram
        if ram.read(0, 4) is None:
            return {"state": "no-emulator"}
        file = self._file()
        if file is None:
            return {"state": "not-outbreak", "game": {"currentFile": 255}}
        L = LAYOUT[file]
        gi = self._game(file, L)
        out = {"game": gi}
        if gi["scenario"] == "":
            out["state"] = "lobby" if (gi.get("hoststatus") or 0) >= 3 else "menus"
            return out
        if (gi.get("frames") or 0) <= 0:
            out["state"] = "loading"
            return out
        out["state"] = "in-game"

        items = self._pickups(L)
        out["players"] = self._players(file, L, items)
        out["roomItems"] = self._room_items(L, items)
        out["items"] = [dict(it) for it in items
                        if (it["number"] or 0) > 0 and (it["type"] or 0) > 0]
        out["enemies"] = self._enemies(L)
        return out

    # -- pieces ----------------------------------------------------------------

    def _file(self) -> int | None:
        for file, addr in FILE_PROBE:
            if self.ram.u8(addr) == SIG:
                return file
        return None

    def _game(self, file: int, L: dict) -> dict:
        r = self.ram
        sc, diff = r.u16(L["scenario"]), r.u8(L["difficulty"])
        gi = {
            "currentFile": file,
            "scenario": SCENARIOS.get(sc, "") if sc is not None else "",
            "difficulty": DIFFICULTIES.get(diff, "") if diff is not None else "",
            "frames": r.u32(L["frames"]),
            "playernum": r.u8(L["playernum"]),
            "hoststatus": r.u8(L["hoststatus"]),
            "itemrandom": r.u8(L["itemrandom"]),
            "itemrandom2": r.u8(L["itemrandom2"]),
            "puzzlerandom": r.u8(L["puzzlerandom"]),
        }
        if L["file2_only"]:
            for k, (addr, fmt) in F2_EXTRA.items():
                gi[k] = _u(fmt, r.read(addr, struct.calcsize(fmt)))
            c = r.read(F2_COIN, 7)
            gi["coin"] = (c[0] + c[2] + c[4] + c[6]) & 0xFF if c else None
        else:
            gi.update(gasflag=0, gasrandom=0, fighttime2=0, killedzombies=0, coin=0)
        return gi

    def _pickups(self, L: dict) -> list[dict]:
        """The level's 255 item placements, one 60-byte record each."""
        b = self.ram.read(L["pickups"], PICKUPS * PICKUP_SIZE)
        out = []
        for i in range(PICKUPS):
            o = i * PICKUP_SIZE
            out.append({
                "slot": i + 1, "id": i + 1,
                "number": _u("<B", b, o + 4), "type": _u("<H", b, o + 6),
                "count": _u("<H", b, o + 36), "pick": _u("<H", b, o + 40),
                "present": _u("<I", b, o + 32), "mix": _u("<B", b, o + 27),
                "roomid": _u("<B", b, o + 1),
            })
        return out

    @staticmethod
    def _item(items: list[dict], idx: int | None) -> dict:
        if not idx or idx > len(items):
            return dict(EMPTY_ITEM)
        it = items[idx - 1]
        return {k: it[k] for k in ("id", "type", "count", "pick", "present")}

    def _players(self, file: int, L: dict, items: list[dict]) -> list[dict]:
        r, P = self.ram, L["p"]
        size = max(P.values()) + 8
        out = []
        for i in range(4):
            b = r.read(L["player"] + i * L["pstride"], size)
            if b is None:
                continue
            enabled, ingame = b[P_COMMON["enabled"]], b[P_COMMON["ingame"]]
            if not (enabled or ingame):
                continue
            ctype = b[P["ctype"]]
            stat = status_text(file, b[P["status"]])
            cur = _u("<i", b, P["virus"])
            mx = _u("<i", r.read(L["virusmax"] + 4 * ctype, 4)) if ctype < 8 else None
            virus = cur / mx * 100.0 if (cur is not None and mx) else None
            if stat in ("Dead", "Zombie"):
                inv = r.read(L["dead_inv"] + 8 * i, 4) or b"\0\0\0\0"
            else:
                inv = bytes(b[P["inventory"]:P["inventory"] + 4])
            name = CHARACTERS[ctype] if ctype < 8 else "Unknown"
            out.append({
                "slot": i + 1,
                "enabled": bool(enabled), "inGame": bool(ingame),
                "HP": _u("<H", b, P["hp"]), "maxHP": _u("<H", b, P["maxhp"]),
                "bleedtime": _u("<H", b, P["bleed"]),
                "antivirustime": _u("<H", b, P["antivirus"]),
                "type": name,
                # The DLL named AI partners from its own table. The disc keys those
                # names through a runtime roster we have not located (see
                # extracted_data/characters.json "_open"), so a partner shows its
                # type here. Human players were always their type anyway.
                "name": name,
                "nameID": b[P["nameid"]],
                "virus": _f(virus),
                "size": _f(_u("<f", b, P_COMMON["size"])),
                "speed": _f(_u("<f", b, P["speed"])),
                "power": _f(_u("<f", b, P["power"])),
                "positionX": _f(_u("<f", b, P_COMMON["x"])),
                "positionY": _f(_u("<f", b, P_COMMON["y"])),
                "roomID": _u("<H", b, P_COMMON["room"]),
                "status": stat,
                "equipped": b[P["equipped"]],
                "inventory": [self._item(items, x) for x in inv],
            })
        return out

    def _room_items(self, L: dict, items: list[dict]) -> list[dict]:
        """The camera room's 32 item slots. Each holds a 1-based pickup index."""
        ptr = self.ram.u32(L["roomitem_ptr"])
        if not ptr:
            return []
        stride = L["roomitem_stride"]
        start = (ptr & 0x1FFFFFF) + 0x40 - stride * ROOM_SLOTS // 2
        b = self.ram.read(start, stride * ROOM_SLOTS)
        if b is None:
            return []
        out = []
        for j in range(ROOM_SLOTS):
            o = j * stride
            idx, en = b[o], _u("<H", b, o + 10)
            if not idx or en != 0xFFFF or idx > len(items):
                continue
            it = items[idx - 1]
            if it["count"] == 0xFFFF or it["mix"] == 0x20 or not (it["type"] or 0) > 0:
                continue
            row = self._item(items, idx)
            row["slot"] = j + 1
            out.append(row)
        return out

    def _enemies(self, L: dict) -> list[dict]:
        b = self.ram.read(L["roster"], ROSTER_ROWS * ROSTER_SIZE)
        if b is None:
            return []
        out = []
        for i in range(ROSTER_ROWS):
            o = i * ROSTER_SIZE
            mx = _u("<H", b, o + 0x1E)
            if not mx:
                continue
            nid = b[o + 2]
            out.append({
                "slot": i + 1,
                "number": b[o], "flag": b[o + 1], "nameID": nid, "type": b[o + 3],
                "HP": _u("<H", b, o + 0x1C), "maxHP": mx,
                "roomID": b[o + 0x22], "status": b[o + 0x45],
                "name": self.names.get(nid, ""),
            })
        return out


# --------------------------------------------------------------------------
# shadow mode: both readers, every disagreement written down
# --------------------------------------------------------------------------

# Fields the DLL carried that nothing reads. Not compared, because this reader
# does not produce them.
SKIP = {"t", "polls", "fast", "note", "enemyProbe", "doors", "specialInventory",
        "specialItem", "critBonus", "herbtime", "antivirusgtime", "cleared",
        "escapetime", "fighttime", "garagetime", "gastime", "wttime", "hosttime",
        "hostdifficulty", "hostmaxplayer", "hostplayer", "hostscenario",
        "passwt", "passdt1", "passdt2", "passdt3", "pass1", "pass2", "pass3",
        "pass4", "pass5", "pass6", "passub1", "passub2", "passub3", "mix",
        # Names are ours by design (see _players / _enemies); compared by id instead.
        "name"}
FILE2_GAME = set(F2_EXTRA) | {"coin"}


def diff(dll: dict, ours: dict, path: str = "", out: list | None = None,
         file: int | None = None) -> list:
    """Every leaf where the two snapshots disagree, as (path, dll, ours)."""
    if out is None:
        out = []
        file = (dll.get("game") or {}).get("currentFile")
    if isinstance(dll, dict) and isinstance(ours, dict):
        for k in dll:
            if k in SKIP or k.startswith("virusEstimate") or (
                    path == "game" and file == 1 and k in FILE2_GAME):
                continue
            if k not in ours:
                out.append((f"{path}.{k}".lstrip("."), dll[k], "<missing>"))
                continue
            diff(dll[k], ours[k], f"{path}.{k}".lstrip("."), out, file)
    elif isinstance(dll, list) and isinstance(ours, list):
        if len(dll) != len(ours):
            out.append((f"{path}#len", len(dll), len(ours)))
        for i, (a, b) in enumerate(zip(dll, ours)):
            diff(a, b, f"{path}[{i}]", out, file)
    elif isinstance(dll, float) or isinstance(ours, float):
        if dll is None or ours is None or abs(float(dll) - float(ours)) > 1e-3 * max(1.0, abs(float(dll))):
            if not (dll is None and ours is None):
                out.append((path, dll, ours))
    elif dll != ours:
        out.append((path, dll, ours))
    return out
