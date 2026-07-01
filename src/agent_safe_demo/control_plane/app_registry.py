"""Discovers app manifests under ``app_plane/`` and exposes them as AppSpecs.

An :class:`AppSpec` is the resolved, ready-to-use view of one ``statefork.yaml``
manifest: absolute Dockerfile directory, runtime command, and the paths the
control plane uses to embed the app. ``DEMO_VISIBLE_APP_IDS`` (comma-separated)
restricts which discovered apps the UI offers; ``DEMO_APP_ID`` picks the default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_safe_demo.control_plane.manifest import (
    StateForkManifest,
    interpolate_template,
    load_manifest,
)

APP_PLANE_DIR = Path(__file__).resolve().parents[1] / "app_plane"
# repo root: control_plane -> agent_safe_demo -> src -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AppSpec:
    id: str
    label: str
    description: str
    manifest_path: Path
    dockerfile_dir: Path
    runtime_command: str
    runtime_cwd: str
    runtime_port_env: str
    runtime_env: dict[str, str] = field(default_factory=dict)
    health_path: str = "/health"
    ui_path: str = "/"

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "health_path": self.health_path,
            "ui_path": self.ui_path,
            "runtime_command": self.runtime_command,
            "manifest_path": str(self.manifest_path),
            "dockerfile_dir": str(self.dockerfile_dir),
        }


def _spec_from_manifest(path: Path, manifest: StateForkManifest) -> AppSpec:
    app_dir = path.parent
    raw_dir = interpolate_template(
        manifest.build.dockerfile_dir,
        {"APP_DIR": str(app_dir), "PROJECT_ROOT": str(REPO_ROOT)},
    )
    dockerfile_dir = Path(raw_dir)
    if not dockerfile_dir.is_absolute():
        dockerfile_dir = (app_dir / dockerfile_dir).resolve()
    return AppSpec(
        id=manifest.id,
        label=manifest.name,
        description=manifest.description,
        manifest_path=path,
        dockerfile_dir=dockerfile_dir,
        runtime_command=manifest.runtime.command,
        runtime_cwd=manifest.runtime.cwd,
        runtime_port_env=manifest.runtime.port_env,
        runtime_env=dict(manifest.runtime.env),
        health_path=manifest.runtime.health_path,
        ui_path=manifest.runtime.ui_path,
    )


def _manifest_specs(app_plane_dir: Path) -> tuple[dict[str, AppSpec], list[dict[str, str]]]:
    specs: dict[str, AppSpec] = {}
    errors: list[dict[str, str]] = []
    if not app_plane_dir.exists():
        return specs, errors
    for path in sorted(app_plane_dir.glob("*/statefork.yaml")):
        try:
            manifest = load_manifest(path)
            if manifest.id in specs:
                raise ValueError(f"Duplicate manifest id: {manifest.id}")
            specs[manifest.id] = _spec_from_manifest(path, manifest)
        except Exception as error:
            errors.append({"path": str(path), "error": str(error)})
    return specs, errors


def _visible_app_ids() -> frozenset[str] | None:
    """User-facing allowlist from DEMO_VISIBLE_APP_IDS (comma-separated). When
    unset, all discovered apps are shown."""
    raw = os.getenv("DEMO_VISIBLE_APP_IDS", "").strip()
    if not raw:
        return None
    ids = frozenset(part.strip() for part in raw.split(",") if part.strip())
    return ids or None


def build_app_specs(app_plane_dir: Path | None = None) -> dict[str, AppSpec]:
    specs, _ = _manifest_specs(app_plane_dir or APP_PLANE_DIR)
    visible = _visible_app_ids()
    if visible is not None:
        filtered = {app_id: spec for app_id, spec in specs.items() if app_id in visible}
        # Ignore the filter if it would hide everything (misconfigured env), so
        # the selector never ends up empty.
        if filtered:
            return filtered
    return specs


def list_manifest_errors(app_plane_dir: Path | None = None) -> list[dict[str, str]]:
    _, errors = _manifest_specs(app_plane_dir or APP_PLANE_DIR)
    return errors


def list_app_specs() -> list[AppSpec]:
    return list(build_app_specs().values())


def get_app_spec(app_id: str | None = None) -> AppSpec:
    specs = build_app_specs()
    explicit = app_id is not None
    selected = app_id or os.getenv("DEMO_APP_ID", "shop_clothing")
    if selected in specs:
        return specs[selected]
    # For the default/env-resolved app, fall back to the first available app
    # rather than crash. An explicit request for a missing app is still an error.
    if not explicit and specs:
        return specs[sorted(specs)[0]]
    available = ", ".join(sorted(specs))
    raise ValueError(f"Unknown app id: {selected}. Available apps: {available}")
