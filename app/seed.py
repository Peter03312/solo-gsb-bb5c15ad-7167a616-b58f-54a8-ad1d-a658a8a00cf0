"""一键启动时自动初始化的演示数据。

场馆示意（节点，-> 为有向通道，括号内为 通行分钟/每分钟容量）::

    1 --P1(4/30)--> 2 --P2(3/30)--> 3 --P3(2/30)--> 5(出口)
    |              |                               ^
    P4(5/20)       P5(2/40)                        |
    v              v                               |
    4 ----------- 6(出口)          7 --P7(5/20)----+
"""
from __future__ import annotations

SEED_SCENARIO = {
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
    "closures": [
        # 通道 1（1->2，通行 4 分钟）在第 3..10 分钟封闭 [3,10)；
        # t=0 进入会占用 [0,4) 与之相交，因此最早第 10 分钟才能进入
        {"edge_id": 1, "start_minute": 3, "end_minute": 10},
    ],
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
