"""Fire Node — simulates one zone's MCU.

One process per zone (per docs/PROJECT_SPEC.md). Each instance:
  - subscribes to its own zone's raw sensor topic and runs the fusion formula
  - publishes its own hazard cost to fire/nodes/{zone_id}/hazard
  - subscribes to fire/system/routing, the routing_coordinator.py broadcast
    of the single global Dijkstra solve (see routing_coordinator.py for why
    this is centralized rather than each of the 36 nodes solving the whole
    graph independently)
  - applies local hysteresis + LED color logic and publishes its own LED state
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

import paho.mqtt.client as mqtt

import graph_config
import routing_common as rc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

HYSTERESIS_MARGIN = 0.05
MIN_DWELL_MS = 500

# --- Fail-safe constants ---
SENSOR_TIMEOUT_S = 5.0
HEALTH_PUBLISH_INTERVAL_S = 2.0
WATCHDOG_INTERVAL_S = 1.0

TOPIC_SENSOR = "fire/sensors/{zone_id}"
TOPIC_HAZARD = "fire/nodes/{zone_id}/hazard"
TOPIC_ROUTING = "fire/system/routing"
TOPIC_LED = "fire/nodes/{zone_id}/led"
TOPIC_HEALTH = "fire/system/health/{zone_id}"


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

        # Own zone's last fused cost/flame, from its own sensor readings —
        # authoritative locally, never sourced from the coordinator's
        # (one-hop-stale) broadcast copy.
        self._own_cost = rc.BASELINE_COST
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

    def _on_connect(self, client, userdata, flags, rc_code):
        self.log.info("connected to broker (rc=%s)", rc_code)
        client.subscribe(TOPIC_SENSOR.format(zone_id=self.zone_id), qos=1)
        client.subscribe(TOPIC_ROUTING, qos=1)
        self._publish_health("ok")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.log.warning("discarding malformed payload on %s", msg.topic)
            return

        if msg.topic == TOPIC_SENSOR.format(zone_id=self.zone_id):
            self._handle_sensor_reading(payload)
        elif msg.topic == TOPIC_ROUTING:
            self._handle_routing_update(payload)

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
        self._own_cost = rc.zone_cost(temp, smoke, flame)

        self._publish_hazard(self._own_cost, payload.get("ts", time.time()))
        # The LED update itself is driven by the coordinator's routing
        # broadcast (_handle_routing_update), which arrives once it has
        # processed this hazard publish — not computed locally here.

    def _handle_routing_update(self, payload):
        try:
            hazard_costs = payload["hazard_costs"]
            dist_to_exit = payload["dist_to_exit"]
            next_hop_map = payload["next_hop"]
            src_ts = payload["src_ts"]
        except (KeyError, TypeError):
            self.log.warning("discarding malformed routing payload: %r", payload)
            return

        self._apply_routing(hazard_costs, dist_to_exit, next_hop_map, src_ts)

    # -- Routing (local: hysteresis + color only, no graph solve) ----------------------------------------------------

    def _apply_routing(self, hazard_costs, dist_to_exit, next_hop_map, src_ts):
        own_dist = dist_to_exit.get(self.zone_id)
        unreachable = own_dist is None
        at_exit = self.is_exit

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
            color = rc.color_for_cost(self._own_cost, flame=self._own_flame)
        else:
            candidate_hop = next_hop_map.get(self.zone_id)
            candidate_cost = own_dist
            chosen_hop = self._apply_hysteresis(
                candidate_hop, candidate_cost, hazard_costs, dist_to_exit
            )
            direction = rc.direction_between(self.graph, self.zone_id, chosen_hop)
            state = "normal"
            color = rc.color_for_cost(self._own_cost, flame=self._own_flame)

        self._publish_led(color, direction, state, src_ts)

    def _apply_hysteresis(self, candidate_hop, candidate_cost, neighbor_costs, dist_to_exit):
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
        # the latest broadcast (not the stale cost captured when we last
        # switched). If the current hop is now a hard block, its cost is
        # infinite: that is safety-critical and bypasses damping just like
        # flame at the node's own zone.
        current_hop = self._displayed_next_hop
        hop_cost = neighbor_costs.get(current_hop, float("inf"))
        if self._own_cost >= rc.BLOCKED_COST_THRESHOLD or hop_cost >= rc.BLOCKED_COST_THRESHOLD:
            current_route_cost = float("inf")
        else:
            hop_dist = dist_to_exit.get(current_hop)
            hop_dist = float("inf") if hop_dist is None else hop_dist
            current_route_cost = rc.edge_weight(self._own_cost, hop_cost) + hop_dist

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
