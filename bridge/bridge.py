"""Outbreak Field Guide bridge -- serves the guide and feeds it live game state.

One process.  It does three things:

  1. loads lua51.dll + luaoutbreaktracker.dll and polls PCSX2 for game state
  2. serves the field guide HTML over http://localhost:8777
  3. serves that state at /live.json for the guide to read

Design law: the bridge may only ever REMOVE work from the player.  If PCSX2 is
not running, if the DLLs are missing, if the game is on the title screen -- the
guide still opens and behaves exactly as it does off disk.  Nothing here is
allowed to become a step the player has to perform.

Read-only.  luaoutbreaktracker.dll imports ReadProcessMemory and not
WriteProcessMemory; we never write a byte into the emulator.  This is what makes
the tool safe to run against the obsrv.org public server.

Usage:
    py bridge.py                 serve + open a browser
    py bridge.py --lan           also accept connections from the local network
    py bridge.py --no-browser    don't open a browser
    py bridge.py --port 9000     use a different port
    py bridge.py --probe         print one snapshot as JSON and exit
    py bridge.py --no-record     do not save the raw memory of a match to sessions/

Also served: /virus, a page for typing in a virus figure a teammate reads out.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import copy
import ctypes
import gzip
import http.server
import json
import math
import os
import re
import socket
import socketserver
import struct
import sys
import threading
import time
import webbrowser
import zlib
from pathlib import Path

import game_reader

APP = "Outbreak Field Guide"
MAKER = "ALYSSA:C 100%"      # the maker's pen name; build_public_repo.py checks it is still here
DEFAULT_PORT = 8777
POLL_HZ = 4.0
FAST_HZ = 15.0              # FastPositions; the game runs about 30, so every other frame
REINIT_EVERY = 5.0          # seconds between tracker.init() retries, as the tracker does

# --------------------------------------------------------------------------
# locating our pieces
# --------------------------------------------------------------------------

def data_dir() -> Path:
    """Where the packaged app keeps what it writes: downloaded guides, logs.

    Not next to the exe. A user who double-clicks it inside the zip runs it from
    a throwaway temp folder, and anything saved there is gone next time.
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP


def guide_cache() -> Path:
    return data_dir() / "guides"


def _bases() -> list[Path]:
    """Every directory we are willing to look in, best first.

    The guide is picked by version, not by folder, so the order matters only for
    the DLLs. Packaged, three guides can be in play: the one built into the exe,
    one the user dropped next to it, and one the update check downloaded. The
    newest wins.
    """
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    exe = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else here
    out = [exe, here, exe / "_internal", exe / "lib", here / "lib", Path.cwd()]
    if getattr(sys, "frozen", False):
        out.append(guide_cache())
    # running from the repo: the tracker and the guide live a couple of levels up
    root = Path(__file__).resolve().parent.parent.parent
    out += [root / "Outbreak_Tracker", root / "_Field Guide", root]
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def find_file(name: str) -> Path | None:
    for b in _bases():
        p = b / name
        if p.is_file():
            return p
    return None


_VER = re.compile(r"-v(\d+)\.(\d+)", re.I)


def find_guide() -> Path | None:
    """Highest-numbered obsrv-field-guide-v*.html we can see.

    Never assume a version: list the folder and take the newest.
    """
    best, best_key = None, ()
    for b in _bases():
        if not b.is_dir():
            continue
        for p in b.glob("obsrv-field-guide-v*.html"):
            m = _VER.search(p.name)
            key = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            if key > best_key:
                best, best_key = p, key
    return best


def guide_version(p: Path | None) -> tuple[int, int]:
    m = _VER.search(p.name) if p else None
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# Where the packaged bridge asks whether a newer guide exists. Empty until the
# public site is up; --update-url overrides it. The address serves JSON:
#     {"version": "14.50", "url": "obsrv-field-guide-v14.50.html", "sha256": "..."}
# `url` may be relative to the JSON's own address. `sha256` is optional but checked
# when present.
UPDATE_URL = "https://outbreak-field-guide.outbreak-intel.workers.dev/guide-version.json"
UPDATE_MAX = 32 * 1024 * 1024


class GuideUpdater(threading.Thread):
    """Fetches a newer guide in the background, once, at startup.

    The design law applies: this may only remove work. It never delays the guide
    opening, never asks anything, and any failure (offline, slow, a bad file) is
    silent apart from one line in the console. A downloaded guide is saved under
    guide_cache() and the handler picks it up on the next page load, because
    find_guide() re-resolves by version on every request.
    """

    daemon = True
    TIMEOUT = 6.0

    def __init__(self, url: str, current: Path | None):
        super().__init__(name="guide-update")
        self.url, self.current = url, current
        self.result = "not run"

    def _get(self, url: str, limit: int) -> bytes:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP} bridge"})
        with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
            body = r.read(limit + 1)
        if len(body) > limit:
            raise ValueError("too large")
        return body

    def run(self):
        import hashlib
        import urllib.parse
        try:
            if not self.url.lower().startswith("https://"):
                raise ValueError("update address must be https")
            info = json.loads(self._get(self.url, 64 * 1024).decode("utf-8"))
            m = re.fullmatch(r"(\d+)\.(\d+)", str(info.get("version", "")))
            if not m:
                raise ValueError("no version in the update file")
            have = guide_version(self.current)
            want = (int(m.group(1)), int(m.group(2)))
            if want <= have:
                self.result = f"up to date (v{have[0]}.{have[1]})"
                return
            src = urllib.parse.urljoin(self.url, str(info.get("url") or ""))
            if not src.lower().startswith("https://"):
                raise ValueError("guide address must be https")
            body = self._get(src, UPDATE_MAX)
            sha = info.get("sha256")
            if sha and hashlib.sha256(body).hexdigest() != str(sha).lower():
                raise ValueError("checksum mismatch")
            head = body[:512].removeprefix(b"\xef\xbb\xbf").lstrip().lower()   # the guide has a BOM
            if not (head.startswith(b"<!doctype html") or head.startswith(b"<html")):
                raise ValueError("not an HTML page")
            folder = guide_cache()
            folder.mkdir(parents=True, exist_ok=True)
            out = folder / f"obsrv-field-guide-v{want[0]}.{want[1]}.html"
            tmp = out.with_suffix(".part")
            tmp.write_bytes(body)
            tmp.replace(out)
            self.result = f"downloaded v{want[0]}.{want[1]}, reload the page to use it"
        except Exception as e:                      # noqa: BLE001 - offline is normal
            self.result = f"skipped ({type(e).__name__}: {e})"
        finally:
            print(f"  Update    {self.result}")


def zip_trap() -> bool:
    """True when Windows is running us straight out of a zip.

    Double-clicking an exe inside a zip in Explorer extracts that one file to
    %TEMP%\\Temp1_<name>.zip\\ and runs it. Packaged as one file, the bridge
    still works from there, but nothing beside it (the share .bat, a dropped-in
    guide) does, so the banner says so.
    """
    if not getattr(sys, "frozen", False):
        return False
    tmp = os.environ.get("TEMP", "")
    if not tmp:
        return False
    p = str(Path(sys.executable).resolve()).lower()
    return p.startswith(str(Path(tmp).resolve()).lower()) and ".zip" in p


# --------------------------------------------------------------------------
# the Lua side
# --------------------------------------------------------------------------

# Serialises the tracker's tables to JSON inside Lua, so Python only ever has to
# carry one string back across the ctypes boundary.  Everything is pcall-wrapped:
# a single failing read must not cost us the whole snapshot.
LUA_PRELUDE = r"""
local fmt, byte, gsub = string.format, string.byte, string.gsub

local function esc(s)
  return (gsub(tostring(s), '[%c"\\]', function(c)
    if c == '"'  then return '\\"'  end
    if c == '\\' then return '\\\\' end
    if c == '\n' then return '\\n'  end
    if c == '\r' then return '\\r'  end
    if c == '\t' then return '\\t'  end
    return fmt('\\u%04x', byte(c))
  end))
end

local enc

local function isarray(t)
  local n = 0
  for k in pairs(t) do
    if type(k) ~= 'number' then return false end
    n = n + 1
  end
  return n == #t
end

local function val(v)
  local t = type(v)
  if t == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then return 'null' end
    if v == math.floor(v) and v < 1e15 and v > -1e15 then return fmt('%d', v) end
    return fmt('%.14g', v)
  elseif t == 'boolean' then return tostring(v)
  elseif t == 'nil'     then return 'null'
  elseif t == 'table'   then return enc(v)
  else return '"' .. esc(v) .. '"' end
end

enc = function(t)
  local out = {}
  if isarray(t) then
    for i = 1, #t do out[#out + 1] = val(t[i]) end
    return '[' .. table.concat(out, ',') .. ']'
  end
  local keys = {}
  for k in pairs(t) do keys[#keys + 1] = tostring(k) end
  table.sort(keys)
  for _, k in ipairs(keys) do
    local v = t[k]
    if v == nil then v = t[tonumber(k)] end
    out[#out + 1] = '"' .. esc(k) .. '":' .. val(v)
  end
  return '{' .. table.concat(out, ',') .. '}'
end

local function try(fn, ...)
  local ok, r = pcall(fn, ...)
  if ok then return r end
  return nil
end

INITED = false

function BRIDGE_INIT()
  INITED = try(tracker.init) and true or false
  return INITED
end

-- One poll.  Mirrors the tracker's own order: lobby, room, entities, then read.
function BRIDGE_SNAP()
  local out = {}

  if not INITED then
    out.state = 'no-emulator'
    return enc(out)
  end

  try(tracker.updateLobby)
  try(tracker.updateRoom)
  try(tracker.update)

  local gi = try(tracker.getGameInfo)
  if type(gi) ~= 'table' then
    out.state = 'no-game-info'
    return enc(out)
  end
  out.game = gi

  -- currentFile 255 is the tracker's "attached, but this is not Outbreak"
  local file = gi.currentFile or 0
  if file == 255 then
    out.state = 'not-outbreak'
    return enc(out)
  end
  if file == 0 then
    out.state = 'booting'
    return enc(out)
  end

  local scen   = gi.scenario or ''
  local frames = gi.frames or 0
  if scen == '' then
    out.state = (gi.hoststatus or 0) >= 3 and 'lobby' or 'menus'
    return enc(out)
  end
  if frames <= 0 then
    out.state = 'loading'
    return enc(out)
  end
  out.state = 'in-game'

  -- players -------------------------------------------------------------
  local players = {}
  for i = 1, 4 do
    local p = try(tracker.getPlayer, i)
    if type(p) == 'table' and (p.inGame or p.enabled) then
      p.slot = i
      players[#players + 1] = p
    end
  end
  out.players = players

  -- items in the room the camera is in (32 spawn slots, with pick/present) --
  local cur = try(tracker.getItem, 1)
  if type(cur) == 'table' and type(cur.roomitem) == 'table' then
    local live = {}
    for i = 1, 32 do
      local it = cur.roomitem[i]
      if type(it) == 'table' and it.id ~= -1 and (it.type or 0) > 0 then
        it.slot = i
        live[#live + 1] = it
      end
    end
    out.roomItems = live
  end

  -- every item placement in the level, carrying its room id ---------------
  local all = {}
  for i = 1, 255 do
    local it = try(tracker.getItem2, i)
    if type(it) == 'table' and (it.number or 0) > 0 and (it.type or 0) > 0 then
      it.slot = i
      all[#all + 1] = it
    end
  end
  out.items = all

  -- the level's enemy roster ---------------------------------------------
  local en = {}
  for i = 1, 80 do
    local e = try(tracker.getEnemyList, i)
    if type(e) == 'table' and (e.maxHP or 0) > 0 then
      e.slot = i
      en[#en + 1] = e
    end
  end
  out.enemies = en

  -- getEnemy is a SEPARATE getter from getEnemyList and has never been called
  -- here. getEnemyList returns no coordinates; whether this one does is the
  -- open question behind live enemy positions, and the DLL's field-name pool
  -- is shared across every getter so it cannot be answered by inspection.
  -- Two slots, merged in only where they add a key the roster lacks, so the
  -- answer shows up in a capture without bloating every poll.
  local probe = {}
  for i = 1, 2 do
    local e = try(tracker.getEnemy, i)
    if type(e) == 'table' then
      probe[#probe + 1] = e
    end
  end
  if #probe > 0 then
    out.enemyProbe = probe
  end

  -- doors, from the getter the tracker's DoorCard draws. It lists 1 to 4 on
  -- Outbreak. Nothing has recorded these yet, so they go in the rec lines too.
  if scen == 'outbreak' then
    local doors = {}
    for i = 1, 4 do
      local d = try(tracker.getDoor, i)
      if type(d) == 'table' then
        d.id = i
        doors[#doors + 1] = d
      end
    end
    out.doors = doors
  end

  return enc(out)
end
"""


class LuaError(RuntimeError):
    pass


class Vm:
    """A Lua 5.1 state with luaoutbreaktracker loaded into it.

    Owned by exactly one thread -- the poller.  Lua states are not re-entrant and
    neither is the tracker DLL's global emulator handle.
    """

    def __init__(self, lua_dll: Path, tracker_dll: Path):
        os.add_dll_directory(str(lua_dll.parent))
        self.lua = ctypes.CDLL(str(lua_dll))
        L = self.lua
        L.luaL_newstate.restype = ctypes.c_void_p
        L.luaL_openlibs.argtypes = [ctypes.c_void_p]
        L.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        L.luaL_loadstring.restype = ctypes.c_int
        L.lua_pcall.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 3
        L.lua_pcall.restype = ctypes.c_int
        L.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_size_t)]
        L.lua_tolstring.restype = ctypes.c_void_p
        L.lua_settop.argtypes = [ctypes.c_void_p, ctypes.c_int]

        self.S = L.luaL_newstate()
        if not self.S:
            raise LuaError("could not create a Lua state")
        L.luaL_openlibs(ctypes.c_void_p(self.S))

        self.mod = ctypes.CDLL(str(tracker_dll))
        opener = getattr(self.mod, "luaopen_luaoutbreaktracker")
        opener.argtypes = [ctypes.c_void_p]
        opener.restype = ctypes.c_int
        opener(ctypes.c_void_p(self.S))

        if self.run("return type(tracker)") != "table":
            raise LuaError("luaoutbreaktracker loaded but exposed no 'tracker' table")
        self.run(LUA_PRELUDE + "\nreturn 'ok'")

    def _str(self) -> str:
        n = ctypes.c_size_t(0)
        ptr = self.lua.lua_tolstring(ctypes.c_void_p(self.S), -1, ctypes.byref(n))
        if not ptr:
            return ""
        return ctypes.string_at(ptr, n.value).decode("utf-8", "replace")

    def run(self, src: str) -> str:
        S = ctypes.c_void_p(self.S)
        if self.lua.luaL_loadstring(S, src.encode("utf-8")) != 0:
            msg = self._str()
            self.lua.lua_settop(S, 0)
            raise LuaError(f"compile: {msg}")
        if self.lua.lua_pcall(S, 0, 1, 0) != 0:
            msg = self._str()
            self.lua.lua_settop(S, 0)
            raise LuaError(f"run: {msg}")
        out = self._str()
        self.lua.lua_settop(S, 0)
        return out

    def init(self) -> bool:
        return self.run("return tostring(BRIDGE_INIT())") == "true"

    def snap(self) -> dict:
        return json.loads(self.run("return BRIDGE_SNAP()"))


# --------------------------------------------------------------------------
# a second, read-only window on the game
# --------------------------------------------------------------------------

class Ram:
    """Reads the emulated PS2 memory directly, for fields the tracker DLL does not expose.

    PCSX2 exports a pointer named EEmem to its 32 MB of PS2 RAM; this finds it
    in the running process and reads from it. The process is opened with query
    and read rights only -- there is no write call in this class, which keeps
    the bridge's promise that it never writes a byte into the emulator.

    Everything degrades to None: no emulator, a PCSX2 build without the export,
    a failed read. Callers must treat None as "not available" and carry on.
    """

    EXE = ("pcsx2-qt.exe", "pcsx2-qtx64.exe", "pcsx2-qtx64-avx2.exe")
    RETRY = 5.0             # seconds between attempts to find the emulator

    def __init__(self):
        self.h = None
        self.ee = 0
        self._tried = 0.0
        try:
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
            self.k32.OpenProcess.restype = ctypes.c_void_p
            self.k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            self.k32.CloseHandle.argtypes = [ctypes.c_void_p]
            self.k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            self.k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                                                            ctypes.POINTER(ctypes.c_uint32)]
            self.psapi.EnumProcesses.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
            self.psapi.EnumProcessModules.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                                      ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        except Exception:                           # noqa: BLE001 - not Windows, or no access
            self.k32 = None

    def _raw(self, addr: int, size: int) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        ok = self.k32.ReadProcessMemory(self.h, addr, buf, size, ctypes.byref(n))
        return buf.raw if ok and n.value == size else None

    def _close(self):
        if self.h:
            self.k32.CloseHandle(self.h)
        self.h, self.ee = None, 0

    def _open(self) -> bool:
        if self.k32 is None or time.time() - self._tried < self.RETRY:
            return False
        self._tried = time.time()
        self._close()
        pids = (ctypes.c_uint32 * 4096)()
        got = ctypes.c_uint32()
        if not self.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(got)):
            return False
        for pid in pids[:got.value // 4]:
            h = self.k32.OpenProcess(0x0410, False, pid)        # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
            if not h:
                continue
            name = ctypes.create_unicode_buffer(520)
            size = ctypes.c_uint32(520)
            if self.k32.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(size)) \
                    and name.value.lower().rsplit("\\", 1)[-1] in self.EXE:
                self.h = h
                break
            self.k32.CloseHandle(h)
        if not self.h:
            return False
        try:
            self.ee = self._find_eemem()
        except Exception:                           # noqa: BLE001
            self.ee = 0
        if not self.ee:
            self._close()
        return bool(self.ee)

    def _find_eemem(self) -> int:
        """Walk the emulator's export table for EEmem and follow it."""
        mods = (ctypes.c_void_p * 4)()
        need = ctypes.c_uint32()
        if not self.psapi.EnumProcessModules(self.h, mods, ctypes.sizeof(mods), ctypes.byref(need)):
            return 0
        base = mods[0]
        u32 = lambda a: struct.unpack("<I", self._raw(a, 4))[0]
        exports = base + u32(base + u32(base + 0x3C) + 0x88)
        d = self._raw(exports, 40)
        count = struct.unpack_from("<I", d, 24)[0]
        funcs, names, ords = struct.unpack_from("<III", d, 28)
        for i in range(count):
            if (self._raw(base + u32(base + names + 4 * i), 6) or b"") == b"EEmem\0":
                o = struct.unpack("<H", self._raw(base + ords + 2 * i, 2))[0]
                return struct.unpack("<Q", self._raw(base + u32(base + funcs + 4 * o), 8))[0]
        return 0

    def read(self, addr: int, size: int) -> bytes | None:
        """`size` bytes of PS2 memory at `addr`, or None."""
        if not (0 <= addr and addr + size <= 0x2000000):
            return None
        if not self.ee and not self._open():
            return None
        b = self._raw(self.ee + addr, size)
        if b is None:                               # the emulator went away, or restarted
            self._close()
        return b

    def u8(self, addr: int) -> int | None:
        b = self.read(addr, 1)
        return b[0] if b else None

    def u16(self, addr: int) -> int | None:
        b = self.read(addr, 2)
        return struct.unpack("<H", b)[0] if b else None

    def u32(self, addr: int) -> int | None:
        b = self.read(addr, 4)
        return struct.unpack("<I", b)[0] if b else None


# --------------------------------------------------------------------------
# teammates' virus -- an estimate, because the game does not have the number
# --------------------------------------------------------------------------

class VirusEstimator:
    """Works out a virus figure for the players whose gauge this game is not running.

    Online, each player's own game runs their virus, and the gauge itself is
    never sent. What IS sent with every player update is a two-bit band of it
    -- under 50%, 50-80, 80-90, 90 and over -- and the receiving game writes a
    stand-in gauge from that: band floor x gauge size + 5 units (File 2:
    virus_band 0x63cc60, mk_gauge 0x63cce0; _docs/mips-re-notes.md,
    2026-10-11). That is why a teammate reads 0.0014% for most of a run, and
    will read 50.00x%, 80.00x% or 90.00x% later. Seen live 2026-10-11.

    So for those players the bridge adds `virusBand` (exact, from the game)
    and `virusEstimate`, which is kept inside the band and is snapped to the
    band's floor at the moment the band steps up -- the one instant a
    teammate's true figure is known. The estimate is built from what the
    bridge CAN see:

      passive   the character's own rate (2.DAT @0x1e5500: gauge units per
                one-second tick and the gauge size), run only while the local
                player's gauge is itself ticking -- that is the clock, because
                the gauge stops in door loads and cutscenes and the frame
                counter does not. The tick (0x63b1f0) was read in full on
                2026-10-11 (extracted_data/combat/virus-passive-model.json):
                condition, poison, bleeding and difficulty do NOT change it.
                What does: holding the Charm (x0.75), playing an NPC character
                (x0.5 to x1.6 by class), and two fast modes in which it runs
                every frame -- standing in gas, and being downed (x60 each,
                x120 together; x2 and x90 in Underbelly). Fractions are cut
                off on every add, as the game does. The gas and downed flags
                and the NPC class come from the teammate's record in memory
                (File 2 only); without them the plain rate is used
      hits      a sudden health drop is matched against the disc's attack
                table (attack-virus.json, both games): the enemies alive in the
                teammate's room, and which of their attacks can deal exactly
                that much on this difficulty. The virus added is the mean of
                the matching attacks' virus rows -- often zero; many attacks
                carry none. If the table is missing or nothing matches, it
                falls back to one typical hit (the median of the old table).
                File 1's rows are the ones its code reads, which are not the
                ones its data looks authored for -- unchecked in play, and the
                first thing to test there. With no living enemy listed in the room, a
                small drop is taken as bleeding or poison and adds nothing,
                and a large one still counts as a typical hit -- whether this
                machine's enemy list is current for rooms the local player is
                not in has NOT been checked

      the game's own word, where it has one (File 2, read straight from memory
      through Ram; layouts from the disc, extracted_data/combat/
      net-remote-knowledge.json, NOT yet seen on a running game, so every
      value is sanity-checked and the older rules above remain the fallback):
                when a teammate is hit their machine sends who did it. Player
                record +0x148 is the attacker's kind (2 = enemy) and +0x149
                its index in the enemy roster, whose +0x02 is the enemy type:
                the lookup is then confined to that one enemy. +0x08 says
                whether it was a strike (3) or a grab (5), which confines it
                further. A hit from another player or a partner adds nothing.
                +0xcbe is the id of the last item used on them, which catches
                cures from the herb case and cures given by someone else

    Every decision is written to virus-log.jsonl beside the bridge, one line
    per event, so a session can be read back afterwards and the rules judged
    against what the players say happened.
      cures     a teammate's inventory IS visible. When an antivirus or a
                blue herb leaves it -- and was not mixed into something or
                handed to another player in the same moment -- it is taken as
                used, and the gauge stands still for that item's time: Anti
                Virus 90 s, Anti Virus (L) 180 s, and 30 s for EVERY other
                recovery item -- herbs of any colour, mixes, sprays, pills,
                hemostat, antidote. Only Daylight does nothing. These are the
                mask bytes of item0xx.dat, identical in every File 2 scenario.
                A second dose restarts the pause, it does not extend it; and
                while a pause runs, hits add no virus either. Nothing in the
                game ever lowers the gauge.

    File 1 differs, and has its own layout (LAYOUT) and rules: no gas mode,
    no Charm, Yoko ticks slower, the gauge always starts at 0, Daylight pins
    it at 0 for the rest of the scenario, and there is no record of the last
    item used, so File 1 cures come from the inventory alone. Jim playing
    dead (x10) is recognised on both games from his personal-action state;
    in another room the game only shows that he went down, not when the
    lying-still part began, so the x10 starts a moment early there.

    NOT modelled: the White capsule round (arms a 90 s pause on whoever it
    hits), an item dropped on the floor (reads as used). Apart from File 1's
    Daylight the estimate only ever goes up. `virus` itself is left exactly
    as the game reports it.
    """

    # File 1 keeps the same gauge sizes but ticks Yoko at 30, not 45 (GAME.BIN 0x6e6c90):
    # 0.52 %/min against File 2's 0.78 -- the two figures the obsrv wiki gives for her
    YOKO_FILE1 = 30
    # character -> (gauge units added per tick, gauge size); a tick is 30 frames
    GAUGE = {"Jim": (24, 100800), "Mark": (60, 273600), "Kevin": (30, 151200),
             "George": (60, 345600), "David": (60, 345600), "Alyssa": (30, 201600),
             "Cindy": (15, 100800), "Yoko": (45, 345600)}
    # difficulty -> gauge units for one typical hit (median of the non-zero
    # attack types in extracted_data/combat/virus-table.json)
    HIT = {"easy": 900, "normal": 1500, "hard": 1800, "very hard": 2400}

    # (file, type, kind, difficulty) -> (gauge units, how many measurements).
    # Read off live runs where the game named the attacker, for the attacks
    # whose disc rows cannot be told apart by damage alone and whose matching
    # rows carry very different virus. Measured 2026-10-25 across four File 1
    # network Very Hard runs (virus-log.jsonl; sessions/rec-20261024-234925
    # and the three that follow it). These REPLACE the row average, so add a
    # line only with the samples to back it, and only for the difficulty
    # actually measured.
    #   ("1", 1, "g"): 11 samples, all 4200-5100 in 300 steps, mean 4809,
    #       median 4800. The table gave 3300 / 3825 / 4688: its [400,700] and
    #       [100,300] grab rows share a 2100-mean gradient that never fired.
    #   ("1", 19, "s"): 5 samples -- 0, 0, 0, 120, 800, mean 184. No row
    #       matched, so each fell back to HIT["very hard"] = 2400. Thin, and
    #       the spread is wide: see the ROADMAP item before trusting it.
    MEASURED = {
        ("1", 1, "g", "very hard"): (4800, 11),
        ("1", 19, "s", "very hard"): (180, 5),
    }
    # item type -> seconds the gauge stands still after using it (312, Daylight, is absent: none)
    CURE = {**{k: 30 for k in (300, 301, 302, 303, 304, 305, 306, 307, 308, 309, 310, 311, 313, 314, 315)},
            316: 90, 317: 180}
    CHARM = 420             # held anywhere in the inventory: the tick is x0.75
    NPC_CLASS = (0.5, 0.7, 0.9, 1.0, 1.2, 1.4, 1.6, 1.0)     # by record +0xbd2 & 7, when +0xbc8 != 0
    START = (0.0, 0.3, 0.5, 0.8)                             # session option u8 0x308094 & 3: starting gauge
    START_OPT = 0x308094
    # herbs and medicines: one of these appearing as a cure leaves means it was mixed, not used
    MIXES = set(range(300, 319))
    BANDS = ((0.0, 50.0), (50.0, 80.0), (80.0, 90.0), (90.0, 100.0))
    HOLD = 90               # frames a gauge must sit on a band floor to count as a teammate's
    CLOCK_GRACE = 90        # frames the clock stays open after the local gauge last moved

    def __init__(self):
        self._reset()
        self.attacks = {}
        self.told = {}          # slot -> a gauge figure a teammate read out, waiting for the next poll
        self.file = None        # which game is running: 1 or 2
        self.ram = Ram()
        self.log_path = (data_dir() if getattr(sys, "frozen", False)
                         else Path(__file__).resolve().parent) / "virus-log.jsonl"
        try:
            f = find_file("attack-virus.json")
            if f:
                doc = json.loads(f.read_text(encoding="utf-8"))
                self.attacks = doc.get("games") or {}        # "1" / "2" -> enemy type -> attacks
        except Exception:                           # noqa: BLE001 - the fallback covers it
            self.attacks = {}

    DIFF = {"easy": 0, "normal": 1, "hard": 2, "very hard": 3}

    SMALL = 0.05            # of max health: below this, an unexplained drop is bleeding or poison

    def _log(self, snap: dict, what: str, p: dict, **more):
        """One line per decision, for reading a session back. Never allowed to fail the poll."""
        try:
            game = snap.get("game") or {}
            row = {"t": round(time.time(), 1), "what": what, "scenario": game.get("scenario"),
                   "difficulty": game.get("difficulty"), "frames": game.get("frames"),
                   "slot": p.get("slot"), "name": p.get("name") or p.get("type"),
                   "room": p.get("roomID"), "HP": p.get("HP"), "est": round(self.est.get(p.get("slot"), 0), 2)}
            row.update(more)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:                           # noqa: BLE001
            pass

    # Where things are in each game's memory (runtime addresses). File 2 from
    # net-remote-knowledge.json and virus-passive-model.json, File 1 from
    # file1-remote-and-tick.json. The two games share the shape and almost none
    # of the numbers -- the status bits in particular mean different things.
    LAYOUT = {
        2: dict(rec=0x47bd30, stride=0x1100, hp=0x540, flags=0xbb8, flags2=0xbbc, item=0xcbe,
                roster=0x760630, count=0x49135e, master=0x7d7a90, start=0x308094,
                gas=0x04, down=0x08, full=0x10, daylight=0, jim=0x20, npc=(0xbc8, 0xbd2)),
        1: dict(rec=0x476dd0, stride=0x10e0, hp=0x544, flags=0xba0, flags2=0xba4, item=None,
                roster=0x71a030, count=0x48c01e, master=0x825810, start=None,
                gas=0, down=0x10, full=0x400, daylight=0x1000, jim=0x16, npc=None),
    }

    def _raw(self, p, snap: dict) -> dict | None:
        """What the game itself holds about this player beyond the tracker's fields, or None."""
        L = self.LAYOUT.get((snap.get("game") or {}).get("currentFile"))
        slot = p.get("slot") or 0
        if not L or not 1 <= slot <= 4:
            return None
        rec = self.ram.read(L["rec"] + (slot - 1) * L["stride"], 0xd00)
        if not rec or rec[2] != 0 or rec[3] != slot - 1:        # not a player record for this slot
            return None
        if struct.unpack_from("<h", rec, L["hp"])[0] != (p.get("HP") or 0):
            return None                                         # not the record the tracker is describing
        flags = struct.unpack_from("<I", rec, L["flags"])[0]
        flags2 = struct.unpack_from("<I", rec, L["flags2"])[0]
        out = {"state": rec[0x08], "sub": rec[0x09], "step": rec[0x0a], "sub2": rec[0x0b],
               "akind": rec[0x148], "aid": rec[0x149], "flags": flags, "room": rec[0x1e2],
               "item": struct.unpack_from("<H", rec, L["item"])[0] if L["item"] else None,
               "gas": bool(flags & L["gas"]), "down": bool(flags & L["down"]), "full": bool(flags & L["full"]),
               "daylight": bool(flags2 & L["daylight"]),
               # the personal action is state 2, and its sub-state is a base plus the character;
               # for Jim that is lying down playing dead, which the tick runs ten times faster
               "playdead": rec[0x08] == 2 and rec[0x09] == L["jim"] and rec[0x0a] not in (1, 2, 6),
               "held": rec[0x08] == 1 and rec[0x09] == 0xBA}    # File 2: in enemy type 44's hold (x30)
        if L["npc"]:
            npc = struct.unpack_from("<H", rec, L["npc"][0])[0]
            out["npc"], out["cls"] = npc, rec[L["npc"][1]] & 7
        out["master"] = self.ram.u8(L["master"] + out["room"] * 4)
        if out["akind"] == 2:
            count = self.ram.u8(L["count"]) or 0
            row = self.ram.read(L["roster"] + out["aid"] * 0x60, 0x24) if out["aid"] < count else None
            if row and row[1] != 0xff:
                out["atype"], out["aroom"] = row[2], row[0x22]
        return out

    def _hit_units(self, p, lost: int, snap: dict, raw: dict | None = None) -> tuple[float, str, list]:
        """(gauge units, rule used, enemies believed in the room) for a health drop of `lost`."""
        game = snap.get("game") or {}
        diff = str(game.get("difficulty") or "").lower()
        typical = self.HIT.get(diff, 1500)
        there = [[e.get("nameID"), e.get("name"), e.get("HP")] for e in (snap.get("enemies") or [])
                 if e.get("roomID") == p.get("roomID")]
        table = self.attacks.get(str(game.get("currentFile"))) or {}
        if not table or diff not in self.DIFF:
            return typical, "typical: no table for this game", there
        col = self.DIFF[diff] + 1                   # attack rows are [kind, E, N, H, VH]
        named = raw is not None and raw["state"] in (3, 5)       # a hit or grab the game is still showing
        if named and raw["akind"] in (0, 1):
            return 0.0, "none: hit by a player or partner", there
        if named and "atype" in raw and str(raw["atype"]) in table:
            types, kind, how = {raw["atype"]}, "g" if raw["state"] == 5 else "s", "attacker named by the game"
            seen = self.MEASURED.get((str(game.get("currentFile")), raw["atype"], kind, diff))
            if seen:                            # a live reading beats averaging rows that collide
                return float(seen[0]), f"measured live, {seen[1]} samples", there
        else:
            types, kind, how = {e[0] for e in there if 0 < (e[2] or 0) < 0x8000}, None, "enemies in the room"
            if not types:
                if lost < self.SMALL * (p.get("maxHP") or 0):
                    return 0.0, "none: small drop, no living enemy listed", there
                return typical, "typical: large drop, no living enemy listed", there
        for want in (kind, None):                   # first strike-or-grab as the game says, then either
            fits = [a[col][2] for tid in types for a in table.get(str(tid), ())
                    if (want is None or a[0] == want) and a[col][0] <= lost <= a[col][1]]
            if fits:
                return sum(fits) / len(fits), f"matched {len(fits)} attack(s), {how}", there
            if kind is None:
                break
        return typical, f"typical: no attack deals that, {how}", there

    def _reset(self):
        self.scen = None
        self.frames = None
        self.est = {}           # slot -> estimate, %
        self.hp = {}            # slot -> last HP seen
        self.bag = {}           # slot -> {item type: count} last seen
        self.calm = {}          # slot -> frames of passive still to be skipped (a cure in effect)
        self.ref_virus = None
        self.ref_moved = None   # frame the local gauge last moved
        self.seen = {}          # slot -> (gauge reading, frame it was first seen at that value)
        self.item = {}          # slot -> id of the last item used on them, as the game records it
        self.loc = None         # the local player's last reading, for _watch_local
        self.cured = {}         # slot -> frame a cure was last counted, so one use is not counted twice
        self.band = {}          # slot -> band index last seen

    def _per_frame(self, p, raw: dict | None = None, scenario: str = "") -> float | None:
        """Percent of gauge added per frame, by the tick's own arithmetic for the running game."""
        who = p.get("type") or p.get("name")
        g = self.GAUGE.get(who)
        if not g:
            return None
        one = self.file == 1
        rate = float(self.YOKO_FILE1 if who == "Yoko" and one else g[0])
        fast, mult = 0, 1.0
        if not one and self.CHARM in self._bag(p):              # File 1's tick has no Charm term
            rate *= 0.75
        if raw is not None:
            jim = who == "Jim" and raw["playdead"]
            if raw.get("npc"):
                mult = self.NPC_CLASS[raw["cls"]]
            if one:                                             # downed, or Jim playing dead; no gas mode
                fast = 10 if jim else 60 if raw["down"] else 0
            else:
                under = "underbelly" in scenario.lower()
                gas, down = raw["gas"], raw["down"]
                if raw["held"]:
                    fast = 30
                elif jim:
                    fast = (12 if under else 20) if gas else 10
                elif gas and down:
                    fast = 90 if under else 120
                elif down:
                    fast = 60
                elif gas:
                    fast = 2 if under else 60
        rate *= mult
        units = int(rate * fast / 30) if fast else int(rate) / 30.0     # every frame, or once a second
        return units * 100.0 / g[1]

    @staticmethod
    def _bag(p) -> dict:
        out = {}
        for it in (p.get("inventory") or []) + (p.get("specialInventory") or []):
            if isinstance(it, dict) and (it.get("id", -1) != -1) and (it.get("type") or 0) > 0:
                out[it["type"]] = out.get(it["type"], 0) + max(1, it.get("count") or 0)
        return out

    def _cures_used(self, players) -> dict:
        """slot -> seconds of cure taken since the last look."""
        now = {p.get("slot"): self._bag(p) for p in players}
        gained = {s: {k for k, v in bag.items() if v > self.bag.get(s, {}).get(k, 0)}
                  for s, bag in now.items()}
        used = {}
        for s, bag in now.items():
            old = self.bag.get(s)
            if old is None:
                continue
            for kind, secs in self.CURE.items():
                lost = old.get(kind, 0) - bag.get(kind, 0)
                if lost <= 0:
                    continue
                mixed = bool(gained[s] & self.MIXES)                       # became another herb or pill
                handed = any(kind in g for o, g in gained.items() if o != s)  # someone else now has it
                if not mixed and not handed:
                    used[s] = used.get(s, 0) + secs * lost
        self.bag = now
        return used

    def _band_of(self, p, frames) -> int | None:
        """Band index if this gauge is a stand-in written from the network, else None."""
        v, slot = p.get("virus") or 0.0, p.get("slot")
        g = self.GAUGE.get(p.get("type") or p.get("name"))
        last = self.seen.get(slot)
        if last is None or last[0] != v:
            self.seen[slot] = last = (v, frames)
        if not g:
            return None
        for i, (floor, _) in enumerate(self.BANDS):
            if abs(v - (floor + 500.0 / g[1])) < 1e-4:          # floor + 5 units, as a percent
                held = frames - last[1] >= self.HOLD or slot in self.band
                return i if held else None
        return None

    # ---- the local player, whose gauge is real: the check on every rule above ----

    def _watch_local(self, ref, snap: dict, frames: int):
        """Log what the local gauge actually does, beside what the rules here would have said.

        The local player's gauge is exact, so each of their hits, heals and
        minutes in gas is a measurement: how much virus that attack really
        added, how long that item really paused the tick, what the rate
        really was. Nothing here changes what the bridge reports; it only
        writes `local-*` lines to the log for review_session.py.
        """
        who = ref.get("type") or ref.get("name")
        g = self.GAUGE.get(who)
        if not g:
            return
        units = round((ref.get("virus") or 0) / 100.0 * g[1])
        raw = self._raw(ref, snap)
        scen = str((snap.get("game") or {}).get("scenario") or "")
        key = None if raw is None else [raw["gas"], raw["down"], raw["playdead"], raw["held"]]
        charm = self.CHARM in self._bag(ref)
        L = self.loc
        if not L or L.get("slot") != ref.get("slot") or units < L["units"]:
            self.loc = L = {"slot": ref.get("slot"), "units": units, "frames": frames, "moved": frames,
                            "hp": ref.get("HP") or 0, "room": ref.get("roomID"), "bag": self._bag(ref),
                            "item": raw["item"] if raw else None, "hits": [],
                            "seg": {"frames": frames, "units": units, "key": key, "charm": charm, "hits": 0,
                                    "expect": self._per_frame(ref, raw, scen)}}
            return
        # heals: the game's own record of the item, and what left the inventory
        bag = self._bag(ref)
        gone = [k for k, v in L["bag"].items() if bag.get(k, 0) < v and k in self.MIXES]
        if raw and raw["item"] != L["item"]:
            self._log(snap, "local-item", ref, item=raw["item"], how="the game's record", units=units)
            L["last_item"], L["item_frames"] = raw["item"], frames
        elif gone:
            self._log(snap, "local-item", ref, item=gone, how="left the inventory", units=units,
                      gained=[k for k, v in bag.items() if v > L["bag"].get(k, 0)])
            L["last_item"], L["item_frames"] = gone, frames
        L["bag"], L["item"] = bag, raw["item"] if raw else None
        # stalls: how long the gauge stood still (a heal's pause, a room load, a cutscene)
        if units != L["units"]:
            still = frames - L["moved"]
            if still >= 75:
                self._log(snap, "local-stall", ref, frames=still, seconds=round(still / 30.0, 1),
                          room_from=L["room"], last_item=L.get("last_item"),
                          item_to_resume=None if "item_frames" not in L else frames - L["item_frames"])
            L["moved"], L["room"] = frames, ref.get("roomID")
        # hits: note the gauge now, look again two seconds later
        hp = ref.get("HP") or 0
        if L["hp"] - hp >= max(30, 0.02 * (ref.get("maxHP") or 0)):
            pred, rule, there = self._hit_units(ref, L["hp"] - hp, snap, raw)
            L["hits"].append({"frames": frames, "lost": L["hp"] - hp, "before": L["units"], "pred": round(pred),
                              "rule": rule, "enemies": there, "raw": raw, "pool": self._attack_pool(snap),
                              "since_item": None if "item_frames" not in L else frames - L["item_frames"]})
            L["seg"]["hits"] += 1
        for h in [h for h in L["hits"] if frames - h["frames"] >= 60]:
            L["hits"].remove(h)
            self._log(snap, "local-hit", ref, lost=h["lost"], gained=units - h["before"], span=frames - h["frames"],
                      tick=g[0], predicted=h["pred"], rule=h["rule"], enemies=h["enemies"], raw=h["raw"],
                      pool=h["pool"], since_item=h["since_item"], hit_frames=h["frames"])
        # rate: close a segment when the fast-mode flags change, or every 30 s
        seg = L["seg"]
        if key != seg["key"] or charm != seg["charm"] or frames - seg["frames"] >= 900:
            span = frames - seg["frames"]
            if span >= 60:
                self._log(snap, "local-rate", ref, frames=span, units=units - seg["units"], hits=seg["hits"],
                          per_second=round((units - seg["units"]) * 30.0 / span, 2),
                          expected_per_second=None if seg["expect"] is None
                          else round(seg["expect"] * g[1] / 100.0 * 30, 2),
                          flags=seg["key"], charm=seg["charm"])
            L["seg"] = {"frames": frames, "units": units, "key": key, "charm": charm, "hits": 0,
                        "expect": self._per_frame(ref, raw, scen)}
        L["units"], L["frames"], L["hp"] = units, frames, hp

    POOL = (0x3bc500, 64, 0x40)     # File 2: attack objects in play (+0x0c live, +0x0d record index, +0x2a table)

    def _attack_pool(self, snap: dict) -> list | None:
        """File 2: the attack records being executed right now -- the direct answer to "which attack"."""
        if (snap.get("game") or {}).get("currentFile") != 2:
            return None
        base, count, size = self.POOL
        b = self.ram.read(base, count * size)
        if not b:
            return None
        return [[i, b[i * size + 0x0d], b[i * size + 0x2a]] for i in range(count) if b[i * size + 0x0c] == 1]

    def said(self, slot: int, pct: float):
        """A teammate read their gauge out. Taken up by the next poll: logged, and the estimate reset to it."""
        self.told[int(slot)] = float(pct)

    def feed(self, snap: dict):
        game = snap.get("game") or {}
        players = snap.get("players") or []
        frames = game.get("frames") or 0
        self.file = game.get("currentFile")
        if snap.get("state") != "in-game" or not players:
            self._reset()
            return
        if game.get("scenario") != self.scen or self.frames is None or frames < self.frames:
            self._reset()
            self.scen = game.get("scenario")
        bands = {p.get("slot"): self._band_of(p, frames) for p in players}
        stuck = [p for p in players if bands[p.get("slot")] is not None]
        live = [p for p in players if bands[p.get("slot")] is None and (p.get("virus") or 0) > 0.01]
        if live:
            try:
                self._watch_local(live[0], snap, frames)
            except Exception:                       # noqa: BLE001 - a measurement must not cost the estimate
                pass
        if not stuck or not live:
            self.frames = frames
            return
        ref = live[0]

        # the clock: is the local gauge ticking?
        if self.ref_virus is None or ref["virus"] != self.ref_virus:
            self.ref_moved = frames
        self.ref_virus = ref["virus"]
        step = frames - self.frames if self.frames is not None else 0
        self.frames = frames
        if not 0 <= step < 300:
            step = 0
        ticking = (ref.get("antivirustime") or 0) > 0 or frames - (self.ref_moved or 0) <= self.CLOCK_GRACE

        cures = self._cures_used(players)
        for p in stuck:
            slot = p.get("slot")
            raw = self._raw(p, snap)
            rate = self._per_frame(p, raw, str(game.get("scenario") or ""))
            if rate is None:
                continue
            secs, how = cures.get(slot, 0), "left their inventory"
            if raw is not None:
                if slot in self.item and raw["item"] != self.item[slot] and raw["item"] in self.CURE:   # File 2
                    secs, how = self.CURE[raw["item"]], "the game recorded the item used on them"
                self.item[slot] = raw["item"]
            if secs and frames - self.cured.get(slot, -999) > 90:
                self.cured[slot] = frames
                self.calm[slot] = max(self.calm.get(slot, 0), secs * 30)
                self._log(snap, "cure", p, seconds=secs, how=how, raw=raw)
            if slot not in self.est:
                # first sight (or the bridge was restarted mid-run): assume the
                # teammate has been ticking for as long as the local player has
                ref_rate, plain = self._per_frame(ref), self._per_frame(p)
                opt = self.ram.u8(self.START_OPT) if game.get("currentFile") == 2 else None   # File 1 starts at 0
                start = self.START[opt & 3] * 100.0 if opt is not None else 0.0
                since = max(0.0, ref["virus"] - start) / ref_rate if ref_rate else frames
                self.est[slot] = start + since * plain
            elif ticking and (p.get("antivirustime") or 0) <= 0:
                # HP 0 is downed, not dead: the tick runs x60 there (live, 2026-10-24), so no HP gate
                skip = min(step, self.calm.get(slot, 0))
                self.calm[slot] = self.calm.get(slot, 0) - skip
                self.est[slot] += (step - skip) * rate
            hp, was = p.get("HP") or 0, self.hp.get(slot)
            if was is not None and (p.get("bleedtime") or 0) <= 0 \
                    and was - hp >= max(30, 0.02 * (p.get("maxHP") or 0)):
                units, rule, there = self._hit_units(p, was - hp, snap, raw)
                if self.calm.get(slot, 0) > 0:
                    units, rule = 0.0, "none: a cure is in effect (" + rule + ")"
                self.est[slot] += units * 100.0 / self.GAUGE[p.get("type") or p.get("name")][1]
                self._log(snap, "hit", p, lost=was - hp, units=round(units), rule=rule, enemies=there,
                          local_room=ref.get("roomID"), raw=raw)
            self.hp[slot] = hp
            if slot in self.told:                   # they read it out: that is the truth, start again from it
                heard = self.told.pop(slot)
                self._log(snap, "truth", p, said=heard, est_before=round(self.est[slot], 2), band=bands[slot], raw=raw)
                self.est[slot] = heard
            b = bands[slot]
            lo, hi = self.BANDS[b]
            if b > self.band.get(slot, b):
                self._log(snap, "band", p, band=[lo, hi], est_before=round(self.est[slot], 2))
                self.est[slot] = lo                 # the band just stepped up: they are AT its floor
            self.band[slot] = b
            self.est[slot] = min(max(self.est[slot], lo), hi - 0.1 if hi < 100 else 100.0)
            if raw is not None and raw["full"]:                 # the game says their gauge is full
                self.est[slot] = 100.0
            if raw is not None and raw["daylight"]:             # File 1: Daylight pins the gauge at zero for good
                self.est[slot] = 0.0
            p["virusEstimate"] = round(self.est[slot], 2)
            p["virusBand"] = [lo, hi]
            if self.calm.get(slot, 0) > 0:
                p["virusCalm"] = round(self.calm[slot] / 30)        # seconds of cure left


# --------------------------------------------------------------------------
# the recorder -- keeps the raw bytes, so a session can be re-read later
# --------------------------------------------------------------------------

class EnemyPositions:
    """Where every enemy is, from the game's own two records of it.

    getEnemyList returns no coordinates, but the game holds them twice:

      entity  the enemy itself, run every frame, but only for the room this
              machine has loaded. Position is f32 at +0x38 (X, height, Z).
      roster  one 0x60 row per enemy in the level, kept for every room. Position
              is s32 at +0x10, written from network messages, so it moves in
              steps: median 3 s apart, 14 s at the 90th percentile, across all 15
              recordings. +0x01 is a pool index or 0xff, and 0 means never built.

    So the entity wins where there is one and the roster covers everywhere else.
    `src` says which: e is exact and current, r is the last known place, s is
    the authored spawn point of something nobody has met yet.

    The join is the game's own (File 2 0x6efe00, File 1 0x60d670): scan the pool
    for a slot in use whose roster index matches. Roster +0x01 is not used as an
    index, because that is not how the game finds it either. Everything here is
    read-only and every failure leaves `enemies` as it was.

    HP has the same two copies. `HP` stays the roster's, which is what the tracker
    shows and everything recorded so far was measured on. It covers every room but
    only moves when a hit-type message lands. `liveHP` is the entity's own, added
    only where its max equals the roster's, so a wrong offset falls back to the
    roster instead of showing a wrong number. /status `enemyHp` counts how often
    the two agree.

    `boss` is the display name of anything in BOSSES, for the overlay and the guide.
    The list is by type because HP cannot do it: Nyx's body is 2,000 on Easy and
    Fire (type 60) is 5,000 on every difficulty. Types 17, 26, 37 and 60 are 5,000
    and left out: hazards and scenery, none took a real run of damage on record.
    Gnaw (16) is left out by choice. A type 33 sharing the Stalker's room is one of
    the two lionesses in that fight, and gets a bar of its own.
    """

    # base, stride, offset of the roster index, offset of HP and max HP
    POOL = {2: (0x403db0, 0xdb0, 0xbb0, 0x540), 1: (0x3ffca0, 0xd90, 0xb98, 0x544)}
    SLOTS = 50
    BOSSES = {15: "Nyx Tyrant", 19: "Giant Leech", 20: "Suspended", 29: "G-Type",
              32: "Titan", 33: "Stalker", 41: "Tyrant", 42: "Nyx Body", 43: "Thanatos",
              44: "Nyx Core", 45: "Axe Man", 51: "Dorothy", 64: "Giga Bite"}
    STALKER_MIN = 4000      # type 33 is also every ordinary lioness, 800 to 2,300

    # The one door played as a fight: File 1, Outbreak, the north doors of Behind the
    # apartment, which the cop shoots. Room-door HP table 0x472f90, s16, 0x30 a room,
    # room 19 sub 16, set by r001.evb 0x518. Open is script flag 0x48ad92 & 0x80.
    # getDoor's 19 slots do not include it. _docs/combat-datamine-notes.md.
    BOSS_DOOR = (0x473340, 0x48ad92, 0x80, 19)
    BOSS_DOOR_HP = {"easy": 10500, "normal": 14000, "hard": 17500, "very hard": 30000}

    def __init__(self, ram: "Ram"):
        self.ram = ram

    def feed(self, snap: dict):
        file = (snap.get("game") or {}).get("currentFile")
        L, P = VirusEstimator.LAYOUT.get(file), self.POOL.get(file)
        if snap.get("state") != "in-game" or not L or not P:
            return
        count = self.ram.u8(L["count"]) or 0
        roster = self.ram.read(L["roster"], count * 0x60) if count else None
        self.apply(snap, roster, self.ram.read(P[0], self.SLOTS * P[1]), file)
        self.boss_door(snap)

    def boss_door(self, snap: dict):
        game = snap.get("game") or {}
        if game.get("currentFile") != 1 or game.get("scenario") != "outbreak":
            return
        hp_at, open_at, open_bit, room = self.BOSS_DOOR
        hp, flags = self.ram.read(hp_at, 2), self.ram.u8(open_at)
        if hp and flags is not None:
            snap["bossDoor"] = {"HP": struct.unpack("<h", hp)[0], "room": room, "open": bool(flags & open_bit),
                                "maxHP": self.BOSS_DOOR_HP.get(game.get("difficulty"))}

    @classmethod
    def entities(cls, pool: bytes | None, file: int) -> list:
        """[[pool slot, roster index, x, height, z, heading, room, type, HP, max HP], ...]"""
        P = cls.POOL.get(file)
        if not pool or not P:
            return []
        out = []
        for n in range(cls.SLOTS):
            b = n * P[1]
            if b + P[1] > len(pool) or not pool[b] or pool[b + 2] != 2:
                continue
            x, y, z = struct.unpack_from("<fff", pool, b + 0x38)
            if not all(math.isfinite(v) and abs(v) < 1e6 for v in (x, y, z)):
                continue                                # a torn read, or not an enemy
            out.append([n, pool[b + P[2]], round(x, 1), round(y, 1), round(z, 1),
                        struct.unpack_from("<H", pool, b + 0x92)[0],
                        struct.unpack_from("<H", pool, b + 0x1e2)[0], pool[b + 3],
                        *struct.unpack_from("<hh", pool, b + P[3])])
        return out

    @classmethod
    def boss(cls, e: dict) -> str | None:
        kind, top = e.get("nameID"), e.get("maxHP") or 0
        if top >= 32767 or (kind == 33 and top < cls.STALKER_MIN):
            return None                                 # 32767 is a prop: type 41 has four
        return cls.BOSSES.get(kind)

    @staticmethod
    def live_hp(e: dict, q: list) -> bool:
        """Put the entity's HP on e if q really is e. Rows recorded before 2026-11 have none."""
        if len(q) > 9 and q[9] > 0 and q[9] == e.get("maxHP"):
            e["liveHP"] = max(0, q[8])                  # s16, and a killing blow can pass zero
            return True
        return False

    @classmethod
    def apply(cls, snap: dict, roster: bytes | None, pool: bytes | None, file: int):
        """Put x / y / z / facing / src / liveHP / boss on each enemy with a roster row."""
        ents = cls.entities(pool, file)
        live = {}
        for e in ents:
            live.setdefault(e[1], e)                    # first in the pool, as the game's scan
        fix = {"entity": 0, "roster": 0, "spawn": 0}
        hp = {"entity": 0, "differs": 0}
        count = len(roster) // 0x60 if roster else 0
        lair = {e.get("roomID") for e in snap.get("enemies") or []
                if e.get("nameID") == 33 and cls.boss(e)}
        for e in snap.get("enemies") or []:
            name = cls.boss(e)
            if not name and e.get("nameID") == 33 and e.get("roomID") in lair \
                    and (e.get("maxHP") or 0) < 32767:
                name = "Lioness"
            if name:
                e["boss"] = name
            i = (e.get("slot") or 0) - 1
            if not 0 <= i < count:
                continue
            row = roster[i * 0x60:(i + 1) * 0x60]
            if row[0] != i:
                continue
            q = live.get(i)
            if q and row[1] != 0xff:
                e.update(x=q[2], y=q[3], z=q[4], facing=q[5], posRoom=q[6], src="e")
                fix["entity"] += 1
                if cls.live_hp(e, q):
                    hp["entity"] += 1
                    hp["differs"] += e["liveHP"] != e.get("HP")
                continue
            x, y, z = struct.unpack_from("<iii", row, 0x10)
            e.update(x=x, y=y, z=z, facing=struct.unpack_from("<I", row, 0x0c)[0] & 0xffff,
                     src="s" if row[1] == 0 else "r")
            fix["spawn" if row[1] == 0 else "roster"] += 1
        snap["enemyFix"] = fix
        snap["enemyHp"] = hp
        if pool is not None:
            snap["entities"] = ents

    @classmethod
    def fixes(cls, roster: bytes | None, pool: bytes | None, file: int) -> list:
        """[[roster index, x, z, facing, room], ...], by the same tests as apply()."""
        count = len(roster) // 0x60 if roster else 0
        live = {}
        for e in cls.entities(pool, file):
            live.setdefault(e[1], e)
        return [[i, q[2], q[4], q[5], q[6]] for i, q in sorted(live.items())
                if i < count and roster[i * 0x60] == i and roster[i * 0x60 + 1] != 0xff]


class FastPositions(threading.Thread):
    """Enemy and player positions at FAST_HZ, on their own, for the map.

    The 4 Hz poll is a full read of the game through the tracker, and the virus
    estimator, the recorder and watch_run all hang off it, so it stays as it is.
    This reads only the coordinates, straight from memory, and pushes them down
    /pos as a stream: one connection, sent as they change, so there is no second
    clock to beat against the first and nothing to reconnect for each update.

    It has its own Ram, so it never touches the Lua state, and it reads nothing
    unless someone is listening or a run is being recorded. Losing it costs only
    the smoothness: the guide is still drawing from /live.json at 4 Hz underneath.

    While the Recorder has a run open, every changed message also goes to
    sessions/pos-<start>.jsonl.gz, the same <start> as its rec file, because
    nothing else keeps this rate. Written in batches so a killed bridge loses
    LOG_EVERY seconds at most, and --replay-rec plays the file back down /pos.
    """

    daemon = True
    LOG_EVERY = 5.0

    def __init__(self, poller, ram=None):
        super().__init__(name="fast-positions")
        self.poller = poller
        self.ram = ram or Ram()
        self.viewers = 0
        self.seq = 0
        self.body = None
        self.sent = 0
        self._cv = threading.Condition()
        self._halt = threading.Event()
        self._log_path = None
        self._log_lines = []
        self._log_at = 0.0

    def read(self, snap: dict) -> dict | None:
        file = (snap.get("game") or {}).get("currentFile")
        L, P = VirusEstimator.LAYOUT.get(file), EnemyPositions.POOL.get(file)
        if snap.get("state") != "in-game" or not L or not P:
            return None
        count = self.ram.u8(L["count"]) or 0
        roster = self.ram.read(L["roster"], count * 0x60) if count else None
        players = []
        for i in range(4):
            rec = self.ram.read(L["rec"] + i * L["stride"], 0x44)
            if not rec or rec[2] != 0 or rec[3] != i:
                continue
            x, _, z = struct.unpack_from("<fff", rec, 0x38)
            if math.isfinite(x) and math.isfinite(z):
                players.append([i + 1, round(x, 1), round(z, 1)])
        return {"e": EnemyPositions.fixes(roster, self.ram.read(P[0], EnemyPositions.SLOTS * P[1]), file),
                "p": players}

    def join_stream(self, n: int):
        with self._cv:
            self.viewers += n

    def wait(self, seen: int, timeout: float) -> tuple[int, bytes | None]:
        """The newest message after `seen`, or None if nothing changed in `timeout`."""
        with self._cv:
            self._cv.wait_for(lambda: self.seq != seen, timeout)
            return self.seq, (self.body if self.seq != seen else None)

    def tick(self):
        m = self.read(self.poller.snapshot)
        body = json.dumps(m, separators=(",", ":")).encode("utf-8") if m else None
        changed = bool(body) and body != self.body
        if changed:
            with self._cv:
                self.body = body
                self.seq += 1
                self.sent += 1
                self._cv.notify_all()
        self._log(m if changed else None)

    def _recording(self) -> Path | None:
        rec = getattr(self.poller, "recorder", None)
        return rec.path if rec and rec.key else None

    def _log(self, m: dict | None):
        path = self._recording()
        if path != self._log_path:
            self.flush()
            self._log_path = path
        if path and m:
            snap = self.poller.snapshot
            self._log_lines.append(json.dumps(
                {"t": round(time.time(), 3), "frames": (snap.get("game") or {}).get("frames"),
                 "rooms": [[p.get("slot"), p.get("roomID")] for p in snap.get("players") or []],
                 **m}, separators=(",", ":")))
        if time.time() - self._log_at >= self.LOG_EVERY:
            self.flush()

    def flush(self):
        lines, self._log_lines = self._log_lines, []
        self._log_at = time.time()
        if lines and self._log_path:
            path = self._log_path.with_name(self._log_path.name.replace("rec-", "pos-", 1))
            with gzip.open(path, "ab") as f:
                f.write(("\n".join(lines) + "\n").encode("utf-8"))

    def stop(self):
        self._halt.set()

    def run(self):
        # A fixed schedule, not a sleep per loop: Windows rounds a wait up to its
        # 15.6 ms tick, which turned 67 ms into 78 until the overshoot was paid back.
        period = 1.0 / FAST_HZ
        due = time.time()
        while not self._halt.is_set():
            if not self.viewers and not self._recording():
                try:
                    self._log(None)                 # a run that just ended still owes its tail
                except Exception:                   # noqa: BLE001 - nor must the recording
                    pass
                self._halt.wait(0.25)
                due = time.time()
                continue
            try:
                self.tick()
            except Exception:                       # noqa: BLE001 - a position must never cost the bridge
                pass
            due = max(due + period, time.time() - period)
            self._halt.wait(max(0.0, due - time.time()))
        try:
            self.flush()
        except Exception:                           # noqa: BLE001 - nor must the recording
            pass


class Recorder:
    """Once a second, save the memory the estimator reads, as it was.

    The record layouts in VirusEstimator.LAYOUT were read off the disc and had
    not been seen on a running game when this was written. If one of them is
    wrong, a log of decoded values only shows that something is off; the bytes
    show what is right. So during a match this writes the four player records,
    the enemy roster, the room-master table and (File 2) the attack pool to
    sessions/rec-<start>.jsonl.gz beside the bridge: one JSON line a second,
    and every poll for three seconds after anyone's health changes. About
    30 MB an hour. Off in the packaged build and with --no-record.
    """

    LIMIT = 400 * 1024 * 1024       # stop a file that has somehow grown past this

    def __init__(self, ram: Ram, on: bool = True):
        self.ram, self.on = ram, on and not getattr(sys, "frozen", False)
        self.path = None
        self.key = None
        self.last = -999
        self.hot = -999
        self.hp = {}

    @staticmethod
    def _pack(b: bytes | None) -> str | None:
        return base64.b64encode(zlib.compress(b, 6)).decode("ascii") if b else None

    def feed(self, snap: dict):
        game = snap.get("game") or {}
        L = VirusEstimator.LAYOUT.get(game.get("currentFile"))
        if not self.on or snap.get("state") != "in-game" or not L:
            self.key = None
            return
        frames = game.get("frames") or 0
        players = snap.get("players") or []
        key = (game.get("scenario"), game.get("currentFile"))
        if key != self.key or frames < self.last:           # a new run: a new file
            self.key = key
            folder = Path(__file__).resolve().parent / "sessions"
            folder.mkdir(exist_ok=True)
            self.path = folder / time.strftime("rec-%Y%m%d-%H%M%S.jsonl.gz")
            self.last = -999
        hp = {p.get("slot"): p.get("HP") for p in players}
        if hp != self.hp:
            self.hp, self.hot = hp, frames
        if frames - self.last < 30 and frames - self.hot > 90:
            return
        if self.path.exists() and self.path.stat().st_size > self.LIMIT:
            return
        self.last = frames
        count = self.ram.u8(L["count"]) or 0
        row = {"t": round(time.time(), 2), "frames": frames, "scenario": game.get("scenario"),
               "difficulty": game.get("difficulty"), "file": game.get("currentFile"),
               "players": [[p.get("slot"), p.get("name"), p.get("HP"), p.get("virus"), p.get("roomID"),
                            p.get("virusEstimate")] for p in players],
               "enemies": [[e.get("slot"), e.get("nameID"), e.get("HP"), e.get("roomID")]
                           for e in (snap.get("enemies") or [])],
               "rec": [self._pack(self.ram.read(L["rec"] + i * L["stride"], L["stride"])) for i in range(4)],
               "roster": self._pack(self.ram.read(L["roster"], count * 0x60)) if count else None,
               "masters": self._pack(self.ram.read(L["master"], 0x100)),
               "entities": snap.get("entities"), "doors": snap.get("doors"), "bossDoor": snap.get("bossDoor"),
               "killedzombies": game.get("killedzombies"), "fighttime2": game.get("fighttime2")}
        if game.get("currentFile") == 2:
            base, n, size = VirusEstimator.POOL
            row["pool"] = self._pack(self.ram.read(base, n * size))
        if not any(row["rec"]):
            return                                          # no emulator to read: nothing worth a line
        with gzip.open(self.path, "ab") as f:
            f.write((json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# the poller
# --------------------------------------------------------------------------

_ENEMY_NAMES = None


def enemy_names() -> dict[int, str]:
    """Enemy type id -> name, from extracted_data/combat/enemy-type-names.json.

    Only names the file gives; an unnamed type stays unnamed. The packaged app
    carries the file inside it. Missing file: no names, which the guide does not
    need -- it joins on nameID itself.
    """
    global _ENEMY_NAMES
    if _ENEMY_NAMES is None:
        _ENEMY_NAMES = {}
        f = find_file("enemy-type-names.json") or \
            (Path(__file__).resolve().parent.parent.parent / "extracted_data" / "combat" / "enemy-type-names.json")
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            for k, v in (doc.get("types") or {}).items():
                if v.get("name"):
                    _ENEMY_NAMES[int(k)] = v["name"]
        except Exception:                           # noqa: BLE001
            pass
        for k, v in EnemyPositions.BOSSES.items():
            _ENEMY_NAMES.setdefault(k, v)
    return _ENEMY_NAMES


class ShadowLog:
    """Runs our reader beside the DLL and writes down where they disagree.

    This is the evidence that lets the DLL go: once a run of each file passes
    with nothing here but the known differences, our reader is trusted alone.
    In-game polls only. One line a second at most, and only when something
    differs, to sessions/shadow-<start>.jsonl.gz. Totals per field go to /status.
    """

    EVERY = 1.0

    def __init__(self):
        self.compared = 0
        self.mismatched = 0
        self.fields: dict[str, int] = {}
        self.path = None
        self.key = None
        self._last = 0.0

    @staticmethod
    def _field(path: str) -> str:
        return re.sub(r"\[\d+\]", "[]", path)

    def feed(self, dll: dict, ours: dict):
        if dll.get("state") != "in-game" and ours.get("state") != "in-game":
            return
        self.compared += 1
        d = game_reader.diff(dll, ours)
        if not d:
            return
        self.mismatched += 1
        for path, _, _ in d:
            f = self._field(path)
            self.fields[f] = self.fields.get(f, 0) + 1
        now = time.time()
        if now - self._last < self.EVERY:
            return
        self._last = now
        game = dll.get("game") or {}
        key = (game.get("scenario"), game.get("currentFile"))
        if key != self.key or self.path is None:
            self.key = key
            folder = (data_dir() if getattr(sys, "frozen", False)
                      else Path(__file__).resolve().parent) / "sessions"
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / time.strftime("shadow-%Y%m%d-%H%M%S.jsonl.gz")
        row = {"t": round(now, 2), "frames": game.get("frames"), "scenario": game.get("scenario"),
               "file": game.get("currentFile"), "diff": d[:200], "n": len(d)}
        with gzip.open(self.path, "ab") as f:
            f.write((json.dumps(row, separators=(",", ":"), default=str) + "\n").encode("utf-8"))

    def summary(self) -> dict:
        top = sorted(self.fields.items(), key=lambda kv: -kv[1])[:25]
        return {"compared": self.compared, "mismatched": self.mismatched,
                "fields": dict(top), "log": self.path.name if self.path else None}


class Poller(threading.Thread):
    """Owns the reader (and the Lua state, if the DLL is used); publishes the newest snapshot."""

    daemon = True

    def __init__(self, lua_dll: Path | None, tracker_dll: Path | None, record: bool = True,
                 reader: str = "auto"):
        super().__init__(name="poller")
        self.reader = reader            # auto | native | dll | shadow
        self.using = None               # which reader is actually publishing
        self.shadow = None              # ShadowLog when both run
        self._lock = threading.Lock()
        self._snap = {"state": "starting"}
        self._stop = threading.Event()
        self.lua_dll, self.tracker_dll = lua_dll, tracker_dll
        self.polls = 0
        self.note = ""
        self.virus = VirusEstimator()
        self.enemy_pos = EnemyPositions(self.virus.ram)
        self.recorder = Recorder(self.virus.ram, on=record)
        self.fast = FastPositions(self)

    @property
    def snapshot(self) -> dict:
        with self._lock:
            return self._snap

    def _publish(self, d: dict):
        d["t"] = time.time()
        d["polls"] = self.polls
        d["fast"] = self.fast.is_alive()
        with self._lock:
            self._snap = d

    def stop(self):
        self.fast.stop()
        self._stop.set()

    def _open_reader(self):
        """The reader that publishes, and a second one when shadowing.

        Our own reader needs nothing but the emulator, so it is always available.
        The DLL is used only when asked for, or when it is present and the mode
        is auto or shadow. A DLL that fails to load drops us to our own reader,
        never to nothing.
        """
        native = game_reader.GameReader(self.virus.ram, enemy_names())
        if self.reader == "native":
            return native, None, "native"
        dll = None
        if self.lua_dll and self.tracker_dll:
            try:
                dll = Vm(self.lua_dll, self.tracker_dll)
            except Exception as e:                  # noqa: BLE001 - degrade, never die
                self.note = f"tracker DLL did not load ({type(e).__name__}: {e}); using our reader"
        elif self.reader in ("dll", "shadow"):
            self.note = "tracker DLL not found; using our reader"
        if dll is None:
            return native, None, "native"
        if self.reader == "shadow":
            return dll, native, "dll+shadow"
        return dll, None, "dll"

    def run(self):
        vm, second, self.using = self._open_reader()
        if second is not None:
            self.shadow = ShadowLog()

        self.fast.start()
        period = 1.0 / POLL_HZ
        virus = self.virus
        last_init = 0.0
        inited = False
        while not self._stop.is_set():
            t0 = time.time()
            try:
                if not inited and t0 - last_init >= REINIT_EVERY:
                    last_init = t0
                    inited = vm.init()
                snap = vm.snap()
                self.polls += 1
                snap["reader"] = self.using
                if second is not None:
                    try:
                        self.shadow.feed(snap, second.snap())
                    except Exception:               # noqa: BLE001 - a comparison must never cost the snapshot
                        pass
                # a state that says the emulator went away -> re-init next tick
                if snap.get("state") in ("no-emulator", "not-outbreak"):
                    inited = False
                try:
                    virus.feed(snap)
                except Exception:                   # noqa: BLE001 - an estimate must never cost the snapshot
                    pass
                try:
                    self.enemy_pos.feed(snap)
                except Exception:                   # noqa: BLE001 - nor must a position
                    pass
                try:
                    self.recorder.feed(snap)
                except Exception:                   # noqa: BLE001 - nor must the recording
                    pass
                self._publish(snap)
            except Exception as e:                  # noqa: BLE001
                self._publish({"state": "reader-error", "note": f"{type(e).__name__}: {e}"})
                inited = False
            self._stop.wait(max(0.0, period - (time.time() - t0)))


# --------------------------------------------------------------------------
# the replay -- how you test this without the game
# --------------------------------------------------------------------------

class RecReplay:
    """Stand-in for Poller: plays back a real recorded run, enemies and all.

    --replay walks a player between disc spawn points. This one invents nothing:
    every position comes out of the raw memory in a sessions/rec-*.jsonl.gz, the
    players from their records at +0x38 and the enemies from the roster, so what
    moves on the map is what moved in that run. Runs recorded before 2026-10-31
    have no entity pool, so their enemies are all roster fixes and step every
    few seconds. A run recorded since carries `entities` and shows the real
    thing, and one with a pos file beside it streams /pos at the rate it had live.

    Every snapshot carries replay: true, so the guide prints REPLAY.
    """

    def __init__(self, path: Path, speed: float = 1.0, start: float = 0.0):
        self.path, self.speed, self.skip = path, max(0.1, speed), max(0.0, start)
        self.polls = 0
        self.t0 = time.time()
        self.lines, self.times = [], []
        with gzip.open(path, "rt", encoding="utf-8") as f:
            try:
                for line in f:
                    r = json.loads(line)
                    self.times.append(r["t"])
                    self.lines.append(json.dumps(self._snap(r), separators=(",", ":")).encode("utf-8"))
            except (EOFError, OSError, json.JSONDecodeError):
                pass                                    # a run cut off mid-line is still a run
        if not self.lines:
            raise SystemExit(f"replay: nothing readable in {path}")
        self.span = max(1.0, self.times[-1] - self.times[0])
        self.note = f"{len(self.lines)} samples, {self.span / 60:.1f} min"
        pos = path.with_name(path.name.replace("rec-", "pos-", 1))
        self.fast = PosReplay(pos, self.at) if pos.is_file() else None
        if self.fast:
            self.note += f", {len(self.fast.times)} stream messages"

    @staticmethod
    def _blob(b: str | None) -> bytes | None:
        return zlib.decompress(base64.b64decode(b)) if b else None

    @staticmethod
    def _max_hp(roster: bytes | None, slot: int, hp: int) -> int:
        """Rec lines keep current HP only; the max is in the raw roster beside them."""
        at = (slot - 1) * 0x60 + 0x1e
        return struct.unpack_from("<H", roster, at)[0] if roster and at + 2 <= len(roster) else hp

    @classmethod
    def _snap(cls, r: dict) -> dict:
        file = r.get("file")
        L = VirusEstimator.LAYOUT.get(file) or {}
        roster = cls._blob(r.get("roster"))
        players = []
        for slot, name, hp, virus, room, est in r.get("players") or []:
            p = dict(slot=slot, name=name, HP=hp, virus=virus, roomID=room, virusEstimate=est,
                     enabled=True, inGame=True)
            rec = cls._blob((r.get("rec") or [None] * 4)[slot - 1])
            if rec and rec[2] == 0 and rec[3] == slot - 1:
                p["positionX"], _, p["positionY"] = struct.unpack_from("<fff", rec, 0x38)
                p["maxHP"] = struct.unpack_from("<h", rec, L["hp"] + 2)[0]
            players.append(p)
        snap = {"state": "in-game", "replay": True,
                "game": {"currentFile": file, "scenario": r.get("scenario"),
                         "difficulty": r.get("difficulty"), "frames": r.get("frames"),
                         "killedzombies": r.get("killedzombies"), "fighttime2": r.get("fighttime2")},
                "players": players, "items": [], "roomItems": [], "doors": r.get("doors"), "bossDoor": r.get("bossDoor"),
                "enemies": [dict(slot=s, nameID=n, HP=hp, maxHP=cls._max_hp(roster, s, hp), roomID=room)
                            for s, n, hp, room in r.get("enemies") or []]}
        EnemyPositions.apply(snap, roster, None, file)
        ents = r.get("entities")
        if ents is not None:                            # written decoded, so join by hand
            live = {}
            for q in ents:
                live.setdefault(q[1], q)
            for e in snap["enemies"]:
                q = live.get((e.get("slot") or 0) - 1)
                if q:
                    e.update(x=q[2], y=q[3], z=q[4], facing=q[5], posRoom=q[6], src="e")
                    EnemyPositions.live_hp(e, q)
        return snap

    def start(self):
        self.t0 = time.time()

    def stop(self):
        pass

    def at(self) -> float:
        """Where playback is, in the recording's own time."""
        return self.times[0] + (self.skip + (time.time() - self.t0) * self.speed) % self.span

    @property
    def snapshot(self) -> dict:
        self.polls += 1
        d = json.loads(self.lines[min(bisect.bisect_left(self.times, self.at()), len(self.lines) - 1)])
        d["t"] = time.time()
        d["polls"] = self.polls
        if self.fast:
            d["fast"] = True
        return d


class PosReplay:
    """FastPositions' place in a --replay-rec: the recorded stream, on RecReplay's clock."""

    def __init__(self, path: Path, clock):
        self.clock = clock
        self.times, self.bodies = [], []
        self.viewers = self.sent = 0
        with gzip.open(path, "rt", encoding="utf-8") as f:
            try:
                for line in f:
                    r = json.loads(line)
                    self.times.append(r["t"])
                    self.bodies.append(json.dumps({"e": r["e"], "p": r["p"]},
                                                  separators=(",", ":")).encode("utf-8"))
            except (EOFError, OSError, json.JSONDecodeError):
                pass                                    # a run cut off mid-line is still a run

    def join_stream(self, n: int):
        self.viewers += n

    def wait(self, seen: int, timeout: float) -> tuple[int, bytes | None]:
        """As FastPositions.wait; `seen` is a position in the file, and goes back on a loop."""
        end = time.time() + timeout
        while True:
            at = bisect.bisect_right(self.times, self.clock())
            if at and at != seen:
                self.sent += 1
                return at, self.bodies[at - 1]
            if time.time() >= end:
                return seen, None
            time.sleep(0.01)


class Replay:
    """Stand-in for Poller: replays a saved capture with the player walking.

    Test scaffolding, not a second game reader.  It loads no DLL and never
    touches the emulator, so the entire live path -- position, room, zone, sheet
    and floor changes, FOLLOW, the stream view, the no-spawn-point message --
    can be exercised and shown with nothing running but this script.

    It invents no coordinates.  The route is walked between REAL disc item
    spawn points: world coordinates out of extracted_data/spawn-positions.json,
    joined to their room through the same Item Maps fit that the guide's own
    LIVE_MAP is built from.  So the input is the same kind of number the game
    emits, taken from the same disc, and a dot that lands wrong lands visibly
    wrong.  Levels are matched by NAME -- the three room/level code spaces in
    this project do not line up, and a name does.

    Every snapshot carries replay: true.  The guide prints REPLAY when it sees
    that, so a rehearsal can never be mistaken for a live read.

    No thread and no lock: there is nothing to poll, so the snapshot is simply
    computed from the clock when a request asks for it.
    """

    DWELL = 6.0                 # seconds spent walking one room before moving on
    WAYPOINTS = 8               # most spawn points to thread through in a room

    def __init__(self, capture: Path, players: int = 0):
        self.capture = capture
        self.players = players
        self.polls = 0
        self.note = ""
        self.t0 = time.time()
        self.base = json.loads(capture.read_text(encoding="utf-8"))
        self.route = self._route()

    # -- the plumbing Handler and main() expect of a poller ------------------

    def start(self):
        pass

    def stop(self):
        pass

    @property
    def snapshot(self) -> dict:
        self.polls += 1
        d = copy.deepcopy(self.base)
        d["replay"] = True
        d["t"] = time.time()
        d["polls"] = self.polls
        crew = d.get("players") or []
        if self.players > 1 and crew:
            one = crew[0]
            crew = []
            for i in range(self.players):
                q = copy.deepcopy(one)
                q["slot"] = i + 1
                if i:
                    q["name"] = f"PLAYER {i + 1}"
                crew.append(q)
            d["players"] = crew
        self._place(crew)
        return d

    # -- building the route --------------------------------------------------

    @staticmethod
    def _norm(s: str) -> str:
        return "".join(c for c in s.lower() if c.isalnum())

    def _repo(self) -> Path | None:
        """Where the disc-derived data lives.  Only the repo has it; the packaged
        exe does not, and a replay from there simply does not move."""
        for b in _bases():
            if (b / "Item Maps" / "data").is_dir() and (b / "extracted_data").is_dir():
                return b
        return None

    def _route(self) -> list:
        """[(room_no, [(worldX, worldZ), ...]), ...] -- the tour, in room order."""
        want = self._norm((self.base.get("game") or {}).get("scenario") or "")
        root = self._repo()
        if not want:
            self.note = "the capture names no scenario -- replaying it unmoved"
            return []
        if not root:
            self.note = "no disc data next to me -- replaying the capture unmoved"
            return []
        for p in sorted((root / "Item Maps" / "data").glob("r0*-spawn-map.json")):
            doc = json.loads(p.read_text(encoding="utf-8"))
            if self._norm(doc.get("level") or "") == want:
                return self._tour(root, doc)
        self.note = f"no item map for {want!r} -- replaying the capture unmoved"
        return []

    def _tour(self, root: Path, doc: dict) -> list:
        pos = json.loads((root / "extracted_data" / "spawn-positions.json")
                         .read_text(encoding="utf-8"))
        world = {P["pt"]: (P["x"], P["z"])
                 for P in (pos.get(doc.get("scenario")) or {}).get("points", [])
                 if P and P.get("pt") is not None}
        rooms: dict[int, list] = {}
        for f in doc.get("floors", {}).values():
            for q in f.get("points", []):
                w = world.get(q.get("pt"))
                if w:
                    rooms.setdefault(q["room_no"], []).append(w)
        route = [(n, rooms[n][:self.WAYPOINTS]) for n in sorted(rooms)]
        # A room the guide cannot place anyone in, if the level has one, so the
        # replay walks the honest "no spawn point in this room" path as well.
        blank = self._roomless(root, doc.get("scenario"), set(rooms))
        if blank and route:
            route.insert(len(route) // 2, (blank[0], []))
        self.note = (f"{doc.get('level')}: {len(route)} rooms, "
                     f"{sum(len(w) for _, w in route)} real spawn points"
                     + (f", 1 with none ({len(blank)} in the level)" if blank else ""))
        return route

    @staticmethod
    def _roomless(root: Path, scen: str, anchored: set) -> list:
        """Real rooms of this scenario that no item ever spawns in, so the guide
        has nothing to anchor a position against and says so instead of guessing.

        Straight off the disc's own room-name table, not inferred: a room number
        here is a place you can stand, whether or not anything is lying in it.
        Standing in one is the only way to rehearse that message."""
        try:
            names = json.loads((root / "extracted_data" / "room-names.json")
                               .read_text(encoding="utf-8"))
            return [r["no"] for r in names.get(scen, {}).get("rooms", [])
                    if r.get("no") and r["no"] not in anchored]
        except Exception:                           # noqa: BLE001 - optional leg
            return []

    # -- walking it ----------------------------------------------------------

    def _place(self, crew: list):
        if not (self.route and crew):
            return
        n = len(self.route)
        t = time.time() - self.t0
        for k, p in enumerate(crew):
            room, way = self.route[(int(t / self.DWELL) + k * 3) % n]
            p["roomID"] = room
            if way:                     # a room with none keeps its last fix;
                p["positionX"], p["positionY"] = \
                    self._along(way, (t % self.DWELL) / self.DWELL)

    @staticmethod
    def _along(way: list, u: float) -> tuple[float, float]:
        """Position u (0..1) of the way round the room's own spawn points."""
        if len(way) == 1:
            return way[0]
        seg = u * len(way)
        i = int(seg) % len(way)
        f = seg - int(seg)
        (x0, z0), (x1, z1) = way[i], way[(i + 1) % len(way)]
        return x0 + (x1 - x0) * f, z0 + (z1 - z0) * f


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------

# A teammate reads their gauge out; the streamer types it here. One tap per reading.
VIRUS_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Teammate virus</title>
<style>
 body{background:#0a0c0f;color:#e6edf3;font:16px/1.4 'JetBrains Mono',Consolas,monospace;margin:0;padding:18px;max-width:560px}
 h1{font:400 26px 'Bebas Neue',Impact,sans-serif;letter-spacing:.1em;color:#e8a000;margin:0 0 4px}
 p{color:#8b98a8;font-size:12px;margin:0 0 16px}
 .row{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #1a1f28}
 .who{flex:1} .who b{font-size:19px} .who span{display:block;color:#8b98a8;font-size:12px}
 input{width:92px;background:#131920;border:1px solid #2a3340;color:#fff;font:20px inherit;padding:8px;text-align:right}
 button{background:#e8a000;border:0;color:#0a0c0f;font:700 15px inherit;padding:11px 16px;cursor:pointer}
 .ok{color:#5fd35f} .none{color:#8b98a8;padding:20px 0}
</style>
<h1>TEAMMATE VIRUS</h1>
<p>When a teammate reads their virus out, type it and press Set. It is logged against the estimate, and the estimate carries on from the real figure.</p>
<div id=list class=none>waiting for a match...</div>
<script>
 const list=document.getElementById('list'); let sig='';
 async function tick(){
   let d; try{ d=await (await fetch('/live.json',{cache:'no-store'})).json(); }catch(e){ return; }
   const ps=(d.players||[]).filter(p=>p.virusEstimate!=null);
   const s=ps.map(p=>p.slot+p.name).join();
   if(!ps.length){ if(sig!=='-'){ sig='-'; list.className='none'; list.textContent=d.state==='in-game'?'no teammates in this match':'waiting for a match...'; } return; }
   if(s!==sig){ sig=s; list.className='';
     list.innerHTML=ps.map(p=>`<div class=row><div class=who><b>P${p.slot} ${p.name}</b><span id=e${p.slot}></span></div>
       <input id=i${p.slot} inputmode=decimal placeholder="%" onkeydown="if(event.key==='Enter')set(${p.slot})"><button onclick="set(${p.slot})">Set</button></div>`).join(''); }
   ps.forEach(p=>{ const e=document.getElementById('e'+p.slot); if(e&&!e.dataset.hold) e.textContent='estimate ~'+p.virusEstimate.toFixed(1)+'%  ·  band '+p.virusBand.join('–')+'%'; });
 }
 async function set(slot){
   const i=document.getElementById('i'+slot), v=parseFloat(i.value); if(isNaN(v)||v<0||v>100) return;
   await fetch('/virus/set?slot='+slot+'&pct='+v); i.value='';
   const e=document.getElementById('e'+slot); e.dataset.hold=1; e.className='ok'; e.textContent='set to '+v+'% — logged';
   setTimeout(()=>{ delete e.dataset.hold; e.className=''; },2500);
 }
 tick(); setInterval(tick,1000);
</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OutbreakBridge/1.0"
    poller: Poller
    guide: Path | None
    pinned = False          # --guide was given: honour it, never re-resolve
    served = 0              # guide pages handed out -- proves a browser connected

    def log_message(self, fmt, *a):                 # quiet; the console is for the player
        pass

    def _send(self, body: bytes, ctype: str, cache: str = "no-store", extra=None):
        enc = None
        if len(body) > 1400 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body, enc = gzip.compress(body, 6), "gzip"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if enc:
            self.send_header("Content-Encoding", enc)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _stream(self, feed: FastPositions):
        """Server-sent events from FastPositions, until the browser goes away."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # Nagle would hold each small message for the last one's ACK
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        feed.join_stream(1)
        seen = 0
        try:
            while True:
                seen, body = feed.wait(seen, 1.0)
                # the comment is what finds out a closed tab while the game is idle
                self.wfile.write(b"data: " + body + b"\n\n" if body else b": \n\n")
        except OSError:
            pass                                     # the tab closed; Windows raises several kinds
        finally:
            feed.join_stream(-1)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path in ("/", "/guide", "/index.html"):
                return self._guide()
            if path == "/live.json":
                return self._send(
                    json.dumps(self.poller.snapshot).encode("utf-8"),
                    "application/json; charset=utf-8",
                    # the stream overlay is opened from disk (file://) and
                    # reads this cross-origin; without the header it gets nothing
                    extra={"Access-Control-Allow-Origin": "*"})
            if path == "/pos" and getattr(self.poller, "fast", None):
                return self._stream(self.poller.fast)
            if path == "/virus/set":
                q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv) \
                    if "?" in self.path else {}
                try:
                    slot, pct = int(q["slot"]), float(q["pct"])
                    assert 1 <= slot <= 4 and 0 <= pct <= 100
                except Exception:                    # noqa: BLE001
                    return self.send_error(400, "need slot=1..4 and pct=0..100")
                est = getattr(self.poller, "virus", None)
                if est is not None:
                    est.said(slot, pct)
                return self._send(b'{"ok":true}', "application/json; charset=utf-8")
            if path == "/virus":
                return self._send(VIRUS_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/status":
                s = self.poller.snapshot
                fast = getattr(self.poller, "fast", None)
                return self._send(json.dumps({
                    "app": APP,
                    "state": s.get("state"),
                    "polls": self.poller.polls,
                    "guideServed": Handler.served,
                    "note": s.get("note") or self.poller.note,
                    "enemyFix": s.get("enemyFix"),
                    "enemyHp": s.get("enemyHp"),
                    "fastFeed": fast and {"viewers": fast.viewers, "sent": fast.sent},
                    "reader": getattr(self.poller, "using", None),
                    "shadow": self.poller.shadow.summary() if getattr(self.poller, "shadow", None) else None,
                    "guide": self.guide.name if self.guide else None,
                }, indent=1).encode("utf-8"), "application/json; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            return                                   # browser navigated away mid-send
        self.send_error(404, "no such thing here")

    def _guide(self):
        # Re-resolve on every page load. The read-me promises a newer guide can
        # be dropped in beside the exe and picked up; pinning the answer at
        # startup quietly broke that, and the symptom is nasty -- the banner
        # keeps naming the version you replaced, and if an older process is
        # still holding the port you can be reading a build from days ago
        # without a single thing looking wrong. A glob per page load is free.
        if not Handler.pinned:
            found = find_guide()
            if found and found != self.guide:
                Handler.guide = found
                print(f"  Guide     now serving {found.name}")
        if not self.guide or not self.guide.is_file():
            msg = (f"<h1>{APP}</h1><p>The bridge is running, but no "
                   f"<code>obsrv-field-guide-v*.html</code> was found next to it."
                   f"</p><p>Live data is still available at "
                   f"<a href='/live.json'>/live.json</a>.</p>")
            return self._send(msg.encode("utf-8"), "text/html; charset=utf-8")
        Handler.served += 1
        st = self.guide.stat()
        tag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            self.send_header("ETag", tag)
            self.end_headers()
            return
        self._send(self.guide.read_bytes(), "text/html; charset=utf-8",
                   cache="no-cache", extra={"ETag": tag})


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # NOT on Windows. SO_REUSEADDR there does not mean "reuse a socket in
    # TIME_WAIT", it means "two processes may bind the same port", and the OS
    # then hands each connection to whichever it likes. Two copies of this
    # bridge could sit on 8777 at once, and a browser tab would read an old one
    # at random -- which happened, and cost a test run to a banner that kept
    # naming the version that had just been replaced. Off, so a second copy
    # fails to bind, bumps to the next port, and says so.
    allow_reuse_address = os.name != "nt"


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))            # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def bind(host: str, port: int, tries: int = 12) -> tuple[Server, int]:
    for p in range(port, port + tries):
        try:
            return Server((host, p), Handler), p
        except OSError:
            continue
    raise SystemExit(f"could not bind any port in {port}..{port + tries - 1}")


# --------------------------------------------------------------------------

READER_LABEL = {"native": "our own", "dll": "tracker DLL",
                "auto": "tracker DLL if present, else our own",
                "shadow": "tracker DLL, ours compared beside it"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"{APP} bridge")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--lan", action="store_true",
                    help="accept connections from other machines on your network")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-record", action="store_true",
                    help="do not save the raw memory of a match to sessions/")
    ap.add_argument("--probe", action="store_true",
                    help="print one snapshot as JSON and exit")
    ap.add_argument("--save", type=Path, default=None,
                    help="with --probe, also write the snapshot to this file")
    ap.add_argument("--guide", type=Path, default=None)
    ap.add_argument("--update-url", default=None, metavar="URL",
                    help="check this address for a newer guide at startup "
                         "(the packaged app does this by itself once a site is set)")
    ap.add_argument("--no-update", action="store_true",
                    help="never check for a newer guide")
    ap.add_argument("--reader", choices=("auto", "native", "dll", "shadow"), default=None,
                    help="which game reader: our own (native), the tracker DLL, or both "
                         "compared (shadow). Default: shadow when the DLL is here, else native")
    ap.add_argument("--replay", type=Path, nargs="?", metavar="CAPTURE",
                    const=Path("capture.json"), default=None,
                    help="replay a saved capture with the player walking, "
                         "instead of reading the game -- no emulator needed")
    ap.add_argument("--replay-players", type=int, default=0, metavar="N",
                    help="with --replay, clone the captured player into N slots")
    ap.add_argument("--replay-rec", metavar="REC",
                    help="play back a real recorded run, enemies included: a "
                         "sessions/rec-*.jsonl.gz, or 'latest'")
    ap.add_argument("--replay-speed", type=float, default=1.0, metavar="X",
                    help="with --replay-rec, playback speed")
    ap.add_argument("--replay-from", type=float, default=0.0, metavar="SEC",
                    help="with --replay-rec, start this many seconds into the run")
    args = ap.parse_args(argv)

    try:                                # the banner is the only feedback a
        sys.stdout.reconfigure(line_buffering=True)   # double-clicked exe gives
    except (AttributeError, OSError):
        pass

    lua_dll = find_file("lua51.dll")
    tracker_dll = find_file("luaoutbreaktracker.dll")
    guide = args.guide or find_guide()
    reader = args.reader or ("shadow" if (lua_dll and tracker_dll) else "native")

    if args.probe:
        p = Poller(lua_dll, tracker_dll, record=False, reader=reader)
        p.start()
        for _ in range(40):                          # up to ~10s for init to land
            time.sleep(0.25)
            if p.snapshot.get("state") not in ("starting",):
                if p.snapshot.get("state") != "no-emulator":
                    break
        snap = p.snapshot
        text = json.dumps(snap, indent=1)
        if args.save:
            # A capture needs a running game to make, so it is not a file we are
            # allowed to overwrite. Any existing one is stepped aside first,
            # stamped with its own mtime -- the evidence keeps accumulating.
            if args.save.is_file() and args.save.stat().st_size:
                stamp = time.strftime("%Y%m%d-%H%M%S",
                                      time.localtime(args.save.stat().st_mtime))
                kept = args.save.with_name(f"{args.save.stem}-{stamp}.json")
                if not kept.exists():
                    args.save.replace(kept)
                    print(f"kept the previous capture as {kept.name}")
            args.save.write_text(text, encoding="utf-8")
            print(f"wrote {args.save}  ({len(text)} bytes)")
        print(text[:20000])
        return 0

    if args.replay_rec:
        cap = Path(args.replay_rec)
        if args.replay_rec == "latest":
            found = sorted((Path(__file__).resolve().parent / "sessions").glob("rec-*.jsonl.gz"))
            cap = found[-1] if found else cap
        if not cap.is_file():
            print(f"replay: no such recording -- {args.replay_rec}")
            return 2
        poller = RecReplay(cap, args.replay_speed, args.replay_from)
    elif args.replay:
        cap = args.replay if args.replay.is_file() else \
            (find_file(args.replay.name) or args.replay)
        if not cap.is_file():
            print(f"replay: no such capture -- {args.replay}")
            return 2
        poller = Replay(cap, args.replay_players)
    else:
        poller = Poller(lua_dll, tracker_dll, record=not args.no_record, reader=reader)
    Handler.poller = poller
    Handler.guide = guide
    Handler.pinned = args.guide is not None
    poller.start()

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    srv, port = bind(host, args.port)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 58)
    print(f"  {APP}")
    print(f"  Raccoon City, filed by {MAKER}")
    print("=" * 58)
    print()
    print(f"  Guide     {guide.name if guide else '(not found -- put the HTML next to me)'}")
    if args.replay or args.replay_rec:
        print(f"  Reader    REPLAY -- {cap.name}, NOT the game")
    else:
        print(f"  Reader    {READER_LABEL.get(reader, reader)}")
    print(f"  Open      {url}")
    if port != args.port:
        # Bumping quietly is how you end up reading a guide from days ago: the
        # old process keeps 8777 and the browser you already had open never
        # moved. Say it loudly, and say what to do about it.
        print()
        print(f"  !! PORT {args.port} WAS ALREADY TAKEN -- this one is on {port}.")
        print(f"  !! Something else is still serving {args.port}, probably an")
        print(f"  !! older copy of this bridge. Close it, or any browser tab")
        print(f"  !! on {args.port} is reading THAT one, not this.")
    if args.lan:
        print(f"  Friends   http://{lan_ip()}:{port}/   (same network, nothing to install)")
    if zip_trap():
        print()
        print("  Running from inside the zip.  That works, but for the share")
        print("  shortcut and your own guide files, extract the zip first.")
    print()
    if args.replay_rec:
        print("  This is a RECORDED RUN.  Everything on the map moved like this in")
        print(f"  that run.  Turn on TRACKING > ENEMIES.  {poller.note}, x{args.replay_speed:g}.")
    elif args.replay:
        print("  This is a REHEARSAL.  Nothing is being read from a game: the")
        print("  player is walked between real disc spawn points so the overlay")
        print("  can be watched moving.  The guide says REPLAY while it runs.")
        print(f"  Route     {poller.note or '(the capture, unmoved)'}")
    else:
        print("  Start PCSX2 whenever you like -- this waits and picks the game up")
        print("  on its own.  Close this window to stop.")
    print("=" * 58)
    print()

    update_url = args.update_url or (UPDATE_URL if getattr(sys, "frozen", False) else "")
    if update_url and not args.no_update and not args.guide:
        GuideUpdater(update_url, guide).start()

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        poller.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
