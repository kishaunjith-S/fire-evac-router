"""Fire Node — simulates one zone's MCU.

One process per zone (per docs/PROJECT_SPEC.md). Each instance:
  - subscribes to its own zone's raw sensor topic and runs the fusion formula
  - subscribes to fire/nodes/+/hazard to maintain the global hazard vector
    (a global Dijkstra needs global state; neighbor-only is insufficient)
  - recomputes the reverse multi-source Dijkstra from the exits on every
    hazard update and publishes its own LED state
  - registers an MQTT Last Will so the broker reports node death immediately,
    and separately watches for sensor silence >5s as a fail-safe fallback

Run one instance per zone:
    python fire_node.py --zone r0c0
"""

import argparse
import json
import logging
import threading
import time

import networkx as nx
import paho.mqtt.client as mqtt

import graph_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

# --- Sensor fusion constants (docs/PROJECT_SPEC.md: Sensor fusion formula) ---
ALPHA = 0.05
BETA = 0.002
AMBIENT_C = 22.0
FLAME_PENALTY = 1000
BASELINE_COST = 1.0

# Color thresholds on zone_cost. Tunable, same spirit as alpha/beta/FLAME_PENALTY.
YELLOW_COST_THRESHOLD = 5.0
RED_COST_THRESHOLD = 50.0

# --- Pathfinding constants ---
EXIT_SINK = "__EXIT__"
HYSTERESIS_MARGIN = 0.05
MIN_DWELL_MS = 500

# A zone at or above this cost is a hard block, not just expensive: edges
# touching it are removed from the routing graph entirely. Without this, an
# isolated zone never actually reaches float("inf") — FLAME_PENALTY is only
# a very large weight, and a route through it, however absurd, always exists
# on an unchanged topology. Removing the edge is what makes shelter-in-place
# ("no path to any exit") a real, reachable state instead of a dead branch.
BLOCKED_COST_THRESHOLD = FLAME_PENALTY

# --- Fail-safe constants ---
SENSOR_TIMEOUT_S = 5.0
HEALTH_PUBLISH_INTERVAL_S = 2.0
WATCHDOG_INTERVAL_S = 1.0

TOPIC_SENSOR = "fire/sensors/{zone_id}"
TOPIC_HAZARD = "fire/nodes/{zone_id}/hazard"
TOPIC_HAZARD_WILDCARD = "fire/nodes/+/hazard"
TOPIC_LED = "fire/nodes/{zone_id}/led"
TOPIC_HEALTH = "fire/system/health/{zone_id}"


def zone_cost(temp, smoke, flame):
    """Fusion formula: raw sensor readings -> per-zone hazard cost."""
    return (
        BASELINE_COST
        + ALPHA * max(0.0, temp - AMBIENT_C)
        + BETA * (smoke ** 1.5)
        + (FLAME_PENALTY if flame else 0.0)
    )


def edge_weight(cost_u, cost_v):
    """Zone cost -> edge weight: average of both endpoints (see spec)."""
    return (cost_u + cost_v) / 2.0


def direction_between(graph, from_zone, to_zone):
    """Compass direction to move from from_zone to the adjacent to_zone."""
    from_row, from_col = graph.nodes[from_zone]["row"], graph.nodes[from_zone]["col"]
    to_row, to_col = graph.nodes[to_zone]["row"], graph.nodes[to_zone]["col"]
    if to_row < from_row:
        return "N"
    if to_row > from_row:
        return "S"
    if to_col > from_col:
        return "E"
    if to_col < from_col:
        return "W"
    return None


class FireNode:
    def __init__(self, zone_id, broker_host="localhost", broker_port=1883):
        if zone_id not in graph_config.build_graph().nodes:
            raise ValueError(f"unknown zone_id: {zone_id}")

        self.zone_id = zone_id
        self.broker_host = broker_host
        self.broker_port = broker_port

        self.graph = graph_config.build_graph()
        self.is_exit = self.graph.nodes[zone_id]["is_exit"]

        self.log = logging.getLogger(f"fire_node[{zone_id}]")

        self._lock = threading.Lock()

        # Global hazard vector, seeded at baseline until real readings arrive.
        self._hazard_costs = {z: BASELINE_COST for z in self.graph.nodes}

        # Own zone's last raw reading, needed for the flame-forced color rule
        # and the hysteresis safety bypass.
        self._own_flame = False
        self._last_sensor_ts = None

        # Displayed (post-hysteresis) routing state.
        self._displayed_next_hop = None
        self._displayed_route_cost = None
        self._last_change_ts = 0.0

        self._degraded = False
        self._stopping = False

        self._client = mqtt.Client(client_id=f"fire_node_{zone_id}")
        will_payload = json.dumps({"state": "offline", "ts": time.time()})
        self._client.will_set(
            TOPIC_HEALTH.format(zone_id=zone_id), will_payload, qos=0, retain=True
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # -- MQTT lifecycle ----------------------------------------------------

    def start(self):
        self._client.connect(self.broker_host, self.broker_port)
        self._client.loop_start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def stop(self):
        self._stopping = True
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        self.log.info("connected to broker (rc=%s)", rc)
        client.subscribe(TOPIC_SENSOR.format(zone_id=self.zone_id), qos=1)
        client.subscribe(TOPIC_HAZARD_WILDCARD, qos=1)
        self._publish_health("ok")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.log.warning("discarding malformed payload on %s", msg.topic)
            return

        if msg.topic == TOPIC_SENSOR.format(zone_id=self.zone_id):
            self._handle_sensor_reading(payload)
        elif msg.topic.startswith("fire/nodes/") and msg.topic.endswith("/hazard"):
            self._handle_hazard_update(msg.topic, payload)

    # -- Message handlers ----------------------------------------------------

    def _handle_sensor_reading(self, payload):
        try:
            temp = float(payload["temp"])
            smoke = float(payload["smoke"])
            flame = bool(payload["flame"])
        except (KeyError, TypeError, ValueError):
            self.log.warning("discarding malformed sensor payload: %r", payload)
            return

        self._last_sensor_ts = time.time()
        if self._degraded:
            self._degraded = False
            self._publish_health("ok")

        self._own_flame = flame
        cost = zone_cost(temp, smoke, flame)

        with self._lock:
            self._hazard_costs[self.zone_id] = cost

        self._publish_hazard(cost, payload.get("ts", time.time()))
        self._recompute_and_publish_led(src_ts=payload.get("ts", time.time()))

    def _handle_hazard_update(self, topic, payload):
        # fire/nodes/{zone_id}/hazard
        zone_id = topic.split("/")[2]
        if zone_id == self.zone_id or zone_id not in self._hazard_costs:
            return
        try:
            cost = float(payload["cost"])
        except (KeyError, TypeError, ValueError):
            self.log.warning("discarding malformed hazard payload: %r", payload)
            return

        with self._lock:
            self._hazard_costs[zone_id] = cost

        self._recompute_and_publish_led(src_ts=payload.get("ts", time.time()))

    # -- Pathfinding ----------------------------------------------------

    def _weighted_graph_with_sink(self):
        with self._lock:
            costs = dict(self._hazard_costs)

        g = nx.Graph()
        g.add_nodes_from(self.graph.nodes)
        for u, v in self.graph.edges:
            if costs[u] >= BLOCKED_COST_THRESHOLD or costs[v] >= BLOCKED_COST_THRESHOLD:
                continue  # hard block: edge removed, not just expensive
            g.add_edge(u, v, weight=edge_weight(costs[u], costs[v]))

        g.add_node(EXIT_SINK)
        for exit_zone in graph_config.EXIT_ZONES:
            g.add_edge(EXIT_SINK, exit_zone, weight=0.0)

        return g

    def _compute_routing(self):
        """Reverse multi-source Dijkstra from the exits.

        Returns (dist_to_exit, next_hop) dicts covering every real zone.
        next_hop[z] is None for exits (already arrived) and for zones with
        no route to any exit (shelter-in-place).
        """
        g = self._weighted_graph_with_sink()
        lengths, paths = nx.single_source_dijkstra(g, source=EXIT_SINK)

        dist_to_exit = {}
        next_hop = {}
        for z in self.graph.nodes:
            if z not in lengths:
                dist_to_exit[z] = float("inf")
                next_hop[z] = None
                continue
            dist_to_exit[z] = lengths[z]
            path = paths[z]  # [EXIT_SINK, ..., z]
            next_hop[z] = path[-2] if len(path) > 1 and path[-2] != EXIT_SINK else None

        return dist_to_exit, next_hop

    def _recompute_and_publish_led(self, src_ts):
        dist_to_exit, next_hop = self._compute_routing()

        unreachable = dist_to_exit[self.zone_id] == float("inf")
        at_exit = self.is_exit

        with self._lock:
            own_cost = self._hazard_costs[self.zone_id]
            all_costs = dict(self._hazard_costs)

        if unreachable:
            self._displayed_next_hop = None
            self._displayed_route_cost = float("inf")
            self._last_change_ts = time.time()
            direction = None
            state = "shelter_in_place"
            color = "red"
        elif at_exit:
            self._displayed_next_hop = None
            self._displayed_route_cost = 0.0
            direction = None
            state = "normal"
            color = self._color_for_cost(own_cost, flame=self._own_flame)
        else:
            candidate_hop = next_hop[self.zone_id]
            candidate_cost = dist_to_exit[self.zone_id]
            chosen_hop = self._apply_hysteresis(
                candidate_hop, candidate_cost, all_costs, dist_to_exit
            )
            direction = direction_between(self.graph, self.zone_id, chosen_hop)
            state = "normal"
            color = self._color_for_cost(own_cost, flame=self._own_flame)

        self._publish_led(color, direction, state, src_ts)

    def _apply_hysteresis(self, candidate_hop, candidate_cost, all_costs, dist_to_exit):
        now = time.time()

        # First computation, or leaving shelter-in-place: nothing displayed yet.
        if self._displayed_next_hop is None or self._displayed_route_cost is None:
            self._displayed_next_hop = candidate_hop
            self._displayed_route_cost = candidate_cost
            self._last_change_ts = now
            return candidate_hop

        # Flame at own zone is safety-critical: bypass damping entirely.
        if self._own_flame:
            if candidate_hop != self._displayed_next_hop:
                self._displayed_next_hop = candidate_hop
                self._displayed_route_cost = candidate_cost
                self._last_change_ts = now
            return candidate_hop

        if candidate_hop == self._displayed_next_hop:
            self._displayed_route_cost = candidate_cost
            return self._displayed_next_hop

        # Cost of staying on the currently displayed route, recomputed against
        # the latest hazard vector (not the stale cost captured when we last
        # switched). If the current hop is now a hard block, its cost is
        # infinite: that is safety-critical and bypasses damping just like
        # flame at the node's own zone.
        current_hop = self._displayed_next_hop
        own_cost = all_costs[self.zone_id]
        hop_cost = all_costs.get(current_hop, float("inf"))
        if own_cost >= BLOCKED_COST_THRESHOLD or hop_cost >= BLOCKED_COST_THRESHOLD:
            current_route_cost = float("inf")
        else:
            current_route_cost = edge_weight(own_cost, hop_cost) + dist_to_exit.get(
                current_hop, float("inf")
            )

        if current_route_cost == float("inf"):
            self._displayed_next_hop = candidate_hop
            self._displayed_route_cost = candidate_cost
            self._last_change_ts = now
            return candidate_hop

        dwell_elapsed_ms = (now - self._last_change_ts) * 1000.0
        cheap_enough = candidate_cost < current_route_cost * (1 - HYSTERESIS_MARGIN)

        if cheap_enough and dwell_elapsed_ms >= MIN_DWELL_MS:
            self._displayed_next_hop = candidate_hop
            self._displayed_route_cost = candidate_cost
            self._last_change_ts = now
            return candidate_hop

        self._displayed_route_cost = current_route_cost
        return current_hop

    @staticmethod
    def _color_for_cost(cost, flame):
        if flame or cost > RED_COST_THRESHOLD:
            return "red"
        if cost > YELLOW_COST_THRESHOLD:
            return "yellow"
        return "green"

    # -- Publishing ----------------------------------------------------

    def _publish_hazard(self, cost, ts):
        payload = json.dumps({"cost": cost, "ts": ts})
        self._client.publish(
            TOPIC_HAZARD.format(zone_id=self.zone_id), payload, qos=1, retain=True
        )

    def _publish_led(self, color, direction, state, src_ts):
        payload = json.dumps(
            {
                "color": color,
                "direction": direction,
                "state": state,
                "src_ts": src_ts,
                "ts": time.time(),
            }
        )
        self._client.publish(
            TOPIC_LED.format(zone_id=self.zone_id), payload, qos=1, retain=True
        )

    def _publish_health(self, state):
        payload = json.dumps({"state": state, "ts": time.time()})
        self._client.publish(
            TOPIC_HEALTH.format(zone_id=self.zone_id), payload, qos=0, retain=True
        )

    # -- Fail-safe watchdog ----------------------------------------------------

    def _watchdog_loop(self):
        last_health_publish = 0.0
        while not self._stopping:
            time.sleep(WATCHDOG_INTERVAL_S)
            now = time.time()

            if self._last_sensor_ts is not None:
                silent_for = now - self._last_sensor_ts
                if silent_for > SENSOR_TIMEOUT_S and not self._degraded:
                    self._degraded = True
                    self.log.warning(
                        "no sensor update for %.1fs, falling back to last-known-safe path",
                        silent_for,
                    )
                    self._publish_health("degraded")

            if now - last_health_publish >= HEALTH_PUBLISH_INTERVAL_S:
                self._publish_health("degraded" if self._degraded else "ok")
                last_health_publish = now


def main():
    parser = argparse.ArgumentParser(description="Fire node MCU simulator")
    parser.add_argument("--zone", required=True, help="zone id, e.g. r0c0")
    parser.add_argument("--broker-host", default="localhost")
    parser.add_argument("--broker-port", type=int, default=1883)
    args = parser.parse_args()

    node = FireNode(args.zone, broker_host=args.broker_host, broker_port=args.broker_port)
    node.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()


if __name__ == "__main__":
    main()
