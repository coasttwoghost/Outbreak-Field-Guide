# Outbreak Field Guide

A field guide for Resident Evil Outbreak (File 1 and File 2), built to sit open next to the game. Items, enemies, floor plans and puzzle answers for every level, at every difficulty.

Use it online: https://outbreak-field-guide.outbreak-intel.workers.dev

## Live tracking

The download below runs the guide on your own PC and lets it follow the game in PCSX2. With it running, the guide:

- opens the level you are in and sets the difficulty
- works out which item pattern the run rolled, so you don't have to check
- draws you and your teammates on the floor plan, room by room
- can show enemies, remaining items and the Desperate Times gas, each behind its own switch (off by default)

It only reads the emulator's memory. It never writes anything into the game or the emulator, so it can't change your run or anyone else's, online or offline.

## Getting it

1. Download the zip (`Outbreak.Field.Guide.zip`) from the [latest release](../../releases/latest).
2. Extract it anywhere.
3. Double-click `Outbreak Field Guide.exe`. Your browser opens on the guide.
4. Start PCSX2 and load Outbreak, before or after. The guide picks the game up within a few seconds.

Nothing to install: no Python, no other tools. Leave the small black window open while you play; closing it stops the guide.

Windows will probably say "Windows protected your PC" the first time, because the program isn't code-signed. Click "More info", then "Run anyway".

### Playing with friends

Run `Outbreak Field Guide (share).bat` instead and send people the `http://192.168...` address it prints. They open it in a browser on the same network, on a phone or a second screen, with nothing to download. Windows will ask once whether to allow it on your network.

### Putting the map on stream

Add an OBS browser source pointing at `http://127.0.0.1:8777/?cast`. It shows the floor plan alone and follows the game. Options go on the address:

| Option | What it does |
|---|---|
| `&hide=items,enemies,gas` | leave some layers out |
| `&zoom=1.5` | zoom in when a floor plan is too big to show whole |
| `&me=2` | which player (1 to 4) the map follows |

Start the guide before OBS.

## Requirements

- Windows 10 or 11
- PCSX2 2.x (the Qt version, `pcsx2-qt.exe`)
- The Japanese release of File 1 or File 2. The English fan translation patch works.

## Running from source

```
py bridge/bridge.py
```

Python 3.10 or newer, standard library only. `py bridge/package.py` builds the zip with PyInstaller.

## Credits

Field guide and bridge filed by ALYSSA:C 100%.

The memory layout this reads was first mapped by the Outbreak Tracker project (Fothsid, killme, phoe-nix and madgamer98), whose tracker made live tools for this game possible. This project does not include or use their code or binaries. Room names, item data and enemy data come from the game discs and the Outbreak community's research.

Resident Evil is a trademark of Capcom. This is a fan project, not affiliated with or endorsed by Capcom.

## License

MIT, see [LICENSE](LICENSE).
