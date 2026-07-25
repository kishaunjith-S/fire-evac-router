"""Shared, stateless routing helpers used by both fire_node.py (per-zone
fusion + local hysteresis) and routing_coordinator.py (the single global
Dijkstra solve). Keeping the formulas here means both processes agree on
one definition instead of maintaining drifting copies.
"""

import networkx as nx

# --- Sensor fusion constants (docs/PROJECT_SPEC.md: Sensor fusion formula) ---
ALPHA = 0.05
BETA = 0.002
AMBIENT_C = 22.0
FLAME_PENALTY = 1000
BASELINE_COST = 1.0

# Color thresholds on zone_cost. Tunable, same spirit as alpha/beta/FLAME_PENALTY.
YELLOW_COST_THRESHOLD = 5.0
RED_COST_THRESHOLD = 50.0

EXIT_SINK = "__EXIT__"

# A zone at or above this cost is a hard block, not just expensive: edges
# touching it are removed from the routing graph entirely. Without this, an
# isolated zone never actually reaches float("inf") — FLAME_PENALTY is only
# a very large weight, and a route through it, however absurd, always exists
# on an unchanged topology. Removing the edge is what makes shelter-in-place
# ("no path to any exit") a real, reachable state instead of a dead branch.
BLOCKED_COST_THRESHOLD = FLAME_PENALTY


def zone_cost(temp, smoke, flame):
    """Fusion formula: raw sensor readings -> per-zone hazard cost."""
    return (
        BASELINE_COST
        + ALPHA * max(0.0, temp - AMBIENT_C)
        + BETA * (smoke ** 1.5)
        + (FLAME_PENALTY if flame else 0.0)
    )


def edge_weight(cost_u, cost_v):
    """Zone cost -> edge weight: average of both endpoints."""
    return (cost_u + cost_v) / 2.0


def color_for_cost(cost, flame):
    if flame or cost > RED_COST_THRESHOLD:
        return "red"
    if cost > YELLOW_COST_THRESHOLD:
        return "yellow"
    return "green"


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


def build_weighted_graph_with_sink(base_graph, hazard_costs, exit_zones):
    """base_graph's edges plus a virtual sink wired to the exits, with
    hard-blocked edges removed (see BLOCKED_COST_THRESHOLD)."""
    g = nx.Graph()
    g.add_nodes_from(base_graph.nodes)
    for u, v in base_graph.edges:
        if hazard_costs[u] >= BLOCKED_COST_THRESHOLD or hazard_costs[v] >= BLOCKED_COST_THRESHOLD:
            continue  # hard block: edge removed, not just expensive
        g.add_edge(u, v, weight=edge_weight(hazard_costs[u], hazard_costs[v]))

    g.add_node(EXIT_SINK)
    for exit_zone in exit_zones:
        g.add_edge(EXIT_SINK, exit_zone, weight=0.0)

    return g


def compute_routing(base_graph, hazard_costs, exit_zones):
    """Single reverse multi-source Dijkstra from the exits.

    Returns (dist_to_exit, next_hop) dicts covering every real zone in
    base_graph. dist_to_exit[z] is float("inf") and next_hop[z] is None for
    zones with no route to any exit (shelter-in-place). next_hop[z] is also
    None for exit zones themselves (already arrived).
    """
    g = build_weighted_graph_with_sink(base_graph, hazard_costs, exit_zones)
    lengths, paths = nx.single_source_dijkstra(g, source=EXIT_SINK)

    dist_to_exit = {}
    next_hop = {}
    for z in base_graph.nodes:
        if z not in lengths:
            dist_to_exit[z] = float("inf")
            next_hop[z] = None
            continue
        dist_to_exit[z] = lengths[z]
        path = paths[z]  # [EXIT_SINK, ..., z]
        next_hop[z] = path[-2] if len(path) > 1 and path[-2] != EXIT_SINK else None

    return dist_to_exit, next_hop
