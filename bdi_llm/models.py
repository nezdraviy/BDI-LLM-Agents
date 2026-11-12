from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict


class KnowledgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    content: str
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    status: Literal["approved", "proposed", "rejected"] = "approved"
    source: str | None = None
    applicable_task_types: list[str] = Field(default_factory=list)


class PreviousAttempt(BaseModel):
    attempt: int
    summary: str
    validation_errors: list[str] = Field(default_factory=list)


class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    agent_id: str
    task_type: str
    goal: str
    context: dict[str, Any] = Field(default_factory=dict)
    knowledge: list[KnowledgeItem] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    previous_attempts: list[PreviousAttempt] = Field(default_factory=list)


class ProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["proposal", "insufficient_data", "no_solution"]
    problem: str
    proposal: ProposalBody | None = None
    reasoning_summary: str = ""
    assumptions: list[str] = Field(default_factory=list)
    knowledge_references: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ValidationReport(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LLMResult(BaseModel):
    task_id: str
    status: Literal["accepted", "needs_human_review", "failed"]
    proposal: Proposal | None = None
    validation: ValidationReport
    attempts_used: int
    message: str
