# Debugging Real-Time Recompute Latency: A Two-Bug Post-Mortem

## Context

The spec requires on-device routing logic to update within **300ms** of a
sensor state change (Algorithm Responsiveness & Sensor Fusion, 30% of
evaluation). During live testing with all 36 zone nodes running against a
local MQTT broker, observed latency climbed from ~2s to over 21s within
minutes of runtime — a symptom pattern (steadily worsening, not just
uniformly slow) that pointed to accumulation, not simple inefficiency.

Two distinct, unrelated bugs were compounding this. Fixing them separately
is the reason this doc has two sections.

---

## Bug 1: O(N²) redundant recomputation (the "amplification" bug)

### Symptom
Latency was high (1999ms–3898ms) but *not yet* climbing over time in the
first measurement pass.

### Root cause
Each of the 36 `FireNode` instances subscribed directly to
`fire/nodes/+/hazard` — i.e., **every node listened to every other node's
hazard updates** — and independently ran a full multi-source Dijkstra
solve on every message it received.

The math: 36 nodes each publish a hazard update per sensor tick → 36
messages × 36 independently-recomputing subscribers = **1,296 redundant
graph solves per tick**, for a computation that should only need to happen
**once** (a single Dijkstra run from both exits produces the entire
building's routing table in one pass).

Running all 36 as threads sharing one Python process (one GIL) serialized
this redundant work, turning what should have been ~100µs solves into a
multi-second backlog.

### Why this is easy to miss
The architecture *looked* reasonable in isolation — "each node computes its
own routing" sounds decentralized and robust. The bug only becomes visible
at the system level: decentralizing a computation that has a single
correct global answer (shortest-path-to-exit for a static graph with
global hazard weights) doesn't add robustness, it multiplies work.

### Fix
Introduced a single coordinator process (`routing_coordinator.py`) as the
**only** subscriber to `fire/nodes/+/hazard`. It runs one multi-source
Dijkstra per hazard update and broadcasts the complete routing table
(`hazard_costs`, `dist_to_exit`, `next_hop`) on a retained topic,
`fire/system/routing`. Individual `FireNode` instances no longer import
`networkx` or hold a graph at all — they subscribe only to their own
sensor topic and the routing broadcast, doing O(1) local work (hysteresis
check, LED color/direction decision) per update.

Shared pure logic (the fusion formula, edge-weight calculation,
`compute_routing()`) was factored into `routing_common.py` so both the
coordinator and any future consumer use one definition — avoids drift
between "the real algorithm" and "what each node thinks it's doing."

### Result
Confirmed via in-process test harness: exactly 36 recomputes per tick
(one per hazard message, at the coordinator), not 1,296.

---

## Bug 2: MQTT publish storm / unbounded outgoing queue (the "leak")

### Symptom
After fixing Bug 1, latency was *still* climbing over time (though more
slowly) — the telltale sign of an actual leak rather than inefficient but
constant-cost work.

### Diagnosis approach
Rather than guessing, we instrumented before touching anything:
- Logged thread count, `paho-mqtt` internal queue size
  (`_out_messages`, `_out_packet`), recomputes/10s, and LED
  publishes/10s, sampled every 10 seconds.
- Built an in-process harness (coordinator + all 36 nodes wired together,
  bypassing the real broker) and fed it repeated *unchanging* ambient
  sensor ticks — the quietest possible steady state — specifically to
  isolate whether the system was doing unnecessary work even when nothing
  had changed.

Result: `recompute_count` scaled correctly (36/tick, confirming Bug 1's
fix held). But `total_led_publishes` scaled at a flat **36× multiplier per
tick** — 1,296 LED publishes per tick, even when literally nothing had
changed since the previous tick.

### Root cause
Every `FireNode` unconditionally republished its LED state (`color`,
`direction`, `state`) every time it received a routing broadcast from the
coordinator — regardless of whether its own LED state had actually
changed. Since the coordinator broadcasts once per hazard update (36×/tick)
and all 36 nodes receive every broadcast, that's 36 × 36 = 1,296
unconditional QoS-1 publishes per tick, ~97% of which carried information
identical to what was just sent moments earlier.

This is not a Python-level memory leak (no growing list/dict in
application code — verified by grepping for `.append`/`+=`/`insert`/
`extend` across the codebase). It's a **message-volume leak**: `paho-mqtt`
queues unacknowledged QoS-1 publishes in an internal `OrderedDict`
(`_out_messages`) with **no default cap** (`_max_queued_messages` defaults
to 0 = unbounded). At sustained high publish volume across 37 client
objects sharing one GIL for both network I/O and callback dispatch, once
the ack rate falls even slightly behind the publish rate, that queue —
and the real backlog behind it — grows a little more every tick and never
fully drains. That produces exactly the "climbs steadily over time"
signature observed, as distinct from Bug 1's "high but flat" signature.

### Fix
Two changes:

1. **Change-detection before publish**: each `FireNode` now tracks its
   last-published `(color, direction, state)` tuple and only calls
   `_publish_led()` when that tuple actually differs from the last
   publish — collapsing the steady-state publish ratio from 36:1 down to
   ~1:1 (each zone publishes once when its state is established, then
   goes silent). A periodic keepalive republish (every 5s) is retained
   regardless of state change, so a late-joining subscriber to the
   retained LED topic never sees stale data.

2. **Queue safety net**: `max_queued_messages_set(100)` applied to both
   the coordinator and every `FireNode`'s MQTT client, so if this class of
   bug reoccurs in the future, `paho-mqtt` drops excess queued messages
   once the cap is hit instead of growing the backlog without bound. This
   converts a silent, unbounded-growth failure mode into a bounded,
   observable one.

### Verification
Regression-tested with the same in-process harness:
- Identical repeated state → suppressed (no republish)
- Real state change → publishes immediately (no added latency for actual
  safety-critical transitions)
- Repeated identical state after a change → suppressed again
- 5s elapsed with no change → keepalive republish fires correctly
- Safety-critical transitions (flame detected, shelter-in-place) always
  publish immediately and are never suppressed, verified explicitly
  against the "isolated zone under flashover" scenario
- Under sustained unchanging load: LED:recompute publish ratio dropped
  from a flat 36.0 to 1.0 on the first tick, then 0.0 on subsequent ticks

---

## Lessons for anyone hitting a similar "climbing latency" symptom

1. **A latency number that's high-but-flat and a latency number that's
   climbing over time are different bugs with different causes.** Don't
   apply one fix and assume it addressed both — verify the *shape* of the
   degradation, not just the magnitude, before and after each fix.
2. **"Each node computes independently" sounds robust but often means
   "each node redundantly recomputes the same global answer."** If there's
   one correct global result (shortest path on a shared graph), compute it
   once and broadcast it, rather than letting N consumers each re-derive
   it.
3. **Unconditional republish-on-any-upstream-update is a common source of
   fan-out amplification** in pub/sub systems. Always ask: does this
   subscriber need to act on every message it receives, or only on
   messages that represent an actual change to *its own* state?
4. **Check library defaults for unbounded queues.** `paho-mqtt`'s
   `_max_queued_messages` defaulting to unbounded is a reasonable default
   for low-volume use, but silently dangerous once publish volume is high
   enough that ack latency can fall behind — worth capping explicitly in
   any high-throughput MQTT deployment.
5. **Build a broker-free in-process test harness early.** Being able to
   drive the coordinator and nodes together without a live MQTT broker
   made it possible to get deterministic publish/recompute counts under
   controlled load, rather than trying to diagnose a live system with
   noisy, hard-to-reproduce timing.