"""Runs the routing coordinator plus all 36 zone nodes in a single process,
for a one-command demo.

docs/PROJECT_SPEC.md describes fire_node.py as "one Python process per
zone, simulates an MCU" — that's still true here: each zone gets its own
FireNode instance with its own MQTT client (own client_id, own connection,
own subscriptions/publishes on its own topics) and its own watchdog thread.
The single global Dijkstra solve, however, runs once in one
RoutingCoordinator instance (see routing_coordinator.py) rather than being
redone independently by each of the 36 nodes. This script hosts the
coordinator and all 36 FireNode instances as threads in one process, so
the whole demo starts with one command.

Run:
    python run_all_nodes.py
"""

import argparse
import logging
import sys
import threading
import time

import graph_config
from fire_node import FireNode
from routing_coordinator import RoutingCoordinator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("run_all_nodes")
diag_log = logging.getLogger("diagnostic")

STAGGER_S = 0.02

# TEMPORARY DIAGNOSTIC — investigating a latency-climbs-over-time leak.
# Remove this whole block (and the counters it reads in fire_node.py /
# routing_coordinator.py) once the root cause is confirmed and fixed.
DIAG_INTERVAL_S = 10.0


def _diagnostic_loop(coordinator, nodes, stop_event):
    prev_led_counts = {n.zone_id: 0 for n in nodes}
    prev_recompute_count = 0

    while not stop_event.is_set():
        stop_event.wait(DIAG_INTERVAL_S)
        if stop_event.is_set():
            break

        thread_count = threading.active_count()

        # paho-mqtt tracks unacknowledged QoS>0 publishes in _out_messages
        # (an unbounded OrderedDict — _max_queued_messages defaults to 0,
        # i.e. no cap). If publish rate outpaces ack throughput, this grows
        # every interval instead of settling to a steady size.
        out_messages_total = sum(len(n._client._out_messages) for n in nodes)
        out_messages_total += len(coordinator._client._out_messages)
        out_packet_total = sum(len(n._client._out_packet) for n in nodes)
        out_packet_total += len(coordinator._client._out_packet)

        led_counts = {n.zone_id: n._led_publish_count for n in nodes}
        total_led_publishes = sum(led_counts.values())
        led_delta = total_led_publishes - sum(prev_led_counts.values())
        prev_led_counts = led_counts

        recompute_delta = coordinator._recompute_count - prev_recompute_count
        prev_recompute_count = coordinator._recompute_count

        diag_log.info(
            "threads=%d out_messages=%d out_packet=%d "
            "recomputes/%ds=%d led_publishes/%ds=%d (led:recompute ratio=%.1f)",
            thread_count,
            out_messages_total,
            out_packet_total,
            int(DIAG_INTERVAL_S),
            recompute_delta,
            int(DIAG_INTERVAL_S),
            led_delta,
            (led_delta / recompute_delta) if recompute_delta else 0.0,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Run the routing coordinator and all zone fire nodes in one process"
    )
    parser.add_argument("--broker-host", default="localhost")
    parser.add_argument("--broker-port", type=int, default=1883)
    args = parser.parse_args()

    zone_ids = list(graph_config.build_graph().nodes)
    coordinator = RoutingCoordinator(broker_host=args.broker_host, broker_port=args.broker_port)
    nodes = [
        FireNode(zone_id, broker_host=args.broker_host, broker_port=args.broker_port)
        for zone_id in zone_ids
    ]

    log.info(
        "starting routing coordinator + %d fire nodes against %s:%s",
        len(nodes),
        args.broker_host,
        args.broker_port,
    )

    started = []
    try:
        # Coordinator first: its routing broadcast should exist (retained)
        # before nodes come online and subscribe to it.
        coordinator.start()
        for node in nodes:
            node.start()
            started.append(node)
            time.sleep(STAGGER_S)
    except Exception:
        log.exception(
            "failed to start (is the Mosquitto broker running at %s:%s?) "
            "— stopping the %d node(s) that did start",
            args.broker_host,
            args.broker_port,
            len(started),
        )
        for node in started:
            node.stop()
        coordinator.stop()
        sys.exit(1)

    log.info(
        "routing coordinator + all %d fire nodes running — press Ctrl+C to stop", len(nodes)
    )

    diag_stop_event = threading.Event()
    diag_thread = threading.Thread(
        target=_diagnostic_loop, args=(coordinator, nodes, diag_stop_event), daemon=True
    )
    diag_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        diag_stop_event.set()
        log.info("stopping all fire nodes and the routing coordinator")
        for node in nodes:
            node.stop()
        coordinator.stop()


if __name__ == "__main__":
    main()
