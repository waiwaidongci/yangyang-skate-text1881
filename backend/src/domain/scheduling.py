"""Assign a shift's unclaimed repair orders to limited station slots.

Scheduling is lexicographic: first route as many orders as possible into
stations, then take the plan with the highest total dispatch score. The plan
is computed as a min-cost flow with integer gains, so the result is exact
and deterministic for a given input order.
"""

from dataclasses import dataclass
from heapq import heappop, heappush

from .settings import STATIONS

SCALE = 1_000_000
ZERO_HIT_REASON = "未命中历史字符片段"
TIE_REASON = "最高两类分数并列"
THRESHOLD_REASON = "未达到自动分站置信线"
CAPACITY_REASON = "工位名额不足，转人工分流"


@dataclass(frozen=True)
class PoolOrder:
    ticket_id: str
    scores: dict[str, float]
    matched_ngrams: int
    gap: float
    threshold: float


@dataclass(frozen=True)
class ScheduleResult:
    assignments: dict[str, tuple[str, float]]
    manual: dict[str, str]


def manual_reason(order: PoolOrder) -> str | None:
    if order.matched_ngrams == 0:
        return ZERO_HIT_REASON
    if order.gap <= 1e-12:
        return TIE_REASON
    return None


def eligible_stations(order: PoolOrder) -> dict[str, float]:
    if manual_reason(order) is not None:
        return {}
    return {label: score for label, score in order.scores.items() if score >= order.threshold}


def _max_gain_assignment(gains: list[dict[int, int]], capacities: list[int]) -> list[int | None]:
    order_count = len(gains)
    station_count = len(capacities)
    sink = 1 + order_count + station_count
    graph: list[list[list]] = [[] for _ in range(sink + 1)]

    def link(frm: int, to: int, cap: int, cost: int) -> None:
        graph[frm].append([to, cap, cost, len(graph[to])])
        graph[to].append([frm, 0, -cost, len(graph[frm]) - 1])

    for index, order_gains in enumerate(gains):
        node = 1 + index
        link(0, node, 1, 0)
        link(node, sink, 1, 0)  # manual lane: every order can stay at the counter
        for station, gain in order_gains.items():
            link(node, 1 + order_count + station, 1, -gain)
    for station, capacity in enumerate(capacities):
        link(1 + order_count + station, sink, max(0, capacity), 0)

    potential = [0] * (sink + 1)
    for station in range(station_count):
        incoming = [-gain for order_gains in gains for key, gain in order_gains.items() if key == station]
        potential[1 + order_count + station] = min(incoming) if incoming else 0
    potential[sink] = min([0] + potential[1 + order_count:sink])

    while True:
        dist: list[int | None] = [None] * (sink + 1)
        dist[0] = 0
        previous: list[tuple[int, list] | None] = [None] * (sink + 1)
        heap = [(0, 0)]
        while heap:
            cost_so_far, node = heappop(heap)
            if cost_so_far > dist[node]:
                continue
            for edge in graph[node]:
                to, capacity, cost, _ = edge
                if capacity <= 0:
                    continue
                candidate = cost_so_far + cost + potential[node] - potential[to]
                if dist[to] is None or candidate < dist[to]:
                    dist[to] = candidate
                    previous[to] = (node, edge)
                    heappush(heap, (candidate, to))
        if previous[sink] is None or dist[sink] + potential[sink] - potential[0] >= 0:
            break
        for node in range(sink + 1):
            if dist[node] is not None:
                potential[node] += dist[node]
        node = sink
        while node != 0:
            frm, edge = previous[node]
            edge[1] -= 1
            graph[node][edge[3]][1] += 1
            node = frm

    assigned: list[int | None] = [None] * order_count
    for index in range(order_count):
        for edge in graph[1 + index]:
            if 1 + order_count <= edge[0] < sink and edge[1] == 0:
                assigned[index] = edge[0] - (1 + order_count)
    return assigned


def schedule(orders: list[PoolOrder], capacities: dict[str, int]) -> ScheduleResult:
    stations = list(STATIONS)
    big = (len(orders) + 1) * SCALE
    gains = []
    manual = {}
    for order in orders:
        reason = manual_reason(order)
        order_gains = {}
        if reason is None:
            for index, label in enumerate(stations):
                score = order.scores.get(label)
                if score is not None and score >= order.threshold:
                    order_gains[index] = big + round(score * SCALE)
            if not order_gains:
                reason = THRESHOLD_REASON
        if reason is not None:
            manual[order.ticket_id] = reason
        gains.append(order_gains)
    chosen = _max_gain_assignment(gains, [capacities.get(label, 0) for label in stations])
    assignments = {}
    for order, station in zip(orders, chosen, strict=True):
        if station is None:
            manual.setdefault(order.ticket_id, CAPACITY_REASON)
        else:
            label = stations[station]
            assignments[order.ticket_id] = (label, order.scores[label])
    return ScheduleResult(assignments=assignments, manual=manual)
