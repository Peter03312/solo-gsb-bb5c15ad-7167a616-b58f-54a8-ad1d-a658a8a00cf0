#!/usr/bin/env python3
"""场馆疏散通道编排服务 —— Python 调用示例（仅依赖标准库）。

运行：  python3 examples/python_client.py [BASE_URL]
默认 BASE_URL=http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def show(title, status, payload):
    print(f"\n==== {title}（HTTP {status}）====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def summarize(plan):
    print(f"\n当前分钟: {plan['current_minute']}")
    for g in plan["groups"]:
        if g["status"] in ("scheduled", "fixed"):
            print(f"  组 {g['group_id']:>2} [{g['status']:>9}] "
                  f"出口 {g['exit_id']}  出发 {g['departure_minute']:>3} "
                  f"到达 {g['arrival_minute']:>3}  通道序列 {g['route']}"
                  + (f"  已走前缀 {g['prefix_step_count']} 段"
                     if g.get("prefix_step_count") is not None else ""))
        elif g["status"] == "awaiting_rescue":
            print(f"  组 {g['group_id']:>2} [待救援    ] "
                  f"滞留于已走前缀（{g['prefix_step_count']} 段，"
                  f"通道序列 {g['route']}）原因 {g['reason']}：{g['detail']}")
        else:
            print(f"  组 {g['group_id']:>2} [{g['status']:>9}] "
                  f"原因 {g['reason']}：{g['detail']}")


if __name__ == "__main__":
    show("健康检查", *call("GET", "/health"))

    scenario = {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
            {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
            {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
            {"id": 4, "src": 1, "dst": 4, "minutes": 5, "capacity": 20},
            {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
            {"id": 6, "src": 4, "dst": 6, "minutes": 3, "capacity": 30},
            {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20},
        ],
        "exits": [5, 6],
        "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
        "groups": [
            {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 20, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 25},
            {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
             "priority": 20, "deadline": 20},
            {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0,
             "priority": 5, "deadline": 15},
        ],
    }
    status, plan = call("POST", "/scenario", scenario)
    show("初始化场景并编排", status, plan)
    summarize(plan)

    status, plan = call("POST", "/groups", [
        {"id": 9, "size": 40, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 12}])
    show("新增组 9（人数 40，截止很紧，演示无法安排的原因）", status, plan)
    summarize(plan)

    status, plan = call("POST", "/tick", {"minutes": 1})
    show("推进 1 分钟", status, plan)
    summarize(plan)

    # 组 3 在 t=0 进入通道 5（2->6），占用 [0,2)；当前分钟 1 时仍在途，
    # 与其剩余区间 [1,2) 相交的封闭应被整次拒绝
    status, payload = call("POST", "/closures",
                           {"edge_id": 5, "start_minute": 1, "end_minute": 3})
    show("触及已出发组剩余路线的封闭（预期 409）", status, payload)

    status, plan = call("GET", "/plan")
    show("拒绝后查询，原编排保持不变", status, plan)
    summarize(plan)

    # ---- 容量管制（临时限流）----
    # 组 3 已出发，剩余占用通道 5 的 [1,2) 共 10 人；上限 5 会使其超载，
    # 整次拒绝并返回冲突分钟、占用量与相关组
    status, payload = call("POST", "/capacity-controls",
                           {"edge_id": 5, "start_minute": 1, "end_minute": 3,
                            "capacity": 5})
    show("使已出发组超载的容量管制（预期 409）", status, payload)

    # 通道 1 第 10..14 分钟限流至 10 人/分钟：组 1（30 人）在管制时段内
    # 无法进入通道 1，重排后推迟到管制结束的第 14 分钟进入
    status, plan = call("POST", "/capacity-controls",
                        {"edge_id": 1, "start_minute": 10, "end_minute": 14,
                         "capacity": 10})
    show("登记容量管制（通道 1 第 10..14 分钟限流 10 人/分钟）",
         status, plan)
    summarize(plan)

    status, payload = call("GET", "/passages/1/load")
    show("通道 1 逐分钟占用与每分钟有效上限", status, payload)
    for m in payload["minutes"]:
        print(f"  第 {m['minute']:>2} 分钟：占用 {m['used']:>2} / "
              f"有效上限 {m['effective_capacity']}")

    status, payload = call("GET", "/capacity-controls")
    show("当前容量管制列表", status, payload)

    # 同一通道的管制时段不得重叠：与 [10,14) 相交 -> 409
    status, payload = call("POST", "/capacity-controls",
                           {"edge_id": 1, "start_minute": 12,
                            "end_minute": 16, "capacity": 20})
    show("与已有管制重叠的时段（预期 409）", status, payload)

    # ---- 人群组纠正（批量纠正未出发组的属性）----
    # 组 9 此前因人数 40 超容量而编排失败；纠正人数与截止分钟后可安排
    status, plan = call("POST", "/group-corrections",
                        [{"group_id": 9, "size": 20, "deadline": 30}])
    show("纠正组 9（人数 40->20、截止 12->30，整批生效并重排）",
         status, plan)
    summarize(plan)

    # 组 3 已出发（fixed）：纠正整批拒绝，状态与修订历史不变
    status, payload = call("POST", "/group-corrections",
                           [{"group_id": 3, "size": 5}])
    show("纠正已出发的组（预期 409）", status, payload)

    # 同批组编号重复 / 最终字段不合法等无效请求：422，整批拒绝
    status, payload = call("POST", "/group-corrections",
                           [{"group_id": 9, "size": 15},
                            {"group_id": 9, "priority": 3}])
    show("同批组编号重复的纠正批（预期 422）", status, payload)

    # ---- 现场节点滞留报告 ----
    # 重置为一个便于演示的小场景：组 1 的路线为通道1[0,4)+通道2[4,6)
    status, plan = call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
            {"id": 3, "src": 2, "dst": 3, "minutes": 2, "capacity": 10},
            {"id": 4, "src": 3, "dst": 5, "minutes": 2, "capacity": 10},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 30},
        ],
    })
    assert status == 200
    # 推进到第 4 分钟：组 1 已到达节点 2（下一通道进入分钟恰为当前分钟，
    # 仍可在节点被报告）
    status, plan = call("POST", "/tick", {"minutes": 4})
    show("推进到第 4 分钟（组 1 已到达节点 2）", status, plan)
    summarize(plan)

    # 未出发组 / 错误节点 / 恢复分钟在过去：均被拒绝，不改编排
    status, payload = call("POST", "/groups/2/delay-report",
                           {"node_id": 1, "recovery_minute": 4})
    show("报告尚未出发的组 2（预期 409 NOT_DEPARTED）", status, payload)
    status, payload = call("POST", "/groups/1/delay-report",
                           {"node_id": 1, "recovery_minute": 4})
    show("组 1 报告错误节点（实际在节点 2，预期 409 NODE_MISMATCH）",
         status, payload)
    status, payload = call("POST", "/groups/1/delay-report",
                           {"node_id": 2, "recovery_minute": 3})
    show("恢复分钟早于当前分钟（预期 422）", status, payload)

    # 正式报告：节点 2 滞留，预计第 7 分钟恢复。已走前缀通道1[0,4) 保留，
    # 旧预约通道2[4,6) 释放，新余程通道2[7,9) 作为固定占用；组 2 随之重排
    status, plan = call("POST", "/groups/1/delay-report",
                        {"node_id": 2, "recovery_minute": 7})
    show("报告组 1 在节点 2 滞留、第 7 分钟恢复（前缀保留+重找余程）",
         status, plan)
    summarize(plan)
    g1 = next(g for g in plan["groups"] if g["group_id"] == 1)
    print(f"  已走前缀步数 prefix_step_count={g1['prefix_step_count']}："
          f"steps[0] 已走过固定保留，steps[1] 为恢复后的新余程")

    # 等价的集合式写法（组号放在请求体内）
    status, payload = call("POST", "/delay-reports",
                           {"group_id": 2, "node_id": 1,
                            "recovery_minute": 4})
    show("集合式写法 /delay-reports（组 2 未出发，预期同样 409）",
         status, payload)

    # 待救援：唯一余程通道自第 5 分钟封闭，报告恢复 6 -> 无余程
    status, plan = call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5,
                      "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}],
    })
    assert status == 200
    call("POST", "/tick", {"minutes": 4})
    status, plan = call("POST", "/groups/1/delay-report",
                        {"node_id": 2, "recovery_minute": 6})
    show("报告组 1 恢复 6（余程被封闭阻断，预期 awaiting_rescue）",
         status, plan)
    summarize(plan)

    # 后续时间推进不会自动重排待救援组
    status, plan = call("POST", "/tick", {"minutes": 6})
    show("再推进 6 分钟（待救援组不自动重排）", status, plan)
    summarize(plan)

    # ---- 修订历史：追溯每次编排变更 ----
    status, payload = call("GET", "/revisions")
    show("修订列表（每次成功变更一条，拒绝不产生）", status, payload)

    latest = payload["revisions"][-1]["rev"]
    status, detail = call("GET", f"/revisions/{latest}")
    show(f"最新修订 rev={latest} 的快照与逐组差异", status, detail)
    for ch in detail["changes"]:
        if ch["change"] == "changed":
            print(f"  组 {ch['group_id']} 变化字段 {ch['changed_fields']}: "
                  f"{ch['before']['status']} -> {ch['after']['status']}")
        else:
            g = ch["after"] or ch["before"]
            print(f"  组 {ch['group_id']} {ch['change']} "
                  f"(status={g['status']})")

    status, payload = call("GET", "/revisions/999999")
    show("未知修订号（预期 404）", status, payload)
