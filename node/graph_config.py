"""Building graph for the fire evacuation router.

6x6 grid of zones. Zone ids are "r{row}c{col}", 0-indexed. Two exits sit at
opposite corners. Edge weight defaults to 1 and is later overwritten at
runtime from live hazard costs per docs/PROJECT_SPEC.md
(w(u, v) = (zone_cost(u) + zone_cost(v)) / 2).
"""

import json

import networkx as nx

GRID_ROWS = 6
GRID_COLS = 6

EXIT_ZONES = ["r0c0", "r5c5"]

DEFAULT_EDGE_WEIGHT = 1


def zone_id(row, col):
    return f"r{row}c{col}"


def build_graph():
    """Build the 6x6 zone grid graph with default edge weights."""
    graph = nx.Graph()

    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            graph.add_node(
                zone_id(row, col),
                row=row,
                col=col,
                is_exit=zone_id(row, col) in EXIT_ZONES,
            )

    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            here = zone_id(row, col)
            if col + 1 < GRID_COLS:
                graph.add_edge(here, zone_id(row, col + 1), weight=DEFAULT_EDGE_WEIGHT)
            if row + 1 < GRID_ROWS:
                graph.add_edge(here, zone_id(row + 1, col), weight=DEFAULT_EDGE_WEIGHT)

    return graph


def graph_to_json(graph):
    """Export the graph as a JSON-serializable dict for the dashboard.

    Shape:
        {
          "nodes": [{"id": str, "row": int, "col": int, "is_exit": bool}, ...],
          "edges": [{"source": str, "target": str, "weight": float}, ...],
          "exits": [str, ...]
        }
    """
    nodes = [
        {
            "id": node,
            "row": data["row"],
            "col": data["col"],
            "is_exit": data["is_exit"],
        }
        for node, data in graph.nodes(data=True)
    ]

    edges = [
        {"source": u, "target": v, "weight": data.get("weight", DEFAULT_EDGE_WEIGHT)}
        for u, v, data in graph.edges(data=True)
    ]

    return {"nodes": nodes, "edges": edges, "exits": EXIT_ZONES}


def export_graph_json(path=None):
    """Build the graph and export it as JSON, returning the JSON string.

    If `path` is given, also writes the JSON to that file.
    """
    payload = graph_to_json(build_graph())
    text = json.dumps(payload, indent=2)

    if path is not None:
        with open(path, "w") as f:
            f.write(text)

    return text


if __name__ == "__main__":
    print(export_graph_json())
