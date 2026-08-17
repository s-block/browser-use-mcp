from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field


class BrowserControlCommand(StrEnum):
    CLICK = "click"
    FILL = "fill"
    TYPE = "type"
    KEY_PRESS = "key_press"
    TEXT = "text"
    TITLE = "title"
    URL = "url"


ProfileName = Annotated[str, Field(min_length=1, max_length=64)]
SessionId = Annotated[str, Field(min_length=16, max_length=64)]
Instruction = Annotated[str, Field(min_length=1, max_length=10_000)]
Selector = Annotated[str, Field(min_length=1, max_length=4_096)]
ControlValue = Annotated[str, Field(max_length=16_384)]
NavigationURL = Annotated[str, Field(min_length=1, max_length=2_048)]


class SessionStarted(BaseModel):
    session_id: str
    persistent: bool
    profile_name: str | None


class SessionClosed(BaseModel):
    closed: bool = True


class NavigationResult(BaseModel):
    url: str
    title: str
    status: int | None


class ActResult(BaseModel):
    success: bool
    message: str


class ObservedAction(BaseModel):
    selector: str
    description: str
    method: str | None


class ObserveResult(BaseModel):
    actions: list[ObservedAction]
    truncated: bool = False


class ExtractResult(BaseModel):
    extraction: str
    truncated: bool = False


class ControlResult(BaseModel):
    value: str | None = None
    truncated: bool = False


class ProfileInfo(BaseModel):
    name: str
    created_at: str
    updated_at: str


class ProfileListResult(BaseModel):
    profiles: list[ProfileInfo]
