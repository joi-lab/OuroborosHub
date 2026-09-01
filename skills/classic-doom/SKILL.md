---
name: classic-doom
description: Fully playable Classic Doom (1993) standalone 2.5D FPS raycaster extension widget with textured walls, procedural sprite enemies, authentic weapons, levels, HUD, Web Audio sound and heavy metal music synthesizer.
version: 1.0.0
author: Ouroboros Swarm
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [widget, route]
env_from_settings: []
when_to_use: When the user wants to play an authentic Classic Doom (1993) 2.5D FPS raycaster game directly in an Ouroboros extension widget with monsters, weapons, sound effects, and heavy metal synth.
timeout_sec: 60
ui_tab:
  tab_id: classic_doom
  title: "Classic Doom (1993)"
  icon: crosshairs
  span: 2
  render:
    kind: module
    entry: widget.js
    height: 820
    max_height: 960
---

# Classic Doom (1993) Extension Widget

An authentic, fully self-contained, high-performance 2.5D raycaster FPS game engine modeled after the legendary 1993 id Software classic, running directly inside an Ouroboros extension widget.

## Features

- **2.5D Raycaster Engine**: Depth-shaded textured walls, variable lighting, diminishing light colormaps, animated doors, and scanline rendering with zero external CDNs.
- **Authentic Weapons**: Fist, Pistol, Shotgun, Chaingun, and Plasma Gun with firing animations, recoil, muzzle flashes, and ammo management.
- **Classic Monsters**: Zombieman, Shotgun Guy, Imp (fireballs), Demon (Pinky charge), and Boss encounter (Cyberdemon / Baron).
- **Multiple Atmospheric Maps**:
  - E1M1: Hangar (Zigzag corridor, courtyard window, nukage pool, exit chamber)
  - E1M2: Nuclear Plant (Tech maze, computer terminals, keycard doors, ambushes)
  - E1M8: Phobos Anomaly (Hellish arena, pentagram floor, boss battle, victory teleporter)
- **100% Procedural Audio & Music**: Web Audio API sound effects (gunfire, monster grunts, doors, pickups) + synthesized heavy metal background tracks without any external files.
- **Authentic Doom HUD**: Animated Doomguy status face (damage reactions, grins, health states), ammo counts, health/armor %, and key inventory.
- **Menus & Game States**: Title screen, difficulty selector, level intermission tally, game over, victory screen, and pause menu.

## Controls

- **WASD / Arrow Keys**: Move and strafe
- **Mouse / Left-Right Arrows**: Turn and look
- **Left Click / Ctrl**: Fire active weapon
- **Space**: Open doors, flip switches, activate items
- **1 - 5**: Select weapon (1: Fist, 2: Pistol, 3: Shotgun, 4: Chaingun, 5: Plasma Gun)
- **Shift**: Speed run
- **M**: Mute / unmute background music
- **Esc**: Pause / Menu
