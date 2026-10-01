"""SQLite 持久化层（经 aiosqlite 异步访问，服务层串行化所有变更）。"""
from __future__ import annotations
import json
from contextlib import asynccontextmanager

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id       INTEGER PRIMARY KEY,
    src      INTEGER NOT NULL,
    dst      INTEGER NOT NULL,
    minutes  INTEGER NOT NULL,
    capacity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exits (
    node_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS closures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id      INTEGER NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute   INTEGER NOT NULL
);

-- 容量管制时段（临时限流）：左闭右开 [start_minute, end_minute)，
-- 时段内该通道每分钟有效上限收紧为 capacity；同一通道的时段互不重叠
CREATE TABLE IF NOT EXISTS capacity_controls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id      INTEGER NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute   INTEGER NOT NULL,
    capacity     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id                 INTEGER PRIMARY KEY,
    size               INTEGER NOT NULL,
    origin             INTEGER NOT NULL,
    earliest_departure INTEGER NOT NULL,
    priority           INTEGER NOT NULL,
    deadline           INTEGER NOT NULL,
    -- pending | scheduled | fixed | failed | awaiting_rescue
    status             TEXT NOT NULL,
    departure_minute   INTEGER,
    arrival_minute     INTEGER,
    exit_id            INTEGER,
    fail_reason        TEXT,
    fail_detail        TEXT
);

CREATE TABLE IF NOT EXISTS route_steps (
    group_id     INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    edge_id      INTEGER NOT NULL,
    from_node    INTEGER NOT NULL,
    to_node      INTEGER NOT NULL,
    enter_minute INTEGER NOT NULL,
    exit_minute  INTEGER NOT NULL,
    PRIMARY KEY (group_id, seq)
);

-- 现场节点滞留报告：每个被报告过的组最多一条（UPSERT 覆盖）。
-- prefix_step_count 为已走前缀步数：route_steps 中 seq < prefix_step_count
-- 的段落为报告时已经走完、固定保留的前缀；恢复后的余程从该 seq 续编。
-- 组随后恢复（fixed）或仍待救援（awaiting_rescue）均保留本行，使当前
-- 编排得以区分"已走前缀"与"新余程"。
CREATE TABLE IF NOT EXISTS delay_reports (
    group_id          INTEGER PRIMARY KEY,
    node_id           INTEGER NOT NULL,
    recovery_minute   INTEGER NOT NULL,
    reported_minute   INTEGER NOT NULL,
    prefix_step_count INTEGER NOT NULL
);

-- 修订历史：每次成功的编排变更（场景重置/新增组/纠正组/登记封闭/
-- 登记容量管制/现场节点滞留报告/推进时间）与编排变更在同一事务内写入；
-- 快照为提交时刻的完整编排 JSON，落库后不再改动、查询时不按当前场景重算。
-- 场景重置（reset_db）与服务重启均不影响本表。
CREATE TABLE IF NOT EXISTS revisions (
    rev         INTEGER PRIMARY KEY,  -- 全局递增修订号（按实际提交顺序）
    operation   TEXT NOT NULL,        -- scenario | groups | group-corrections |
                                      -- closures | capacity-controls |
                                      -- delay-reports | tick
    detail      TEXT NOT NULL,        -- JSON：触发操作的参数摘要
    minute      INTEGER NOT NULL,     -- 该修订提交后的当前分钟
    plan_before TEXT NOT NULL,        -- JSON：提交前完整编排（PlanResponse）
    plan_after  TEXT NOT NULL         -- JSON：提交后完整编排（PlanResponse）
);
"""


async def init_db(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)
    await db.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('current_minute', 0)")
    await db.commit()


async def reset_db(db: aiosqlite.Connection) -> None:
    """清空全部业务表并归零当前分钟（调用方负责事务）。

    修订历史（revisions 表）不在清空之列：场景重置后旧修订仍可查询，
    修订号继续全局递增。
    """
    for table in ("delay_reports", "route_steps", "groups", "closures",
                  "capacity_controls", "exits", "edges", "meta"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "INSERT INTO meta(key, value) VALUES('current_minute', 0)")


@asynccontextmanager
async def transaction(db: aiosqlite.Connection):
    """显式事务：连接以 autocommit 打开，提交/回滚由此处统一管理。"""
    await db.execute("BEGIN")
    try:
        yield
        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def get_current_minute(db: aiosqlite.Connection) -> int:
    async with db.execute(
            "SELECT value FROM meta WHERE key='current_minute'") as cur:
        row = await cur.fetchone()
    return int(row[0])


async def set_current_minute(db: aiosqlite.Connection, minute: int) -> None:
    await db.execute(
        "UPDATE meta SET value=? WHERE key='current_minute'", (minute,))


async def load_world(db: aiosqlite.Connection):
    """加载构造规划世界所需的全部静态/动态数据。"""
    from .planner import Edge, World

    edges: dict[int, Edge] = {}
    async with db.execute(
            "SELECT id, src, dst, minutes, capacity FROM edges") as cur:
        for row in await cur.fetchall():
            e = Edge(id=row[0], src=row[1], dst=row[2],
                     minutes=row[3], capacity=row[4])
            edges[e.id] = e

    adjacency: dict[int, list[int]] = {}
    for e in edges.values():
        adjacency.setdefault(e.src, []).append(e.id)
    for ids in adjacency.values():
        ids.sort()  # 按通道编号升序扩展

    async with db.execute("SELECT node_id FROM exits ORDER BY node_id") as cur:
        exits = [row[0] for row in await cur.fetchall()]

    closures: dict[int, list[tuple[int, int]]] = {}
    async with db.execute(
            "SELECT edge_id, start_minute, end_minute FROM closures") as cur:
        for edge_id, a, b in await cur.fetchall():
            closures.setdefault(edge_id, []).append((a, b))
    for segs in closures.values():
        segs.sort()

    capacity_controls: dict[int, list[tuple[int, int, int]]] = {}
    async with db.execute(
            """SELECT edge_id, start_minute, end_minute, capacity
               FROM capacity_controls
               ORDER BY edge_id, start_minute""") as cur:
        for edge_id, a, b, cap in await cur.fetchall():
            capacity_controls.setdefault(edge_id, []).append((a, b, cap))

    # 已固定（已出发）组的占用：仅计当前分钟起尚未走完的部分
    minute_now = await get_current_minute(db)
    fixed_load: dict[tuple[int, int], int] = {}
    async with db.execute(
            """
            SELECT rs.edge_id, rs.enter_minute, rs.exit_minute, g.size
            FROM route_steps rs JOIN groups g ON g.id = rs.group_id
            WHERE g.status='fixed'
            """) as cur:
        for edge_id, enter_m, exit_m, size in await cur.fetchall():
            for minute in range(max(enter_m, minute_now), exit_m):
                key = (edge_id, minute)
                fixed_load[key] = fixed_load.get(key, 0) + size

    world = World(edges=edges, adjacency=adjacency, exits=exits,
                  closures=closures, fixed_load=fixed_load,
                  capacity_controls=capacity_controls)

    async with db.execute(
            """SELECT id, size, origin, earliest_departure, priority,
                      deadline, status
               FROM groups WHERE status IN ('scheduled', 'failed', 'pending')
               ORDER BY priority DESC, earliest_departure ASC, id ASC"""
            ) as cur:
        pending = [dict(zip(
            ("id", "size", "origin", "earliest_departure",
             "priority", "deadline", "status"),
            row)) for row in await cur.fetchall()]

    return world, pending


async def load_fixed_routes(db: aiosqlite.Connection):
    """返回已固定组的路线段及当前分钟（用于封闭冲突检查）。

    封闭只可能触及“剩余路线”：进入分钟早于当前分钟的占用部分已经过去，
    剩余占用区间取 [max(enter_minute, 当前分钟), exit_minute)。
    """
    minute_now = await get_current_minute(db)
    async with db.execute(
            """SELECT g.id, rs.edge_id, rs.enter_minute, rs.exit_minute
               FROM route_steps rs JOIN groups g ON g.id = rs.group_id
               WHERE g.status='fixed'""") as cur:
        rows = await cur.fetchall()
    return minute_now, rows


async def persist_replan(db: aiosqlite.Connection, results: list[dict],
                         minute_now: int) -> None:
    """整体替换所有待出发组的规划结果（调用方负责开启事务）。

    先删后插保证不会出现"部分重排"：事务提交前任何读取看到的都是旧状态。
    """
    pending_ids = [r["group_id"] for r in results]
    if pending_ids:
        placeholders = ",".join("?" for _ in pending_ids)
        await db.execute(
            f"DELETE FROM route_steps WHERE group_id IN ({placeholders})",
            pending_ids)
        await db.execute(
            f"""UPDATE groups SET status='pending', departure_minute=NULL,
                arrival_minute=NULL, exit_id=NULL,
                fail_reason=NULL, fail_detail=NULL
                WHERE id IN ({placeholders})""", pending_ids)
    for r in results:
        gid = r["group_id"]
        if r["status"] == "scheduled":
            await db.execute(
                """UPDATE groups SET status='scheduled',
                   departure_minute=?, arrival_minute=?, exit_id=?,
                   fail_reason=NULL, fail_detail=NULL WHERE id=?""",
                (r["departure_minute"], r["arrival_minute"],
                 r["exit_id"], gid))
            for seq, step in enumerate(r["steps"]):
                await db.execute(
                    """INSERT INTO route_steps
                       (group_id, seq, edge_id, from_node, to_node,
                        enter_minute, exit_minute)
                       VALUES (?,?,?,?,?,?,?)""",
                    (gid, seq, step["edge_id"], step["from_node"],
                     step["to_node"], step["enter_minute"],
                     step["exit_minute"]))
        else:
            await db.execute(
                """UPDATE groups SET status='failed',
                   departure_minute=NULL, arrival_minute=NULL, exit_id=NULL,
                   fail_reason=?, fail_detail=? WHERE id=?""",
                (r["reason"], r["detail"], gid))


# --------------------------------------------------------------------------
# 现场节点滞留报告
# --------------------------------------------------------------------------

async def load_delay_prefixes(db: aiosqlite.Connection) -> dict[int, int]:
    """返回 group_id -> 已走前缀步数（仅被报告过的组）。"""
    async with db.execute(
            "SELECT group_id, prefix_step_count FROM delay_reports") as cur:
        return {gid: k for gid, k in await cur.fetchall()}


async def load_delay_report(db: aiosqlite.Connection,
                            group_id: int) -> dict | None:
    async with db.execute(
            """SELECT group_id, node_id, recovery_minute, reported_minute,
                      prefix_step_count
               FROM delay_reports WHERE group_id=?""", (group_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"group_id": row[0], "node_id": row[1],
            "recovery_minute": row[2], "reported_minute": row[3],
            "prefix_step_count": row[4]}


async def upsert_delay_report(db: aiosqlite.Connection, *, group_id: int,
                              node_id: int, recovery_minute: int,
                              reported_minute: int,
                              prefix_step_count: int) -> None:
    """登记/覆盖一组的现场滞留报告（调用方负责事务）。"""
    await db.execute(
        """INSERT INTO delay_reports(group_id, node_id, recovery_minute,
                                     reported_minute, prefix_step_count)
           VALUES(?,?,?,?,?)
           ON CONFLICT(group_id) DO UPDATE SET
               node_id=excluded.node_id,
               recovery_minute=excluded.recovery_minute,
               reported_minute=excluded.reported_minute,
               prefix_step_count=excluded.prefix_step_count""",
        (group_id, node_id, recovery_minute, reported_minute,
         prefix_step_count))


# --------------------------------------------------------------------------
# 修订历史（编排变更的审计追溯）
# --------------------------------------------------------------------------

async def insert_revision(db: aiosqlite.Connection, operation: str,
                          detail: dict, plan_before: dict,
                          plan_after: dict) -> int:
    """在调用方事务内写入一条修订记录，返回全局递增修订号。

    修订号取 MAX(rev)+1：所有变更均由服务层互斥锁串行化，编号顺序
    即实际提交顺序，连续无重复。本函数必须与编排变更处于同一事务，
    保证查询不会只看到其中一方。
    """
    async with db.execute(
            "SELECT COALESCE(MAX(rev), 0) + 1 FROM revisions") as cur:
        rev = int((await cur.fetchone())[0])
    await db.execute(
        """INSERT INTO revisions(rev, operation, detail, minute,
                                 plan_before, plan_after)
           VALUES(?,?,?,?,?,?)""",
        (rev, operation,
         json.dumps(detail, ensure_ascii=False),
         int(plan_after["current_minute"]),
         json.dumps(plan_before, ensure_ascii=False),
         json.dumps(plan_after, ensure_ascii=False)))
    return rev


async def load_revision(db: aiosqlite.Connection, rev: int) -> dict | None:
    """按修订号读取修订（含前后快照）；不存在时返回 None。"""
    async with db.execute(
            """SELECT operation, detail, minute, plan_before, plan_after
               FROM revisions WHERE rev=?""", (rev,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "rev": rev,
        "operation": row[0],
        "detail": json.loads(row[1]),
        "minute": int(row[2]),
        "plan_before": json.loads(row[3]),
        "plan_after": json.loads(row[4]),
    }


async def list_revisions(db: aiosqlite.Connection) -> list[dict]:
    """按修订号升序列出全部修订的元信息（不含快照正文）。"""
    async with db.execute(
            "SELECT rev, operation, detail, minute FROM revisions"
            " ORDER BY rev") as cur:
        rows = await cur.fetchall()
    return [{"rev": int(r), "operation": op, "detail": json.loads(d),
             "minute": int(m)} for r, op, d, m in rows]
