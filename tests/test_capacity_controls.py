"""容量管制时段（临时限流）集成测试。

覆盖：
* 登记管制后按通行期间每一分钟的有效上限重排未出发组，已出发组路线保留；
* 通道不存在 / 起止无效 / 起点早于当前分钟 / 时段重叠的整次拒绝；
* 新上限使已出发组剩余路线超载时返回冲突分钟、占用量与相关组；
* 占用查询展示各分钟有效上限（管制分钟无占用也列出）；
* 管制产生含操作参数与编排差异的修订；场景重置清除管制但旧修订可查。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 与其他测试文件同进程运行时 app.main 只加载一次（以先加载者的环境变量
# 为准）；此处设置保证本文件可独立运行。断言均不依赖修订号绝对取值。
_tmpdir = tempfile.mkdtemp(prefix="evac-test-controls-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


SCENARIO = {
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
        resp = c.post("/scenario", json=SCENARIO)
        assert resp.status_code == 200
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def controls(c):
    return c.get("/capacity-controls").json()["controls"]


def revs(c):
    return c.get("/revisions").json()["revisions"]


def latest_rev(c):
    return revs(c)[-1]["rev"]


# --------------------------------------------------------------------------
# 登记成功与重排
# --------------------------------------------------------------------------

def test_control_replans_undeparted_by_per_minute_effective_capacity(client):
    # 通道 5 第 0..10 分钟限流至 5 人/分钟：组 3（10 人）在 [0,10) 内无法
    # 进入通道 5，等到第 10 分钟到达 12 不如改道 [2,3] 第 5 分钟到达
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 10, "capacity": 5})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[3]["status"] == "scheduled"
    assert groups[3]["route"] == [2, 3]
    assert groups[3]["departure_minute"] == 0
    assert groups[3]["arrival_minute"] == 5
    # 组 1 走通道 1、5（通道 5 占用 [14,16)，在管制时段外）不受影响
    assert groups[1]["route"] == [1, 5]
    assert groups[1]["arrival_minute"] == 16
    # 管制记录可查
    assert controls(client) == [
        {"edge_id": 5, "start_minute": 0, "end_minute": 10, "capacity": 5}]


def test_control_waits_outside_window_when_detour_is_slower(client):
    # 管制 [0,2) 上限 5：组 3 等到第 2 分钟进入通道 5（到达 4），
    # 仍优于改道 [2,3]（到达 5）
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 2, "capacity": 5})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[3]["route"] == [5]
    assert groups[3]["departure_minute"] == 2
    assert groups[3]["arrival_minute"] == 4


def test_control_above_base_capacity_is_noop(client):
    # 管制上限高于原容量时有效上限仍为原容量（管制只收紧不放松）
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 10, "capacity": 99})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[3]["route"] == [5]
    assert groups[3]["departure_minute"] == 0
    assert groups[3]["arrival_minute"] == 2


def test_control_after_tick_keeps_departed_group_route(client):
    # 推进 1 分钟：组 3 固定在通道 5（占用 [0,2)，剩余 [1,2)）
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    # 管制通道 5 第 2..20 分钟限流至 25：与组 3 剩余路线 [1,2) 不相交；
    # 组 1（30 人）无法在 [2,20) 进入通道 5，改道 [1,2,3] 第 19 分钟到达
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 2, "end_minute": 20, "capacity": 25})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[3]["status"] == "fixed"
    assert groups[3]["route"] == [5]
    assert groups[3]["departure_minute"] == 0
    assert groups[1]["status"] == "scheduled"
    assert groups[1]["route"] == [1, 2, 3]
    assert groups[1]["arrival_minute"] == 19


# --------------------------------------------------------------------------
# 整次拒绝：请求无效（422）
# --------------------------------------------------------------------------

def test_control_unknown_edge_rejected(client):
    resp = client.post("/capacity-controls", json={
        "edge_id": 999, "start_minute": 0, "end_minute": 5, "capacity": 10})
    assert resp.status_code == 422
    assert controls(client) == []


def test_control_invalid_window_rejected(client):
    # 起点不早于终点
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 5, "end_minute": 5, "capacity": 10})
    assert resp.status_code == 422
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 8, "end_minute": 5, "capacity": 10})
    assert resp.status_code == 422
    # 上限必须为正整数
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 5, "capacity": 0})
    assert resp.status_code == 422
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 5, "capacity": -3})
    assert resp.status_code == 422
    assert controls(client) == []


def test_control_start_before_current_minute_rejected(client):
    assert client.post("/tick", json={"minutes": 2}).status_code == 200
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 5, "capacity": 10})
    assert resp.status_code == 422
    # 起点等于当前分钟允许
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 2, "end_minute": 5, "capacity": 40})
    assert resp.status_code == 201
    assert len(controls(client)) == 1


# --------------------------------------------------------------------------
# 整次拒绝：时段重叠（409）
# --------------------------------------------------------------------------

def test_control_overlap_rejected(client):
    assert client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 5, "end_minute": 10,
        "capacity": 10}).status_code == 201
    # 与已有时段相交（部分重叠、包含、被包含）均拒绝
    for start, end in ((8, 12), (0, 6), (6, 8), (0, 30)):
        resp = client.post("/capacity-controls", json={
            "edge_id": 5, "start_minute": start, "end_minute": end,
            "capacity": 10})
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "CAPACITY_CONTROL_OVERLAP"
        assert body["conflicts"][0]["existing"] == [5, 10]
        assert body["conflicts"][0]["requested"] == [start, end]
    # 左闭右开：起点等于已有时段终点不重叠
    assert client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 10, "end_minute": 12,
        "capacity": 10}).status_code == 201
    # 不同通道的时段互不相干
    assert client.post("/capacity-controls", json={
        "edge_id": 2, "start_minute": 5, "end_minute": 10,
        "capacity": 10}).status_code == 201
    assert len(controls(client)) == 3


# --------------------------------------------------------------------------
# 整次拒绝：已出发组剩余路线超载（409）
# --------------------------------------------------------------------------

def test_control_overloading_departed_group_rejected(client):
    # 推进 1 分钟：组 3（10 人）固定在通道 5，占用 [0,2)，剩余 [1,2)
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    n0 = len(revs(client))
    plan_before = client.get("/plan").json()

    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 5, "capacity": 5})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "CAPACITY_CONTROL_OVERLOADS_DEPARTED_GROUP"
    conflict = body["conflicts"][0]
    assert conflict["edge_id"] == 5
    assert conflict["minute"] == 1
    assert conflict["used"] == 10
    assert conflict["effective_capacity"] == 5
    assert conflict["groups"] == [{"group_id": 3, "size": 10}]

    # 整次拒绝：不改状态、不产生修订
    assert controls(client) == []
    assert client.get("/plan").json() == plan_before
    assert len(revs(client)) == n0

    # 上限能容纳已出发组占用（10 人）时允许登记
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 5, "capacity": 10})
    assert resp.status_code == 201


def test_control_window_misses_remaining_route_is_allowed(client):
    # 组 3 剩余路线为通道 5 的 [1,2)；管制 [2,6) 与之不相交 -> 允许
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    resp = client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 2, "end_minute": 6, "capacity": 1})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[3]["status"] == "fixed"
    assert groups[3]["route"] == [5]


# --------------------------------------------------------------------------
# 占用查询：各分钟有效上限
# --------------------------------------------------------------------------

def test_load_query_shows_per_minute_effective_capacity(client):
    # 通道 1 封闭 [3,10)，再登记管制 [10,14) 上限 10：
    # 组 1（30 人）在 [3,14) 均无法进入通道 1，推迟到第 14 分钟进入
    assert client.post("/capacity-controls", json={
        "edge_id": 1, "start_minute": 10, "end_minute": 14,
        "capacity": 10}).status_code == 201
    groups = by_id(client.get("/plan").json())
    assert groups[1]["route"] == [1, 5]
    assert groups[1]["departure_minute"] == 14
    assert groups[1]["arrival_minute"] == 20

    load = client.get("/passages/1/load").json()
    assert load["capacity"] == 30
    assert load["capacity_controls"] == [
        {"edge_id": 1, "start_minute": 10, "end_minute": 14, "capacity": 10}]
    slots = {m["minute"]: m for m in load["minutes"]}
    # 管制覆盖的分钟无占用也列出，有效上限为 min(30, 10)=10
    for m in (10, 11, 12, 13):
        assert slots[m]["used"] == 0
        assert slots[m]["effective_capacity"] == 10
    # 管制时段外仍用原容量
    for m in (14, 15, 16, 17):
        assert slots[m]["used"] == 30
        assert slots[m]["effective_capacity"] == 30
    assert 18 not in slots

    # ?minute=n 只看某一分钟
    one = client.get("/passages/1/load?minute=11").json()
    assert [m["minute"] for m in one["minutes"]] == [11]
    assert one["minutes"][0]["effective_capacity"] == 10


# --------------------------------------------------------------------------
# 修订与重置
# --------------------------------------------------------------------------

def test_control_creates_revision_with_params_and_diff(client):
    n0 = len(revs(client))
    client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 10, "capacity": 5})
    entries = revs(client)
    assert len(entries) == n0 + 1
    entry = entries[-1]
    assert entry["operation"] == "capacity-controls"
    assert entry["detail"] == {"edge_id": 5, "start_minute": 0,
                               "end_minute": 10, "capacity": 5}

    detail = client.get(f"/revisions/{entry['rev']}").json()
    assert detail["operation"] == "capacity-controls"
    changes = {ch["group_id"]: ch for ch in detail["changes"]}
    ch3 = changes[3]
    assert ch3["change"] == "changed"
    assert "route" in ch3["changed_fields"]
    assert ch3["before"]["route"] == [5]
    assert ch3["after"]["route"] == [2, 3]


def test_rejected_controls_create_no_revision(client):
    n0 = len(revs(client))
    # 通道不存在 / 起止无效 -> 422，不产生修订
    assert client.post("/capacity-controls", json={
        "edge_id": 999, "start_minute": 0, "end_minute": 5,
        "capacity": 1}).status_code == 422
    assert client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 5, "end_minute": 5,
        "capacity": 1}).status_code == 422
    # 一条成功管制（通道 2）+ 一条与之重叠的拒绝（409）
    assert client.post("/capacity-controls", json={
        "edge_id": 2, "start_minute": 0, "end_minute": 10,
        "capacity": 40}).status_code == 201
    assert client.post("/capacity-controls", json={
        "edge_id": 2, "start_minute": 5, "end_minute": 8,
        "capacity": 40}).status_code == 409
    # 推进 1 分钟（成功）后，新上限使已出发组超载 -> 409
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    n1 = len(revs(client))
    assert client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 3,
        "capacity": 5}).status_code == 409
    assert len(revs(client)) == n1  # 超载拒绝不产生修订
    assert n1 == n0 + 2  # 仅一条成功管制 + 一条 tick


def test_scenario_reset_clears_controls_but_keeps_revisions(client):
    client.post("/capacity-controls", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 10, "capacity": 5})
    assert len(controls(client)) == 1
    rev = latest_rev(client)
    detail_before = client.get(f"/revisions/{rev}").json()

    # 场景重置清除管制记录
    assert client.post("/scenario", json=SCENARIO).status_code == 200
    assert controls(client) == []
    # 占用查询恢复为原容量，且无管制列表
    load = client.get("/passages/5/load").json()
    assert load["capacity_controls"] == []
    used = {m["minute"]: m for m in load["minutes"]}
    assert used[0]["effective_capacity"] == 40
    # 旧修订仍可查询，内容不变
    assert client.get(f"/revisions/{rev}").json() == detail_before
    assert revs(client)[-1]["rev"] == rev + 1
