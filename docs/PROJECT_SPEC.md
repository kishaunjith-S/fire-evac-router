# Dynamic Fire Evacuation Router with Real-Time Hazard Mapping

## Constraint context
Time-boxed hackathon build. NO physical hardware — fully simulated/virtual.
Optional stretch: port node logic to MicroPython/Arduino C++ for Wokwi (browser ESP32 simulator) if time allows, purely for demo credibility.

## Problem Statement (source of truth — do not deviate)
Design a localized module that continuously ingests data from multiple fire
sensors, computes a dynamically updating safest exit path across a simulated
indoor layout, and drives an intelligent visual indicator (LED strip) matrix
to guide occupants away from active hazards.

## Architecture (all software, no hardware)

```
[Injector: Flask + HTML grid UI]
        |  publishes simulated sensor readings via MQTT
        v
[Mosquitto MQTT broker @ localhost:1883]
        |
        v
[Fire Node processes] (one Python process per zone, simulates an MCU)
   - subscribes to its own zone's sensor topic + neighbor hazard topics
   - runs sensor fusion (see formula below)
   - runs Dijkstra/A* pathfinding on the building graph
   - publishes: (a) its own hazard cost, (b) computed LED state/path
        |
        v
[Node-RED Dashboard] subscribes to all node topics, renders 2D floor grid,
   live hazard heatmap, current exit paths, system health
```

## Building layout
- 6x6 grid graph (networkx), each cell = one "zone" = one simulated node
- 2 exit nodes marked at opposite corners/edges
- Edges = walkable corridors between adjacent zones
- Edge weight = dynamic hazard cost (see formula), default weight = 1

## MQTT topic design
- `fire/sensors/{zone_id}` — injector publishes raw simulated sensor payload:
  `{"temp": float C, "smoke": float ppm, "flame": bool, "ts": epoch}`
- `fire/nodes/{zone_id}/hazard` — each node publishes its computed edge cost
  after fusion: `{"cost": float, "ts": epoch}`
- `fire/nodes/{zone_id}/led` — each node publishes its LED state:
  `{"color": "green"|"yellow"|"red", "direction": "N"|"S"|"E"|"W"|null}`
- `fire/system/health` — heartbeat per node for Fail-Safe Operation criterion

## Sensor fusion formula (must NOT be simple binary threshold)
Exponential weighting combining all three vectors:

```
cost = 1 + (alpha * temp) + (beta * smoke_ppm ** 1.5) + (flame * FLAME_PENALTY)

where:
  alpha = 0.05           # tunable
  beta  = 0.002           # tunable
  FLAME_PENALTY = 1000    # near-infinite cost, effectively blocks the edge
```
This cost becomes the edge weight for all corridors touching that zone.
Rationale for report: exponential smoke term reflects how visibility/toxicity
degrades non-linearly; flame presence acts as a hard block; temperature is
linear contribution since thermal risk scales more predictably at these ranges.

## Pathfinding
- networkx `dijkstra_path` from occupant's current zone to nearest reachable
  exit, using live edge weights from fusion formula
- On each new sensor reading -> recompute affected edges -> re-run Dijkstra
  -> if path changed, push new LED direction to affected nodes
- Must satisfy: recompute within 300ms of a state change (simulated, so just
  keep the compute path lightweight — avoid recomputing entire graph if only
  one zone changed; recompute only paths through affected zone)

## LED state logic
- green = on safest path, no local hazard
- yellow = zone is a high-smoke alternate route (elevated but passable cost)
- pulsing red = immediate danger / do not traverse (flame present or cost > threshold)
- direction = which neighbor to move toward (derived from next hop in Dijkstra path)

## Fail-safe requirements
- If a node stops receiving sensor updates (no message for >5s), fall back to
  last-known-safe default path, mark system/health as degraded for that zone
- If payload is malformed/corrupted, discard and log, do not crash, retain last
  good state

## Deliverables checklist (per problem statement)
1. Simulation/Injection Tool — Flask app, clickable 2D grid, "trigger flashover"
   button per zone, publishes to MQTT
2. Firmware Source Code — Python (node/fire_node.py) representing MCU logic;
   optional MicroPython port for Wokwi
3. Fire Commander Dashboard — Node-RED flow, 2D grid, live hazard nodes,
   current paths, system health panel
4. Engineering Report & Presentation — flowchart of sensor threshold -> edge
   weight -> path recompute pipeline; pitch deck using provided template

## Evaluation weight priorities (build effort should match this)
1. Algorithm Responsiveness & Sensor Fusion — 30%
2. Simulation Quality & Demonstration — 20%
3. Visual Interface & Usability Clarity — 15%
4. Solution Pitch & Presentation — 15%
5. Multi-Node Communication Logic — 10%
6. Fail-Safe Operation — 10%

## Folder structure
```
fire-evac-router/
├── injector/
│   ├── app.py
│   ├── templates/index.html
│   └── static/
├── node/
│   ├── fire_node.py
│   └── graph_config.py
├── dashboard/
│   └── flow.json
├── firmware_wokwi/         (optional stretch)
├── docs/
│   ├── PROJECT_SPEC.md      (this file)
│   ├── engineering_report.md
│   └── pitch_deck.pptx
└── requirements.txt
```

## Build order (for Claude Code prompts)
1. node/graph_config.py — building graph, exits, JSON export
2. node/fire_node.py — MQTT sub/pub, fusion formula, Dijkstra, fail-safe
3. injector/app.py + templates/index.html — grid UI, flashover trigger, MQTT publish
4. dashboard/flow.json — Node-RED flow (or manual UI build in Node-RED editor)
5. docs/engineering_report.md — flowchart + formula explanation