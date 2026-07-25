# Dynamic Fire Evacuation Router with Real-Time Hazard Mapping

A localized, decentralized module that ingests simulated multi-sensor fire
data, computes a dynamically updating safest exit path across a simulated
building floor, and drives an intelligent visual (and audible) indicator
system to guide occupants away from active hazards in real time.

Built fully in software — no physical hardware required. The architecture
is hardware-agnostic by design: simulated sensor input and simulated LED
output are both isolated behind MQTT topic contracts, so swapping in real
ESP32 nodes with DHT22/MQ-2/IR sensors would require no change to the
core routing or coordination logic.

---

## What this is

In large commercial facilities, static exit signage can route occupants
directly into danger if a fire breaks out between them and that exit.
This system models a 6×6 zone building floor where each zone continuously
receives (simulated) temperature, smoke, and flame readings, fuses them
into a hazard cost via a non-linear formula, and recomputes the safest
path to the nearest exit in real time — driving both a live control-panel
UI and a central monitoring dashboard.

---

## Architecture

```
[Injector: Flask + control-panel UI]
        |  publishes simulated sensor readings via MQTT
        v
[Mosquitto MQTT broker @ localhost:1883]
        |
        v
[Routing Coordinator]                    [36x Fire Node processes]
   - sole subscriber to hazard topics       - one per building zone
   - runs ONE multi-source Dijkstra            - subscribes to own sensor
     per update (exits -> all zones)             topic + routing broadcast
   - broadcasts full routing table            - does O(1) local work:
     (retained, fire/system/routing)             hysteresis, LED color/
                                                  direction decision,
                                                  fail-safe/shelter logic
        |                                        |
        v                                        v
        +--------------------> [Node-RED Fire Commander Dashboard]
                                   live 2D floor grid, hazard levels,
                                   exit paths, system health
```

See [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) for the full
architecture spec, sensor fusion formula, MQTT topic design, and
evaluation-criteria mapping.

---

## Repo structure

```
fire-evac-router/
├── injector/                  Simulation/injection tool (Flask + control-panel UI)
│   ├── app.py
│   └── templates/index.html
├── node/
│   ├── graph_config.py        Building graph (6x6 grid, exits, JSON export)
│   ├── routing_common.py      Shared pure logic: fusion formula, Dijkstra
│   ├── routing_coordinator.py Single coordinator process (see architecture)
│   ├── fire_node.py           Per-zone node: sensors -> LED decision
│   └── run_all_nodes.py       Runs coordinator + all 36 zone nodes together
├── dashboard/                 Node-RED flow (Fire Commander Dashboard)
├── docs/
│   ├── PROJECT_SPEC.md            Full architecture & requirements spec
│   ├── latency_debugging_notes.md Post-mortem: two real bugs found & fixed
│   ├── DEMO_RUNBOOK.md             Startup sequence, demo script, troubleshooting
│   ├── engineering_report.md      (pending)
│   └── pitch_deck.pptx            (pending)
└── requirements.txt
```

---

## Quick start

Full detail, verification checklist, and demo script in
[`docs/DEMO_RUNBOOK.md`](docs/DEMO_RUNBOOK.md). Short version:

```powershell
# 1. Mosquitto broker (usually already running as a Windows service)
Get-Service -Name "mosquitto"

# 2. Coordinator + all 36 zone nodes
python node/run_all_nodes.py

# 3. Injector (Flask control panel)
python injector/app.py
# -> open http://127.0.0.1:5000

# 4. Node-RED (Fire Commander Dashboard)
node-red
# -> open http://127.0.0.1:1880/ui
```

Requires Python 3.9+ and Node.js. Install Python deps with:
```powershell
pip install -r requirements.txt
```

---

## Key design decisions

- **Sensor fusion is exponential, not binary-threshold**: smoke density
  contributes non-linearly to hazard cost, reflecting how visibility and
  toxicity degrade faster than a simple linear/threshold model would
  suggest. Full formula in `docs/PROJECT_SPEC.md`.
- **Routing is centralized, not per-node**: a single coordinator runs one
  multi-source Dijkstra (from both exits) per hazard update and broadcasts
  the result. Early per-node independent recomputation caused a 36x
  redundant-work amplification bug — see
  `docs/latency_debugging_notes.md` for the full diagnosis and fix.
- **LED state publishes on change, not on every broadcast**: change
  detection + a periodic keepalive prevents MQTT publish-volume storms
  while keeping the retained topic fresh for late subscribers — also
  covered in the debugging notes.
- **Fail-safe by design**: nodes fall back to a `shelter_in_place` state
  (never a misleading movement arrow) if surrounded by hazard, and MQTT
  Last-Will-and-Testament + health heartbeats let the dashboard detect a
  degraded or offline node.

---

## Known scope limitations (deliberate, documented)

A few spec-mentioned elements were consciously scoped out given project
time constraints, rather than left as silent gaps:

- **Occupancy/access-control data** — not simulated; would require
  camera/access-control integration beyond this project's scope.
- **Occupant density/floor weighting in routing** — current routing is
  purely hazard-cost-based; occupancy-aware routing is a reasonable
  future extension.
- **Dataset-calibrated fusion constants** — the fusion formula's
  coefficients were manually tuned for demo purposes rather than fitted
  against NIST/Kaggle fire datasets as the spec suggests is possible.
- **Physical hardware / Wokwi firmware port** — this build is fully
  simulated by design; see the architecture note above on why porting to
  real ESP32 hardware would be a low-friction next step, not a redesign.

---

## Status

- [x] Building graph & exit topology
- [x] Sensor fusion + centralized multi-source Dijkstra routing
- [x] Simulation/injection tool with live control-panel UI
- [x] Fail-safe (shelter-in-place, degraded/offline health states)
- [x] Multi-node MQTT communication (retained topics, LWT, QoS)
- [x] Audible + visual distress alarm
- [x] Fire Commander Dashboard (Node-RED)
- [ ] Engineering report
- [ ] Pitch deck