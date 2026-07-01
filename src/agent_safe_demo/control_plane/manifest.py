"""Schema for the per-app ``statefork.yaml`` manifest.

Every directory under ``app_plane/`` that contains a ``statefork.yaml`` is
discovered as a selectable app (see ``app_registry``). The manifest declares
everything the control plane needs to build and run the app under StateFork:
the Dockerfile to build, the command that starts the app inside the
checkpointed container, and the paths used to health-check and embed its UI.

Adding a new shop = adding a directory with a Dockerfile and a manifest; no
control-plane code changes are required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_TEMPLATE_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


class RuntimeManifest(BaseModel):
    """How the app runs inside the StateFork-managed container."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    cwd: str = "/"
    port_env: str = "PORT"
    health_path: str = "/health"
    ui_path: str = "/"
    # Extra environment for the runtime process (values may use ${PORT},
    # ${HOST}, ${BRANCH_WORKDIR} templates).
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("command", "cwd", "port_env", "health_path", "ui_path")
    @classmethod
    def not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped

    @field_validator("env")
    @classmethod
    def env_values_are_not_blank(cls, values: dict[str, str]) -> dict[str, str]:
        normalized = {}
        for key, value in values.items():
            clean_key = key.strip()
            clean_value = value.strip()
            if not clean_key or not clean_value:
                raise ValueError("runtime.env keys and values cannot be blank")
            normalized[clean_key] = clean_value
        return normalized


class BuildManifest(BaseModel):
    """Where the app's Dockerfile lives, relative to the manifest."""

    model_config = ConfigDict(extra="forbid")

    dockerfile_dir: str = "."

    @field_validator("dockerfile_dir")
    @classmethod
    def dockerfile_dir_not_blank(cls, value: str) -> str:
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
    build: BuildManifest = Field(default_factory=BuildManifest)

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
    """Replace ``${NAME}`` placeholders; unknown names are left as-is."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            return match.group(0)
        return str(variables[key])

    return _TEMPLATE_RE.sub(replace, value)
