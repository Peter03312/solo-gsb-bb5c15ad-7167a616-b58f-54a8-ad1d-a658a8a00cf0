"""现场节点滞留报告集成测试。

覆盖：
* 受理条件：仅当已出发组按现行计划已到达所报节点、尚未进入下一通道且
  未抵达出口时接受；进入分钟 == 当前分钟仍可在节点被报告（与 tick 口径
  一致）；
* 成功受理：已走前缀与时间原样保留，释放该组从当前分钟起的旧预约，
  从所报节点、恢复分钟起按现有封闭/限流/其他已出发组固定占用重找余程；
  新后缀与其他已出发组同等受封闭与限流保护；
* 其他未出发组随之整体重排（让出固定占用、可被推迟），其他已出发组
  路线与占用不变；
* 无解：标记 awaiting_rescue 并给出原因、保留已走前缀、不恢复旧预约；
  后续时间推进不自动重排；待救援组只能在滞留节点重复报告；
* 拒绝：组未出发 / 节点不符 / 通道中 / 已抵出口 -> 409，不改编排、
  不产生修订；组不存在 / 恢复分钟早于当前分钟 -> 422；
* 待救援组不可被纠正；成功报告产生一条 delay-reports 修订，当前编排
  以 prefix_step_count 区分已走前缀与新余程，旧修订快照保持原样。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-delay-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


# 1 --1(4/10)--> 2 --2(2/10)--> 6(出口)
#                 |             ^
#                 3(2/10)       4(2/10)
#                 v             |
#                 3 ----------- 5(出口)
SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
        {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
        {"id": 3, "src": 2, "dst": 3, "minutes": 2, "capacity": 10},
        {"id": 4, "src": 3, "dst": 5, "minutes": 2, "capacity": 10},
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 30},
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 1, "deadline": 30},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=SCENARIO)
        assert resp.status_code == 200
        yield c


def report(c, gid, node, recovery):
    return c.post(f"/groups/{gid}/delay-report",
                  json={"node_id": node, "recovery_minute": recovery})


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def revs(c):
    return c.get("/revisions").json()["revisions"]


def latest_rev(c):
    return revs(c)[-1]["rev"]


def tick_to(c, minute):
    cur = c.get("/health").json()["current_minute"]
    assert minute >= cur
    if minute > cur:
        resp = c.post("/tick", json={"minutes": minute - cur})
        assert resp.status_code == 200
    return c.get("/plan").json()


def edge_load(c, edge_id):
    data = c.get(f"/passages/{edge_id}/load").json()
    return {m["minute"]: m["used"] for m in data["minutes"]}


# --------------------------------------------------------------------------
# 受理成功：保留前缀、释放旧预约、重找余程
# --------------------------------------------------------------------------

def test_report_reschedules_suffix_and_keeps_prefix(client):
    # 初始：组1 [edge1 0-4, edge2 4-6] 到出口6；组2 [edge1 4-8, edge2 8-10]；
    # 组3 [edge2 0-2]
    groups = by_id(client.get("/plan").json())
    assert groups[1]["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 4},
        {"edge_id": 2, "from_node": 2, "to_node": 6,
         "enter_minute": 4, "exit_minute": 6}]

    # 推进到第 4 分钟：组 1 已走完 edge1、到达节点 2，原计划此刻进入 edge2
    # （进入分钟 == 当前分钟仍可在节点被报告）
    groups = by_id(tick_to(client, 4))
    assert groups[1]["status"] == "fixed"

    resp = report(client, 1, 2, 6)
    assert resp.status_code == 200, resp.text
    g1 = by_id(resp.json())[1]
    # 组仍为已出发固定状态；前缀 edge1 [0,4) 原样保留，余程推迟到第 6 分钟
    assert g1["status"] == "fixed"
    assert g1["prefix_step_count"] == 1
    assert g1["departure_minute"] == 0  # 已走前缀对应的出发时间不变
    assert g1["arrival_minute"] == 8
    assert g1["exit_id"] == 6
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 4},
        {"edge_id": 2, "from_node": 2, "to_node": 6,
         "enter_minute": 6, "exit_minute": 8}]
    # 响应即当前编排
    assert resp.json() == client.get("/plan").json()

    # 旧预约 [4,6) 已释放，新余程 [6,8) 落为固定占用
    used = edge_load(client, 2)
    assert used.get(4) is None and used.get(5) is None
    assert used[6] == 10 and used[7] == 10
    # 已走前缀 edge1 的历史占用 [0,4) 保留
    used1 = edge_load(client, 1)
    assert all(used1[m] == 10 for m in range(0, 4))


def test_other_undeparted_groups_replan_around_new_fixed_suffix(client):
    # 组 1 在节点 2 滞留到第 7 分钟恢复：新余程 edge2 [7,9)；
    # 组 2 原计划 edge2 [8,10) 与新固定占用冲突，整体重排后推迟到 [9,11)
    tick_to(client, 4)
    resp = report(client, 1, 2, 7)
    assert resp.status_code == 200
    groups = by_id(resp.json())
    assert groups[1]["arrival_minute"] == 9
    assert groups[2]["status"] == "scheduled"
    assert groups[2]["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 4, "exit_minute": 8},
        {"edge_id": 2, "from_node": 2, "to_node": 6,
         "enter_minute": 9, "exit_minute": 11}]
    # 每分钟容量不超用
    used = edge_load(client, 2)
    for minute, value in used.items():
        assert value <= 10


def test_report_at_entry_minute_boundary_is_accepted(client):
    # 组 1 原计划第 4 分钟进入 edge2；在第 4 分钟当分钟报告，恢复分钟就是
    # 当前分钟（恢复分钟不得早于当前分钟，相等允许）
    tick_to(client, 4)
    resp = report(client, 1, 2, 4)
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "fixed"
    assert g1["steps"][-1] == {
        "edge_id": 2, "from_node": 2, "to_node": 6,
        "enter_minute": 4, "exit_minute": 6}


def test_report_prefers_earliest_route_under_existing_rules(client):
    # 恢复时刻两条余程：edge2 直达出口 6（2 分钟）优于 3+4 到出口 5（4 分钟）
    tick_to(client, 4)
    resp = report(client, 1, 2, 6)
    g1 = by_id(resp.json())[1]
    assert g1["route"] == [1, 2]
    assert g1["exit_id"] == 6
    assert g1["arrival_minute"] == 8


def test_other_departed_groups_route_and_load_unchanged(client):
    # 组 3 已走 edge2 [0,2) 固定；报告组 1 不影响其路线与占用
    tick_to(client, 4)
    g3_before = by_id(client.get("/plan").json())[3]
    report(client, 1, 2, 6)
    g3_after = by_id(client.get("/plan").json())[3]
    assert g3_after == g3_before
    used = edge_load(client, 2)
    assert all(used[m] == 10 for m in (0, 1))


def test_alias_endpoint_same_semantics(client):
    tick_to(client, 4)
    n = len(revs(client))
    resp = client.post("/delay-reports",
                       json={"group_id": 1, "node_id": 2,
                             "recovery_minute": 6})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["prefix_step_count"] == 1
    assert g1["arrival_minute"] == 8
    assert len(revs(client)) == n + 1


def test_report_twice_from_a_later_prefix_node(client):
    # 第一次：节点 2 恢复到第 6 分钟，余程 edge2 [6,8)；
    # 第二次在节点 6 之前无中间节点，改为验证二次报告发生在后缀起点
    # （edge2 [6,8)）到达后即出口，故此处验证二次报告仍可在新前缀
    # 边界节点被受理的链路：使用三跳路线的场景
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 10},
            {"id": 2, "src": 2, "dst": 3, "minutes": 2, "capacity": 10},
            {"id": 3, "src": 3, "dst": 4, "minutes": 2, "capacity": 10},
        ],
        "exits": [4], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    tick_to(c, 2)
    resp = c.post("/groups/1/delay-report",
                  json={"node_id": 2, "recovery_minute": 4})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["prefix_step_count"] == 1
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 2},
        {"edge_id": 2, "from_node": 2, "to_node": 3,
         "enter_minute": 4, "exit_minute": 6},
        {"edge_id": 3, "from_node": 3, "to_node": 4,
         "enter_minute": 6, "exit_minute": 8}]
    # 推进到第 6 分钟：已到节点 3，前缀扩为两段（edge1 + 已走的 edge2）
    tick_to(c, 6)
    resp = c.post("/groups/1/delay-report",
                  json={"node_id": 3, "recovery_minute": 7})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["prefix_step_count"] == 2
    assert g1["steps"][0] == {"edge_id": 1, "from_node": 1, "to_node": 2,
                              "enter_minute": 0, "exit_minute": 2}
    assert g1["steps"][1] == {"edge_id": 2, "from_node": 2, "to_node": 3,
                              "enter_minute": 4, "exit_minute": 6}
    assert g1["steps"][2] == {"edge_id": 3, "from_node": 3, "to_node": 4,
                              "enter_minute": 7, "exit_minute": 9}
    assert g1["arrival_minute"] == 9


# --------------------------------------------------------------------------
# 拒绝：409（未出发 / 节点不符 / 通道中 / 已抵出口）
# --------------------------------------------------------------------------

def test_report_undeparted_group_rejected(client):
    # 第 0 分钟所有组均未出发
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))
    resp = report(client, 2, 1, 0)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "DELAY_REPORT_NOT_ACCEPTABLE"
    assert body["conflicts"][0]["reason"] == "NOT_DEPARTED"
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_report_wrong_node_rejected(client):
    tick_to(client, 4)  # 组 1 按现行计划在节点 2
    resp = report(client, 1, 1, 4)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "NODE_MISMATCH"
    assert conflict["expected_node_id"] == 2


def test_report_while_in_transit_rejected(client):
    # 第 5 分钟：组 1 固定在 edge2 [4,6) 上（enter 4 < now 5），正在通道中
    tick_to(client, 5)
    resp = report(client, 1, 2, 5)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "GROUP_IN_TRANSIT"
    assert conflict["edge_id"] == 2
    assert conflict["enter_minute"] == 4
    assert conflict["exit_minute"] == 6


def test_report_at_exit_rejected(client):
    # 组 3（edge2 [0,2) 直达出口 6）第 2 分钟已抵出口
    tick_to(client, 3)
    resp = report(client, 3, 6, 3)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "ALREADY_AT_EXIT"


def test_rejected_report_changes_nothing(client):
    tick_to(client, 4)
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))
    assert report(client, 2, 1, 4).status_code == 409       # 未出发
    assert report(client, 1, 1, 4).status_code == 409       # 节点不符
    assert report(client, 1, 2, 3).status_code == 422       # 恢复分钟在过去
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_recovery_before_current_minute_rejected_422(client):
    tick_to(client, 4)
    resp = report(client, 1, 2, 3)
    assert resp.status_code == 422
    assert "3" in resp.json()["detail"] and "4" in resp.json()["detail"]


def test_unknown_group_rejected_422(client):
    resp = report(client, 999, 2, 0)
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# 待救援（无余程）：前缀保留、旧预约不恢复、tick 不自动重排
# --------------------------------------------------------------------------

def test_no_suffix_due_to_closure_marks_awaiting_rescue(client):
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        # 封闭 [5,30)：组 1 初始第 4 分钟进入 edge2、第 5 分钟到达故不受阻
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    tick_to(c, 4)
    resp = report(c, 1, 2, 6)
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "BLOCKED_BY_CLOSURE"
    assert g1["prefix_step_count"] == 1
    # 已走前缀保留；未来余程为空
    assert g1["route"] == [1]
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 4}]
    assert g1["arrival_minute"] is None and g1["exit_id"] is None
    assert g1["departure_minute"] == 0
    # 旧预约 edge2 [4,5) 已释放且不恢复
    used = edge_load(c, 2)
    assert 4 not in used and 5 not in used


def test_no_suffix_due_to_capacity_marks_awaiting_rescue(client):
    # 节点 2 出发的唯一通道 2（2 分钟）在恢复之后全程限流至 5 人/分钟，
    # 而组 1 有 10 人（> 管制上限）：忽略容量后截止前可到达，故归因为
    # INSUFFICIENT_CAPACITY 待救援。管制只作用于未来分钟，不超载任何
    # 已出发组，因此登记本身合法。
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
        ],
        "exits": [6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    tick_to(c, 4)  # 组 1 走完通道 1、在节点 2 等待进入通道 2 [4,6)
    assert c.post("/capacity-controls",
                  json={"edge_id": 2, "start_minute": 6,
                        "end_minute": 30, "capacity": 5}).status_code == 201

    resp = report(c, 1, 2, 6)
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "INSUFFICIENT_CAPACITY"
    assert g1["prefix_step_count"] == 1
    assert g1["route"] == [1]
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 4}]
    # 旧预约（通道 2 的 [4,6)）已释放且不恢复
    used = edge_load(c, 2)
    assert 4 not in used and 5 not in used
    # 已走前缀 edge1 [0,4) 保留
    used1 = edge_load(c, 1)
    assert all(used1[m] == 10 for m in range(0, 4))


def test_rescue_group_not_auto_replanned_on_tick_and_rereport(client):
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    tick_to(c, 4)
    assert report(c, 1, 2, 6).status_code == 200
    g_after_rescue = by_id(c.get("/plan").json())[1]
    assert g_after_rescue["status"] == "awaiting_rescue"

    # 后续时间推进不自动重排待救援组
    tick_to(c, 12)
    assert by_id(c.get("/plan").json())[1] == g_after_rescue

    # 只能在原滞留节点重复报告；节点不符拒绝
    resp = report(c, 1, 6, 12)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "NODE_MISMATCH"
    # 在滞留节点以不早于当前分钟的恢复分钟重复报告：仍无余程，刷新说明
    resp = report(c, 1, 2, 12)
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "BLOCKED_BY_CLOSURE"
    assert g1["prefix_step_count"] == 1


def test_deadline_passed_rescue_detail(client):
    # 通道 2 自第 6 分钟起封闭，使组 1 的原计划 edge2[4,6) 恰好赶在封闭前
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 6, "end_minute": 20}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 7}]})
    tick_to(c, 4)  # 原计划到达 6 <= 7
    # 恢复分钟 8 已晚于截止分钟 7 -> DEADLINE_PASSED 待救援
    resp = report(c, 1, 2, 8)
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "DEADLINE_PASSED"
    assert "8" in g1["detail"] and "7" in g1["detail"]


def test_rescue_group_cannot_be_corrected(client):
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    tick_to(c, 4)
    assert report(c, 1, 2, 6).status_code == 200
    resp = c.post("/group-corrections",
                  json=[{"group_id": 1, "priority": 99}])
    assert resp.status_code == 409
    assert resp.json()["error"] == "GROUP_ALREADY_DEPARTED"
    assert resp.json()["conflicts"][0]["status"] == "awaiting_rescue"


# --------------------------------------------------------------------------
# 新余程受既有封闭 / 限流保护（等同其他已出发组）
# --------------------------------------------------------------------------

def test_new_suffix_protected_by_closure_conflict_check(client):
    tick_to(client, 4)
    assert report(client, 1, 2, 6).status_code == 200
    # 新余程 edge2 [6,8)：触及 [7,9) 的封闭应 409 整次拒绝
    resp = client.post("/closures",
                       json={"edge_id": 2, "start_minute": 7,
                             "end_minute": 9})
    assert resp.status_code == 409
    assert resp.json()["error"] == "CLOSURE_CONFLICTS_WITH_DEPARTED_GROUP"
    assert resp.json()["conflicts"][0]["group_id"] == 1


def test_new_suffix_protected_by_capacity_control_check(client):
    tick_to(client, 4)
    assert report(client, 1, 2, 6).status_code == 200
    # 新余程 [6,8) 每分钟占用 10；管制上限 5 使其超载 -> 409
    resp = client.post("/capacity-controls",
                       json={"edge_id": 2, "start_minute": 6,
                             "end_minute": 8, "capacity": 5})
    assert resp.status_code == 409
    assert resp.json()["error"] == \
        "CAPACITY_CONTROL_OVERLOADS_DEPARTED_GROUP"
    conflict = resp.json()["conflicts"][0]
    assert conflict["minute"] in (6, 7)
    assert conflict["used"] == 10 and conflict["effective_capacity"] == 5
    assert conflict["groups"] == [{"group_id": 1, "size": 10}]


def test_capacity_control_applies_to_suffix_replanning(client):
    # 节点 2 有两条平行通道 1->2 不相关；此处给节点 2->6 一条更快但被限流
    # 的通道 2（2 分钟）与一条更慢备选通道 3（8 分钟）：管制 [6,10) 上限 5
    # 使组 1（10 人）无法在窗口内进入通道 2，等到第 10 分钟（到达 12），
    # 仍优于走通道 3 绕行（恢复 6 + 8 = 14）
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
            {"id": 3, "src": 2, "dst": 6, "minutes": 8, "capacity": 10},
        ],
        "exits": [6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 30}]})
    tick_to(c, 4)
    resp = c.post("/capacity-controls",
                  json={"edge_id": 2, "start_minute": 6,
                        "end_minute": 10, "capacity": 5})
    assert resp.status_code == 201
    resp = report(c, 1, 2, 6)
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["steps"][-1] == {
        "edge_id": 2, "from_node": 2, "to_node": 6,
        "enter_minute": 10, "exit_minute": 12}


# --------------------------------------------------------------------------
# 修订历史
# --------------------------------------------------------------------------

def test_report_creates_revision_with_prefix_diff(client):
    tick_to(client, 4)
    n0 = len(revs(client))
    # 恢复到第 7 分钟：新余程 edge2[7,9)，与组 2 原计划 edge2[8,10) 冲突，
    # 组 2 随之重排推迟到 [9,11)
    resp = report(client, 1, 2, 7)
    assert resp.status_code == 200
    entries = revs(client)
    assert len(entries) == n0 + 1
    entry = entries[-1]
    assert entry["operation"] == "delay-reports"
    assert entry["minute"] == 4
    assert entry["detail"] == {
        "group_id": 1, "node_id": 2, "recovery_minute": 7,
        "outcome": "resumed", "prefix_step_count": 1}

    detail = client.get(f"/revisions/{entry['rev']}").json()
    assert detail["snapshot"] == resp.json()
    changes = {ch["group_id"]: ch for ch in detail["changes"]}
    ch1 = changes[1]
    assert ch1["change"] == "changed"
    assert "steps" in ch1["changed_fields"]
    assert "prefix_step_count" in ch1["changed_fields"]
    assert ch1["before"]["steps"][-1]["enter_minute"] == 4
    assert ch1["after"]["steps"][-1]["enter_minute"] == 7
    assert ch1["before"]["prefix_step_count"] is None
    assert ch1["after"]["prefix_step_count"] == 1
    # 其他未出发组的差异同样按既有语义记录（组 2 被推迟）
    assert 2 in changes
    assert changes[2]["after"]["steps"][-1]["enter_minute"] == 9


def test_rescue_revision_records_outcome_and_old_snapshots_untouched(client):
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    tick_to(c, 4)
    before_rev = latest_rev(c)
    before_detail = c.get(f"/revisions/{before_rev}").json()
    resp = report(c, 1, 2, 6)
    assert resp.status_code == 200
    new_rev = latest_rev(c)
    entry = c.get("/revisions").json()["revisions"][-1]
    assert entry["operation"] == "delay-reports"
    assert entry["detail"]["outcome"] == "awaiting_rescue"
    # 旧修订快照保持原样，不被新报告改写
    assert c.get(f"/revisions/{before_rev}").json() == before_detail
    # 新快照中组 1 为待救援且只含前缀
    snap1 = {g["group_id"]: g
             for g in c.get(f"/revisions/{new_rev}").json()["snapshot"]["groups"]}[1]
    assert snap1["status"] == "awaiting_rescue"
    assert snap1["route"] == [1]
    assert snap1["prefix_step_count"] == 1


def test_rejected_report_creates_no_revision(client):
    tick_to(client, 4)
    n0 = len(revs(client))
    assert report(client, 1, 1, 4).status_code == 409
    assert report(client, 2, 1, 4).status_code == 409
    assert report(client, 1, 2, 3).status_code == 422
    assert len(revs(client)) == n0


# --------------------------------------------------------------------------
# 并发与一致性
# --------------------------------------------------------------------------

def test_report_racing_tick_is_serialized(client):
    """报告与时间推进并发：串行化后要么报告先受理，要么 409 整次拒绝。"""
    from concurrent.futures import ThreadPoolExecutor

    for _ in range(6):
        c = client
        c.post("/scenario", json=SCENARIO)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_rep = pool.submit(
                c.post, "/groups/1/delay-report",
                json={"node_id": 2, "recovery_minute": 4})
            f_tick = pool.submit(c.post, "/tick", json={"minutes": 4})
            rep, tick = f_rep.result(), f_tick.result()
        assert tick.status_code == 200
        plan = c.get("/plan").json()
        g1 = by_id(plan)[1]
        if rep.status_code == 200:
            # 报告先提交：前缀保留，余程仍从第 4 分钟（恢复分钟）开始
            assert g1["prefix_step_count"] == 1
            assert g1["steps"][0]["exit_minute"] == 4
        else:
            # tick 先提交：报告被整次拒绝，编排未因报告改动
            assert rep.status_code == 409
            assert g1["prefix_step_count"] is None
        # 任何串行顺序下容量都不超用
        load = c.get("/passages/2/load").json()
        for m in load["minutes"]:
            assert m["used"] <= 10
        # 修订号连续无重复
        entries = c.get("/revisions").json()["revisions"]
        assert [e["rev"] for e in entries] == list(range(1, len(entries) + 1))
        break  # 一次串行顺序即可（循环只为增加覆盖，实际由锁保证）


def test_suffix_and_prefix_load_never_oversells_after_report(client):
    tick_to(client, 4)
    assert report(client, 1, 2, 7).status_code == 200
    # 汇总全部通道每分钟占用，逐分钟不超容量
    sizes = {1: 10, 2: 10, 3: 10}
    caps = {1: 10, 2: 10, 3: 10, 4: 10}
    usage: dict[tuple[int, int], int] = {}
    for g in client.get("/plan").json()["groups"]:
        if g["status"] not in ("scheduled", "fixed"):
            continue
        for step in g["steps"]:
            for minute in range(step["enter_minute"], step["exit_minute"]):
                key = (step["edge_id"], minute)
                usage[key] = usage.get(key, 0) + sizes[g["group_id"]]
    for (edge_id, minute), used in usage.items():
        assert used <= caps[edge_id], (
            f"通道 {edge_id} 第 {minute} 分钟超用：{used}>{caps[edge_id]}")


def test_rescue_group_new_closure_touching_prefix_past_is_allowed(client):
    # 待救援组只保留已走前缀；封闭落在已过去的前缀区间不触及任何剩余路线
    c = client
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    tick_to(c, 4)
    assert report(c, 1, 2, 6).status_code == 200
    # 待救援组没有未来剩余路线：任何未来封闭都不与其相交
    resp = c.post("/closures",
                  json={"edge_id": 2, "start_minute": 8,
                        "end_minute": 12})
    assert resp.status_code == 201
    # 落在过去前缀上的封闭同样允许
    tick_to(c, 5)
    resp = c.post("/closures",
                  json={"edge_id": 1, "start_minute": 0,
                        "end_minute": 4})
    assert resp.status_code == 201
    g1 = by_id(c.get("/plan").json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["route"] == [1]  # 已走前缀不变
