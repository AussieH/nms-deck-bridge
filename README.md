# NMS Deck bridge

The No Man's Sky half of [NMS Deck](https://teatimeservers.ca/plugins/nms-deck), a Stream Deck plugin. It is a mod
for [NMS.py](https://github.com/monkeyman192/NMS.py): once a second, on the game's own loop, it reads the player's
state and writes it to `%APPDATA%\NMSDeck\state.json`, which the plugin draws its keys from.

It only reads. It changes nothing in the game or your save, opens no network connection, and has nothing to do with
online play. This repository is here so you can see exactly what it does.

## What it reads

- Health, shield, ship hull, units, nanites, quicksilver.
- The exosuit's life support and hazard protection, and the flown ship's launch thruster, pulse engine, hyperdrive and
  deflector charges.
- Where you are: your galactic address, and whether you are on foot, in your ship or in an exocraft.
- Your speed, from your position a second apart.
- The flown ship's name and class, and where its power is going (balanced, weapons, engines or shields).
- The solar system (economy, wealth, conflict, race, star, number of planets) and the planet you are on (type,
  weather, flora, fauna, resources, scrap), with the planet's words through the game's own translator.

It also writes `%APPDATA%\NMSDeck\probe.txt`: every field, anything that could not be read, the game build, and for
each place you have been (space, a station, on foot...) each step of the system and planet reads and which check
turned one down. It is rewritten when a place is new or its result changes.

## How it finds things

NMS.py finds functions by byte pattern and fields by offset, so a game update can move what it reads. The bridge
leans on shapes rather than offsets where it can: the player state by its run of galactic addresses, the player's
position by its transform, inventories by their item lists, and the current solar system by the game's own pointer to
it (tried where it is known to live, then searched for), taken only when what it points at is your system. Every read
through a pointer uses `ReadProcessMemory`, which fails politely on a bad address where a plain read would take the
game down, and values read at a fixed distance are checked against what the game allows (enums, flags, a planet's own
index, an inventory's class), each on its own, so a field that has moved reads as unknown rather than wrong and the
fields beside it still show. Besides NMS.py's main loop it watches one game function,
`cGcSpaceshipComponent::UpdateControlled`, which the game runs for the ship you are flying: the bridge keeps only
that ship's address, to read its power setting, and does nothing else there.

Written against game build 179666 with NMS.py 179105.0. 0.6.1 follows build 180383 (2026-09-30), which moved the
solar system pointer.

## Installing

The NMS Deck plugin installs it for you: **Install the bridge** in any NMS Deck key's settings. By hand:

1. Install Python 3.9 to 3.13 from [python.org](https://www.python.org/downloads/), then NMS.py:
   `python -m pip install nmspy`.
2. Put `nms_deck_bridge.py` in the mods folder NMS.py is set to: `mod_dir` in
   `%APPDATA%\pymhf\nmspy\pymhf.local.toml`. Running `pymhf run nmspy` once asks for it if it is not set.
3. Start the game through NMS.py: `pymhf run nmspy`.

## The state file

JSON, rewritten once a second, atomically (written to a temporary file and renamed into place). `protocol` is the
file's format version; `inGame` is false at the menu. Charges are fractions from 0 to 1; `system` holds the game's
enum numbers; `planet` holds the game's own words and is null in space; `where.stable` is the game's
`EnvironmentLocation` (1 space, 2 station, 3 on foot, 4 in the ship on a planet, 5 in an exocraft); `power` is the
flown ship's power setting (0 balanced, 1 weapons, 2 engines, 3 shields, the order Cycle Power steps through) and null
when you are not flying. Anything that could not be read is null.

## Licence

MIT, see `LICENSE`. NMS Deck is an unofficial fan project, not affiliated with or endorsed by Hello Games. No Man's Sky
is a trademark of Hello Games Ltd. NMS.py and pyMHF are separate open-source projects by monkeyman192.
