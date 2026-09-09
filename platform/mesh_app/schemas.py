"""FastAPI 请求和公共错误结构。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """拒绝悄然忽略前端拼写错误。"""

    model_config = ConfigDict(extra="forbid")


class ControlChange(StrictModel):
    """结构化控制变更；具体 set/clear 约束由控制服务统一校验。"""

    key: str = Field(min_length=1, max_length=200)
    selector: str = Field(min_length=1, max_length=500)
    op: Literal["set", "clear"]
    value: Any | None = None


class ControlPreviewRequest(StrictModel):
    parent_run_id: str = Field(min_length=1, max_length=200)
    changes: list[ControlChange] = Field(default_factory=list, max_length=2000)


class CreateRunRequest(ControlPreviewRequest):
    expected_version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=200)
    confirm_required_clears: bool = False


class RetryRunRequest(StrictModel):
    expected_version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=200)


class ExperienceNoteRequest(StrictModel):
    note: str = Field(max_length=20000)
    expected_note_version: int = Field(ge=0)


class CompleteSessionRequest(StrictModel):
    run_id: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail


__all__ = [
    "CompleteSessionRequest",
    "ControlChange",
    "ControlPreviewRequest",
    "CreateRunRequest",
    "ErrorResponse",
    "ExperienceNoteRequest",
    "LoginRequest",
    "RetryRunRequest",
]
