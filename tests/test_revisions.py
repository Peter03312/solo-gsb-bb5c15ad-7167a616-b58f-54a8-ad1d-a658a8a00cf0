"""修订历史（编排变更追溯）集成测试。

覆盖：
* 四类成功变更各产生一条全局递增修订，被拒绝的请求不产生修订；
* 按修订号查询返回当时快照与各组前后差异（新增/移除/状态、出发与
  到达分钟、路线、失败原因），未知修订号 404；
* 场景重置与服务重启后旧修订仍可查；
* 历史快照按落库内容原样返回，不按当前场景重算；
* 并发变更按实际提交顺序连续编号，修订与编排变更一次提交。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 与 tests/test_api.py 同进程运行时 app.main 只会加载一次（以先加载者
# 的环境变量为准）；此处设置保证本文件可独立运行。断言均不依赖修订号
# 的绝对取值，共享数据库时同样成立。
_tmpdir = tempfile.mkdtemp(prefix="evac-test-revisions-")
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


def revs(c):
    return c.get("/revisions").json()["revisions"]


def latest_rev(c):
    return revs(c)[-1]["rev"]


def changes_by_id(detail):
    return {ch["group_id"]: ch for ch in detail["changes"]}


def groups_by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


# --------------------------------------------------------------------------
# 修订的产生与编号
# --------------------------------------------------------------------------

def test_each_successful_change_creates_exactly_one_revision(client):
    n0 = len(revs(client))
    assert client.post("/groups", json=[
        {"id": 9, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 15, "deadline": 20}]).status_code == 201
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    assert client.post("/closures", json={
        "edge_id": 2, "start_minute": 25, "end_minute": 30}
        ).status_code == 201
    assert client.post("/scenario", json=SCENARIO).status_code == 200

    entries = revs(client)
    assert len(entries) == n0 + 4
    tail = entries[-4:]
    assert [e["operation"] for e in tail] == [
        "groups", "tick", "closures", "scenario"]
    # 修订号全局递增、连续、按提交顺序
    new_revs = [e["rev"] for e in tail]
    assert new_revs == list(range(new_revs[0], new_revs[0] + 4))
    # 触发操作的参数摘要随修订保存
    assert tail[0]["detail"]["groups"][0]["id"] == 9
    assert tail[1]["detail"] == {"minutes": 1}
    assert tail[2]["detail"]["edge_id"] == 2
    assert tail[3]["detail"]["groups"] == 3


def test_rejected_requests_create_no_revision(client):
    n0 = len(revs(client))
    # 重复组编号 -> 409
    assert client.post("/groups", json=[
        {"id": 1, "size": 1, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 5}]).status_code == 409
    # 封闭不存在的通道 -> 422
    assert client.post("/closures", json={
        "edge_id": 999, "start_minute": 0, "end_minute": 5}
        ).status_code == 422
    # 起点非法的新组 -> 422
    assert client.post("/groups", json=[
        {"id": 77, "size": 1, "origin": 999, "earliest_departure": 0,
         "priority": 1, "deadline": 5}]).status_code == 422
    assert len(revs(client)) == n0

    # 与已出发组剩余路线相交的封闭 -> 409，同样不产生修订
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    n1 = len(revs(client))
    assert n1 == n0 + 1  # tick 本身成功，产生一条
    resp = client.post("/closures", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 409
    assert len(revs(client)) == n1


def test_revision_snapshot_matches_mutation_response(client):
    """变更响应、修订快照、随后 /plan 三者一致（一次提交，无中间态）。"""
    resp = client.post("/tick", json={"minutes": 1})
    plan = resp.json()
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["snapshot"] == plan
    assert client.get("/plan").json() == plan


# --------------------------------------------------------------------------
# 按修订号查询：快照与前后差异
# --------------------------------------------------------------------------

def test_revision_detail_added_group(client):
    client.post("/groups", json=[
        {"id": 9, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 15, "deadline": 20}])
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["operation"] == "groups"
    assert detail["minute"] == 0
    assert detail["snapshot"]["current_minute"] == 0
    by_id = changes_by_id(detail)
    assert by_id[9]["change"] == "added"
    assert by_id[9]["before"] is None
    assert by_id[9]["after"]["status"] == "scheduled"
    assert by_id[9]["after"]["route"] == [5]


def test_revision_detail_failed_group_keeps_reason(client):
    client.post("/groups", json=[
        {"id": 99, "size": 200, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 10}])
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    by_id = changes_by_id(detail)
    assert by_id[99]["change"] == "added"
    assert by_id[99]["after"]["status"] == "failed"
    assert by_id[99]["after"]["reason"] == "INSUFFICIENT_CAPACITY"


def test_tick_revision_records_status_change_and_fixed_steps(client):
    client.post("/tick", json={"minutes": 1})
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["operation"] == "tick"
    assert detail["minute"] == 1
    assert detail["snapshot"]["current_minute"] == 1
    by_id = changes_by_id(detail)
    ch3 = by_id[3]
    assert ch3["change"] == "changed"
    assert "status" in ch3["changed_fields"]
    assert ch3["before"]["status"] == "scheduled"
    assert ch3["after"]["status"] == "fixed"
    # 历史快照保留当时固定组的逐段通行时刻
    assert ch3["after"]["steps"] == [
        {"edge_id": 5, "from_node": 2, "to_node": 6,
         "enter_minute": 0, "exit_minute": 2}]
    snap3 = groups_by_id(detail["snapshot"])[3]
    assert snap3["status"] == "fixed"
    assert snap3["steps"][0]["enter_minute"] == 0
    assert snap3["steps"][0]["exit_minute"] == 2


def test_closure_revision_records_route_change(client):
    # 通道 5 全时段封闭：组 3 改道 [2,3]，组 1 改道 [1,2,3]
    client.post("/closures", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 30})
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["operation"] == "closures"
    by_id = changes_by_id(detail)
    ch3 = by_id[3]
    assert ch3["change"] == "changed"
    assert "route" in ch3["changed_fields"]
    assert ch3["before"]["route"] == [5]
    assert ch3["after"]["route"] == [2, 3]
    assert ch3["before"]["departure_minute"] == 0
    assert ch3["after"]["arrival_minute"] == 5
    ch1 = by_id[1]
    assert ch1["before"]["route"] == [1, 5]
    assert ch1["after"]["route"] == [1, 2, 3]


def test_scenario_reset_diff_shows_removed_and_added(client):
    resp = client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 10}],
        "exits": [2],
        "closures": [],
        "groups": [
            {"id": 50, "size": 5, "origin": 1, "earliest_departure": 0,
             "priority": 1, "deadline": 10}],
    })
    assert resp.status_code == 200
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["operation"] == "scenario"
    assert detail["minute"] == 0
    by_id = changes_by_id(detail)
    assert by_id[1]["change"] == "removed"
    assert by_id[1]["after"] is None
    assert by_id[1]["before"]["status"] == "scheduled"
    assert by_id[3]["change"] == "removed"
    assert by_id[50]["change"] == "added"
    assert by_id[50]["after"]["status"] == "scheduled"
    assert by_id[50]["after"]["arrival_minute"] == 2


def test_unknown_revision_returns_404(client):
    resp = client.get("/revisions/99999999")
    assert resp.status_code == 404
    assert "99999999" in resp.json()["detail"]


# --------------------------------------------------------------------------
# 持久性与历史不可变
# --------------------------------------------------------------------------

def test_revisions_survive_scenario_reset(client):
    old_rev = latest_rev(client)
    old_detail = client.get(f"/revisions/{old_rev}").json()
    old_listing = revs(client)
    # 重置为完全不同的场景
    assert client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 10}],
        "exits": [2], "closures": [], "groups": []}).status_code == 200
    # 旧修订全部保留，编号继续递增
    entries = revs(client)
    assert entries[-1]["rev"] == old_rev + 1
    assert [e["rev"] for e in entries[:-1]] == [
        e["rev"] for e in old_listing]
    # 旧修订详情逐字节不变（仍是旧场景的快照）
    assert client.get(f"/revisions/{old_rev}").json() == old_detail


def test_historical_snapshot_not_recomputed(client):
    rev = latest_rev(client)  # fixture 的场景重置修订
    snap0 = client.get(f"/revisions/{rev}").json()["snapshot"]
    # 之后做一系列变更：推进时间使组 3 固定、再加新组
    client.post("/tick", json={"minutes": 5})
    client.post("/groups", json=[
        {"id": 9, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 15, "deadline": 20}])
    detail = client.get(f"/revisions/{rev}").json()
    # 快照保持当时状态：分钟 0、组 3 仍为 scheduled，而非现在的 fixed
    assert detail["snapshot"] == snap0
    assert detail["snapshot"]["current_minute"] == 0
    g3 = groups_by_id(detail["snapshot"])[3]
    assert g3["status"] == "scheduled"
    assert g3["steps"] == [
        {"edge_id": 5, "from_node": 2, "to_node": 6,
         "enter_minute": 0, "exit_minute": 2}]


def test_revisions_survive_restart():
    """服务重启（重开同一数据库文件）后修订历史完整可查。"""
    with TestClient(app) as c1:
        assert c1.post("/scenario", json=SCENARIO).status_code == 200
        assert c1.post("/tick", json={"minutes": 2}).status_code == 200
        entries = revs(c1)
        rev = entries[-1]["rev"]
        detail = c1.get(f"/revisions/{rev}").json()
    with TestClient(app) as c2:
        assert revs(c2) == entries
        assert c2.get(f"/revisions/{rev}").json() == detail
        # 重启后新变更的修订号继续递增，不回退
        assert c2.post("/tick", json={"minutes": 1}).status_code == 200
        assert revs(c2)[-1]["rev"] == rev + 1


# --------------------------------------------------------------------------
# 并发编号
# --------------------------------------------------------------------------

def test_concurrent_changes_numbered_in_commit_order(client):
    """并发变更：每个成功请求恰好一条修订，编号连续无重复无空洞。"""
    from concurrent.futures import ThreadPoolExecutor

    n0 = len(revs(client))
    payloads = [
        [{"id": gid, "size": 5, "origin": 2, "earliest_departure": 0,
          "priority": 1, "deadline": 60}]
        for gid in (20, 21, 22, 23, 24)]

    def post(payload):
        return client.post("/groups", json=payload)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(post, payloads))
    assert all(r.status_code == 201 for r in responses)

    entries = revs(client)
    assert len(entries) == n0 + 5
    new = entries[n0:]
    assert [e["operation"] for e in new] == ["groups"] * 5
    new_revs = [e["rev"] for e in new]
    assert new_revs == list(range(new_revs[0], new_revs[0] + 5))
    # 五条修订各自对应一个新增组，合起来正好覆盖全部并发请求
    added = {e["detail"]["groups"][0]["id"] for e in new}
    assert added == {20, 21, 22, 23, 24}
    for e in new:
        assert client.get(f"/revisions/{e['rev']}").status_code == 200
