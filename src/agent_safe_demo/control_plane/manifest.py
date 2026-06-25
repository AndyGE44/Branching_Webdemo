from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


_TEMPLATE_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


class RuntimeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["process", "checkpoint_exec"] = "process"
    command: str = Field(min_length=1)
    cwd: str = "."
    port_env: str = "PORT"
    health_path: str = "/api/state"
    ui_path: str = "/"

    @field_validator("command", "cwd", "port_env", "health_path", "ui_path")
    @classmethod
    def not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped


class StateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # May be empty for apps whose state is in-memory and captured by the
    # container/process checkpoint (e.g. the shopgym shops) rather than a file.
    files: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def files_are_not_blank(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("state.files cannot contain blank paths")
        return normalized

    @field_validator("env")
    @classmethod
    def env_values_are_not_blank(cls, values: dict[str, str]) -> dict[str, str]:
        normalized = {}
        for key, value in values.items():
            clean_key = key.strip()
            clean_value = value.strip()
            if not clean_key or not clean_value:
                raise ValueError("state.env keys and values cannot be blank")
            normalized[clean_key] = clean_value
        return normalized


class BuildManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dockerfile_dir: str = "."

    @field_validator("dockerfile_dir")
    @classmethod
    def dockerfile_dir_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped


class ObservabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_summary_path: str = "/api/state"

    @field_validator("state_summary_path")
    @classmethod
    def path_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped


class StateForkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    runtime: RuntimeManifest
    state: StateManifest
    observability: ObservabilityManifest
    build: BuildManifest | None = None

    @field_validator("id", "name")
    @classmethod
    def required_strings_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped


def load_manifest(path: Path) -> StateForkManifest:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML in {path}: {error}") from error

    if not isinstance(raw, dict):
        raise ValueError(f"Manifest {path} must contain a YAML mapping")

    try:
        return StateForkManifest.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"Invalid manifest {path}: {error}") from error


def interpolate_template(value: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            return match.group(0)
        return str(variables[key])

    return _TEMPLATE_RE.sub(replace, value)
