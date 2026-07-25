"""Injector — Flask control panel that simulates fire sensors.

Publishes raw sensor payloads to fire/sensors/{zone_id} (see
docs/PROJECT_SPEC.md: MQTT topic design). Also subscribes to the nodes'
hazard/LED/health topics itself so the browser panel can render live state
over Server-Sent Events, without requiring an MQTT-over-websocket bridge.

Run:
    python app.py
Requires a running Mosquitto broker and one or more node/fire_node.py
processes to see hazard/LED/health data update.
"""

import json
import os
import queue
import random
import sys
import threading
import time

from flask import Flask, Response, jsonify, render_template, request
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "node"))
import graph_config  # noqa: E402

MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))

TOPIC_SENSOR = "fire/sensors/{zone_id}"
TOPIC_HAZARD_WILDCARD = "fire/nodes/+/hazard"
TOPIC_LED_WILDCARD = "fire/nodes/+/led"
TOPIC_HEALTH_WILDCARD = "fire/system/health/+"

AMBIENT_TICK_S = 1.0
AMBIENT_TEMP_RANGE = (20.0, 24.0)
AMBIENT_SMOKE_RANGE = (0.0, 5.0)
FLASHOVER_TEMP_RANGE = (550.0, 650.0)
FLASHOVER_SMOKE_RANGE = (2500.0, 3500.0)

app = Flask(__name__)

_graph = graph_config.build_graph()
_zone_ids = list(_graph.nodes)

_state_lock = threading.Lock()
_zone_state = {
    z: {
        "zone_id": z,
        "cost": None,
        "color": None,
        "direction": None,
        "state": None,
        "health": "unknown",
        "last_sensor": None,
    }
    for z in _zone_ids
}
_active_flashovers = set()

_subscribers = []  # list of queue.Queue, one per connected SSE client
_subscribers_lock = threading.Lock()


def _broadcast(event):
    payload = json.dumps(event)
    with _subscribers_lock:
        for q in _subscribers:
            q.put(payload)


def _on_connect(client, userdata, flags, rc):
    client.subscribe(TOPIC_HAZARD_WILDCARD, qos=1)
    client.subscribe(TOPIC_LED_WILDCARD, qos=1)
    client.subscribe(TOPIC_HEALTH_WILDCARD, qos=0)


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return

    parts = msg.topic.split("/")
    event = None

    if msg.topic.startswith("fire/nodes/") and msg.topic.endswith("/hazard"):
        zone_id = parts[2]
        if zone_id not in _zone_state:
            return
        with _state_lock:
            _zone_state[zone_id]["cost"] = payload.get("cost")
        event = {"type": "hazard", "zone_id": zone_id, **payload}

    elif msg.topic.startswith("fire/nodes/") and msg.topic.endswith("/led"):
        zone_id = parts[2]
        if zone_id not in _zone_state:
            return
        with _state_lock:
            _zone_state[zone_id]["color"] = payload.get("color")
            _zone_state[zone_id]["direction"] = payload.get("direction")
            _zone_state[zone_id]["state"] = payload.get("state")
        event = {"type": "led", "zone_id": zone_id, **payload}

    elif msg.topic.startswith("fire/system/health/"):
        zone_id = parts[3]
        if zone_id not in _zone_state:
            return
        with _state_lock:
            _zone_state[zone_id]["health"] = payload.get("state")
        event = {"type": "health", "zone_id": zone_id, **payload}

    if event is not None:
        _broadcast(event)


_client = mqtt.Client(client_id="injector_app")
_client.on_connect = _on_connect
_client.on_message = _on_message


def _publish_sensor_reading(zone_id, temp, smoke, flame):
    ts = time.time()
    payload = json.dumps({"temp": temp, "smoke": smoke, "flame": flame, "ts": ts})
    _client.publish(TOPIC_SENSOR.format(zone_id=zone_id), payload, qos=1)
    with _state_lock:
        _zone_state[zone_id]["last_sensor"] = {
            "temp": temp,
            "smoke": smoke,
            "flame": flame,
            "ts": ts,
        }
    _broadcast(
        {
            "type": "sensor",
            "zone_id": zone_id,
            "temp": temp,
            "smoke": smoke,
            "flame": flame,
            "ts": ts,
        }
    )


def _ambient_reading():
    return (
        random.uniform(*AMBIENT_TEMP_RANGE),
        random.uniform(*AMBIENT_SMOKE_RANGE),
        False,
    )


def _flashover_reading():
    return (
        random.uniform(*FLASHOVER_TEMP_RANGE),
        random.uniform(*FLASHOVER_SMOKE_RANGE),
        True,
    )


def _ambient_ticker():
    while True:
        time.sleep(AMBIENT_TICK_S)
        with _state_lock:
            flashing = set(_active_flashovers)
        for zone_id in _zone_ids:
            if zone_id in flashing:
                temp, smoke, flame = _flashover_reading()
            else:
                temp, smoke, flame = _ambient_reading()
            _publish_sensor_reading(zone_id, temp, smoke, flame)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/graph")
def api_graph():
    return jsonify(graph_config.graph_to_json(_graph))


@app.route("/api/state")
def api_state():
    with _state_lock:
        zones = {z: dict(s) for z, s in _zone_state.items()}
        flashovers = list(_active_flashovers)
    return jsonify({"zones": zones, "active_flashovers": flashovers})


@app.route("/api/trigger", methods=["POST"])
def api_trigger():
    body = request.get_json(silent=True) or {}
    zone_id = body.get("zone_id")
    action = body.get("action")

    if zone_id not in _zone_state:
        return jsonify({"ok": False, "error": "unknown zone_id"}), 400
    if action not in ("flashover", "clear"):
        return jsonify({"ok": False, "error": "action must be flashover or clear"}), 400

    if action == "flashover":
        with _state_lock:
            _active_flashovers.add(zone_id)
        temp, smoke, flame = _flashover_reading()
    else:
        with _state_lock:
            _active_flashovers.discard(zone_id)
        temp, smoke, flame = _ambient_reading()

    _publish_sensor_reading(zone_id, temp, smoke, flame)
    return jsonify({"ok": True})


@app.route("/api/events")
def api_events():
    client_queue = queue.Queue()
    with _subscribers_lock:
        _subscribers.append(client_queue)

    def stream():
        try:
            while True:
                payload = client_queue.get()
                yield f"data: {payload}\n\n"
        finally:
            with _subscribers_lock:
                if client_queue in _subscribers:
                    _subscribers.remove(client_queue)

    return Response(stream(), mimetype="text/event-stream")


def _start_background_threads():
    _client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
    _client.loop_start()
    threading.Thread(target=_ambient_ticker, daemon=True).start()


if __name__ == "__main__":
    _start_background_threads()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
