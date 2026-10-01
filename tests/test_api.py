import asyncio
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


SIMPLE_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
        {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
        {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
        {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
        {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20},
    ],
    "exits": [5, 6],
    "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
    "groups": [
        {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 20, "deadline": 20},
        {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0,
         "priority": 5, "deadline": 15},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=SIMPLE_SCENARIO)
        assert resp.status_code == 200
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def test_seed_scenario_and_priority_order(client):
    plan = client.get("/plan").json()
    groups = by_id(plan)
    # 组 3 优先级最高：2 -> 通道5 -> 出口6，t=0 出发 t=2 到达
    assert groups[3]["status"] == "scheduled"
    assert groups[3]["exit_id"] == 6
    assert groups[3]["route"] == [5]
    assert groups[3]["departure_minute"] == 0
    assert groups[3]["arrival_minute"] == 2
    # 组 1（30 人）：通道4 容量 20 不可用；通道1 封闭 [3,10) 使其无法
    # 在 t=0 进入（[0,4) 与封闭相交），推迟到第 10 分钟进入；
    # 到节点2 为第 14 分钟，走通道5 到出口6 第 16 分钟（优于经 2、3 到出口5 的 19 分钟）
    g1 = groups[1]
    assert g1["status"] == "scheduled"
    assert g1["exit_id"] == 6
    assert g1["route"] == [1, 5]
    assert g1["departure_minute"] == 10
    assert g1["arrival_minute"] == 16
    # 组 4：7->5 直达
    assert groups[4]["route"] == [7]
    assert groups[4]["arrival_minute"] == 5


def test_add_group_only_replans_undeparted(client):
    resp = client.post("/groups", json=[
        {"id": 9, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 15, "deadline": 20}])
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[9]["status"] == "scheduled"


def test_duplicate_group_rejected(client):
    resp = client.post("/groups", json=[
        {"id": 1, "size": 1, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 5}])
    assert resp.status_code == 409
    # 数据未被改动
    assert len(client.get("/plan").json()["groups"]) == 3


def test_capacity_query(client):
    load = client.get("/passages/1/load").json()
    assert load["capacity"] == 30
    # 组1 30 人在通道1 占用分钟 10..14
    used = {m["minute"]: m["used"] for m in load["minutes"]}
    assert used[10] == 30
    assert used[13] == 30
    assert 14 not in used
    # 组 4 在通道 7 占用 0..5，共 15 人
    load7 = client.get("/passages/7/load").json()
    used7 = {m["minute"]: m["used"] for m in load7["minutes"]}
    assert used7[0] == 15
    assert used7[4] == 15
    assert 5 not in used7


def test_tick_fixes_departed_group_and_replans(client):
    # 组 3 t=0 出发；推进 1 分钟后固定。组 1 原计划 t=10 出发，保持可重排
    resp = client.post("/tick", json={"minutes": 1})
    assert resp.status_code == 200
    groups = by_id(resp.json())
    assert groups[3]["status"] == "fixed"
    assert groups[3]["route"] == [5]
    assert groups[1]["status"] == "scheduled"


def test_closure_touching_departed_remaining_route_rejected(client):
    client.post("/tick", json={"minutes": 1})
    # 组 3 固定在通道 5 上，占用 [0,2)；当前分钟 1，剩余 [1,2)
    resp = client.post("/closures",
                       json={"edge_id": 5, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "CLOSURE_CONFLICTS_WITH_DEPARTED_GROUP"
    assert body["conflicts"][0]["group_id"] == 3
    # 封闭未登记：再查通道5占用不变，且 closures 仍为 1 条
    load = client.get("/passages/5/load").json()
    assert any(m["minute"] == 1 for m in load["minutes"])


def test_closure_entirely_in_past_is_allowed(client):
    client.post("/tick", json={"minutes": 3})
    # 组 3 在通道 5 的占用 [0,2) 已全部过去，封闭 [0,2) 不触及剩余路线
    resp = client.post("/closures",
                       json={"edge_id": 5, "start_minute": 0, "end_minute": 2})
    assert resp.status_code == 201


def test_future_closure_replans_undeparted(client):
    # 通道 5 全时段封闭：组 3 改道经通道 2、3 到出口 5；组 1 同样改道
    resp = client.post("/closures",
                       json={"edge_id": 5, "start_minute": 0, "end_minute": 30})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[1]["status"] == "scheduled"
    assert groups[1]["exit_id"] == 5
    assert groups[1]["route"] == [1, 2, 3]
    assert groups[3]["route"] == [2, 3]


def test_failed_group_reason(client):
    resp = client.post("/groups", json=[
        {"id": 99, "size": 200, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 10}])
    groups = by_id(resp.json())
    assert groups[99]["status"] == "failed"
    assert groups[99]["reason"] == "INSUFFICIENT_CAPACITY"


def test_concurrent_changes_never_oversell_capacity(client):
    """并发新增多个竞争同一通道的组：串行化后任意一分钟不超容量。"""
    from concurrent.futures import ThreadPoolExecutor

    payloads = [
        [{"id": gid, "size": 20, "origin": 1, "earliest_departure": 0,
          "priority": gid, "deadline": 120}]
        for gid in (10, 11, 12, 13, 14)]

    def post(payload):
        return client.post("/groups", json=payload)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(post, payloads))
    assert all(r.status_code == 201 for r in responses)

    plan = client.get("/plan").json()
    # 汇总每条通道每分钟的占用
    usage: dict[tuple[int, int], int] = {}
    sizes = {1: 30, 3: 10, 4: 15,
             **{gid: 20 for gid in (10, 11, 12, 13, 14)}}
    for g in plan["groups"]:
        if g["status"] not in ("scheduled", "fixed"):
            continue
        for step in g["steps"]:
            for m in range(step["enter_minute"], step["exit_minute"]):
                key = (step["edge_id"], m)
                usage[key] = usage.get(key, 0) + sizes[g["group_id"]]
    caps = {1: 30, 2: 30, 3: 30, 5: 40, 7: 20}
    for (edge_id, minute), used in usage.items():
        assert used <= caps[edge_id], (
            f"通道 {edge_id} 第 {minute} 分钟超用：{used}>{caps[edge_id]}")
