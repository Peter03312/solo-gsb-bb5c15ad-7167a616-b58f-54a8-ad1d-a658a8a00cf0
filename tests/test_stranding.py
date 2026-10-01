"""已出发组节点滞留报告（POST /group-stranding）集成测试。

覆盖：
* 受理：仅当该组按现行计划已到达报告节点、尚未进入下一通道且未抵达出口；
  报告成功后保留已走前缀及逐段时刻，释放从当前分钟起的旧预约，从报告
  节点、恢复分钟起按既有最早到达/并列择优规则重寻余程；其他已出发组
  路线与占用不变，其他未出发组随之整体重排；
* 拒绝：组未出发（scheduled/pending/failed）409 GROUP_NOT_DEPARTED；
  在途 / 已抵出口 / 节点不符 / 待救援组节点不符 409
  STRANDING_NODE_OR_TIMING_MISMATCH；组不存在 / 节点非场馆节点 /
  恢复分钟早于当前分钟 422；拒绝不改编排、不产生修订；
* 无解：标记 awaiting_rescue（待救援）并给出原因，旧预约不恢复；
  待救援组可用新恢复分钟再次报告；后续 tick 不自动重排待救援组；
* 修订：成功报告在同一事务产生一条 group-stranding 修订，快照区分
  已走前缀与新余程；旧修订快照保持原样。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-stranding-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


# 1 --1(2)--> 2 --2(2)--> 3 --3(2)--> 5(出口)
#                \--5(4)--> 6(出口)
SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 30},
        {"id": 2, "src": 2, "dst": 3, "minutes": 2, "capacity": 30},
        {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
        {"id": 5, "src": 2, "dst": 6, "minutes": 4, "capacity": 30},
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
        # 组 1：t=0 走 [1,2,3]，t=6 到出口 5
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 40},
        # 组 2：未出发，最早第 2 分钟从节点 2 出发；随滞留报告整体重排
        {"id": 2, "size": 10, "origin": 2, "earliest_departure": 2,
         "priority": 5, "deadline": 40},
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


def revs(c):
    return c.get("/revisions").json()["revisions"]


def latest_rev(c):
    return revs(c)[-1]["rev"]


def tick_to(c, minute):
    cur = c.get("/health").json()["current_minute"]
    if minute > cur:
        resp = c.post("/tick", json={"minutes": minute - cur})
        assert resp.status_code == 200
        return resp.json()
    return c.get("/plan").json()


# --------------------------------------------------------------------------
# 受理与余程重规划
# --------------------------------------------------------------------------

def test_stranding_keeps_prefix_and_replans_remainder(client):
    # 推进到第 2 分钟：组 1 恰好走完通道 1（[0,2)），停留在节点 2
    plan = tick_to(client, 2)
    g1 = by_id(plan)[1]
    assert g1["status"] == "fixed"
    assert g1["steps"][0]["exit_minute"] == 2

    # 报告节点 2 滞留，预计第 4 分钟恢复；旧余程 [2,3]（通道2 t=2 进入、
    # 通道3 t=4 进入，原到达 6）被释放，新余程 t=4 进入通道2、t=6 通道3、
    # t=8 到出口 5
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 4})
    assert resp.status_code == 200, resp.text
    groups = by_id(resp.json())
    g1 = groups[1]
    assert g1["status"] == "fixed"
    assert g1["exit_id"] == 5
    assert g1["route"] == [1, 2, 3]
    assert g1["arrival_minute"] == 8
    assert g1["departure_minute"] == 0  # 原出发分钟保留
    # 已走前缀原样保留时刻，新余程从恢复分钟起
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 2},
        {"edge_id": 2, "from_node": 2, "to_node": 3,
         "enter_minute": 4, "exit_minute": 6},
        {"edge_id": 3, "from_node": 3, "to_node": 5,
         "enter_minute": 6, "exit_minute": 8}]
    assert g1["stranding"] == {
        "node": 2, "resume_minute": 4, "prefix_steps": 1}
    # 其他未出发组随之整体重排并返回
    assert groups[2]["status"] == "scheduled"
    assert resp.json() == client.get("/plan").json()


def test_stranding_picks_earliest_route_by_existing_rules(client):
    # 恢复分钟推迟到第 10 分钟：通道2+3 需 4 分钟（到达 14），
    # 通道5 需 4 分钟（到达 14）——同到达取出口编号小者 5
    tick_to(client, 2)
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 10})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["exit_id"] == 5
    assert g1["route"] == [1, 2, 3]
    assert g1["arrival_minute"] == 14


def test_stranding_releases_old_reservation_for_other_groups(client):
    # 旧余程占用通道2 的 [2,4)、通道3 的 [4,6)；组 1 在节点 2 滞留至
    # 第 8 分钟后，未出发组 2（起点就是 2）可立即使用通道2 的 [2,4)
    tick_to(client, 2)
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 8})
    assert resp.status_code == 200
    groups = by_id(resp.json())
    g2 = groups[2]
    assert g2["route"] == [2, 3]
    assert g2["departure_minute"] == 2
    assert g2["arrival_minute"] == 6
    # 组 1 新余程 t=8 进入通道2（[8,10)），t=12 到出口
    g1 = groups[1]
    assert g1["steps"][1]["enter_minute"] == 8
    assert g1["arrival_minute"] == 12


def test_stranding_keeps_other_departed_group_route_and_load(client):
    # 专用场景：通道5（2->6）只需 2 分钟，另加一个 t=0 出发的组 9 走
    # [5] 到出口6，占用通道5 的 [0,2)。推进到第 2 分钟：组 9 已固定在
    # 出口6（路线不变），组 1 停留在节点 2。受理组 1 滞留不得改动组 9，
    # 且通道5 第 6 分钟空闲，组 1 余程从第 6 分钟起改走 [5] 到达更早。
    client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 30},
            {"id": 2, "src": 2, "dst": 3, "minutes": 2, "capacity": 30},
            {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
            {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 30},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 40},
            {"id": 9, "size": 7, "origin": 2, "earliest_departure": 0,
             "priority": 1, "deadline": 40},
        ]})
    tick_to(client, 2)
    before9 = by_id(client.get("/plan").json())[9]
    assert before9["status"] == "fixed"
    assert before9["route"] == [5]
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 6})
    assert resp.status_code == 200
    after9 = by_id(resp.json())[9]
    assert after9 == before9
    # 通道5 [6,8) 空闲，改走 [1,5] 第 8 分钟到出口6（[2,3] 需到第 10 分钟）
    g1 = by_id(resp.json())[1]
    assert g1["route"] == [1, 5]
    assert g1["exit_id"] == 6
    assert g1["arrival_minute"] == 8
    assert g1["steps"][1]["enter_minute"] == 6
    # 通道5 第 0、1 分钟的占用只来自组 9（组 1 的旧/新预约都不在那之前）
    load5 = {m["minute"]: m for m in
             client.get("/passages/5/load").json()["minutes"]}
    assert {x["group_id"] for x in load5[0]["groups"]} == {9}
    assert {x["group_id"] for x in load5[6]["groups"]} == {1}


# --------------------------------------------------------------------------
# 拒绝：时机与节点不符
# --------------------------------------------------------------------------

def test_report_while_group_on_edge_rejected(client):
    # 第 1 分钟组 1 正在通道1（[0,2)）在途 -> 时机不符
    tick_to(client, 1)
    n0 = len(revs(client))
    plan0 = client.get("/plan").json()
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 1})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "STRANDING_NODE_OR_TIMING_MISMATCH"
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "GROUP_ON_EDGE"
    assert conflict["edge_id"] == 1
    assert conflict["enter_minute"] == 0
    assert conflict["exit_minute"] == 2
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_report_wrong_node_rejected(client):
    # 第 2 分钟组 1 实际在节点 2，报告节点 3 -> 节点不符
    tick_to(client, 2)
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 3, "resume_minute": 2})
    assert resp.status_code == 409
    assert resp.json()["error"] == "STRANDING_NODE_OR_TIMING_MISMATCH"
    assert resp.json()["conflicts"][0]["reason"] == "NODE_OR_TIMING_MISMATCH"
    assert resp.json()["conflicts"][0]["actual_node"] == 2


def test_report_after_group_reached_exit_rejected(client):
    # 第 6 分钟组 1 已到出口 5 -> 不接受
    tick_to(client, 6)
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 5, "resume_minute": 6})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "STRANDING_NODE_OR_TIMING_MISMATCH"
    assert body["conflicts"][0]["reason"] == "GROUP_ALREADY_AT_EXIT"


def test_report_at_second_intermediate_node(client):
    # 第 4 分钟组 1 走完通道2（[2,4)），停留节点 3；报告节点 3 受理，
    # 只剩通道3，第 5 分钟进入则第 7 分钟到达
    tick_to(client, 4)
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 3, "resume_minute": 5})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["route"] == [1, 2, 3]
    assert g1["steps"][-1] == {
        "edge_id": 3, "from_node": 3, "to_node": 5,
        "enter_minute": 5, "exit_minute": 7}
    assert g1["stranding"]["prefix_steps"] == 2
    assert g1["arrival_minute"] == 7


# --------------------------------------------------------------------------
# 拒绝：未出发 / 参数非法
# --------------------------------------------------------------------------

def test_report_for_undeparted_group_rejected(client):
    # 组 2 尚未出发（scheduled）
    resp = client.post("/group-stranding",
                       json={"group_id": 2, "node": 2, "resume_minute": 0})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "GROUP_NOT_DEPARTED"
    assert body["conflicts"][0]["group_id"] == 2
    assert body["conflicts"][0]["status"] == "scheduled"


def test_report_for_failed_group_rejected(client):
    assert client.post("/groups", json=[
        {"id": 8, "size": 999, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 5}]).status_code == 201
    assert by_id(client.get("/plan").json())[8]["status"] == "failed"
    resp = client.post("/group-stranding",
                       json={"group_id": 8, "node": 1, "resume_minute": 0})
    assert resp.status_code == 409
    assert resp.json()["error"] == "GROUP_NOT_DEPARTED"


def test_report_unknown_group_unknown_node_past_minute(client):
    assert client.post("/group-stranding",
                       json={"group_id": 777, "node": 2, "resume_minute": 0}
                       ).status_code == 422
    tick_to(client, 1)
    # 恢复分钟早于当前分钟
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 1, "resume_minute": 0}
                       ).status_code == 422
    # 节点非场馆节点
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 999, "resume_minute": 1}
                       ).status_code == 422
    # 负分钟 / 非正组号（schema 校验）
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": -1}
                       ).status_code == 422


def test_rejected_report_changes_nothing(client):
    tick_to(client, 1)
    n0 = len(revs(client))
    plan0 = client.get("/plan").json()
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 2,
                             "resume_minute": 1}).status_code == 409
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


# --------------------------------------------------------------------------
# 无解 -> 待救援（awaiting_rescue）
# --------------------------------------------------------------------------

def test_no_remainder_marks_awaiting_rescue_without_restoring(client):
    # 通道2、5 从第 4 分钟起长期封闭：恢复分钟 4 时节点 2 之后所有余程
    # 被封闭阻断（截止 40 内无解，忽略容量后仍不通——封闭原因）
    client.post("/closures",
                json={"edge_id": 2, "start_minute": 4, "end_minute": 100})
    client.post("/closures",
                json={"edge_id": 5, "start_minute": 4, "end_minute": 100})
    tick_to(client, 2)
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 4})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "BLOCKED_BY_CLOSURE"
    assert g1["exit_id"] is None
    assert g1["arrival_minute"] is None
    # 已走前缀保留，旧余程未恢复
    assert g1["route"] == [1]
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 2}]
    assert g1["stranding"] == {
        "node": 2, "resume_minute": 4, "prefix_steps": 1}
    # 旧预约确实释放：通道2 的 [2,4) 与 [4,6) 都不再有组 1 的占用
    # （[2,4) 上仅有重排后进入的未出发组 2）
    load = client.get("/passages/2/load").json()
    slots = {slot["minute"]: slot for slot in load["minutes"]}
    for m in (2, 3, 4, 5):
        assert {x["group_id"] for x in slots[m]["groups"]} != {1}


def test_awaiting_rescue_not_auto_replanned_on_tick(client):
    client.post("/closures",
                json={"edge_id": 2, "start_minute": 4, "end_minute": 100})
    client.post("/closures",
                json={"edge_id": 5, "start_minute": 4, "end_minute": 100})
    tick_to(client, 2)
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 2,
                             "resume_minute": 4}).status_code == 200
    # 继续推进（封闭不变），待救援组不自动重排
    plan = tick_to(client, 32)
    g1 = by_id(plan)[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["route"] == [1]
    assert g1["stranding"]["resume_minute"] == 4


def test_awaiting_rescue_can_report_again_with_earlier_resume(client):
    """待救援组可用新的恢复分钟再次报告；首次误报过晚无解，改报更早的
    恢复分钟后重新寻得余程（专用场景：通道5 绕路需 10 分钟，封闭窗
    [3,4) 只会把第 3 分钟的进入推迟一分钟）。"""
    client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 30},
            {"id": 2, "src": 2, "dst": 3, "minutes": 2, "capacity": 30},
            {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
            {"id": 5, "src": 2, "dst": 6, "minutes": 10, "capacity": 30},
        ],
        "exits": [5, 6],
        "closures": [{"edge_id": 2, "start_minute": 3, "end_minute": 4}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 9},
        ]})
    # 组 1 原计划：通道2 第 2 分钟进入占用 [2,4) 与封闭 [3,4) 相交，
    # 推迟到第 4 分钟进入，第 8 分钟到达出口5
    g1 = by_id(client.get("/plan").json())[1]
    assert g1["route"] == [1, 2, 3]
    assert g1["arrival_minute"] == 8
    tick_to(client, 2)
    # 首次误报恢复分钟 6：最早第 10 分钟到达，晚于截止 9 -> 待救援
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 2,
                             "resume_minute": 6}).status_code == 200
    g1 = by_id(client.get("/plan").json())[1]
    assert g1["status"] == "awaiting_rescue"
    # 改报恢复分钟 2（>= 当前分钟 2）：封闭把进入推迟到第 4 分钟，
    # 第 8 分钟到达，余程恢复
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 2})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "fixed"
    assert g1["route"] == [1, 2, 3]
    assert g1["steps"][0] == {
        "edge_id": 1, "from_node": 1, "to_node": 2,
        "enter_minute": 0, "exit_minute": 2}
    assert g1["steps"][1]["enter_minute"] == 4
    assert g1["arrival_minute"] == 8
    assert g1["stranding"]["resume_minute"] == 2


def test_awaiting_rescue_report_again_still_unsolved(client):
    # 用主场景（通道5 通行 4 分钟）：封闭窗 [4,30) 时，恢复分钟 4 与
    # 恢复分钟 18 都无解（截止 22），第二次报告被受理并重算，组仍为
    # 待救援、现场信息更新
    client.post("/closures",
                json={"edge_id": 2, "start_minute": 4, "end_minute": 30})
    client.post("/closures",
                json={"edge_id": 5, "start_minute": 4, "end_minute": 30})
    # 收紧截止以排除"等到第 30 分钟仍可到"
    client.post("/group-corrections",
                json=[{"group_id": 1, "deadline": 22}])
    tick_to(client, 2)
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 2,
                             "resume_minute": 4}).status_code == 200
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 18})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "BLOCKED_BY_CLOSURE"
    assert g1["route"] == [1]
    assert g1["stranding"]["resume_minute"] == 18


def test_awaiting_rescue_report_wrong_node_rejected(client):
    client.post("/closures",
                json={"edge_id": 2, "start_minute": 4, "end_minute": 100})
    client.post("/closures",
                json={"edge_id": 5, "start_minute": 4, "end_minute": 100})
    tick_to(client, 2)
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 2,
                             "resume_minute": 4}).status_code == 200
    # 待救援组停留在节点 2，报告节点 3 -> 拒绝
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 3, "resume_minute": 6})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "NODE_MISMATCH"
    assert resp.json()["conflicts"][0]["actual_node"] == 2


def test_awaiting_rescue_group_cannot_be_corrected(client):
    client.post("/closures",
                json={"edge_id": 2, "start_minute": 4, "end_minute": 100})
    client.post("/closures",
                json={"edge_id": 5, "start_minute": 4, "end_minute": 100})
    tick_to(client, 2)
    assert client.post("/group-stranding",
                       json={"group_id": 1, "node": 2,
                             "resume_minute": 4}).status_code == 200
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "size": 5}])
    assert resp.status_code == 409
    assert resp.json()["error"] == "GROUP_ALREADY_DEPARTED"
    assert resp.json()["conflicts"][0]["status"] == "awaiting_rescue"


def test_capacity_after_rescue_uses_remaining_capacity(client):
    """余程规划须计入容量管制：通道2 第 4..8 分钟限流至 5 人，组 1（10
    人）在管制结束前无法进入通道2，改走通道5（4 分钟，到达 8）优于
    等待后走 [2,3]（第 8 分钟进入，到达 12）。"""
    tick_to(client, 2)
    assert client.post("/capacity-controls", json={
        "edge_id": 2, "start_minute": 4, "end_minute": 8,
        "capacity": 5}).status_code == 201
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 4})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "fixed"
    assert g1["route"] == [1, 5]
    assert g1["steps"][1]["enter_minute"] == 4
    assert g1["arrival_minute"] == 8


def test_capacity_control_can_force_wait_on_only_remaining_route(client):
    """节点之后只剩通道2、3 时，管制使新余程等待至管制结束再进入。"""
    # 封掉通道5；通道2 第 4..8 分钟限流 5：组 1 只能等到第 8 分钟进通道2
    assert client.post("/closures",
                       json={"edge_id": 5, "start_minute": 0,
                             "end_minute": 100}).status_code == 201
    tick_to(client, 2)
    assert client.post("/capacity-controls", json={
        "edge_id": 2, "start_minute": 4, "end_minute": 8,
        "capacity": 5}).status_code == 201
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 4})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "fixed"
    assert g1["route"] == [1, 2, 3]
    assert g1["steps"][1]["enter_minute"] == 8
    assert g1["arrival_minute"] == 12


# --------------------------------------------------------------------------
# 修订历史
# --------------------------------------------------------------------------

def test_stranding_creates_revision_in_same_transaction(client):
    tick_to(client, 2)
    n0 = len(revs(client))  # scenario + tick
    resp = client.post("/group-stranding",
                       json={"group_id": 1, "node": 2, "resume_minute": 4})
    plan = resp.json()
    assert len(revs(client)) == n0 + 1
    entry = revs(client)[-1]
    assert entry["operation"] == "group-stranding"
    assert entry["detail"] == {
        "group_id": 1, "node": 2, "resume_minute": 4}
    assert entry["minute"] == 2
    detail = client.get(f"/revisions/{entry['rev']}").json()
    assert detail["snapshot"] == plan
    assert client.get("/plan").json() == plan
    changes = {ch["group_id"]: ch for ch in detail["changes"]}
    ch1 = changes[1]
    assert ch1["change"] == "changed"
    assert "steps" in ch1["changed_fields"]
    assert "stranding" in ch1["changed_fields"]
    # 前缀不变、余程变化
    assert ch1["before"]["steps"][0] == ch1["after"]["steps"][0]
    assert ch1["after"]["steps"][1]["enter_minute"] == 4


def test_previous_revision_snapshot_kept_intact(client):
    # tick 修订的快照（旧编排）随后被滞留报告改写后，仍按原样返回
    tick_to(client, 2)
    tick_rev = latest_rev(client)
    tick_snapshot = client.get(f"/revisions/{tick_rev}").json()
    client.post("/group-stranding",
                json={"group_id": 1, "node": 2, "resume_minute": 4})
    assert client.get(f"/revisions/{tick_rev}").json() == tick_snapshot
    g1 = {g["group_id"]: g for g in
          client.get(f"/revisions/{tick_rev}").json()["snapshot"]["groups"]}[1]
    assert g1["steps"][1]["enter_minute"] == 2  # 旧余程时刻，未被改写


def test_unreported_groups_plan_shape_unchanged(client):
    """未报告组的规划字段语义保持兼容（stranding 为 null）。"""
    g2 = by_id(client.get("/plan").json())[2]
    assert g2["stranding"] is None
    tick_to(client, 2)
    g2 = by_id(client.get("/plan").json())[2]
    assert g2["stranding"] is None
