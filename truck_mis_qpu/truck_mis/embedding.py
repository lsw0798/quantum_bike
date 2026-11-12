"""논리 충돌그래프를 원자 좌표로 옮기고 비가중 MIS 목적 보존을 검사한다.

좌표 생성은 입력 인코딩이다. 독립집합 해를 찾거나 노드/엣지를 버리지 않는다.
일반 그래프는 직접 2차원 배치가 불가능할 수 있으며 그런 사례는 실패로 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np


class EmbeddingRejected(ValueError):
    """직접 원자 배치의 충분조건을 만족하지 못한 사례."""


def canonical_edges(count: int, edges: Iterable[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    """노드 인덱스를 검증하고 무방향 엣지의 표현만 정규화한다."""
    result: set[tuple[int, int]] = set()
    for pair in edges:
        if len(pair) != 2:
            raise ValueError("엣지는 두 노드 인덱스로 구성해야 합니다.")
        a, b = pair
        if any(isinstance(v, bool) or not isinstance(v, (int, np.integer)) for v in pair):
            raise ValueError("엣지의 노드 인덱스는 정수여야 합니다.")
        if not (0 <= a < count and 0 <= b < count) or a == b:
            raise ValueError("존재하지 않는 노드 또는 자기 루프가 있습니다.")
        result.add(tuple(sorted((int(a), int(b)))))
    return frozenset(result)


def geometry_certificate(
    node_ids: Sequence[str],
    edges: Iterable[tuple[int, int]],
    coordinates: Sequence[Sequence[float]],
    *,
    c6: float = 1.0,
    final_detuning: float | None = None,
) -> dict[str, Any]:
    """H=-δΣx + Σ(C6/r⁶)xᵢxⱼ의 최종 고전 바닥상태가 MIS임을 인증한다.

    모든 비엣지 상호작용의 합 < δ < 최소 엣지 상호작용이면
    충돌 정점을 제거할 때 에너지가 내려가고, 독립집합의 크기가 커질 때도
    에너지가 내려간다. 유한 진화시간·노이즈에서 최적해 관측을 보장하지 않는다.
    """
    count = len(node_ids)
    if count < 1 or len(set(node_ids)) != count:
        raise ValueError("노드 ID는 비어 있지 않고 유일해야 합니다.")
    edge_set = canonical_edges(count, edges)
    coords = np.asarray(coordinates, dtype=float)
    if coords.shape != (count, 2) or not np.isfinite(coords).all():
        raise ValueError("원자 좌표는 유한한 N×2 배열이어야 합니다.")
    if not math.isfinite(c6) or c6 <= 0:
        raise ValueError("C6는 양의 유한한 값이어야 합니다.")
    interactions: dict[tuple[int, int], float] = {}
    for a in range(count):
        for b in range(a + 1, count):
            distance = float(np.linalg.norm(coords[a] - coords[b]))
            if distance <= 1e-12:
                raise EmbeddingRejected("서로 다른 원자 좌표가 겹칩니다.")
            value = c6 / distance**6
            if not math.isfinite(value):
                raise EmbeddingRejected("원자 사이 상호작용이 유한하지 않습니다.")
            interactions[a, b] = value
    edge_values = [v for p, v in interactions.items() if p in edge_set]
    other_values = [v for p, v in interactions.items() if p not in edge_set]
    tail = math.fsum(other_values)
    edge_min = min(edge_values) if edge_values else None
    if edge_min is not None and tail >= edge_min:
        raise EmbeddingRejected(
            "비엣지 상호작용 합이 최소 엣지 상호작용 이상입니다. "
            "원래 MIS 목적을 보존하는 직접 배치를 인증하지 못했습니다."
        )
    delta = final_detuning
    if delta is None:
        delta = (tail + edge_min) / 2 if edge_min is not None else tail + 1.0
    if not math.isfinite(delta) or not tail < delta:
        raise EmbeddingRejected("최종 detuning이 비엣지 상호작용 합보다 커야 합니다.")
    if edge_min is not None and not delta < edge_min:
        raise EmbeddingRejected("최종 detuning이 최소 엣지 상호작용보다 작아야 합니다.")
    represented = {pair for pair, value in interactions.items() if value > delta}
    if represented != set(edge_set):
        raise EmbeddingRejected("원자 상호작용의 엣지가 원래 충돌그래프와 다릅니다.")
    return {
        "native_adjacency_exact": True,
        "sufficient_final_energy_certificate": True,
        "logical_nodes": count,
        "physical_atoms": count,
        "auxiliary_atoms": 0,
        "minimum_edge_interaction": edge_min,
        "maximum_nonedge_interaction": max(other_values, default=0.0),
        "total_nonedge_interaction_bound": tail,
        "final_detuning": delta,
        "c6": c6,
        "interpretation": "ideal final Hamiltonian only; no finite-time/noise guarantee",
    }


def _structured_coordinates(count: int, edges: frozenset[tuple[int, int]]) -> np.ndarray | None:
    """클릭·경로·사이클 연결요소에는 결정적인 기하 배치를 사용한다."""
    neighbors = {i: set() for i in range(count)}
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    unseen = set(range(count))
    pieces: list[tuple[list[int], np.ndarray]] = []
    while unseen:
        component: set[int] = set()
        queue = [min(unseen)]
        while queue:
            v = queue.pop()
            if v in component:
                continue
            component.add(v)
            queue.extend(sorted(neighbors[v] - component, reverse=True))
        unseen -= component
        ids = sorted(component)
        n = len(ids)
        if n == 1:
            local = np.zeros((1, 2))
        elif all(len(neighbors[v]) == n - 1 for v in ids):
            # 지름이 1인 원 위의 클릭: 모든 쌍이 엣지다.
            angles = np.arange(n) * (2 * math.pi / n)
            local = 0.5 * np.column_stack((np.cos(angles), np.sin(angles)))
        elif all(len(neighbors[v]) <= 2 for v in ids):
            ends = [v for v in ids if len(neighbors[v]) == 1]
            cycle = not ends
            if not cycle and len(ends) != 2:
                return None
            order, last = [], None
            current = ids[0] if cycle else min(ends)
            while current not in order:
                order.append(current)
                next_nodes = sorted(neighbors[current] - ({last} if last is not None else set()))
                if not next_nodes:
                    break
                last, current = current, next_nodes[0]
            if len(order) != n:
                return None
            ids = order
            if cycle:
                radius = 1.0 / (2 * math.sin(math.pi / n))
                angles = np.arange(n) * (2 * math.pi / n)
                local = radius * np.column_stack((np.cos(angles), np.sin(angles)))
            else:
                local = np.column_stack((np.arange(n, dtype=float), np.zeros(n)))
        else:
            # 동일한 닫힌 이웃집합을 갖는 정점은 같은 클릭의 true twin이다.
            # 배치의 중심만 함께 계산한 뒤 모든 원자를 다시 펼친다.
            # 논리 정점/엣지를 합치거나 축약된 문제를 QPU에 보내지 않는다.
            twins: dict[frozenset[int], list[int]] = {}
            for v in ids:
                twins.setdefault(frozenset(neighbors[v] | {v}), []).append(v)
            groups = list(twins.values())
            if len(groups) == n:
                return None
            quotient_edges = frozenset(
                (a, b) for a in range(len(groups)) for b in range(a + 1, len(groups))
                if groups[b][0] in neighbors[groups[a][0]]
            )
            centers = _structured_coordinates(len(groups), quotient_edges)
            if centers is None:
                return None
            local = np.zeros((n, 2))
            positions = {node: k for k, node in enumerate(ids)}
            for g, vertices in enumerate(groups):
                for k, vertex in enumerate(vertices):
                    angle = 2 * math.pi * k / len(vertices)
                    offset = np.array([math.cos(angle), math.sin(angle)]) * 0.3
                    local[positions[vertex]] = centers[g] + (offset if len(vertices) > 1 else 0)
        pieces.append((ids, local - local.mean(axis=0)))
    # 연결요소 사이 거리를 충분히 벌린 뒤 전체 비엣지 꼬리를 다시 검증한다.
    max_width = max(float(np.ptp(p, axis=0).max()) for _, p in pieces)
    spacing = max_width + 2.0
    columns = math.ceil(math.sqrt(len(pieces)))
    coords = np.zeros((count, 2))
    for k, (ids, local) in enumerate(pieces):
        coords[ids] = local + np.array([(k % columns) * spacing, (k // columns) * spacing])
    return coords - coords.mean(axis=0)


@dataclass
class EmbeddedGraph:
    node_ids: tuple[str, ...]
    edges: frozenset[tuple[int, int]]
    coordinates: np.ndarray
    certificate: dict[str, Any]


def embed_graph(node_ids: list[str], edges: list[tuple[int, int]], config: dict) -> EmbeddedGraph:
    """원본 그래프 전체를 한 번에 배치한다. 실패 시 작은 부분문제로 바꾸지 않는다."""
    ids = tuple(node_ids)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("서로 다른 노드 ID가 한 개 이상 필요합니다.")
    edge_set = canonical_edges(len(ids), edges)
    max_nodes = int(config.get("max_nodes", 20))
    if len(ids) > max_nodes:
        raise EmbeddingRejected(f"직접 배치 탐색 한도 {max_nodes}개를 초과했습니다.")
    coordinate_file = config.get("coordinates_path")
    if coordinate_file:
        coordinates = json.loads(Path(coordinate_file).read_text(encoding="utf-8"))["coordinates"]
        if set(coordinates) != set(ids):
            raise ValueError("외부 좌표의 노드 ID가 전체 후보와 정확히 같아야 합니다.")
        coords = np.asarray([coordinates[node] for node in ids], dtype=float)
        certificate = geometry_certificate(ids, edge_set, coords)
        certificate["method"] = "user_coordinates"
        return EmbeddedGraph(ids, edge_set, coords, certificate)
    structured = _structured_coordinates(len(ids), edge_set)
    if structured is not None:
        try:
            certificate = geometry_certificate(ids, edge_set, structured)
            certificate["method"] = "clique_path_cycle_components"
            return EmbeddedGraph(ids, edge_set, structured, certificate)
        except EmbeddingRejected:
            pass
    # 수치 최적화는 원자 위치 인코딩에만 사용하며 MIS 해를 계산하지 않는다.
    from qoolqit.embedding import InteractionEmbedder, InteractionEmbedderConfig

    attempts = int(config.get("attempts", 3))
    maxiter = int(config.get("maxiter", 3000))
    seed = int(config.get("seed", 42))
    if attempts < 1 or maxiter < 1:
        raise ValueError("배치 시도 횟수와 최대 반복 수는 양수여야 합니다.")
    target = np.zeros((len(ids), len(ids)))
    for a, b in edge_set:
        target[a, b] = target[b, a] = 1.0
    for attempt in range(attempts):
        embedder = InteractionEmbedder(InteractionEmbedderConfig(seed=seed + attempt, maxiter=maxiter))
        fitted = embedder.embed(target)
        coords = np.asarray([fitted.coords[i] for i in range(len(ids))])
        try:
            certificate = geometry_certificate(ids, edge_set, coords)
        except EmbeddingRejected:
            continue
        certificate.update(method="interaction_embedding", attempt=attempt + 1, seed=seed + attempt)
        return EmbeddedGraph(ids, edge_set, coords, certificate)
    raise EmbeddingRejected(
        f"고정된 {attempts}회 배치 시도로 충분조건을 통과하지 못했습니다. "
        "이는 일반 그래프의 직접 배치 한계이며 QPU 결과나 최적해가 아닙니다."
    )
