"""疏散通道编排核心算法。

时间语义
--------
* 当前分钟从 0 起按整数推进。
* 一组在第 ``t`` 分钟进入通行分钟数为 ``w`` 的通道，则该组占用此通道的
  分钟区间为左闭右开 ``[t, t+w)``，并在第 ``t+w`` 分钟到达通道另一端。
* 封闭时段同样为左闭右开 ``[a, b)``：只要 ``[t, t+w)`` 与某个 ``[a,b)``
  相交（即 ``t < b 且 a < t+w``），就不允许在第 ``t`` 分钟进入该通道；
  最早可尝试进入的下一分钟为 ``b``。
* 容量按每分钟核算：同一通道同一分钟，所有在途组（已出发固定组 + 本轮
  已排入的待出发组）人数之和不得超过该通道该分钟的有效上限。
* 容量管制：可为通道登记左闭右开的管制时段 ``[a, b)`` 及时段内每分钟
  上限；同一通道的管制时段互不重叠。管制时段内有效上限取
  ``min(原容量, 管制上限)``（管制只收紧不放松），时段外仍为原容量。

择优规则
--------
每组在各自允许出发的时间范围内寻找：到达分钟最早；同到达时出口编号最小；
同出口同到达时通道编号序列字典序最小。由于通道在节点处允许等待（等待由
封闭/容量迫使），边到达时间关于进入时间满足 FIFO，因此用时变最短路求解。
为正确处理"到达相同、通道序列字典序更小"的并列情形，每个节点保留
(到达分钟, 通道序列) 的 Pareto 标签集。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Optional

# 单节点标签数安全上限（演示规模远不会触及）
MAX_LABELS_PER_NODE = 20000


@dataclass(frozen=True)
class Edge:
    id: int
    src: int
    dst: int
    minutes: int
    capacity: int


@dataclass
class World:
    edges: dict[int, Edge]
    adjacency: dict[int, list[int]]
    exits: list[int]
    # edge_id -> 已按开始分钟升序排列的 [(a, b), ...]
    closures: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    # (edge_id, minute) -> 已固定（已出发）组在该分钟占用的总人数
    fixed_load: dict[tuple[int, int], int] = field(default_factory=dict)
    # edge_id -> 已按开始分钟升序排列、互不重叠的 [(a, b, 时段内上限), ...]
    capacity_controls: dict[int, list[tuple[int, int, int]]] = \
        field(default_factory=dict)


class PlanningError(Exception):
    """规划过程内部错误（如图结构异常等）。"""


def effective_capacity(base_capacity: int,
                       controls: list[tuple[int, int, int]] | tuple,
                       minute: int) -> int:
    """通道某分钟的有效上限。

    管制时段 ``[a, b)`` 内取 ``min(原容量, 管制上限)``（管制只收紧不
    放松），时段外为原容量。``controls`` 需按开始分钟升序且互不重叠。
    """
    for a, b, cap in controls:
        if a > minute:
            break
        if minute < b:
            return min(base_capacity, cap)
    return base_capacity


def _dominates(a_time: int, a_seq: tuple[int, ...],
               b_time: int, b_seq: tuple[int, ...]) -> bool:
    """标签 a 是否支配标签 b：到达不晚且通道序列字典序不更大。"""
    return a_time <= b_time and a_seq <= b_seq


def earliest_entry(edge: Edge, start: int, size: int, world: World,
                   horizon: int, *, use_closures: bool,
                   use_loads: bool) -> Optional[int]:
    """返回最早的可进入分钟 t >= start，使 [t, t+w) 完全可用。

    不可用包括：与封闭时段相交、某一分钟占用达到该分钟有效上限（容量
    管制时段内为 min(原容量, 管制上限)）。均不满足（或无法在 horizon
    前完成通行）时返回 None。
    """
    eid = edge.id
    w = edge.minutes
    t = start
    if t + w > horizon:
        return None
    closures = world.closures.get(eid, ())
    while t + w <= horizon:
        advanced = False
        if use_closures:
            for a, b in closures:
                if b <= t:
                    continue
                if a >= t + w:
                    break  # 闭区间已排序，之后的都在通行窗口之后
                # [t, t+w) 与 [a, b) 相交，跳到封闭结束分钟 b 再试
                t = b
                advanced = True
                break
            if advanced:
                continue
        if use_loads:
            if size > edge.capacity:
                return None  # 单组人数已超通道每分钟容量，永不可通行
            controls = world.capacity_controls.get(eid, ())
            for minute in range(t, t + w):
                cap = effective_capacity(edge.capacity, controls, minute)
                used = world.fixed_load.get((eid, minute), 0)
                if used + size > cap:
                    if size > cap:
                        # 单组人数已超该分钟有效上限（管制收紧所致，此时
                        # size <= 原容量，故该分钟必在管制时段内）：整个
                        # 管制时段均不可进入，直接跳到管制结束分钟再试
                        t = next(b for a, b, _c in controls
                                 if a <= minute < b)
                    else:
                        # 容量冲突的这一分钟不能进入；下一分钟再尝试
                        t = minute + 1
                    advanced = True
                    break
            if advanced:
                continue
        return t
    return None


def _search(world: World, *, origin: int, size: int, start: int,
            horizon: int, use_closures: bool, use_loads: bool
            ) -> dict[int, tuple[int, tuple[int, ...]]]:
    """多标签时变 Dijkstra。

    返回每个可达出口的最优标签 (最早到达分钟, 对应通道编号序列)。
    通道序列只取最早到达下字典序最小者；路径不允许重复经过节点
    （疏散路径无需绕环，同时保证标签集有限）。
    """
    # node -> [(arrival, seq), ...] Pareto 标签集
    labels: dict[int, list[tuple[int, tuple[int, ...]]]] = {}
    heap: list[tuple[int, tuple[int, ...], int, frozenset[int]]] = []

    labels[origin] = [(start, ())]
    heapq.heappush(heap, (start, (), origin, frozenset({origin})))

    while heap:
        arrive, seq, node, visited = heapq.heappop(heap)
        current = labels.get(node, [])
        if (arrive, seq) not in current:
            continue  # 已被更优标签支配的过期堆项
        for eid in world.adjacency.get(node, ()):
            edge = world.edges[eid]
            if edge.dst in visited:
                continue
            entry = earliest_entry(edge, arrive, size, world, horizon,
                                   use_closures=use_closures,
                                   use_loads=use_loads)
            if entry is None:
                continue
            nxt_time = entry + edge.minutes
            nxt_seq = seq + (eid,)
            nxt_node = edge.dst

            bucket = labels.setdefault(nxt_node, [])
            if any(_dominates(at2, sq2, nxt_time, nxt_seq)
                   for at2, sq2 in bucket):
                continue
            # 移除被新标签支配的旧标签
            bucket[:] = [(at2, sq2) for at2, sq2 in bucket
                         if not _dominates(nxt_time, nxt_seq, at2, sq2)]
            bucket.append((nxt_time, nxt_seq))
            if len(bucket) > MAX_LABELS_PER_NODE:
                raise PlanningError(
                    f"节点 {nxt_node} 标签数超过上限 {MAX_LABELS_PER_NODE}")
            heapq.heappush(
                heap,
                (nxt_time, nxt_seq, nxt_node, visited | {nxt_node}))

    result: dict[int, tuple[int, tuple[int, ...]]] = {}
    for exit_id in world.exits:
        bucket = labels.get(exit_id)
        if bucket:
            result[exit_id] = min(bucket)  # (到达, 通道序列)
    return result


def _pick(best_by_exit: dict[int, tuple[int, tuple[int, ...]]]
          ) -> Optional[tuple[int, int, tuple[int, ...]]]:
    """跨出口按 (到达分钟, 出口编号, 通道序列) 取最优。"""
    if not best_by_exit:
        return None
    exit_id, (arrive, seq) = min(
        best_by_exit.items(), key=lambda kv: (kv[1][0], kv[0], kv[1][1]))
    return arrive, exit_id, seq


def _walk_route(world: World, origin: int, size: int, start: int,
                horizon: int, seq: tuple[int, ...], *,
                use_closures: bool, use_loads: bool
                ) -> tuple[list[dict], int, dict[tuple[int, int], int]]:
    """按选定通道序列重放，得到每段进入/离开分钟与总负载增量。"""
    steps: list[dict] = []
    node = origin
    clock = start
    load_delta: dict[tuple[int, int], int] = {}
    for eid in seq:
        edge = world.edges[eid]
        entry = earliest_entry(edge, clock, size, world, horizon,
                               use_closures=use_closures,
                               use_loads=use_loads)
        if entry is None or edge.src != node:
            raise PlanningError("路线重放失败")
        leave = entry + edge.minutes
        steps.append({
            "edge_id": eid,
            "from_node": edge.src,
            "to_node": edge.dst,
            "enter_minute": entry,
            "exit_minute": leave,
        })
        for minute in range(entry, leave):
            load_delta[(eid, minute)] = (
                load_delta.get((eid, minute), 0) + size)
        node = edge.dst
        clock = leave
    return steps, clock, load_delta


def plan_group(*, group_id: int, size: int, origin: int,
               earliest_departure: int, deadline: int, current_minute: int,
               world: World) -> dict:
    """为单个组规划，返回 scheduled / failed 结果。

    world.fixed_load 中应已包含此前已规划（同批高优先级）组的占用；
    调用方在拿到 scheduled 结果后须把 load_delta 累加进 fixed_load。
    """
    start = max(current_minute, earliest_departure)
    if start > deadline:
        return {
            "group_id": group_id,
            "status": "failed",
            "reason": "DEADLINE_PASSED",
            "detail": f"当前分钟 {current_minute} 已超过规划截止分钟 {deadline}",
        }

    # 1) 完整约束下寻路
    best = _search(world, origin=origin, size=size, start=start,
                   horizon=deadline, use_closures=True, use_loads=True)
    picked = _pick(best)
    if picked is not None:
        arrive, exit_id, seq = picked
        steps, _, load_delta = _walk_route(
            world, origin, size, start, deadline, seq,
            use_closures=True, use_loads=True)
        departure = steps[0]["enter_minute"] if steps else start
        return {
            "group_id": group_id,
            "status": "scheduled",
            "exit_id": exit_id,
            "departure_minute": departure,
            "arrival_minute": arrive,
            "steps": steps,
            "load_delta": load_delta,
        }

    # 2) 归因：分别放松单一约束
    relaxed_closure = _search(
        world, origin=origin, size=size, start=start,
        horizon=deadline, use_closures=False, use_loads=True)
    relaxed_capacity = _search(
        world, origin=origin, size=size, start=start,
        horizon=deadline, use_closures=True, use_loads=False)

    if _pick(relaxed_closure) is not None:
        reason, detail = (
            "BLOCKED_BY_CLOSURE",
            "忽略封闭时段后可在截止前到达，当前封闭时段阻断了所有可行路线")
    elif _pick(relaxed_capacity) is not None:
        reason, detail = (
            "INSUFFICIENT_CAPACITY",
            "忽略通道容量限制后可在截止前到达，容量竞争阻断了所有可行路线")
    else:
        # 3) 完全放松（含时间窗），判断出口是否物理可达
        total_w = sum(e.minutes for e in world.edges.values())
        far_horizon = deadline + total_w + 1
        plain = _search(world, origin=origin, size=size, start=start,
                        horizon=far_horizon, use_closures=False,
                        use_loads=False)
        if _pick(plain) is None:
            reason, detail = (
                "UNREACHABLE_EXIT",
                f"起点 {origin} 与任何出口之间不存在连通路径")
        else:
            reason, detail = (
                "INSUFFICIENT_CAPACITY",
                "封闭时段与通道容量共同阻断了截止前的所有可行路线")

    return {
        "group_id": group_id,
        "status": "failed",
        "reason": reason,
        "detail": detail,
    }
