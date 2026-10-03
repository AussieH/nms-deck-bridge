"""NMS Deck bridge: the No Man's Sky half of the NMS Deck Stream Deck plugin.

Once a second, on the game's own loop, it reads the player's state (health, shield, ship, currencies, where they are)
and writes it to %APPDATA%\\NMSDeck\\state.json, where the plugin draws its keys from. Read only: it changes nothing
in the game or the save, and has nothing to do with online play.

Built on NMS.py (https://github.com/monkeyman192/NMS.py). Run the game with it: `pymhf run nmspy`.
"""

import ctypes
import json
import os
import struct
import time
import traceback
from logging import getLogger

from pymhf import Mod

import nmspy.data.types as nms
from nmspy.common import gameData
from nmspy.decorators import main_loop, on_fully_booted

logger = getLogger("NMSDeck")

VERSION = "0.6.2"
PROTOCOL = 1
OUT_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NMSDeck")
STATE = os.path.join(OUT_DIR, "state.json")
PROBE = os.path.join(OUT_DIR, "probe.txt")
EVERY = 1.0  # seconds between writes
PROBE_EVERY = 5.0   # seconds at least between probe rewrites


def _text(value):
    """A fixed-size game string as plain text, or None."""
    for attr in ("value", "__str__"):
        try:
            text = str(value) if attr == "__str__" else getattr(value, attr)
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            text = str(text).split("\x00", 1)[0].strip()
            if text and not text.startswith("<"):
                return text
        except Exception:
            continue
    return None


# ---- the player state, found by its shape rather than by NMS.py's offsets ------------------------------------------------
# cGcPlayerState opens with the player's name, then four galactic addresses in a row, 0x18 bytes apart: two start
# locations, where the player is, and where they were. Shield, health, ship health, units, nanites and quicksilver come
# straight after. A game update moves the whole block (build 179666 moved it back 0x88 from where NMS.py 179105 has
# it), so the block is found each session by that shape and everything is read at its distance from it.
SCAN = 0x2000
ADDRESS = 0x18                          # one cGcUniverseAddressData
CURRENT, PREVIOUS = 2 * ADDRESS, 3 * ADDRESS
AFTER = 4 * ADDRESS                     # shield, health, ship health, units, nanites, quicksilver, in that order
_anchor = None


def _address(raw, at):
    """A galactic address at `at`, or None if what is there could not be one."""
    planet, system, x, y, z, galaxy = struct.unpack_from("<iiiiii", raw, at)
    if not (0 <= planet < 16 and 0 <= system < 0x300 and -2048 <= x < 2048 and -128 <= y < 128 and -2048 <= z < 2048 and 0 <= galaxy < 256):
        return None
    # Empty memory is address-shaped too; nobody is ever at the galaxy's centre, so an all-zero address is not one.
    if x == 0 and y == 0 and z == 0 and system == 0:
        return None
    return {"galaxy": galaxy, "x": x, "y": y, "z": z, "system": system, "planet": planet}


def _find_anchor(raw):
    """Where the four addresses start: the first place four address-shaped runs sit 0x18 apart, with health after."""
    for at in range(0, len(raw) - AFTER - 0x18, 4):
        if all(_address(raw, at + k * ADDRESS) for k in range(4)):
            shield, health = struct.unpack_from("<ii", raw, at + AFTER)
            if 0 <= shield <= 10000 and 0 <= health <= 10000:
                return at
    return None


def _player_block(base):
    global _anchor
    raw = ctypes.string_at(base, SCAN)
    if _anchor is None or _address(raw, _anchor + CURRENT) is None:
        _anchor = _find_anchor(raw)
        if _anchor is None:
            return None
        logger.info(f"NMS Deck bridge: player state found at +0x{_anchor:X}")
    shield, health, ship, units, nanites, quicksilver = struct.unpack_from("<iiiIII", raw, _anchor + AFTER)
    return {
        "shield": shield, "health": health, "shipHealth": ship,
        "units": units, "nanites": nanites, "quicksilver": quicksilver,
        "location": _address(raw, _anchor + CURRENT),
        "previous": _address(raw, _anchor + PREVIOUS),
        "anchor": _anchor,
    }


# ---- inventories: technology charge levels ---------------------------------------------------------------------------
# A ship's launch thruster, pulse engine, hyperdrive and deflector shield, and the exosuit's life support, hazard
# protection and jetpack, are technology in an inventory, and their fuel or charge is the item's Amount out of its
# MaxAmount. cGcPlayerState holds 0x21 inventories (exosuit and others) and 12 ship tech inventories, one per ship; each
# is a cGcInventoryStore of 0x248 bytes whose item list (at +0x88) is {allocated, size, pointer} to 0x30-byte items:
# Id (16 characters), index, Amount at +0x18, MaxAmount at +0x20. The stores sit at NMS.py's offsets moved by however
# far the address block moved, and every read through a pointer goes through ReadProcessMemory, which fails politely
# on a bad address where a plain read would take the game down.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentProcess.restype = ctypes.c_void_p
_kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_SELF = _kernel32.GetCurrentProcess()

NMSPY_ANCHOR = 0x830          # where NMS.py 179105 has the address block; everything below moves with it
SHIP_COUNT = 0xC   # ships a player can own
PRIMARY_SHIP = 0x182A0
STORE, STORE_LIST, ITEM = 0x248, 0x88, 0x30


def _safe(addr, n, most=0x10000):
    """n bytes at addr, or None if they cannot be read."""
    if not addr or n <= 0 or n > most:
        return None
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if not _kernel32.ReadProcessMemory(_SELF, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)) or got.value != n:
        return None
    return buf.raw


def _items(store_addr):
    """A store's items as [(id, amount, max)], or None if what is there is not an item list."""
    head = _safe(store_addr + STORE_LIST, 16)
    if head is None:
        return None
    allocated, size, ptr = struct.unpack("<IIQ", head)
    if size == 0:
        return []
    if size > 400 or allocated < size:
        return None
    raw = _safe(ptr, size * ITEM)
    if raw is None:
        return None
    out = []
    for i in range(size):
        rec = raw[i * ITEM:(i + 1) * ITEM]
        name = rec[:16].split(b"\x00", 1)[0]
        if not name or not all(0x20 < c < 0x7F for c in name):
            return None   # not an id: this is not an item list
        amount, max_amount = struct.unpack_from("<i", rec, 0x18)[0], struct.unpack_from("<i", rec, 0x20)[0]
        out.append((name.decode("ascii"), amount, max_amount))
    return out


# Where the inventories sit, measured from the address block, as build 179666 has them (checked against an inventory
# report, 2026-09-24): the exosuit's technology is the second of the 0x21 inventories, and the ships' technology is a
# run of 12 stores, one per ship, starting 0xA2F8 past the block -- 24 stores and 0x10 after the first ship inventory
# at 0x6C28, so general and cargo come first. (NMS.py 179105 puts the technology run 0x100 too early.) The flown ship's
# store was the one with fuel used: launch thruster 159/200, pulse engine 170/200, hyperdrive 48/120.
SUIT_TECH = 0x328
SHIP_TECH_REL = 0xA2F8
PRIMARY_SHIP_REL = PRIMARY_SHIP - NMSPY_ANCHOR

# Technology ids and what the keys call them. Amount out of MaxAmount is the charge or fuel.
SUIT_IDS = {"ENERGY": "lifeSupport", "PROTECT": "hazard"}
SHIP_IDS = {"LAUNCHER": "launch", "SHIPJUMP1": "pulse", "HYPERDRIVE": "hyperdrive", "SHIPSHIELD": "deflector"}


def _charges(items, ids):
    """{name: fraction} for the ids asked for, from a store's items; technology that holds no charge is skipped, and
    so is an amount well past its maximum, which is not a charge but a field that has moved."""
    out = {}
    for name, amount, max_amount in items or []:
        key = ids.get(name)
        if key and key not in out and 0 < max_amount < 1000000 and 0 <= amount <= max_amount * 2:
            out[key] = round(min(1.0, amount / max_amount), 4)
    return out


# ---- sanity checks: a field a game update has moved reads as something else, so what is read at a fixed distance is
# checked against what the game allows, and anything that fails shows as a dash on the key rather than a wrong value.
STORE_CLASS = 0x100   # cGcInventoryStore::mClass: 0 C, 1 B, 2 A, 3 S


def _store_class(store_addr):
    """An inventory store's class, 0 to 3, or None when what is there is not a store."""
    raw = _safe(store_addr + STORE_CLASS, 4)
    cls = struct.unpack("<i", raw)[0] if raw else None
    return cls if cls in (0, 1, 2, 3) else None


def _name(raw, most=0x80):
    """Text that reads as a name, or None: empty, unprintable or runaway text is a field that has moved."""
    s = _cstr(raw)
    if not s or len(s) >= most or "�" in s or not all(c.isprintable() for c in s):
        return None
    return s


def _lang_key(raw):
    """A language key as the game writes them (UI_PARADISE_PLANET, RARITY_HIGH9, WEATHER_GREEN1), or None."""
    s = _cstr(raw)
    if not s or len(s) > 0x40 or not s[0].isalpha() or not all(c.isupper() or c.isdigit() or c == "_" for c in s):
        return None
    return s


def _flag(value):
    """A stored bool: 0 or 1, anything else is not one."""
    return {0: False, 1: True}.get(value)


def _tech(base, anchor):
    """The exosuit's and the flown ship's charge levels, or empty dicts when they cannot be read."""
    # The exosuit's charges need its own technology ids (ENERGY, PROTECT) in a well-formed item list, which moved memory
    # does not produce; its store's class has not been checked in game, so it is not asked for. The ship's has (S).
    suit = _charges(_items(base + anchor + SUIT_TECH), SUIT_IDS)
    raw = _safe(base + anchor + PRIMARY_SHIP_REL, 4)
    primary = struct.unpack("<I", raw)[0] if raw else None
    if primary is not None and not 0 <= primary < SHIP_COUNT:
        primary = None
    ship = {}
    if primary is not None:
        store = base + anchor + SHIP_TECH_REL + primary * STORE
        if _store_class(store) is not None:
            ship = _charges(_items(store), SHIP_IDS)
    return suit, ship, primary


# ---- the solar system and the planet the player is at --------------------------------------------------------------
# cGcSimulation::mpSolarSystem is the game's own pointer to the solar system the player is in, and a game update can
# move it. NMS.py 179105 and build 179666 have it at 0x24DFE0; build 180383 (the 2026-09-30 update) has it at
# 0x25E020, where cGcSimulation::Update reads it to hand to cGcSolarSystem::Update: cGcSimulation grew 0x10040 before
# it, while the solar system's own layout (size, planets, the offsets below) stayed as it was. The places known are
# tried newest first, and the pointer is only taken when what it points at is the system the player is in. If none
# is, a window of cGcSimulation is searched for a pointer that is, at most every SEARCH_EVERY seconds.
SIM_SOLAR_SYSTEM = (0x25E020, 0x24DFE0)
SIM_SEARCH, SEARCH_CHUNK = (0x240000, 0x280000), 0x10000
SEARCH_EVERY = 60.0   # a search reads 256 KB and follows every pointer in it: rare, so it never stutters the game
SYS_TRADING, SYS_CONFLICT, SYS_PLANETS, SYS_STAR = 0x2520, 0x2530, 0x2544, 0x2550   # cGcSolarSystemData
SYS_UA = 0x26B0                 # cGcSolarSystem::mUA, the system's universe address
SYS_HEAD = SYS_UA + 8 - SYS_TRADING
SYS_PLANET0, PLANET_SIZE, PLANET_DATA = 0x2E30, 0xD9170, 0x60                        # cGcSolarSystem::maPlanets
PD_INFO, PD_NAME = 0x3548, 0x3A4E                                                     # cGcPlanetData
# ResourceLevel (0 low, 1 high: activated metals) and HasScrap, checked 2026-09-26 against the discovery summaries of
# four planets in one system: two with scrap, one of them rich.
PD_RESOURCE_LEVEL, PD_HAS_SCRAP = 0x3540, 0x3ACE
PD_PLANET_INDEX = 0x353C   # 0 to 5, checked 2026-09-26: four planets read 0, 1, 2, 3
# cGcPlanetInfo: language keys the game translates for the discovery page. (Its sentinel keys are left out: the
# discovery pages show a different sentinel level from them, taken from somewhere not yet found.)
INFO_FIELDS = [("fauna", 0x200), ("flora", 0x280), ("description", 0x300), ("type", 0x380), ("resources", 0x400), ("weather", 0x480)]


_translations = {}
_translate_broken = False


def _translate(key):
    """The game's own text for a language key (RARITY_HIGH9 -> "Rich"), through NMS.py's Translate, cached. Game
    thread only. After one failure it stops trying and hands back the key."""
    global _translate_broken
    if not key or _translate_broken:
        return key
    if key not in _translations:
        try:
            _translations[key] = nms.cTkLanguageManager.Translate(key) or key
        except Exception:
            _translate_broken = True
            logger.error("NMS Deck bridge: Translate failed\n" + traceback.format_exc())
            return key
    return _translations[key]


def _cstr(raw):
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace") if raw else None


def _byte(addr):
    raw = _safe(addr, 1)
    return raw[0] if raw else None


# Each of the system's numbers is one of the game's enums: TradingClass 0-6, WealthClass 0-3, PlayerConflictData 0-3,
# AlienRace 0-8, GalaxyStarTypes 0-4, and a system has at most six planets. One out of range reads as unknown on its
# own; the rest of the system is still shown.
SYSTEM_FIELDS = (("economy", 0, 6), ("wealth", 0, 3), ("conflict", 0, 3), ("race", 0, 8), ("star", 0, 4), ("planets", 0, 6))


def _system_fields(raw):
    """(the system for the keys, the names of the fields read as unknown): each number checked on its own."""
    system, bad = {}, []
    for name, lo, hi in SYSTEM_FIELDS:
        value = raw.get(name)
        ok = isinstance(value, int) and lo <= value <= hi
        system[name] = value if ok else None
        if not ok:
            bad.append(name)
    return system, bad


def _ua_address(ua):
    """A universe address as {x, y, z, system}: from the low bits X, Z (12 bits each), Y (8) and the system (12), the
    portal code's order backwards, with X, Z and Y signed."""
    def signed(value, bits):
        return value - (1 << bits) if value >= 1 << (bits - 1) else value
    return {"x": signed(ua & 0xFFF, 12), "z": signed((ua >> 12) & 0xFFF, 12), "y": signed((ua >> 24) & 0xFF, 8),
            "system": (ua >> 32) & 0xFFF}


def _ua_matches(ua, loc):
    """Whether a universe address is the player's star system (the planet is left out: a system's is none)."""
    if not ua or not loc:
        return False
    got = _ua_address(ua)
    return all(got[k] == loc.get(k) for k in ("x", "y", "z", "system"))


def _is_system(raw, loc, strict=False):
    """(whether what was read is the solar system the player is in, why), as pure logic over what was read. Its
    address being the player's, or every planet carrying its own index, says so; where the pointer is known to live,
    every number in range (all 0.6.0 asked) is enough as well. A search (strict) asks more of the planets: two or more
    with their own indices and every number in range, since one planet with index 0 is what empty memory looks like."""
    if raw is None:
        return False, "nothing readable there"
    _, bad = _system_fields(raw)
    count, indices = raw.get("planets"), raw.get("indices")
    if _ua_matches(raw.get("ua"), loc):
        return True, "its address is the player's"
    indexed = indices is not None and indices == list(range(count))
    if indexed and (not strict or (count >= 2 and not bad)):
        return True, "every planet carries its own index"
    if not strict and not bad and count >= 1:
        return True, "every number in range"
    why = ["its address is not the player's"]
    if indices is None:
        why.append("no planet count to check indices against")
    elif not indexed:
        why.append(f"planet indices {indices}, wanted {list(range(count))}")
    elif strict:
        why.append("too few planets to be sure" if count < 2 else "")
    if bad:
        why.append("out of range: " + ", ".join(bad))
    return False, "; ".join(w for w in why if w)


def _system_raw(ss):
    """What a candidate solar system holds, as read and unchecked, or None when nothing there can be read."""
    head = _safe(ss + SYS_TRADING, SYS_HEAD) if ss else None
    if head is None:
        return None
    trading, wealth = struct.unpack_from("<ii", head, 0)
    conflict, race = struct.unpack_from("<ii", head, SYS_CONFLICT - SYS_TRADING)
    planets, = struct.unpack_from("<i", head, SYS_PLANETS - SYS_TRADING)
    star, = struct.unpack_from("<i", head, SYS_STAR - SYS_TRADING)
    ua, = struct.unpack_from("<Q", head, SYS_UA - SYS_TRADING)
    raw = {"economy": trading, "wealth": wealth, "conflict": conflict, "race": race, "star": star, "planets": planets,
           "ua": ua, "indices": None}
    if 1 <= planets <= 6:
        indices = []
        for i in range(planets):
            got = _safe(ss + SYS_PLANET0 + i * PLANET_SIZE + PLANET_DATA + PD_PLANET_INDEX, 4)
            indices.append(struct.unpack("<i", got)[0] if got else None)
        raw["indices"] = indices
    return raw


def _describe(raw, loc):
    """What was read at a candidate, for the probe: the numbers, whether the address matched, the planet indices."""
    if raw is None:
        return "nothing readable there"
    nums = " ".join(f"{k}={raw[k]}" for k, _, _ in SYSTEM_FIELDS)
    got = _ua_address(raw["ua"]) if raw["ua"] else None
    ua = "address 0" if got is None else f"address system={got['system']} x={got['x']} y={got['y']} z={got['z']} ({'matches' if _ua_matches(raw['ua'], loc) else 'not the player'})"
    return f"{nums}; {ua}; planet indices {raw['indices']}"


def _pointer_like(value):
    """Whether a number could be a pointer to a heap object."""
    return 0x10000 <= value < 0x7FFFFFFF0000 and value % 8 == 0


def _pointer_at(addr):
    got = _safe(addr, 8)
    return struct.unpack("<Q", got)[0] if got else None


_ss_at = None          # where in cGcSimulation the solar system pointer was last found
_search_after = 0.0    # time.monotonic() before which no new search is made


def _search_system(sim_addr, loc, trace):
    """Where in cGcSimulation's search window a pointer to the player's solar system sits, or None."""
    lo, hi = SIM_SEARCH
    shaped = readable = unread = 0
    for chunk in range(lo, hi, SEARCH_CHUNK):
        raw = _safe(sim_addr + chunk, SEARCH_CHUNK)
        if raw is None:
            unread += 1
            continue
        for i, (value,) in enumerate(struct.iter_unpack("<Q", raw)):
            if not _pointer_like(value):
                continue
            shaped += 1
            cand = _system_raw(value)
            if cand is None:
                continue
            readable += 1
            ok, why = _is_system(cand, loc, strict=True)
            if ok:
                off = chunk + i * 8
                trace.append(f"  search: found at sim+0x{off:X} ({why}): {_describe(cand, loc)}")
                return off
    trace.append(f"  search sim+0x{lo:X}..0x{hi:X}: {shaped} pointer-shaped values, {readable} readable, "
                 f"{unread} unreadable chunks; none is the player's system")
    return None


def _solar_system(sim_addr, loc, trace, now):
    """The player's solar system: (its address, what was read there), or (0, None). Every step goes to trace."""
    global _ss_at, _search_after
    places = ([_ss_at] if _ss_at is not None else []) + [o for o in SIM_SOLAR_SYSTEM if o != _ss_at]
    for off in places:
        ss = _pointer_at(sim_addr + off)
        if not ss:
            trace.append(f"  sim+0x{off:X}: {'pointer null' if ss == 0 else 'not readable'}")
            continue
        raw = _system_raw(ss)
        ok, why = _is_system(raw, loc)
        trace.append(f"  sim+0x{off:X}: pointer set; {_describe(raw, loc)} -> {'taken' if ok else 'not taken'} ({why})")
        if ok:
            if off != _ss_at:
                logger.info(f"NMS Deck bridge: solar system pointer at cGcSimulation+0x{off:X}")
            _ss_at = off
            return ss, raw
    if now < _search_after:
        trace.append(f"  search: not before {_search_after - now:.0f}s from now")
        return 0, None
    _search_after = now + SEARCH_EVERY
    off = _search_system(sim_addr, loc, trace)
    if off is None:
        return 0, None
    logger.info(f"NMS Deck bridge: solar system pointer found by search at cGcSimulation+0x{off:X}")
    _ss_at = off
    ss = _pointer_at(sim_addr + off)
    return ss, _system_raw(ss)


def _world(sim_addr, loc, trace=None, now=None):
    """The solar system and the planet the player is at, for the keys: (system or None, planet or None). Checked
    against the game (build 179666, 2026-09-25): the system's economy, wealth, conflict and race match the galaxy map,
    and the planet's type, weather, flora, fauna and resources match its discovery page, through the game's own
    translations. `loc` is the player's galactic address, which counts planets from 1 (0 is none: space, a station).
    Each step, and which check failed, goes to `trace` for the probe."""
    trace = [] if trace is None else trace
    loc = loc or {}
    ss, raw = _solar_system(sim_addr, loc, trace, time.monotonic() if now is None else now)
    if raw is None:
        trace.append("  system: unknown (no pointer led to the player's solar system)")
        return None, None
    system, bad = _system_fields(raw)
    trace.append("  system: read" + (f"; unknown: {', '.join(f'{k} ({raw[k]})' for k in bad)}" if bad else ", every field in range"))
    planet_number = loc.get("planet")
    if not isinstance(planet_number, int) or planet_number == 0:
        trace.append(f"  planet: none (address planet {planet_number!r}: space or a station)")
        return system, None
    most = system["planets"] or 6   # a count read as unknown still allows six: the planet's own index is checked below
    if not 1 <= planet_number <= most:
        trace.append(f"  planet: none (address planet {planet_number} past the system's {most} planets)")
        return system, None
    pd = ss + SYS_PLANET0 + (planet_number - 1) * PLANET_SIZE + PLANET_DATA
    # The planet's own index (cGcPlanetData::PlanetIndex) must be the one the address points at: planet data that has
    # moved fails this before anything is read from it.
    got = _safe(pd + PD_PLANET_INDEX, 4)
    index = struct.unpack("<i", got)[0] if got else None
    if index != planet_number - 1:
        trace.append(f"  planet: unknown (planet data index {'not readable' if index is None else index}, wanted {planet_number - 1})")
        return system, None
    info = {}
    for label, off in INFO_FIELDS:
        key = _lang_key(_safe(pd + PD_INFO + off, 0x80))
        info[label] = _translate(key) if key else None
    kind = info["type"] or "Planet"
    planet = {
        "name": _name(_safe(pd + PD_NAME, 0x80)),
        "type": (info["description"] or "").replace("%PLANETCLASS%", kind) or None,
        "weather": info["weather"],
        "flora": info["flora"],
        "fauna": info["fauna"],
        "resources": info["resources"],
        "richResources": _flag(_byte(pd + PD_RESOURCE_LEVEL)),
        "scrap": _flag(_byte(pd + PD_HAS_SCRAP)),
    }
    unknown = [k for k, v in planet.items() if v is None]
    trace.append(f"  planet: read (planet {planet_number})" + (f"; unknown: {', '.join(unknown)}" if unknown else ", every field read"))
    return system, planet


# EnvironmentLocation's names, for the probe (see _where).
LOCATIONS = ["None", "Default (space)", "SpaceStation", "PlanetOnFoot", "PlanetInShip", "PlanetInVehicle", "Underwater",
             "Cave", "IndoorInBase", "Freighter", "FreighterInternals", "AbandonedFreighter", "InFleet", "InSpaceObject",
             "Nexus", "Anomaly"]


def _game_build(exe=None):
    """The game's FileVersion (180383 for the 2026-09-30 update), from the running exe's version resource, or None."""
    try:
        ver = ctypes.WinDLL("version")
        if exe is None:
            buf = ctypes.create_unicode_buffer(1024)
            ctypes.WinDLL("kernel32").GetModuleFileNameW(None, buf, 1024)
            exe = buf.value
        size = ver.GetFileVersionInfoSizeW(exe, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(exe, 0, size, data):
            return None
        ptr, n = ctypes.c_void_p(), ctypes.c_uint()
        if not ver.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) or n.value < 4:
            return None
        lang, page = struct.unpack("<HH", ctypes.string_at(ptr.value, 4))
        if not ver.VerQueryValueW(data, f"\\StringFileInfo\\{lang:04x}{page:04x}\\FileVersion", ctypes.byref(ptr), ctypes.byref(n)):
            return None
        return ctypes.wstring_at(ptr.value, n.value).rstrip("\x00").strip() or None
    except Exception:
        return None


# ---- speed -----------------------------------------------------------------------------------------------------------
# The player's transform (cGcPlayerEnvironment::mPlayerTM): three direction rows of length 1, at right angles, then the
# position. Found by that shape near the start of the player environment, not at a fixed offset, in case it has moved.
# Speed is how far the position moved since the last reading, per second; a jump no craft makes (a warp, a teleport,
# the game moving its origin) is not a speed.
MAX_SPEED = 40000.0
# The HUD's speed against the distance the position moves a second, which differs by craft. The ship's is twice it
# (checked 2026-09-25: steady 405 u/s on the HUD, 202 measured). The exocraft's is 3.6 times it, the m/s to km/h
# factor (checked 2026-09-25: 16 a second measured while its HUD swung between 50 and 68 over hills). On foot there
# is no HUD speed; it is shown on the ship's scale.
SPEED_SCALE = 2.0
SPEED_SCALE_EXOCRAFT = 3.6
IN_EXOCRAFT = 5   # EnvironmentLocation.PlanetInVehicle


def _is_transform(raw, off):
    """Whether three rows 0x10 apart at off are directions: length 1, at right angles to each other."""
    if off < 0 or off + 0x3C > len(raw):
        return False
    rows = [struct.unpack_from("<3f", raw, off + r * 0x10) for r in range(3)]
    if not all(abs(sum(c * c for c in row) - 1.0) < 0.01 for row in rows):
        return False
    return all(abs(sum(a * b for a, b in zip(rows[i], rows[j]))) < 0.01 for i, j in ((0, 1), (0, 2), (1, 2)))


def _find_transform(raw):
    """The offset of the position in a rotation-then-position block, or None."""
    for off in range(0, len(raw) - 0x3C, 4):
        if _is_transform(raw, off):
            return off + 0x30
    return None


_pos_at = None


def _position(env_addr):
    """The player's position (x, y, z), or None."""
    global _pos_at
    raw = _safe(env_addr, 0x200)
    if raw is None:
        return None
    if _pos_at is None or not _is_transform(raw, _pos_at - 0x30):
        _pos_at = _find_transform(raw)
        if _pos_at is None:
            return None
    x, y, z = struct.unpack_from("<3f", raw, _pos_at)
    return (x, y, z) if all(abs(v) < 1e9 for v in (x, y, z)) else None


def _speed(prev, now_pos, dt, scale=SPEED_SCALE):
    """The HUD's speed from two readings, or None when it cannot be a speed."""
    if prev is None or now_pos is None or dt <= 0:
        return None
    d = scale * sum((a - b) ** 2 for a, b in zip(prev, now_pos)) ** 0.5 / dt
    return round(d, 1) if d <= MAX_SPEED else None


# Where the player is (cGcPlayerEnvironment::meLocation and meLocationStable, NMS.py 179105): None 0, Default 1,
# SpaceStation 2, PlanetOnFoot 3, PlanetInShip 4, PlanetInVehicle 5, Underwater 6, Cave 7, IndoorInBase 8, Freighter 9,
# FreighterInternals 10, AbandonedFreighter 11, InFleet 12, InSpaceObject 13, Nexus 14, Anomaly 15.
ENV_LOCATION, ENV_LOCATION_STABLE = 0x468, 0x474


def _where(env_addr):
    """{location, stable} as the game's numbers, both 0 to 15, or None if what is there is not that."""
    raw = _safe(env_addr + ENV_LOCATION, 0x10)
    if raw is None:
        return None
    loc, stable = struct.unpack_from("<i", raw, 0)[0], struct.unpack_from("<i", raw, ENV_LOCATION_STABLE - ENV_LOCATION)[0]
    return {"location": loc, "stable": stable} if 0 <= loc <= 15 and 0 <= stable <= 15 else None


# ---- the ship's power setting ------------------------------------------------------------------------------------------
# The ship being flown is a cGcSpaceshipComponent; the game runs its UpdateControlled every frame for that one ship
# only, which is how the bridge learns where it is. Its power setting (the one Cycle Power steps through) is a number
# at +0x674C: 0 balanced, 1 weapons, 2 engines, 3 shields. Found on build 179666 by watching it step 0, 1, 2, 3, 0 on
# every press and at no other time.
SHIP_POWER = 0x674C
POWER_FRESH = 2.0   # seconds: after this without UpdateControlled, the player is not flying


def _pointer(p):
    """A hook argument as an address: NMS.py hands pointers over as ctypes objects or plain numbers."""
    try:
        return ctypes.cast(p, ctypes.c_void_p).value or 0
    except Exception:
        try:
            return int(p)
        except Exception:
            return 0


def _power(ship_addr):
    """The flown ship's power setting, 0 to 3, or None."""
    raw = _safe(ship_addr + SHIP_POWER, 4) if ship_addr else None
    if raw is None:
        return None
    value = struct.unpack_from("<i", raw, 0)[0]
    return value if 0 <= value <= 3 else None


def _ship_info(base, anchor, primary):
    """The flown ship's name and class (0 C, 1 B, 2 A, 3 S, from its technology inventory), or None."""
    if primary is None or not (0 <= primary < SHIP_COUNT):
        return None
    raw_name = _safe(base + anchor + (0x1AD93 - NMSPY_ANCHOR) + primary * 0x20, 0x20)
    cls = _store_class(base + anchor + SHIP_TECH_REL + primary * STORE)
    name = _name(raw_name, 0x20)
    return {"name": name, "class": cls} if name or cls is not None else None


class NMSDeckBridge(Mod):
    __author__ = "TeaTime"
    __description__ = "Writes the player's state to a file for the NMS Deck Stream Deck plugin. Read only."
    __version__ = VERSION

    def __init__(self):
        super().__init__()
        self._last = 0.0
        self._failed = {}   # field -> the first error it gave, for the probe report
        self._probes = {}   # where the player was (EnvironmentLocation) -> what the world read gave there, for the probe
        self._probe_due, self._probe_t = False, -PROBE_EVERY
        self._trace = []    # the last world read's steps
        self._pos, self._pos_t = None, 0.0   # the last position read, and when, for the speed
        self._ship, self._ship_t = 0, 0.0    # the flown ship (cGcSpaceshipComponent), and when the game last updated it
        os.makedirs(OUT_DIR, exist_ok=True)
        self._write({"protocol": PROTOCOL, "mod": VERSION, "inGame": False, "at": time.time()})
        logger.info(f"NMS Deck bridge {VERSION}: writing to {STATE}")

    @on_fully_booted
    def booted(self, *args):
        logger.info("NMS Deck bridge: the game is up")

    # Once a frame, for the ship the player is flying and no other: only its address is kept, nothing else is done here.
    @nms.cGcSpaceshipComponent.UpdateControlled.after
    def flying(self, this, lfTimeStep, *args, **kwargs):
        addr = _pointer(this)
        if addr:
            self._ship, self._ship_t = addr, time.monotonic()

    @main_loop.after
    def tick(self, *args):
        now = time.monotonic()
        if now - self._last < EVERY:
            return
        self._last = now
        try:
            state = self._snapshot()
        except Exception:
            logger.error("NMS Deck bridge: snapshot failed\n" + traceback.format_exc())
            return
        self._write(state)
        if state.get("inGame"):
            self._note_probe(state, now)

    # ---- reading ------------------------------------------------------------------------------------------------------
    def _read(self, name, fn):
        """One field, or None if it could not be read; the first failure of each field is kept for the probe."""
        try:
            return fn()
        except Exception as e:
            self._failed.setdefault(name, f"{type(e).__name__}: {e}")
            return None

    def _snapshot(self):
        ps = self._read("player_state", lambda: gameData.player_state)
        base = {"protocol": PROTOCOL, "mod": VERSION, "at": time.time()}
        if ps is None:
            return {**base, "inGame": False}
        state = self._read("player_state_block", lambda: _player_block(ctypes.addressof(ps)))
        if state is None:
            return {**base, "inGame": False, "why": "the player state could not be found in memory"}
        suit, ship, primary = self._read("tech", lambda: _tech(ctypes.addressof(ps), _anchor)) or ({}, {}, None)
        sim = self._read("simulation", lambda: gameData.simulation)
        loc = state.get("location") or {}
        t = time.monotonic()
        self._trace = trace = [f"  simulation {'found' if sim is not None else 'not found'}"]
        system, planet = (self._read("world", lambda: _world(ctypes.addressof(sim), loc, trace, t)) or (None, None)) if sim is not None else (None, None)
        env = self._read("player_environment", lambda: gameData.player_environment)
        pos = self._read("position", lambda: _position(ctypes.addressof(env))) if env is not None else None
        where = self._read("where", lambda: _where(ctypes.addressof(env))) if env is not None else None
        flying = self._ship and t - self._ship_t < POWER_FRESH
        power = self._read("power", lambda: _power(self._ship)) if flying else None
        scale = SPEED_SCALE_EXOCRAFT if where and where["stable"] == IN_EXOCRAFT else SPEED_SCALE
        speed = _speed(self._pos, pos, t - self._pos_t, scale) if self._pos is not None else None
        self._pos, self._pos_t = pos, t
        return {
            **base,
            "inGame": True,
            **state,
            "suit": suit,          # {lifeSupport, hazard}: 0..1
            "shipTech": ship,      # {launch, pulse, hyperdrive, deflector}: 0..1, for the ship being flown
            "primaryShip": primary,
            "shipInfo": self._read("shipInfo", lambda: _ship_info(ctypes.addressof(ps), _anchor, primary)),   # {name, class 0..3}
            "system": system,      # {economy, wealth, conflict, race, star, planets}: the game's enum numbers
            # {name, type, weather, flora, fauna, resources, richResources, scrap}: the game's own words, or None in space
            "planet": planet,
            "speed": speed,        # the HUD's speed, from the position a second ago; None when unknown or a jump
            "where": where,        # the game's EnvironmentLocation: 3 on foot, 4 in ship, 5 in exocraft... (see _where)
            "power": power,        # the flown ship's power setting: 0 balanced, 1 weapons, 2 engines, 3 shields; None when not flying
        }

    # ---- writing ------------------------------------------------------------------------------------------------------
    def _write(self, state):
        """Written to a temporary file and renamed into place, so a reader never sees half a file. Windows refuses the
        rename while another program has state.json open without delete sharing (iCUE's file watcher reading it for
        the NMS Dashboard widget does, for a moment after each write): then a couple of short retries, and if it is
        still held, the file is rewritten in place in one write, which a held file allows. Readers already skip a
        file that does not parse, so a read caught mid-write costs one second at most."""
        tmp = STATE + ".tmp"
        text = json.dumps(state)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            self._write_failed(e)
            return
        for attempt in range(3):
            try:
                os.replace(tmp, STATE)
                self._write_errors = 0
                return
            except PermissionError as e:
                last = e
                if attempt < 2:
                    time.sleep(0.005)   # on the game's loop: 10 ms at most, then the fallback below
            except OSError as e:
                last = e
                break
        try:
            with open(STATE, "w", encoding="utf-8") as f:
                f.write(text)
            self._write_errors = 0
        except OSError as e:
            self._write_failed(e if e else last)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _write_failed(self, e):
        """Logs a failed write once, then once a minute while it keeps failing, instead of every second."""
        self._write_errors = getattr(self, "_write_errors", 0) + 1
        if self._write_errors == 1 or self._write_errors % 60 == 0:
            logger.warning(f"NMS Deck bridge: could not write the state file ({self._write_errors} in a row): {e}")

    def _note_probe(self, state, now):
        """Keeps, for each place the player has been (space, a station, on foot...), what the system and planet read
        gave there and every step of it; the probe is rewritten when a place is new or its result changes, at most
        every PROBE_EVERY seconds, so one probe at the end of a session covers every place visited."""
        where = state.get("where") or {}
        system = state.get("system")
        result = (system is not None, tuple(k for k, v in (system or {}).items() if v is None), state.get("planet") is not None,
                  (state.get("location") or {}).get("planet"))
        place = where.get("stable")
        seen = self._probes.get(place)
        if seen is None or seen["result"] != result:
            self._probes[place] = {"result": result, "at": time.strftime("%H:%M:%S"), "where": where, "location": state.get("location"),
                                   "system": system, "planet": state.get("planet"), "trace": list(self._trace)}
            self._probe_due = True
        if self._probe_due and now - self._probe_t >= PROBE_EVERY:
            self._probe_due, self._probe_t = False, now
            self._write_probe(state)

    def _write_probe(self, state):
        """In a world: every field and what it read, what could not be read, and how the solar system and planet were
        read in each place the player has been, step by step. Used to check a game update against NMS.py's offsets."""
        lines = [f"NMS Deck bridge {VERSION} probe, {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        try:
            from importlib.metadata import version as _dist_version
            nmspy_version = _dist_version("nmspy")
        except Exception:
            nmspy_version = None
        lines += [f"game build   {_game_build()!r}", f"NMS.py       {nmspy_version!r}", ""]
        # NMS.py's settings as they reached the game: the control panel slows the game, and installs turn it off.
        try:
            import pymhf.core._internal as internal
            lines += [f"NMS.py panel  {internal.CONFIG.get('gui')!r}", f"NMS.py start  paused={internal.CONFIG.get('start_paused')!r}",
                      f"NMS.py mods   {internal.CONFIG.get('mod_dir')!r}", f"NMS.py keys   {sorted(internal.CONFIG)!r}", ""]
        except Exception as e:
            lines += [f"NMS.py settings not readable: {e}", ""]
        for key, value in state.items():
            lines.append(f"{key:12} {value!r}")
        lines.append("")
        lines.append("could not read:" if self._failed else "every field read")
        for key, err in self._failed.items():
            lines.append(f"  {key}: {err}")
        lines += ["", f"solar system pointer: cGcSimulation+0x{_ss_at:X}" if _ss_at is not None else "solar system pointer: not found yet",
                  "", "the system and planet, by where the player was (the last change in each place):"]
        for place in sorted(self._probes, key=lambda p: -1 if p is None else p):
            seen = self._probes[place]
            name = LOCATIONS[place] if isinstance(place, int) and 0 <= place < len(LOCATIONS) else "where unknown"
            lines += ["", f"[{place} {name}] at {seen['at']}, where {seen['where']!r}",
                      f"  location {seen['location']!r}", f"  system   {seen['system']!r}", f"  planet   {seen['planet']!r}"]
            lines += seen["trace"]
        try:
            with open(PROBE, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            logger.info(f"NMS Deck bridge: probe written to {PROBE}")
        except OSError as e:
            logger.warning(f"NMS Deck bridge: could not write the probe: {e}")
