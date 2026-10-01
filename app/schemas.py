"""HTTP 请求/响应模型（字段 snake_case，原因码附中文说明）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class PassageIn(BaseModel):
    id: int = Field(..., ge=1, description="通道编号（正整数，全图唯一）")
    src: int = Field(..., ge=0, description="起点节点编号")
    dst: int = Field(..., ge=0, description="终点节点编号")
    minutes: int = Field(..., gt=0, description="通行分钟数（正整数）")
    capacity: int = Field(..., gt=0, description="每分钟通行人数（正整数）")

    @model_validator(mode="after")
    def _no_self_loop(self):
        if self.src == self.dst:
            raise ValueError("通道起点与终点不能相同")
        return self


class ClosureIn(BaseModel):
    edge_id: int = Field(..., description="被封闭的通道编号")
    start_minute: int = Field(..., ge=0, description="封闭开始分钟（含）")
    end_minute: int = Field(..., ge=0, description="封闭结束分钟（不含）")

    @model_validator(mode="after")
    def _half_open(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("封闭时段需为左闭右开：end_minute 必须大于 start_minute")
        return self


class CapacityControlIn(BaseModel):
    edge_id: int = Field(..., description="被管制的通道编号")
    start_minute: int = Field(..., ge=0, description="管制开始分钟（含，须不早于当前分钟）")
    end_minute: int = Field(..., ge=0, description="管制结束分钟（不含）")
    capacity: int = Field(..., gt=0,
                          description="管制时段内每分钟人数上限（正整数）")

    @model_validator(mode="after")
    def _half_open(self):
        if self.end_minute <= self.start_minute:
            raise ValueError(
                "管制时段需为左闭右开：end_minute 必须大于 start_minute")
        return self


class CapacityControlOut(BaseModel):
    edge_id: int
    start_minute: int
    end_minute: int
    capacity: int


class CapacityControlListOut(BaseModel):
    current_minute: int
    controls: list[CapacityControlOut]


class GroupIn(BaseModel):
    id: int = Field(..., ge=1, description="人群组编号（正整数，唯一）")
    size: int = Field(..., gt=0, description="人数（正整数，整组不可拆分）")
    origin: int = Field(..., ge=0, description="起点节点编号")
    earliest_departure: int = Field(..., ge=0, description="最早出发分钟")
    priority: int = Field(..., ge=0, description="优先级，数值越大越优先")
    deadline: int = Field(..., ge=0, description="规划截止分钟（到达须不晚于此）")

    @model_validator(mode="after")
    def _deadline_ok(self):
        if self.deadline < self.earliest_departure:
            raise ValueError("规划截止分钟不能早于最早出发分钟")
        return self


class ScenarioIn(BaseModel):
    passages: list[PassageIn] = Field(default_factory=list)
    exits: list[int] = Field(default_factory=list, description="出口节点编号列表")
    closures: list[ClosureIn] = Field(default_factory=list)
    groups: list[GroupIn] = Field(default_factory=list)


class GroupCorrectionIn(BaseModel):
    """单条人群组纠正：指定已有组编号及至少一个待纠正字段。

    未指定的字段沿用原值；合并后的最终字段须通过 GroupIn 的全部校验
    （在服务层按合并结果复核，含"截止不早于最早出发"的跨字段约束）。
    """
    group_id: int = Field(..., ge=1, description="要纠正的已有人群组编号")
    size: Optional[int] = Field(None, gt=0, description="新人数（正整数）")
    origin: Optional[int] = Field(None, ge=0,
                                  description="新起点节点编号（须为场馆节点）")
    earliest_departure: Optional[int] = Field(None, ge=0,
                                              description="新最早出发分钟")
    priority: Optional[int] = Field(None, ge=0,
                                    description="新优先级，数值越大越优先")
    deadline: Optional[int] = Field(None, ge=0,
                                    description="新规划截止分钟（到达须不晚于此）")

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if all(v is None for v in (self.size, self.origin,
                                   self.earliest_departure, self.priority,
                                   self.deadline)):
            raise ValueError(
                "每项纠正至少指定一个字段：size/origin/earliest_departure/"
                "priority/deadline")
        return self


class StepOut(BaseModel):
    edge_id: int
    from_node: int
    to_node: int
    enter_minute: int
    exit_minute: int


class GroupPlanOut(BaseModel):
    group_id: int
    status: str
    exit_id: Optional[int] = None
    departure_minute: Optional[int] = None
    arrival_minute: Optional[int] = None
    route: list[int] = Field(default_factory=list, description="通道编号序列")
    steps: list[StepOut] = Field(default_factory=list)
    reason: Optional[str] = None
    detail: Optional[str] = None


class PlanResponse(BaseModel):
    current_minute: int
    groups: list[GroupPlanOut]


class ConflictOut(BaseModel):
    group_id: int
    edge_id: int
    enter_minute: int
    exit_minute: int
    closure: list[int] = Field(description="与之相交的封闭时段 [start,end)")


# --------------------------------------------------------------------------
# 修订历史（编排变更追溯）
# --------------------------------------------------------------------------

class GroupChangeOut(BaseModel):
    """单个组在一次修订前后的差异。"""
    group_id: int
    change: str = Field(description="added=新增 | removed=移除 | changed=编排变化")
    changed_fields: list[str] = Field(
        default_factory=list,
        description="change=changed 时发生变化的字段，如 status/route/steps/reason")
    before: Optional[GroupPlanOut] = Field(
        None, description="提交前该组编排（added 时为 null）")
    after: Optional[GroupPlanOut] = Field(
        None, description="提交后该组编排（removed 时为 null）")


class RevisionSummaryOut(BaseModel):
    rev: int = Field(description="全局递增修订号（按实际提交顺序编号）")
    operation: str = Field(
        description="触发操作：scenario=场景重置 | groups=新增人群组 | "
                    "group-corrections=纠正人群组 | closures=登记封闭 | "
                    "capacity-controls=登记容量管制 | tick=时间推进")
    minute: int = Field(description="该修订提交后的当前分钟")
    detail: dict = Field(default_factory=dict, description="触发操作的参数摘要")


class RevisionListOut(BaseModel):
    revisions: list[RevisionSummaryOut]


class RevisionDetailOut(RevisionSummaryOut):
    snapshot: PlanResponse = Field(
        description="该修订提交后的完整编排快照（当时状态，含固定组逐段"
                    "通行时刻；落库保存，不按当前场景重算）")
    changes: list[GroupChangeOut] = Field(
        description="各组提交前后差异（新增/移除/状态、出发与到达分钟、"
                    "路线、失败原因）")
