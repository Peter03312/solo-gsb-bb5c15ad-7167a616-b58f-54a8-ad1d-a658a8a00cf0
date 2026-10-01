#!/usr/bin/env bash
# 场馆疏散通道编排服务 —— curl 调用示例
# 用法： bash examples/curl_examples.sh [BASE_URL]
set -euo pipefail

BASE="${1:-${BASE:-http://localhost:8000}}"

say() { printf '\n==== %s ====\n' "$1"; }

say "1) 健康检查"
curl -s "$BASE/health" | python3 -m json.tool

say "2) 全量初始化一个自定义场景（通道/出口/封闭/人群组）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
      {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
      {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
      {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
      {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20}
    ],
    "exits": [5, 6],
    "closures": [
      {"edge_id": 1, "start_minute": 3, "end_minute": 10}
    ],
    "groups": [
      {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0, "priority": 20, "deadline": 20},
      {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0, "priority": 5, "deadline": 15}
    ]
  }' | python3 -m json.tool

say "3) 查询编排结果（按优先级、最早出发、组号排序）"
curl -s "$BASE/plan" | python3 -m json.tool

say "4) 新增人群组（仅重排未出发组）"
curl -s -X POST "$BASE/groups" \
  -H 'Content-Type: application/json' \
  -d '[
    {"id": 7, "size": 25, "origin": 1, "earliest_departure": 2, "priority": 8, "deadline": 40}
  ]' | python3 -m json.tool

say "5) 新增不触及已出发组的封闭时段（登记后重排未出发组）"
curl -s -X POST "$BASE/closures" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 2, "start_minute": 20, "end_minute": 30}' \
  | python3 -m json.tool

say "6) 推进当前分钟 1 分钟（t=0 出发的组固定，随后重排未出发组）"
curl -s -X POST "$BASE/tick" \
  -H 'Content-Type: application/json' \
  -d '{"minutes": 1}' | python3 -m json.tool

say "7) 尝试封闭已出发组剩余路线（组3正占用通道5的[1,2)，预期 HTTP 409 整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/closures" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 3}'
python3 -m json.tool /tmp/resp.json || true

say "8) 拒绝后再次查询，原编排保持不变"
curl -s "$BASE/plan" | python3 -m json.tool

say "9) 查询通道 1 每分钟容量占用（核验不超容量）"
curl -s "$BASE/passages/1/load" | python3 -m json.tool

say "10) 登记使已出发组超载的容量管制（组3剩余占用通道5的[1,2)共10人，上限5 -> 预期 HTTP 409）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 3, "capacity": 5}'
python3 -m json.tool /tmp/resp.json || true

say "11) 登记合法容量管制（通道1第10..14分钟限流至10人/分钟，登记后重排未出发组）"
curl -s -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 10, "end_minute": 14, "capacity": 10}' \
  | python3 -m json.tool

say "12) 查询通道 1 占用：管制时段内每分钟有效上限收紧为 10，时段外仍为 30"
curl -s "$BASE/passages/1/load" | python3 -m json.tool

say "13) 列出当前全部容量管制时段"
curl -s "$BASE/capacity-controls" | python3 -m json.tool

say "14) 登记与已有管制重叠的时段（通道1第12..16分钟，预期 HTTP 409 整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 12, "end_minute": 16, "capacity": 20}'
python3 -m json.tool /tmp/resp.json || true

say "15) 纠正未出发组（组7：人数 25->20、优先级 8->12，整批原子生效并重排未出发组）"
curl -s -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 7, "size": 20, "priority": 12}]' | python3 -m json.tool

say "16) 纠正已出发的组 3（预期 HTTP 409 整批拒绝，状态与修订历史不变）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 3, "size": 5}]'
python3 -m json.tool /tmp/resp.json || true

say "17) 含无效项的纠正批（组999不存在，预期 HTTP 422，整批拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 7, "size": 18}, {"group_id": 999, "size": 1}]'
python3 -m json.tool /tmp/resp.json || true

say "18) 列出修订历史（每次成功变更一条，被拒绝的请求不产生）"
curl -s "$BASE/revisions" | python3 -m json.tool

say "19) 按修订号查询：当时编排快照 + 各组前后差异（取最新一条，应为 group-corrections）"
LATEST=$(curl -s "$BASE/revisions" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["revisions"][-1]["rev"])')
curl -s "$BASE/revisions/$LATEST" | python3 -m json.tool

say "20) 查询未知修订号（预期 HTTP 404）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' "$BASE/revisions/999999"
python3 -m json.tool /tmp/resp.json || true

say "完成。交互文档： $BASE/docs"
