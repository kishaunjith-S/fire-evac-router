"""Routing coordinator — the single elected node that solves the global
multi-source Dijkstra once per hazard update and broadcasts the full
routing field.

docs/PROJECT_SPEC.md calls for ONE reverse multi-source Dijkstra from the
exits producing the whole LED map in one pass — not 36 independent zone
processes each redoing the full graph solve on every hazard message from
every other zone. That fan-out (every FireNode subscribing to
fire/nodes/+/hazard and recomputing on each delivery) was O(zones^2) per
ambient tick and, run as threads sharing one GIL, was the actual cause of
multi-second recompute latency. Centralizing the solve here makes it
O(zones): exactly one recompute per incoming hazard message, regardless of
how many FireNode processes are running.

Publishes the full field to fire/system/routing (retained). Every
FireNode subscribes to that single topic instead of the raw hazard topics,
and does O(1) local work (hysteresis + color + own LED publish) on receipt.

Run:
    python routing_coordinator.py
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

TOPIC_HAZARD_WILDCARD = "fire/nodes/+/hazard"
TOPIC_ROUTING = "fire/system/routing"
TOPIC_HEALTH = "fire/system/health/coordinator"

# Safety net: cap paho-mqtt's outgoing queue so a future republish-volume
# bug degrades (drops messages once the queue is full) instead of growing
# this client's outgoing queue without bound.
MAX_QUEUED_MESSAGES = 100


class RoutingCoordinator:
    def __init__(self, broker_host="localhost", broker_port=1883):
        self.broker_host = broker_host
        self.broker_port = broker_port

        self.graph = graph_config.build_graph()
        self.exit_zones = graph_config.EXIT_ZONES

        self.log = logging.getLogger("routing_coordinator")
        self._lock = threading.Lock()

        # Global hazard vector — this is the ONLY place it needs to live now.
        self._hazard_costs = {z: rc.BASELINE_COST for z in self.graph.nodes}

        # TEMPORARY DIAGNOSTIC — remove once the republish-volume leak is fixed.
        self._recompute_count = 0

        self._client = mqtt.Client(client_id="routing_coordinator")
        self._client.max_queued_messages_set(MAX_QUEUED_MESSAGES)
        will_payload = json.dumps({"state": "offline", "ts": time.time()})
        self._client.will_set(TOPIC_HEALTH, will_payload, qos=0, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # -- MQTT lifecycle ----------------------------------------------------

    def start(self):
        self._client.connect(self.broker_host, self.broker_port)
        self._client.loop_start()

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc_code):
        self.log.info("connected to broker (rc=%s)", rc_code)
        client.subscribe(TOPIC_HAZARD_WILDCARD, qos=1)
        self._publish_health("ok")
        # Publish an initial all-baseline routing map immediately, so nodes
        # get a valid LED state without waiting for the first hazard event.
        self._recompute_and_publish(src_ts=time.time())

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.log.warning("discarding malformed payload on %s", msg.topic)
            return

        zone_id = msg.topic.split("/")[2]
        if zone_id not in self._hazard_costs:
            return
        try:
            cost = float(payload["cost"])
        except (KeyError, TypeError, ValueError):
            self.log.warning("discarding malformed hazard payload: %r", payload)
            return

        with self._lock:
            self._hazard_costs[zone_id] = cost

        self._recompute_and_publish(src_ts=payload.get("ts", time.time()))

    # -- Recompute + broadcast ----------------------------------------------------

    def _recompute_and_publish(self, src_ts):
        self._recompute_count += 1  # TEMPORARY DIAGNOSTIC
        with self._lock:
            hazard_costs = dict(self._hazard_costs)

        dist_to_exit, next_hop = rc.compute_routing(self.graph, hazard_costs, self.exit_zones)

        payload = json.dumps(
            {
                "hazard_costs": hazard_costs,
                # JSON has no Infinity; null means "no route to any exit".
                "dist_to_exit": {
                    z: (d if d != float("inf") else None) for z, d in dist_to_exit.items()
                },
                "next_hop": next_hop,
                "src_ts": src_ts,
                "ts": time.time(),
            }
        )
        self._client.publish(TOPIC_ROUTING, payload, qos=1, retain=True)

    def _publish_health(self, state):
        payload = json.dumps({"state": state, "ts": time.time()})
        self._client.publish(TOPIC_HEALTH, payload, qos=0, retain=True)


def main():
    parser = argparse.ArgumentParser(description="Global routing coordinator")
    parser.add_argument("--broker-host", default="localhost")
    parser.add_argument("--broker-port", type=int, default=1883)
    args = parser.parse_args()

    coordinator = RoutingCoordinator(broker_host=args.broker_host, broker_port=args.broker_port)
    coordinator.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        coordinator.stop()


if __name__ == "__main__":
    main()
