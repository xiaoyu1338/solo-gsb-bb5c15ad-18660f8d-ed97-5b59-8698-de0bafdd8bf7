"""Pydantic 请求 / 响应模型。"""
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.profile import MAX_PROFILE_DOCUMENTS


class AnalysisCreate(BaseModel):
    """POST /api/analyses 请求体。"""

    name: str | None = Field(default=None, max_length=255, description="可选的分析名称")
    old_schema: dict[str, Any] = Field(..., description="旧版 JSON Schema")
    new_schema: dict[str, Any] = Field(..., description="新版 JSON Schema")
    samples: list[Any] | None = Field(default=None, description="可选的样例文档列表")


class Issue(BaseModel):
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str
    counterexample: Any | None = None
    counterexample_path: str | None = None


class DirectionSummary(BaseModel):
    errors: int
    warnings: int
    total: int


class ReportSummary(BaseModel):
    backward: DirectionSummary
    forward: DirectionSummary
    total: int
    errors: int
    warnings: int


class SampleResult(BaseModel):
    index: int
    valid_against_old: bool
    valid_against_new: bool
    direction: str | None = None


class Report(BaseModel):
    backward_compatible: bool
    forward_compatible: bool
    directions: dict[str, str]
    summary: ReportSummary
    issues: list[Issue]
    sample_results: list[SampleResult]


class AnalysisResponse(BaseModel):
    id: int
    name: str | None
    created_at: str
    old_schema: dict[str, Any]
    new_schema: dict[str, Any]
    samples: list[Any]
    report: Report


class AnalysisListItem(BaseModel):
    id: int
    name: str | None
    created_at: str
    backward_compatible: bool
    forward_compatible: bool
    errors: int
    warnings: int


class AnalysisListResponse(BaseModel):
    total: int
    items: list[AnalysisListItem]


class CompareRequest(BaseModel):
    """POST /api/analyses/compare 请求体。"""

    baseline_id: int = Field(..., ge=1, description="基线分析记录 ID")
    current_id: int = Field(..., ge=1, description="当前分析记录 ID")


class ComparedIssue(BaseModel):
    """对比结果中的一条 issue（含稳定指纹，不含反例等大字段）。"""

    fingerprint: str
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str


class SeverityChangedIssue(ComparedIssue):
    baseline_severity: Literal["error", "warning"]
    current_severity: Literal["error", "warning"]


class IssueGroups(BaseModel):
    added: list[ComparedIssue]
    fixed: list[ComparedIssue]
    persisting: list[ComparedIssue]
    severity_changed: list[SeverityChangedIssue]


class IssueGroupCounts(BaseModel):
    added: int
    fixed: int
    persisting: int
    severity_changed: int


class CompatibilityChange(BaseModel):
    baseline: bool
    current: bool
    changed: bool


class CompatibilityDiff(BaseModel):
    backward: CompatibilityChange
    forward: CompatibilityChange


class MetricDiff(BaseModel):
    baseline: int
    current: int
    delta: int


class DirectionSummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff


class SummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff
    backward: DirectionSummaryDiff
    forward: DirectionSummaryDiff


class CompareResponse(BaseModel):
    baseline_id: int
    current_id: int
    compatibility: CompatibilityDiff
    summary_diff: SummaryDiff
    issues: IssueGroups
    counts: IssueGroupCounts


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate 发布门禁
# ---------------------------------------------------------------------------


class ExemptionRule(BaseModel):
    """issue 豁免规则：direction / code 字面相等，path 支持单段 ``*`` 通配。"""

    direction: Literal["backward", "forward"] = Field(..., description="兼容方向")
    code: str = Field(..., min_length=1, description="issue 代码（精确匹配）")
    path: str = Field(
        ...,
        description=(
            "JSON Pointer 风格 schema 路径（如 #/properties/a）；"
            "单段 * 为通配（如 #/properties/*），根路径为 #"
        ),
    )

    @model_validator(mode="after")
    def _validate_path(self):
        path = self.path
        if path != "#" and not path.startswith("#/"):
            raise ValueError('path 必须为 "#" 或以 "#/" 开头的 JSON Pointer')
        if path != "#":
            for segment in path[2:].split("/"):
                if segment == "":
                    raise ValueError("path 含空路径段")
        return self


class GateRequest(BaseModel):
    """POST /api/analyses/{id}/gate 请求体。"""

    directions: list[Literal["backward", "forward"]] = Field(
        ..., min_length=1, description="需要检查的兼容方向（至少一个）"
    )
    max_warnings: dict[Literal["backward", "forward"], int] | None = Field(
        default=None,
        description="各方向允许的未豁免 warning 上限（未声明方向视为 0）",
    )
    exemptions: list[ExemptionRule] = Field(
        default_factory=list, description="issue 豁免规则，按声明顺序确定优先级"
    )

    @model_validator(mode="after")
    def _dedupe_directions(self):
        seen: set[str] = set()
        unique: list[str] = []
        for name in self.directions:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        self.directions = unique
        if self.max_warnings:
            for name, value in self.max_warnings.items():
                if value < 0:
                    raise ValueError(f"max_warnings.{name} 不能为负数")
        return self


class GateDirectionCounts(BaseModel):
    errors: int
    warnings: int
    exempted: int
    total: int
    max_warnings: int


class GateBlockingIssue(Issue):
    blocked_reason: Literal["error", "warning_limit_exceeded"]


class GateExemptedIssue(Issue):
    rule_index: int = Field(..., ge=0, description="命中的豁免规则下标（exemptions 从 0 开始）")


class GateResponse(BaseModel):
    # "pass" 是 Python 关键字，模型字段使用 passed，对外序列化为 "pass"
    passed: bool = Field(serialization_alias="pass", description="门禁是否通过")
    counts: dict[Literal["backward", "forward"], GateDirectionCounts]
    blocking: list[GateBlockingIssue]
    exempted: list[GateExemptedIssue]

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/profile 文档画像
# ---------------------------------------------------------------------------


class ProfileRequest(BaseModel):
    """``POST /api/analyses/{id}/profile`` 请求体。

    ``documents`` 为待检 JSON 文档列表：至少 1 个、最多
    ``MAX_PROFILE_DOCUMENTS``（5000）个，且每个文档必须是 JSON 对象
    （dict）；超限或包含非对象文档由 Pydantic 直接拒绝为 422，
    不写入任何分析 / 画像记录。
    """

    documents: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        max_length=MAX_PROFILE_DOCUMENTS,
        description=f"待检 JSON 文档（对象），数量 1~{MAX_PROFILE_DOCUMENTS}",
    )


class ProfileFailureGroup(BaseModel):
    """一条按 JSON Pointer 归并的失败规则。"""

    path: str = Field(..., description="最深公共失败路径（JSON Pointer，根为 #）")
    validator: str = Field(..., description="jsonschema 校验规则（oneOf/type/required…）")
    fingerprint: str = Field(..., description="稳定规则指纹（与输入顺序、实例取值无关）")
    document_count: int = Field(..., description="命中该失败的文档数（同一文档只计一次）")
    example_indices: list[int] = Field(
        ..., description="示例文档在提交列表中的下标（升序，最多保留 10 个最小下标）"
    )
    message: str = Field(..., description="代表性错误信息（同类中字典序最小，稳定）")


class ProfileResponse(BaseModel):
    """画像快照（创建与查询共用）。"""

    analysis_id: int
    total: int = Field(..., description="待检文档总数")
    valid: int = Field(..., description="通过新版 schema 校验的文档数")
    invalid: int = Field(..., description="未通过校验的文档数")
    compliance_rate: float = Field(..., description="合规率 valid/total，保留 6 位小数")
    failures: list[ProfileFailureGroup]
    created_at: str = Field(..., description="快照创建时间（ISO 8601）")
