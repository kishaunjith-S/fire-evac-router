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
   - subscribes to its own zone's sensor topic + ALL nodes' hazard topics
     (`fire/nodes/+/hazard`) — global Dijkstra needs the global hazard vector,
     neighbor-only state is insufficient (see Pathfinding)
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
- Edge weight = derived from the hazard cost of BOTH endpoint zones (see
  formula), default weight = 1

## MQTT topic design
- `fire/sensors/{zone_id}` — injector publishes raw simulated sensor payload:
  `{"temp": float C, "smoke": float ppm, "flame": bool, "ts": epoch}`
  QoS 1. Published on every user interaction AND on a 1 Hz ambient tick (see
  Fail-safe — without the tick every quiet zone would self-report as degraded).
- `fire/nodes/{zone_id}/hazard` — each node publishes its computed zone cost
  after fusion: `{"cost": float, "ts": epoch}`
  QoS 1, **retained**. Retention matters: a node that starts late (or restarts)
  must inherit the current hazard picture from the broker, otherwise it computes
  its first paths against an all-clear graph and emits a wrong LED state.
- `fire/nodes/{zone_id}/led` — each node publishes its LED state:
  `{"color": "green"|"yellow"|"red", "direction": "N"|"S"|"E"|"W"|null,
    "src_ts": epoch, "ts": epoch}`
  QoS 1, retained. `src_ts` is the `ts` of the sensor reading that caused this
  state — carried through the whole pipeline so end-to-end latency is measurable
  (see Pathfinding).
- `fire/system/health/{zone_id}` — per-node heartbeat, QoS 0, retained:
  `{"state": "ok"|"degraded"|"offline", "ts": epoch}`
  Each node registers an MQTT **Last Will & Testament** on this topic with
  `{"state": "offline"}`. The broker then announces node death immediately on
  TCP drop, rather than waiting for the 5s silence timeout to infer it.

Note on QoS choice: hazard/LED/sensor use QoS 1 because a dropped hazard update
leaves the graph stale until the next publish; health is QoS 0 because it is
re-sent every second and LWT covers the failure case.

## Sensor fusion formula (must NOT be simple binary threshold)
Per-zone hazard cost, exponential weighting combining all three vectors:

```
zone_cost(z) = 1
             + alpha * max(0, temp - AMBIENT_C)
             + beta  * smoke_ppm ** 1.5
             + flame * FLAME_PENALTY

where:
  alpha = 0.05            # tunable
  beta  = 0.002           # tunable
  AMBIENT_C = 22.0        # room temperature baseline
  FLAME_PENALTY = 1000    # near-infinite cost, effectively blocks the edge
```

The `max(0, temp - AMBIENT_C)` offset is load-bearing: without it a resting
room at 25 C contributes 1.25 to every zone, more than doubling the baseline
weight of 1 and compressing the dynamic range the hazard signal has to work in.

**Zone cost -> edge weight.** Traversing an edge means leaving one zone and
entering another, so charge half of each endpoint:

```
w(u, v) = (zone_cost(u) + zone_cost(v)) / 2
```

Applying the full zone cost to every incident corridor (the naive reading)
double-counts each intermediate zone along a path — a route crossing zone X
would pay X's cost twice, on entry and on exit, distorting the comparison
between a long safe path and a short slightly-hazardous one.

Rationale for report: exponential smoke term reflects how visibility/toxicity
degrades non-linearly; flame presence acts as a hard block; temperature is
linear contribution since thermal risk scales more predictably at these ranges.

## Pathfinding

**Algorithm: single reverse multi-source Dijkstra from the exits.**

Add a virtual super-sink connected to both exit nodes with zero-weight edges,
then run one `networkx.single_source_dijkstra` from that sink over the graph
with edges reversed. One run yields, for every zone simultaneously:
- `dist_to_exit[z]` — cost of the cheapest route from z to the nearest exit
- `next_hop[z]` — the neighbor minimising `w(z,n) + dist_to_exit[n]`

`next_hop` is exactly the LED arrow direction, so a single computation produces
the entire LED matrix. There is no occupant-location input and no per-occupant
path: every zone always displays the correct direction for anyone standing in
it. (This is why no occupant topic exists in the MQTT design — the routing
field is computed for all zones at once.)

**Recompute strategy: recompute the whole field, every time.**

The graph is 36 nodes / ~60 edges. A full Dijkstra over it is on the order of
100 microseconds in networkx — three orders of magnitude inside the 300 ms
budget. The budget is consumed by MQTT round-trip and process wakeup, not by
compute, so there is nothing to gain from incremental recomputation.

Recomputing "only paths through the affected zone" is also incorrect, and the
spec should not claim it. When a zone's cost *rises*, the set of affected routes
cannot be identified without recomputing — and when a cost *falls* (suppression,
sensor clearing), that zone becomes newly attractive to routes that never
touched it at all. Correct incremental recomputation requires D*Lite / LPA*,
which is not worth the implementation risk here. Full recompute is simpler,
provably correct, and comfortably fast enough.

**Latency instrumentation (the 300 ms claim must be demonstrable).**
The injector stamps `ts` on each sensor payload. That value is carried through
fusion and pathfinding and re-emitted as `src_ts` on `fire/nodes/{id}/led`.
Each node logs `t_publish - src_ts` per update; the dashboard displays current
and worst-case observed latency. The 300 ms requirement is therefore a measured
number on screen during the demo, not an assertion.

**Unreachable exits / shelter-in-place.**
If fire isolates a zone, that zone has no path to the super-sink and
`dist_to_exit` is infinite. This is a normal operating state, not an error —
`dijkstra_path` raising `NetworkXNoPath` must never reach the top level. Such a
zone emits `color: "red"`, `direction: null`, `state: "shelter_in_place"`,
meaning "do not move, no safe egress." Worth staging deliberately in the demo.

**Path hysteresis (anti-flapping).**
With live recompute, two near-equal routes will trade places on every message
and the arrows will visibly oscillate. A node changes its published `next_hop`
only when both hold:
- the new route is more than `HYSTERESIS_MARGIN = 5%` cheaper than the currently
  displayed one, and
- at least `MIN_DWELL_MS = 500` ms have elapsed since the last direction change.

A transition to `shelter_in_place`, or any change caused by flame detection,
bypasses both checks — safety-critical changes are never damped.

## LED state logic
- green = on safest path, no local hazard
- yellow = zone is a high-smoke alternate route (elevated but passable cost)
- pulsing red = immediate danger / do not traverse (flame present or cost > threshold)
- solid red + no direction = shelter in place, zone has no route to any exit
- direction = which neighbor to move toward (`next_hop` from the reverse
  multi-source Dijkstra, subject to the hysteresis rules above)

## Fail-safe requirements
- The injector publishes an ambient reading for every zone at 1 Hz, in addition
  to user-triggered events. Without this baseline traffic the 5s silence rule
  below would mark every idle zone degraded a few seconds after startup, and the
  health panel would be red before the demo begins.
- If a node stops receiving sensor updates (no message for >5s), fall back to
  last-known-safe default path, mark `fire/system/health/{zone_id}` as degraded
  for that zone
- Node death is additionally detected by the broker via the MQTT Last Will
  registered on `fire/system/health/{zone_id}` — immediate on TCP drop, rather
  than waiting out the 5s timeout
- If payload is malformed/corrupted, discard and log, do not crash, retain last
  good state
- If no route to any exit exists, publish shelter-in-place rather than raising
  (see Pathfinding)

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