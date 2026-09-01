---
name: happy_farm
description: Comprehensive, charming, and fully playable Happy Farm game widget with crops, livestock, economy, orders, day-night/weather cycles, and audio.
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [widget, route]
env_from_settings: []
when_to_use: When the user wants to play a cozy farming simulation game in an Ouroboros widget with crops, livestock, crafting, progression, and rich audiovisuals.
timeout_sec: 60
ui_tab:
  tab_id: happy_farm
  title: Happy Farm
  icon: leaf
  span: 2
  render:
    kind: module
    entry: widget.js
---

# Happy Farm (v0.1.0)

A rich, interactive, cozy farming game extension widget for Ouroboros.

## Features
- **Tile-based Farm Grid**: Till soil, plant diverse crops, water, fertilize, and harvest when ripe.
- **Crop Variety**: Wheat, Carrots, Tomatoes, Strawberries, Golden Corn, Watermelons, and Magic Pumpkins.
- **Livestock & Pastures**: Raise and feed Chickens (Eggs), Cows (Milk), and Sheep (Wool) with wandering animations.
- **Economy & Progression**: Coin market, leveling system, unlockable crops and automated Sprinklers.
- **Order Board / Village Quests**: Deliver fresh produce to villagers for bonus coin and XP rewards.
- **Day/Night & Weather**: Sunny, Sunset, Starry Night with fireflies, and Rain (which automatically waters all crops).
- **Web Audio Sound Effects**: Procedurally synthesized pleasant chimes, animal sounds, ambient nature, and water splashes.
- **Offline Growth & Autosave**: Calculates growth progress while away and persists farm state server-side under the skill state directory through the reviewed `/save` route.
