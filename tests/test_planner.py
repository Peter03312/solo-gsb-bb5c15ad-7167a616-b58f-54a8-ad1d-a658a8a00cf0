"""规划算法单元测试。"""
from __future__ import annotations

import pytest

from app.planner import Edge, World, plan_group


def build_world(edges, exits, closures=(), fixed_load=None):
    edge_map = {e.id: e for e in edges}
    adj: dict[int, list[int]] = {}
    for e in edges:
        adj.setdefault(e.src, []).append(e.id)
    for v in adj.values():
        v.sort()
    clo: dict[int, list[tuple[int, int]]] = {}
    for eid, a, b in closures:
        clo.setdefault(eid, []).append((a, b))
    return World(edges=edge_map, adjacency=adj, exits=list(exits),
                 closures=clo, fixed_load=dict(fixed_load or {}))


def plan(world, gid=1, size=10, origin=1, earliest=0, deadline=100, now=0):
    return plan_group(
        group_id=gid, size=size, origin=origin,
        earliest_departure=earliest, deadline=deadline,
        current_minute=now, world=world)


def test_simple_earliest_route():
    # 1 --1(4)--> 2 --2(3)--> 5(出口)
    world = build_world([
        Edge(1, 1, 2, 4, 100), Edge(2, 2, 5, 3, 100),
    ], exits=[5])
    r = plan(world)
    assert r["status"] == "scheduled"
    assert r["exit_id"] == 5
    assert r["departure_minute"] == 0
    assert r["arrival_minute"] == 7
    assert [s["edge_id"] for s in r["steps"]] == [1, 2]


def test_closure_forces_wait_and_half_open_end():
    # 通道 1 封闭 [2,6)；t=0 进入占用 [0,4) 与封闭相交 -> 等；
    # 封闭 [2,6) 结束分钟 6 可进入（左闭右开），[6,10) 不相交
    world = build_world(
        [Edge(1, 1, 2, 4, 100), Edge(2, 2, 3, 1, 100)],
        exits=[3], closures=[(1, 2, 6)])
    r = plan(world)
    assert r["status"] == "scheduled"
    assert r["steps"][0]["enter_minute"] == 6
    assert r["arrival_minute"] == 11


def test_closure_blocked_before_deadline_reason():
    # 唯一通道封闭 [0,50)，截止 10 -> 无法在截止前安排，原因为封闭
    world = build_world(
        [Edge(1, 1, 2, 4, 100)], exits=[2], closures=[(1, 0, 50)])
    r = plan(world, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "BLOCKED_BY_CLOSURE"


def test_unreachable_reason():
    world = build_world([Edge(1, 1, 2, 1, 100)], exits=[9])
    r = plan(world)
    assert r["status"] == "failed"
    assert r["reason"] == "UNREACHABLE_EXIT"


def test_capacity_contention_and_reason():
    # 组 A 先占满通道 1 的分钟 0..4；组 B 同分钟无法进入
    world = build_world(
        [Edge(1, 1, 2, 4, 30), Edge(2, 2, 5, 1, 30)], exits=[5])
    a = plan(world, gid=1, size=30)
    assert a["status"] == "scheduled"
    for k, v in a["load_delta"].items():
        world.fixed_load[k] = world.fixed_load.get(k, 0) + v
    # 容量满且截止 4 -> 容量原因
    r = plan(world, gid=2, size=10, deadline=4)
    assert r["status"] == "failed"
    assert r["reason"] == "INSUFFICIENT_CAPACITY"
    # 放宽截止后可等待
    r2 = plan(world, gid=2, size=10, deadline=10)
    assert r2["status"] == "scheduled"
    assert r2["steps"][0]["enter_minute"] == 4


def test_group_larger_than_capacity():
    world = build_world([Edge(1, 1, 2, 1, 5)], exits=[2])
    r = plan(world, size=6)
    assert r["status"] == "failed"
    assert r["reason"] == "INSUFFICIENT_CAPACITY"


def test_exit_id_tiebreak():
    # 到出口 5 与 6 同为第 5 分钟 -> 出口编号小者 5
    world = build_world([
        Edge(1, 1, 5, 5, 100), Edge(2, 1, 6, 5, 100),
    ], exits=[5, 6])
    r = plan(world)
    assert r["exit_id"] == 5
    assert r["arrival_minute"] == 5


def test_edge_sequence_tiebreak_needs_lex_smaller():
    # 两条到同一出口、同到达的路径：
    #   直连通道 9：1->3 耗时 6
    #   通道 1+2：1->2(3) ->3(3)，序列 (1,2) 字典序小于 (9,)
    world = build_world([
        Edge(1, 1, 2, 3, 100),
        Edge(2, 2, 3, 3, 100),
        Edge(9, 1, 3, 6, 100),
    ], exits=[3])
    r = plan(world)
    assert r["arrival_minute"] == 6
    assert [s["edge_id"] for s in r["steps"]] == [1, 2]


def test_arrival_beats_sequence():
    # (9,) 到达 5；(1,2) 到达 6 -> 到达优先，选 9
    world = build_world([
        Edge(1, 1, 2, 3, 100),
        Edge(2, 2, 3, 3, 100),
        Edge(9, 1, 3, 5, 100),
    ], exits=[3])
    r = plan(world)
    assert [s["edge_id"] for s in r["steps"]] == [9]
    assert r["arrival_minute"] == 5


def test_origin_is_exit_zero_minute_route():
    world = build_world([Edge(1, 1, 2, 1, 100)], exits=[1, 2])
    r = plan(world, origin=1)
    assert r["status"] == "scheduled"
    assert r["exit_id"] == 1
    assert r["arrival_minute"] == 0
    assert r["departure_minute"] == 0
    assert r["steps"] == []


def test_deadline_boundary_inclusive():
    # 通行 4 分钟，截止 4：t=0 进入 [0,4)，第 4 分钟到达，合法
    world = build_world([Edge(1, 1, 2, 4, 100)], exits=[2])
    r = plan(world, deadline=4)
    assert r["status"] == "scheduled"
    assert r["arrival_minute"] == 4


def test_earliest_departure_respected():
    world = build_world([Edge(1, 1, 2, 2, 100)], exits=[2])
    r = plan(world, earliest=7, deadline=20)
    assert r["departure_minute"] == 7
    assert r["arrival_minute"] == 9


def test_deadline_passed():
    world = build_world([Edge(1, 1, 2, 1, 100)], exits=[2])
    r = plan(world, now=11, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "DEADLINE_PASSED"


def test_per_minute_capacity_partial_overlap():
    # A 占用 [2,6)（等待封闭后出发），B 不能与任何一分钟重叠超额
    world = build_world(
        [Edge(1, 1, 2, 4, 30), Edge(2, 2, 3, 1, 30)],
        exits=[3], closures=[(1, 0, 2)])
    a = plan(world, gid=1, size=20, deadline=20)
    assert a["steps"][0]["enter_minute"] == 2
    for k, v in a["load_delta"].items():
        world.fixed_load[k] = world.fixed_load.get(k, 0) + v
    b = plan(world, gid=2, size=20, deadline=20)
    # 容量 30，不能在 2..6 任一分钟与 A 同时在途（20+20>30），最早第 6 分钟
    assert b["steps"][0]["enter_minute"] == 6


# --------------------------------------------------------------------------
# 容量管制时段（有效上限 = min(原容量, 管制上限)，按每分钟核算）
# --------------------------------------------------------------------------

def test_capacity_control_forces_wait_until_window_end():
    # 通道 1 容量 30，管制 [2,6) 上限 10；组 20 人：
    # t=0 进入占用 [0,4)，分钟 2、3 有效上限 10 < 20 -> 不可；
    # 单组人数超管制上限，直接跳到管制结束分钟 6
    world = build_world(
        [Edge(1, 1, 2, 4, 30), Edge(2, 2, 3, 1, 30)], exits=[3])
    world.capacity_controls[1] = [(2, 6, 10)]
    r = plan(world, size=20)
    assert r["status"] == "scheduled"
    assert r["steps"][0]["enter_minute"] == 6
    assert r["arrival_minute"] == 11


def test_capacity_control_partial_window_blocks_entry():
    # 通行 4 分钟，管制 [3,5) 上限 10，组 20 人：
    # t=0/1/2 的占用区间均含管制分钟 -> 不可；t=5 起管制外 -> 可
    world = build_world([Edge(1, 1, 2, 4, 30)], exits=[2])
    world.capacity_controls[1] = [(3, 5, 10)]
    r = plan(world, size=20)
    assert r["steps"][0]["enter_minute"] == 5


def test_capacity_control_outside_window_uses_base_capacity():
    # 管制 [10,20) 上限 5，组 30 人（=原容量）：管制外分钟照常可通行
    world = build_world([Edge(1, 1, 2, 2, 30)], exits=[2])
    world.capacity_controls[1] = [(10, 20, 5)]
    r = plan(world, size=30)
    assert r["status"] == "scheduled"
    assert r["departure_minute"] == 0
    assert r["arrival_minute"] == 2


def test_capacity_control_above_base_capacity_is_noop():
    # 管制上限高于原容量：有效上限仍为原容量（管制只收紧不放松）
    world = build_world([Edge(1, 1, 2, 2, 30)], exits=[2])
    world.capacity_controls[1] = [(0, 100, 50)]
    r = plan(world, size=30)
    assert r["status"] == "scheduled"
    assert r["departure_minute"] == 0


def test_capacity_control_with_fixed_load():
    # 管制 [0,10) 上限 25；A 20 人 t=0 进入占用 [0,4)；
    # B 10 人：分钟 0..3 有效上限 25，20+10>25 -> 等到 A 走完后 t=4
    world = build_world([Edge(1, 1, 2, 4, 30)], exits=[2])
    world.capacity_controls[1] = [(0, 10, 25)]
    a = plan(world, gid=1, size=20)
    assert a["steps"][0]["enter_minute"] == 0
    for k, v in a["load_delta"].items():
        world.fixed_load[k] = world.fixed_load.get(k, 0) + v
    b = plan(world, gid=2, size=10)
    assert b["steps"][0]["enter_minute"] == 4


def test_capacity_control_blocking_all_windows_reason():
    # 唯一通道全程管制 [0,50) 上限 5，组 6 人截止 10 -> 容量原因失败
    world = build_world([Edge(1, 1, 2, 4, 100)], exits=[2])
    world.capacity_controls[1] = [(0, 50, 5)]
    r = plan(world, size=6, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "INSUFFICIENT_CAPACITY"
