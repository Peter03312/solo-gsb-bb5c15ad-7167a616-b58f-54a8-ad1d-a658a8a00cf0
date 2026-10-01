"""人群组纠正清单（批量纠正未出发组属性）集成测试。

覆盖：
* 纠正成功：整批按最终值一次生效并整体重排未出发组，未指定字段沿用
  原值，已出发组路线与占用保持固定；此前编排失败的组同样可纠正；
* 空清单 / 无字段项 / 同批编号重复 / 未知组 / 最终字段不满足既有组
  校验 / 起点非场馆节点 -> 422，整批拒绝且状态与修订历史不变；
* 目标组已出发（含并发时间推进所致）-> 409 GROUP_ALREADY_DEPARTED，
  整批拒绝；
* 成功产生一条 group-corrections 修订：detail 逐项记录纠正前后字段，
  快照与逐组差异沿用既有 /revisions 查询语义，修订号连续递增。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 与其他测试文件同进程运行时 app.main 只加载一次（以先加载者的环境变量
# 为准）；此处设置保证本文件可独立运行。断言均不依赖修订号绝对取值。
_tmpdir = tempfile.mkdtemp(prefix="evac-test-corrections-")
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


def revs(c):
    return c.get("/revisions").json()["revisions"]


def latest_rev(c):
    return revs(c)[-1]["rev"]


# --------------------------------------------------------------------------
# 纠正成功：最终值生效、整体重排
# --------------------------------------------------------------------------

def test_correction_applies_final_values_and_replans(client):
    # 纠正组 4 的起点 7 -> 2，其余字段沿用原值：改经通道 5 到出口 6
    resp = client.post("/group-corrections",
                       json=[{"group_id": 4, "origin": 2}])
    assert resp.status_code == 200
    groups = by_id(resp.json())
    g4 = groups[4]
    assert g4["status"] == "scheduled"
    assert g4["exit_id"] == 6
    assert g4["route"] == [5]
    assert g4["departure_minute"] == 0
    assert g4["arrival_minute"] == 2
    # 其他未出发组随之整体重排（结果不变），响应即完整编排
    assert groups[1]["route"] == [1, 5]
    assert groups[3]["route"] == [5]
    assert resp.json() == client.get("/plan").json()


def test_correction_size_makes_group_unschedulable(client):
    # 组 1 人数 30 -> 45：超通道 1（30）与通道 5（40）容量，编排失败
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "size": 45}])
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "failed"
    assert g1["reason"] == "INSUFFICIENT_CAPACITY"


def test_correction_of_deadline_tightens_and_relaxes(client):
    # 截止 30 -> 12：封闭 [3,10) 使通道 1 最早第 10 分钟进入、到达 14，
    # 超过截止 -> 失败；再放宽回 30 -> 恢复原编排
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "deadline": 12}])
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "failed"
    assert g1["reason"] == "BLOCKED_BY_CLOSURE"

    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "deadline": 30}])
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "scheduled"
    assert g1["route"] == [1, 5]
    assert g1["arrival_minute"] == 16


def test_failed_group_can_be_corrected(client):
    # 此前编排失败的组同样可纠正：组 9 人数 200 超容量失败，
    # 纠正人数与截止分钟后可安排
    client.post("/groups", json=[
        {"id": 9, "size": 200, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 10}])
    assert by_id(client.get("/plan").json())[9]["status"] == "failed"
    resp = client.post("/group-corrections",
                       json=[{"group_id": 9, "size": 10, "deadline": 30}])
    assert resp.status_code == 200
    g9 = by_id(resp.json())[9]
    assert g9["status"] == "scheduled"
    assert g9["route"] == [1, 5]


def test_priority_correction_changes_planning_order(client):
    # 单通道容量 20，两组各 20 人：优先级高者先走
    client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 2, "minutes": 2,
                      "capacity": 20}],
        "exits": [2], "closures": [],
        "groups": [
            {"id": 1, "size": 20, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 10},
            {"id": 2, "size": 20, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 10},
        ]})
    groups = by_id(client.get("/plan").json())
    assert groups[2]["departure_minute"] == 0
    assert groups[1]["departure_minute"] == 2
    # 把组 1 的优先级纠正为最高：重排后组 1 先占用容量
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "priority": 15}])
    assert resp.status_code == 200
    groups = by_id(resp.json())
    assert groups[1]["departure_minute"] == 0
    assert groups[1]["arrival_minute"] == 2
    assert groups[2]["departure_minute"] == 2
    assert groups[2]["arrival_minute"] == 4


def test_correction_keeps_departed_groups_fixed(client):
    # 推进 1 分钟：组 3、组 4（均 t=0 出发）固定；纠正组 1 不影响其路线
    client.post("/tick", json={"minutes": 1})
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "priority": 30}])
    assert resp.status_code == 200
    groups = by_id(resp.json())
    assert groups[3]["status"] == "fixed"
    assert groups[3]["steps"] == [
        {"edge_id": 5, "from_node": 2, "to_node": 6,
         "enter_minute": 0, "exit_minute": 2}]
    assert groups[4]["status"] == "fixed"
    assert groups[4]["route"] == [7]
    # 组 1 按新优先级重排，仍走 [1,5]
    assert groups[1]["status"] == "scheduled"
    assert groups[1]["route"] == [1, 5]


# --------------------------------------------------------------------------
# 整批拒绝：请求无效（422），状态与修订历史不变
# --------------------------------------------------------------------------

def test_empty_correction_list_rejected(client):
    assert client.post("/group-corrections", json=[]).status_code == 422


def test_item_without_any_field_rejected(client):
    resp = client.post("/group-corrections", json=[{"group_id": 1}])
    assert resp.status_code == 422


def test_duplicate_group_ids_in_batch_rejected(client):
    resp = client.post("/group-corrections", json=[
        {"group_id": 1, "size": 10},
        {"group_id": 1, "priority": 3}])
    assert resp.status_code == 422


def test_unknown_group_rejected(client):
    resp = client.post("/group-corrections",
                       json=[{"group_id": 777, "size": 5}])
    assert resp.status_code == 422


def test_invalid_final_fields_rejected(client):
    # 只纠正最早出发为 35：最终 截止30 < 最早出发35，不满足既有组校验
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "earliest_departure": 35}])
    assert resp.status_code == 422
    # 非正人数 / 负优先级 / 负分钟（schema 校验）
    assert client.post("/group-corrections",
                       json=[{"group_id": 1, "size": 0}]).status_code == 422
    assert client.post("/group-corrections",
                       json=[{"group_id": 1, "priority": -1}]
                       ).status_code == 422
    assert client.post("/group-corrections",
                       json=[{"group_id": 1, "deadline": -1}]
                       ).status_code == 422


def test_origin_must_be_venue_node(client):
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "origin": 999}])
    assert resp.status_code == 422


def test_invalid_batch_leaves_state_and_revisions_unchanged(client):
    n0 = len(revs(client))
    plan0 = client.get("/plan").json()
    # 第一项有效、第二项无效 -> 整批拒绝，第一项也不生效
    resp = client.post("/group-corrections", json=[
        {"group_id": 4, "origin": 2},
        {"group_id": 1, "earliest_departure": 35}])
    assert resp.status_code == 422
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


# --------------------------------------------------------------------------
# 整批拒绝：目标组已出发（409）
# --------------------------------------------------------------------------

def test_departed_group_correction_rejected_409(client):
    client.post("/tick", json={"minutes": 1})  # 组 3（t=0 出发）固定
    n0 = len(revs(client))
    plan0 = client.get("/plan").json()
    resp = client.post("/group-corrections",
                       json=[{"group_id": 3, "size": 5}])
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "GROUP_ALREADY_DEPARTED"
    assert body["conflicts"][0]["group_id"] == 3
    assert body["conflicts"][0]["status"] == "fixed"
    assert body["conflicts"][0]["departure_minute"] == 0
    # 状态与修订历史不变
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_batch_with_one_departed_group_fully_rejected(client):
    client.post("/tick", json={"minutes": 1})
    n0 = len(revs(client))
    plan0 = client.get("/plan").json()
    resp = client.post("/group-corrections", json=[
        {"group_id": 1, "priority": 30},  # 未出发，本可有效
        {"group_id": 3, "size": 5}])      # 已出发 -> 整批拒绝
    assert resp.status_code == 409
    assert resp.json()["error"] == "GROUP_ALREADY_DEPARTED"
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_correction_racing_tick_is_serialized(client):
    """纠正与时间推进并发：串行化后要么纠正先生效，要么 409 整批拒绝。"""
    from concurrent.futures import ThreadPoolExecutor

    n0 = len(revs(client))
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_corr = pool.submit(client.post, "/group-corrections",
                             json=[{"group_id": 3, "size": 12}])
        f_tick = pool.submit(client.post, "/tick", json={"minutes": 1})
        corr, tick = f_corr.result(), f_tick.result()
    assert tick.status_code == 200
    g3 = by_id(client.get("/plan").json())[3]
    assert g3["status"] == "fixed"  # 两种次序下组 3 最终都已出发
    if corr.status_code == 200:
        # 纠正先提交：组 3 以新人数重排，随后 tick 使其固定
        assert len(revs(client)) == n0 + 2
    else:
        # tick 先提交：组 3 已出发，纠正整批拒绝且不产生修订
        assert corr.status_code == 409
        assert corr.json()["error"] == "GROUP_ALREADY_DEPARTED"
        assert len(revs(client)) == n0 + 1


# --------------------------------------------------------------------------
# 修订历史：纠正前后字段与编排差异
# --------------------------------------------------------------------------

def test_correction_revision_records_field_and_plan_diffs(client):
    resp = client.post("/group-corrections", json=[
        {"group_id": 3, "size": 12},
        {"group_id": 4, "origin": 2, "priority": 25},
    ])
    assert resp.status_code == 200
    plan = resp.json()

    entries = revs(client)
    assert entries[-1]["operation"] == "group-corrections"
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["operation"] == "group-corrections"
    # 快照与响应、随后 /plan 一致（同一事务提交）
    assert detail["snapshot"] == plan
    assert client.get("/plan").json() == plan

    # detail 逐项记录纠正前后字段，未指定字段沿用原值
    corr = {c["group_id"]: c for c in detail["detail"]["corrections"]}
    assert corr[3]["before"] == {"size": 10, "origin": 2,
                                 "earliest_departure": 0, "priority": 20,
                                 "deadline": 20}
    assert corr[3]["after"]["size"] == 12
    assert corr[3]["after"]["priority"] == 20  # 未指定，沿用原值
    assert corr[3]["changed_fields"] == ["size"]
    assert corr[4]["before"]["origin"] == 7
    assert corr[4]["after"]["origin"] == 2
    assert corr[4]["after"]["priority"] == 25
    assert set(corr[4]["changed_fields"]) == {"origin", "priority"}

    # 编排差异沿用既有逐组 diff 语义
    changes = {ch["group_id"]: ch for ch in detail["changes"]}
    assert changes[4]["change"] == "changed"
    assert "route" in changes[4]["changed_fields"]
    assert changes[4]["before"]["route"] == [7]
    assert changes[4]["after"]["route"] == [5]


def test_rejected_corrections_create_no_revision(client):
    n0 = len(revs(client))
    assert client.post("/group-corrections", json=[]).status_code == 422
    assert client.post(
        "/group-corrections",
        json=[{"group_id": 1, "origin": 999}]).status_code == 422
    client.post("/tick", json={"minutes": 1})
    n1 = len(revs(client))
    assert n1 == n0 + 1  # 仅 tick 产生一条
    assert client.post(
        "/group-corrections",
        json=[{"group_id": 3, "size": 5}]).status_code == 409
    assert len(revs(client)) == n1
