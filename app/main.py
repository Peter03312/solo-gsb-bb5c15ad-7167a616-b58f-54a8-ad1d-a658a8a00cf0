"""场馆疏散通道编排服务 —— FastAPI 应用。

并发安全
========
所有会改变编排结果的请求（初始化场景 / 新增组 / 纠正组 / 新增封闭 /
登记容量管制 / 推进分钟）由同一把 asyncio 锁串行化，且每个变更在单个
SQLite 事务内整体提交：
* 重排未出发组时，先删除这些组的全部旧路线再整体写入新路线，
  不会出现只重排了一部分的中间状态；
* 容量校验基于"已固定组占用 + 本轮已排入组占用"，锁内逐组累加，
  并发变更不可能超用容量；
* 新增封闭若与任一已出发（fixed）组剩余路线相交，直接返回 409，
  数据原样保留、不触发任何重排；
* 登记容量管制若与同通道已有管制重叠，或新上限使已出发组剩余路线在
  任一分钟超载，同样整次拒绝（409），不改动任何状态；
* 纠正人群组时任一项无效（未知组/最终字段不合法/起点非场馆节点），
  或目标组在受理前已因时间推进而出发，整批拒绝（422/409），
  状态与修订历史不变。

修订历史
========
上述六类变更成功提交时，在同一事务内追加一条修订记录：全局递增修订号、
触发操作、提交后的当前分钟、提交前后完整编排快照（纠正类修订另在
detail 中逐项记录纠正前后的字段值）。被拒绝的请求（409/422）不产生
修订。修订记录不随场景重置或服务重启丢失，历史快照按落库内容
原样返回，不按当前场景重算。
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import db
from .planner import effective_capacity, plan_group
from .schemas import (CapacityControlIn, CapacityControlListOut,
                      CapacityControlOut, ClosureIn, ConflictOut,
                      GroupChangeOut, GroupCorrectionIn, GroupIn,
                      GroupPlanOut, PlanResponse, RevisionDetailOut,
                      RevisionListOut, RevisionSummaryOut, ScenarioIn,
                      StepOut)
from .seed import SEED_SCENARIO

DB_PATH = os.getenv("EVAC_DB_PATH", "/data/evacuation.db")
SEED_ON_START = os.getenv("EVAC_SEED", "true").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH, isolation_level=None)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await db.init_db(conn)
    app.state.db = conn
    app.state.lock = asyncio.Lock()

    async with app.state.lock:
        async with conn.execute("SELECT COUNT(*) FROM edges") as cur:
            edge_count = (await cur.fetchone())[0]
        if SEED_ON_START and edge_count == 0:
            await _load_scenario(
                conn, ScenarioIn.model_validate(SEED_SCENARIO))

    yield
    await conn.close()


app = FastAPI(
    title="场馆疏散通道编排服务",
    version="1.1.0",
    description="按优先级逐组编排截止前最早到达的疏散路线，"
                "支持封闭时段、容量管制时段、每分钟容量约束与增量重排。",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# 内部编排逻辑（均在持锁状态下调用）
# --------------------------------------------------------------------------

async def _load_scenario(conn: aiosqlite.Connection,
                         scenario: ScenarioIn) -> PlanResponse:
    """全量写入一个场景，随后立即编排（编排与修订记录整体为一个事务）。"""
    edge_ids = [p.id for p in scenario.passages]
    if len(edge_ids) != len(set(edge_ids)):
        raise HTTPException(422, "通道编号重复")
    group_ids = [g.id for g in scenario.groups]
    if len(group_ids) != len(set(group_ids)):
        raise HTTPException(422, "人群组编号重复")
    if len(scenario.exits) != len(set(scenario.exits)):
        raise HTTPException(422, "出口节点编号重复")

    nodes = {n for p in scenario.passages for n in (p.src, p.dst)}
    nodes.update(scenario.exits)
    for g in scenario.groups:
        nodes.add(g.origin)
    for c in scenario.closures:
        if c.edge_id not in edge_ids:
            raise HTTPException(422, f"封闭时段引用了不存在的通道 {c.edge_id}")
    for g in scenario.groups:
        if g.origin not in nodes:
            raise HTTPException(422, f"组 {g.id} 的起点 {g.origin} 不在场馆节点中")

    detail = {"passages": len(scenario.passages),
              "exits": list(scenario.exits),
              "closures": len(scenario.closures),
              "groups": len(scenario.groups)}
    before = await _build_plan_response(conn)
    async with db.transaction(conn):
        await db.reset_db(conn)
        for p in scenario.passages:
            await conn.execute(
                "INSERT INTO edges(id, src, dst, minutes, capacity) VALUES(?,?,?,?,?)",
                (p.id, p.src, p.dst, p.minutes, p.capacity))
        for node in scenario.exits:
            await conn.execute(
                "INSERT INTO exits(node_id) VALUES(?)", (node,))
        for c in scenario.closures:
            await conn.execute(
                "INSERT INTO closures(edge_id, start_minute, end_minute) VALUES(?,?,?)",
                (c.edge_id, c.start_minute, c.end_minute))
        for g in scenario.groups:
            await conn.execute(
                """INSERT INTO groups
                   (id, size, origin, earliest_departure, priority, deadline, status)
                   VALUES(?,?,?,?,?,?,'pending')""",
                (g.id, g.size, g.origin, g.earliest_departure,
                 g.priority, g.deadline))
        await _replan_pending(conn, in_transaction=True)
        after = await _build_plan_response(conn)
        await db.insert_revision(conn, "scenario", detail,
                                 _plan_dump(before), _plan_dump(after))
    return after


async def _replan_pending(conn: aiosqlite.Connection,
                          in_transaction: bool = False) -> None:
    """重排全部未出发组（scheduled/failed/pending），整体替换。

    读阶段在事务外完成（SQLite autocommit 下读到的是最近一致快照），
    写阶段整体置于一个事务中，杜绝部分重排。
    """
    minute_now = await db.get_current_minute(conn)
    world, pending = await db.load_world(conn)

    node_set: set[int] = set()
    for e in world.edges.values():
        node_set.add(e.src)
        node_set.add(e.dst)
    node_set.update(world.exits)
    for g in pending:
        if g["origin"] not in node_set:
            raise HTTPException(422, f"组 {g['id']} 的起点 {g['origin']} 不连通")

    results = []
    # 已按 优先级 DESC, 最早出发 ASC, 组编号 ASC 排序
    for g in pending:
        result = plan_group(
            group_id=g["id"], size=g["size"], origin=g["origin"],
            earliest_departure=g["earliest_departure"],
            deadline=g["deadline"], current_minute=minute_now, world=world)
        results.append(result)
        if result["status"] == "scheduled":
            for (eid, minute), add in result["load_delta"].items():
                key = (eid, minute)
                world.fixed_load[key] = world.fixed_load.get(key, 0) + add

    if in_transaction:
        await db.persist_replan(conn, results, minute_now)
    else:
        async with db.transaction(conn):
            await db.persist_replan(conn, results, minute_now)


async def _closure_conflicts(conn: aiosqlite.Connection, edge_id: int,
                             a: int, b: int):
    """检查 [a,b) 是否触及已出发组在该通道上的剩余路线。"""
    minute_now, fixed_routes = await db.load_fixed_routes(conn)
    conflicts = []
    for gid, eid, enter_m, exit_m in fixed_routes:
        if eid != edge_id:
            continue
        remain_start = max(enter_m, minute_now)
        if remain_start >= exit_m:
            continue  # 该段已完全在过去，无剩余路线
        # 剩余区间 [remain_start, exit_m) 与封闭 [a,b) 相交
        if remain_start < b and a < exit_m:
            conflicts.append(ConflictOut(
                group_id=gid, edge_id=eid, enter_minute=enter_m,
                exit_minute=exit_m, closure=[a, b]))
    return conflicts


async def _capacity_control_conflicts(conn: aiosqlite.Connection,
                                      edge_id: int, a: int, b: int,
                                      cap: int) -> list[dict]:
    """检查新管制上限是否使已出发组在该通道的剩余路线任一分钟超载。

    已出发组的占用永久固定、不参与重排，故新上限必须能容纳其剩余路线
    的逐分钟占用。返回逐分钟冲突列表：冲突分钟、该分钟占用量、新有效
    上限与相关组（未出发组会随管制重排，不在检查之列）。
    """
    minute_now = await db.get_current_minute(conn)
    async with conn.execute(
            "SELECT capacity FROM edges WHERE id=?", (edge_id,)) as cur:
        base_cap = int((await cur.fetchone())[0])
    effective = min(base_cap, cap)
    async with conn.execute(
            """SELECT g.id, g.size, rs.enter_minute, rs.exit_minute
               FROM route_steps rs JOIN groups g ON g.id = rs.group_id
               WHERE g.status='fixed' AND rs.edge_id=?""",
            (edge_id,)) as cur:
        rows = await cur.fetchall()
    per_minute: dict[int, dict] = {}
    for gid, size, enter_m, exit_m in rows:
        # 剩余路线 [max(enter, 当前分钟), exit) 与管制 [a, b) 的相交部分
        for m in range(max(enter_m, minute_now, a), min(exit_m, b)):
            slot = per_minute.setdefault(m, {"used": 0, "groups": []})
            slot["used"] += size
            slot["groups"].append({"group_id": gid, "size": size})
    return [
        {"edge_id": edge_id, "minute": m, "used": slot["used"],
         "effective_capacity": effective, "groups": slot["groups"]}
        for m, slot in sorted(per_minute.items())
        if slot["used"] > effective
    ]


# --------------------------------------------------------------------------
# 响应组装
# --------------------------------------------------------------------------

async def _build_plan_response(conn: aiosqlite.Connection) -> PlanResponse:
    minute_now = await db.get_current_minute(conn)
    async with conn.execute(
            """SELECT id, status, departure_minute, arrival_minute, exit_id,
                      fail_reason, fail_detail
               FROM groups ORDER BY priority DESC,
                    earliest_departure ASC, id ASC""") as cur:
        group_rows = await cur.fetchall()
    out = []
    for gid, status, dep, arr, exit_id, reason, detail in group_rows:
        steps = []
        async with conn.execute(
                """SELECT edge_id, from_node, to_node, enter_minute, exit_minute
                   FROM route_steps WHERE group_id=? ORDER BY seq""", (gid,)) as sc:
            for row in await sc.fetchall():
                steps.append(StepOut(
                    edge_id=row[0], from_node=row[1], to_node=row[2],
                    enter_minute=row[3], exit_minute=row[4]))
        out.append(GroupPlanOut(
            group_id=gid, status=status, exit_id=exit_id,
            departure_minute=dep, arrival_minute=arr,
            route=[s.edge_id for s in steps], steps=steps,
            reason=reason, detail=detail))
    return PlanResponse(current_minute=minute_now, groups=out)


def _plan_dump(plan: PlanResponse) -> dict:
    """序列化编排快照（JSON 兼容 dict），供修订记录落库。"""
    return plan.model_dump(mode="json")


# diff 时逐字段比较的分组编排字段（route 由 steps 派生，仍单独列出以便阅读）
_GROUP_DIFF_FIELDS = ("status", "exit_id", "departure_minute",
                      "arrival_minute", "route", "steps", "reason", "detail")


def _diff_plans(before: PlanResponse,
                after: PlanResponse) -> list[GroupChangeOut]:
    """对比提交前后的完整编排快照，逐组给出新增/移除/字段级差异。

    输入均为修订记录中落库的快照，纯函数计算，不触碰当前场景状态。
    """
    before_by_id = {g.group_id: g for g in before.groups}
    after_by_id = {g.group_id: g for g in after.groups}
    changes: list[GroupChangeOut] = []
    for gid in sorted(set(before_by_id) | set(after_by_id)):
        b = before_by_id.get(gid)
        a = after_by_id.get(gid)
        if b is None:
            changes.append(GroupChangeOut(
                group_id=gid, change="added", before=None, after=a))
        elif a is None:
            changes.append(GroupChangeOut(
                group_id=gid, change="removed", before=b, after=None))
        else:
            fields = [f for f in _GROUP_DIFF_FIELDS
                      if getattr(b, f) != getattr(a, f)]
            if fields:
                changes.append(GroupChangeOut(
                    group_id=gid, change="changed", changed_fields=fields,
                    before=b, after=a))
    return changes


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

@app.get("/health", tags=["运维"])
async def health(request: Request):
    return {"status": "ok",
            "current_minute": await db.get_current_minute(request.app.state.db)}


@app.post("/scenario", tags=["场景"], response_model=PlanResponse,
          status_code=200)
async def replace_scenario(scenario: ScenarioIn, request: Request):
    """全量重置场景（通道/出口/封闭/组），随后立即编排并返回结果。

    成功时产生一条 scenario 修订；历史修订保留、编号继续递增。
    """
    async with request.app.state.lock:
        return await _load_scenario(request.app.state.db, scenario)


@app.post("/groups", tags=["变更"], response_model=PlanResponse,
          status_code=201)
async def add_groups(groups: list[GroupIn], request: Request):
    """新增一个或多个人群组；只对全部未出发组重新编排。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute("SELECT id FROM groups") as cur:
            existing = {row[0] for row in await cur.fetchall()}
        async with conn.execute(
                "SELECT src FROM edges UNION SELECT dst FROM edges") as cur:
            known_nodes = {row[0] for row in await cur.fetchall()}
        async with conn.execute("SELECT node_id FROM exits") as cur:
            known_nodes |= {row[0] for row in await cur.fetchall()}
        for g in groups:
            if g.id in existing:
                raise HTTPException(409, f"人群组编号 {g.id} 已存在")
            if g.origin not in known_nodes:
                raise HTTPException(422,
                    f"组 {g.id} 的起点 {g.origin} 不是场馆节点")
        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            for g in groups:
                await conn.execute(
                    """INSERT INTO groups
                       (id, size, origin, earliest_departure, priority, deadline,
                        status)
                       VALUES(?,?,?,?,?,?,'pending')""",
                    (g.id, g.size, g.origin, g.earliest_departure,
                     g.priority, g.deadline))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(
                conn, "groups",
                {"groups": [g.model_dump() for g in groups]},
                _plan_dump(before), _plan_dump(after))
        return after


# 可被纠正的组字段（纠正项中未指定的字段沿用原值）
_CORRECTABLE_FIELDS = ("size", "origin", "earliest_departure",
                       "priority", "deadline")


@app.post("/group-corrections", tags=["变更"], response_model=PlanResponse)
async def correct_groups(corrections: list[GroupCorrectionIn],
                         request: Request):
    """批量纠正未出发人群组的属性（非空清单，整批原子生效）。

    每项指定一个已有组编号及至少一个待纠正字段（人数/起点/最早出发分钟/
    优先级/截止分钟），未指定字段沿用原值；同批组编号不得重复。
    仅未出发（含此前编排失败）的组可纠正：若并发时间推进已使目标组
    出发，整批拒绝（409）。每项的最终字段须满足与新增组相同的校验、
    起点须为场馆节点，否则整批拒绝（422）。拒绝时不改动任何状态、
    不产生修订。

    全部有效时在同一事务内应用全部最终值、整体重排未出发组（已出发组
    路线与占用保持固定），记录一条含各项纠正前后字段的修订，并返回
    完整编排。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        if not corrections:
            raise HTTPException(422, "纠正清单不能为空")
        ids = [c.group_id for c in corrections]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, "同批纠正清单中的组编号不得重复")

        async with conn.execute(
                """SELECT id, size, origin, earliest_departure, priority,
                          deadline, status, departure_minute, arrival_minute
                   FROM groups""") as cur:
            rows = await cur.fetchall()
        existing = {row[0]: row for row in rows}

        async with conn.execute(
                "SELECT src FROM edges UNION SELECT dst FROM edges") as cur:
            known_nodes = {row[0] for row in await cur.fetchall()}
        async with conn.execute("SELECT node_id FROM exits") as cur:
            known_nodes |= {row[0] for row in await cur.fetchall()}

        # 存在性与"已出发"检查：并发时间推进使目标组在受理前出发时，
        # 这里读到的即是 fixed，整批拒绝
        departed = []
        for c in corrections:
            row = existing.get(c.group_id)
            if row is None:
                raise HTTPException(422, f"人群组 {c.group_id} 不存在")
            if row[6] == "fixed":
                departed.append({"group_id": c.group_id, "status": row[6],
                                 "departure_minute": row[7],
                                 "arrival_minute": row[8]})
        if departed:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "GROUP_ALREADY_DEPARTED",
                    "message": "目标组已出发，不可纠正，整批拒绝",
                    "conflicts": departed,
                })

        # 合并最终值并按既有组校验复核（任一项无效即整批拒绝）
        merged = []
        for c in corrections:
            row = existing[c.group_id]
            before_fields = dict(zip(_CORRECTABLE_FIELDS, row[1:6]))
            after_fields = dict(before_fields)
            for f in _CORRECTABLE_FIELDS:
                value = getattr(c, f)
                if value is not None:
                    after_fields[f] = value
            try:
                GroupIn(id=c.group_id, **after_fields)
            except ValidationError as exc:
                msgs = "；".join(e["msg"] for e in exc.errors())
                raise HTTPException(
                    422, f"组 {c.group_id} 纠正后的字段不满足校验：{msgs}")
            if after_fields["origin"] not in known_nodes:
                raise HTTPException(
                    422, f"组 {c.group_id} 的起点 "
                         f"{after_fields['origin']} 不是场馆节点")
            merged.append({"group_id": c.group_id,
                           "before": before_fields, "after": after_fields})

        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            for m in merged:
                f = m["after"]
                await conn.execute(
                    """UPDATE groups SET size=?, origin=?,
                       earliest_departure=?, priority=?, deadline=?
                       WHERE id=?""",
                    (f["size"], f["origin"], f["earliest_departure"],
                     f["priority"], f["deadline"], m["group_id"]))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(
                conn, "group-corrections",
                {"corrections": [
                    {"group_id": m["group_id"],
                     "changed_fields": [
                         f for f in _CORRECTABLE_FIELDS
                         if m["before"][f] != m["after"][f]],
                     "before": m["before"],
                     "after": m["after"]}
                    for m in merged]},
                _plan_dump(before), _plan_dump(after))
        return after


@app.post("/closures", tags=["变更"], status_code=201)
async def add_closure(closure: ClosureIn, request: Request):
    """新增左闭右开封闭时段。

    若封闭触及任一已出发组尚未走完的路线，整次拒绝（409）且不改任何数据、
    不产生修订；否则登记封闭并重排全部未出发组，同时产生一条修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute("SELECT 1 FROM edges WHERE id=?", (closure.edge_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(422, f"通道 {closure.edge_id} 不存在")

        conflicts = await _closure_conflicts(
            conn, closure.edge_id, closure.start_minute, closure.end_minute)
        if conflicts:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "CLOSURE_CONFLICTS_WITH_DEPARTED_GROUP",
                    "message": "封闭时段与已出发组的剩余路线相交，整次拒绝",
                    "conflicts": [cf.model_dump() for cf in conflicts],
                })

        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            await conn.execute(
                "INSERT INTO closures(edge_id, start_minute, end_minute) VALUES(?,?,?)",
                (closure.edge_id, closure.start_minute, closure.end_minute))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(conn, "closures", closure.model_dump(),
                                     _plan_dump(before), _plan_dump(after))
        return after


@app.post("/capacity-controls", tags=["变更"], response_model=PlanResponse,
          status_code=201)
async def add_capacity_control(control: CapacityControlIn, request: Request):
    """登记左闭右开的容量管制时段（临时限流）。

    管制时段内该通道每分钟有效上限收紧为 min(原容量, 指定上限)，时段外
    仍为原容量；编排按通行期间每一分钟的有效上限核算，封闭规则照常生效。

    通道不存在、起止无效或起点早于当前分钟时返回 422；与同通道已有管制
    时段重叠时返回 409；新上限使已出发组剩余路线在任一分钟超载时返回
    409 并给出冲突分钟、占用量与相关组。以上均整次拒绝、不改动任何
    状态、不产生修订。成功后仅重排未出发组（已出发组路线保留），
    登记管制并产生一条修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                "SELECT 1 FROM edges WHERE id=?",
                (control.edge_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(422, f"通道 {control.edge_id} 不存在")
        if control.start_minute < minute_now:
            raise HTTPException(
                422, f"管制起点 {control.start_minute} 早于当前分钟 "
                     f"{minute_now}")

        async with conn.execute(
                """SELECT start_minute, end_minute FROM capacity_controls
                   WHERE edge_id=? ORDER BY start_minute""",
                (control.edge_id,)) as cur:
            existing = await cur.fetchall()
        overlaps = [(a, b) for a, b in existing
                    if control.start_minute < b and a < control.end_minute]
        if overlaps:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "CAPACITY_CONTROL_OVERLAP",
                    "message": "同一通道的管制时段不得重叠，整次拒绝",
                    "conflicts": [
                        {"edge_id": control.edge_id,
                         "existing": [a, b],
                         "requested": [control.start_minute,
                                       control.end_minute]}
                        for a, b in overlaps],
                })

        conflicts = await _capacity_control_conflicts(
            conn, control.edge_id, control.start_minute, control.end_minute,
            control.capacity)
        if conflicts:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "CAPACITY_CONTROL_OVERLOADS_DEPARTED_GROUP",
                    "message": "新上限使已出发组剩余路线在部分分钟超载，"
                               "整次拒绝",
                    "conflicts": conflicts,
                })

        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            await conn.execute(
                """INSERT INTO capacity_controls
                   (edge_id, start_minute, end_minute, capacity)
                   VALUES(?,?,?,?)""",
                (control.edge_id, control.start_minute, control.end_minute,
                 control.capacity))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(conn, "capacity-controls",
                                     control.model_dump(),
                                     _plan_dump(before), _plan_dump(after))
        return after


@app.get("/capacity-controls", tags=["查询"],
         response_model=CapacityControlListOut)
async def list_capacity_controls(request: Request):
    """列出当前全部容量管制时段（按通道、开始分钟排序）。

    场景重置会清除全部管制记录；历史管制可通过修订快照追溯。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                """SELECT edge_id, start_minute, end_minute, capacity
                   FROM capacity_controls
                   ORDER BY edge_id, start_minute""") as cur:
            rows = await cur.fetchall()
    return CapacityControlListOut(
        current_minute=minute_now,
        controls=[CapacityControlOut(
            edge_id=row[0], start_minute=row[1], end_minute=row[2],
            capacity=row[3]) for row in rows])


class TickIn(BaseModel):
    minutes: int = Field(1, gt=0, description="推进的分钟数（正整数）")


@app.post("/tick", tags=["时间"], response_model=PlanResponse)
async def advance_minute(request: Request, payload: TickIn | None = None):
    """推进当前分钟（默认 +1，可传 {"minutes": n}）。

    推进时，旧计划中出发分钟已过（< 新当前分钟）的组视为已经出发，
    路线与占用立即固定、不参与重排；其余未出发组再按新当前分钟整体重排
    （新计划允许在当前分钟立即出发）。
    """
    conn = request.app.state.db
    delta = payload.minutes if payload is not None else 1

    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn) + delta
        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            await db.set_current_minute(conn, minute_now)
            # 1) 旧计划中已经出发的组立即固定，不参与本次重排
            await conn.execute(
                """UPDATE groups SET status='fixed'
                   WHERE status='scheduled' AND departure_minute < ?""",
                (minute_now,))
            # 2) 其余未出发组以当前分钟为最早出发重新安排
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(conn, "tick", {"minutes": delta},
                                     _plan_dump(before), _plan_dump(after))
        return after


@app.get("/plan", tags=["查询"], response_model=PlanResponse)
async def get_plan(request: Request):
    """查询当前编排：每组路线、出发/到达分钟或无法安排的原因。"""
    async with request.app.state.lock:
        return await _build_plan_response(request.app.state.db)


@app.get("/passages/{edge_id}/load", tags=["查询"])
async def get_edge_load(edge_id: int, request: Request,
                        minute: int | None = None):
    """查询通道逐分钟占用与每分钟有效上限，便于核验容量；可加 ?minute=n 只看某一分钟。

    每分钟的有效上限：容量管制时段内为 min(原容量, 管制上限)，时段外为
    原容量。管制覆盖的分钟即使无占用也会列出（used=0），响应顶层同时
    给出该通道当前生效的管制时段列表。
    """
    conn = request.app.state.db
    async with conn.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)) as cur:
        if await cur.fetchone() is None:
            raise HTTPException(404, f"通道 {edge_id} 不存在")
    async with request.app.state.lock:
        async with conn.execute(
                """SELECT rs.enter_minute, rs.exit_minute, g.size, g.status, g.id
                   FROM route_steps rs JOIN groups g ON g.id=rs.group_id
                   WHERE rs.edge_id=?""", (edge_id,)) as cur:
            rows = await cur.fetchall()
        per_minute: dict[int, dict] = {}
        for enter_m, exit_m, size, status, gid in rows:
            for m in range(enter_m, exit_m):
                slot = per_minute.setdefault(
                    m, {"minute": m, "used": 0, "groups": []})
                slot["used"] += size
                slot["groups"].append(
                    {"group_id": gid, "size": size, "status": status})
        async with conn.execute(
                "SELECT capacity, minutes FROM edges WHERE id=?", (edge_id,)) as cur:
            cap_row = await cur.fetchone()
        base_cap = cap_row[0]
        async with conn.execute(
                """SELECT start_minute, end_minute, capacity
                   FROM capacity_controls WHERE edge_id=?
                   ORDER BY start_minute""", (edge_id,)) as cur:
            control_rows = await cur.fetchall()
        controls = [(a, b, cap) for a, b, cap in control_rows]
        # 管制覆盖的分钟即使无占用也列出，便于核对管制期间的有效上限
        for a, b, _cap in controls:
            for m in range(a, b):
                per_minute.setdefault(m, {"minute": m, "used": 0, "groups": []})
        slots = []
        for m in sorted(per_minute):
            if minute is not None and m != minute:
                continue
            slot = per_minute[m]
            slot["effective_capacity"] = effective_capacity(
                base_cap, controls, m)
            slots.append(slot)
        return {"edge_id": edge_id, "capacity": base_cap,
                "traversal_minutes": cap_row[1],
                "capacity_controls": [
                    {"edge_id": edge_id, "start_minute": a, "end_minute": b,
                     "capacity": cap} for a, b, cap in controls],
                "minutes": slots}


@app.get("/revisions", tags=["追溯"], response_model=RevisionListOut)
async def list_revisions(request: Request):
    """列出全部修订（按修订号升序）。

    修订号全局递增、按实际提交顺序编号；场景重置与服务重启后均保留。
    """
    async with request.app.state.lock:
        rows = await db.list_revisions(request.app.state.db)
    return RevisionListOut(
        revisions=[RevisionSummaryOut(**row) for row in rows])


@app.get("/revisions/{rev}", tags=["追溯"], response_model=RevisionDetailOut)
async def get_revision(rev: int, request: Request):
    """按修订号查询当时的编排快照与各组前后差异。

    快照为修订提交时落库的完整编排（含当时固定组状态与逐段通行时刻），
    原样返回、不按当前场景重算；未知修订号返回 404。
    """
    async with request.app.state.lock:
        row = await db.load_revision(request.app.state.db, rev)
    if row is None:
        raise HTTPException(404, f"修订号 {rev} 不存在")
    before = PlanResponse.model_validate(row["plan_before"])
    after = PlanResponse.model_validate(row["plan_after"])
    return RevisionDetailOut(
        rev=row["rev"], operation=row["operation"], minute=row["minute"],
        detail=row["detail"], snapshot=after,
        changes=_diff_plans(before, after))
