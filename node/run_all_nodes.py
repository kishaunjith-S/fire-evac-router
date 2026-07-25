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
import time

import graph_config
from fire_node import FireNode
from routing_coordinator import RoutingCoordinator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("run_all_nodes")

STAGGER_S = 0.02


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

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stopping all fire nodes and the routing coordinator")
        for node in nodes:
            node.stop()
        coordinator.stop()


if __name__ == "__main__":
    main()
