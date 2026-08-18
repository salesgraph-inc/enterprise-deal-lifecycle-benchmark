from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import selectors
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from .causal import normalize_official_seeds
from .engine import SELLER_ROLES, TRACE_KINDS, RunEngine
from .models import (
    Actor,
    Artifact,
    Checkpoint,
    Event,
    RoleGrant,
    RunManifest,
    ScenarioManifest,
    TraceEvent,
    stable_hash,
    to_json,
)
from .protocol import (
    MAX_PROTOCOL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    Message,
    ProtocolError,
    ToolCall,
    ToolResult,
    decode,
)

TOOLS: tuple[dict[str, Any], ...] = (
    {"tool": "crm", "actions": ("search", "read", "history", "update", "merge")},
    {"tool": "communications", "actions": ("search", "read", "send")},
    {"tool": "calendar", "actions": ("list", "schedule", "reschedule", "cancel")},
    {"tool": "documents", "actions": ("search", "read", "create", "revise", "attach")},
    {"tool": "approvals", "actions": ("list", "request", "approve", "reject")},
    {"tool": "web", "actions": ("search", "open")},
    {"tool": "team", "actions": ("inbox", "search", "send")},
    {"tool": "run", "actions": ("status", "yield", "complete_checkpoint")},
)
NORMALIZED_ACTIONS = frozenset(
    {"calendar.reschedule", "approvals.approve", "approvals.reject", "web.open"}
)
READ_ACTIONS = frozenset(
    {"search", "read", "history", "list", "inbox", "status", "yield"}
)
ROLE_ORDER = tuple(SELLER_ROLES)


class RunnerError(RuntimeError):
    pass


class BundleError(RunnerError):
    pass


class AgentProcessError(RunnerError):
    pass


class ProtocolViolation(RunnerError):
    pass


@dataclass(frozen=True, slots=True)
class WorldBundle:
    path: Path
    manifest: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    artifacts: tuple[Mapping[str, Any], ...]
    rubric_path: Path
    oracle_path: Path | None
    private: bool
    actor_rows: tuple[Mapping[str, Any], ...] = ()
    checkpoint_rows: tuple[Mapping[str, Any], ...] = ()

    @property
    def world_id(self) -> str:
        return str(self.manifest["world_id"])

    @property
    def split(self) -> str:
        return str(self.manifest.get("split", "dev"))


@dataclass(frozen=True, slots=True)
class RunLimits:
    tool_calls_per_checkpoint: int | None = None
    turns_per_checkpoint: int | None = None
    timeout_seconds: float | None = None
    retries: int = 0

    def __post_init__(self) -> None:
        if self.tool_calls_per_checkpoint is not None and (
            not isinstance(self.tool_calls_per_checkpoint, int)
            or isinstance(self.tool_calls_per_checkpoint, bool)
            or self.tool_calls_per_checkpoint < 1
        ):
            raise ValueError("tool_calls_per_checkpoint must be positive")
        if self.turns_per_checkpoint is not None and (
            not isinstance(self.turns_per_checkpoint, int)
            or isinstance(self.turns_per_checkpoint, bool)
            or self.turns_per_checkpoint < 1
        ):
            raise ValueError("turns_per_checkpoint must be positive")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            not isinstance(self.retries, int)
            or isinstance(self.retries, bool)
            or self.retries < 0
        ):
            raise ValueError("retries must be a non-negative integer")


def _bound_run_limits(engine: RunEngine, limits: RunLimits | None) -> RunLimits:
    manifest_limits = RunLimits(
        engine.manifest.limits.get("tool_calls_per_checkpoint"),
        engine.manifest.limits.get("turns_per_checkpoint"),
        engine.manifest.limits.get("timeout_seconds"),
        engine.manifest.limits.get("retries", 0),
    )
    if limits is not None and limits != manifest_limits:
        raise BundleError("runner limits do not match the run manifest")
    return manifest_limits


def _valid_digest(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(item in "0123456789abcdef" for item in digest)


def normalize_agent_manifest(
    value: Mapping[str, Any] | None,
    *,
    require_resolved: bool = False,
) -> dict[str, Any]:
    if value is None:
        value = {
            "resolved": False,
            "roles": {role: "unresolved" for role in ROLE_ORDER},
            "models": {},
        }
    if set(value) != {"resolved", "roles", "models"}:
        raise BundleError("agent manifest must contain resolved, roles, and models")
    resolved = value.get("resolved")
    roles = value.get("roles")
    models = value.get("models")
    if not isinstance(resolved, bool):
        raise BundleError("agent manifest resolved marker must be boolean")
    if not isinstance(roles, Mapping) or set(roles) != set(ROLE_ORDER):
        raise BundleError("agent manifest must map all four seller roles")
    if any(not isinstance(roles[role], str) or not roles[role] for role in ROLE_ORDER):
        raise BundleError("agent manifest role model keys must be non-empty strings")
    if not isinstance(models, Mapping):
        raise BundleError("agent manifest models must be an object")
    role_models = {role: roles[role] for role in ROLE_ORDER}
    normalized_models: dict[str, Any] = {}
    for key, raw in models.items():
        if not isinstance(key, str) or not key or not isinstance(raw, Mapping):
            raise BundleError("agent manifest model entries are invalid")
        required_fields = {
            "model_id",
            "model_digest",
            "prompt_hash",
            "provider_settings",
            "provider_defaults",
        }
        fields = set(raw)
        if fields != required_fields and fields != required_fields | {
            "provider_defaults_digest"
        }:
            raise BundleError("agent manifest model entry fields are invalid")
        model_id = raw.get("model_id")
        model_digest = raw.get("model_digest")
        prompt_hash = raw.get("prompt_hash")
        provider_settings = raw.get("provider_settings")
        provider_defaults = raw.get("provider_defaults")
        provider_defaults_digest = raw.get("provider_defaults_digest")
        if not isinstance(model_id, str) or not model_id.strip():
            raise BundleError("agent manifest model_id must be non-empty")
        if model_digest is not None and not _valid_digest(model_digest):
            raise BundleError("agent manifest model_digest is invalid")
        if not _valid_digest(prompt_hash):
            raise BundleError("agent manifest prompt_hash is invalid")
        if not isinstance(provider_settings, Mapping):
            raise BundleError("agent manifest provider_settings must be an object")
        if not isinstance(provider_defaults, bool):
            raise BundleError("agent manifest provider_defaults must be boolean")
        if provider_defaults_digest is not None and not _valid_digest(
            provider_defaults_digest
        ):
            raise BundleError("agent manifest provider_defaults_digest is invalid")
        if provider_defaults and provider_defaults_digest is None:
            raise BundleError(
                "resolved provider defaults require a pinned configuration digest"
            )
        try:
            settings = json.loads(
                json.dumps(dict(provider_settings), allow_nan=False, sort_keys=True)
            )
        except (TypeError, ValueError) as exc:
            raise BundleError("agent manifest provider_settings must be JSON") from exc
        normalized_model = {
            "model_id": model_id,
            "model_digest": model_digest,
            "prompt_hash": prompt_hash,
            "provider_settings": settings,
            "provider_defaults": provider_defaults,
        }
        if "provider_defaults_digest" in raw:
            normalized_model["provider_defaults_digest"] = provider_defaults_digest
        normalized_models[key] = normalized_model
    if resolved and (
        not normalized_models
        or any(model_key not in normalized_models for model_key in role_models.values())
    ):
        raise BundleError("resolved agent manifest role models must exist")
    if resolved and any(
        model["model_digest"] is None for model in normalized_models.values()
    ):
        raise BundleError("resolved agent manifest model digests must be pinned")
    if require_resolved and not resolved:
        raise BundleError("external execution requires a resolved agent manifest")
    return {"resolved": resolved, "roles": role_models, "models": normalized_models}


def deterministic_agent_manifest(name: str) -> dict[str, Any]:
    if not name:
        raise ValueError("deterministic agent manifest name is required")
    model_key = "deterministic"
    return normalize_agent_manifest(
        {
            "resolved": True,
            "roles": {role: model_key for role in ROLE_ORDER},
            "models": {
                model_key: {
                    "model_id": name,
                    "model_digest": stable_hash({"model": name}),
                    "prompt_hash": stable_hash({"prompt": name}),
                    "provider_settings": {},
                    "provider_defaults": False,
                    "provider_defaults_digest": None,
                }
            },
        },
        require_resolved=True,
    )


def normalize_environment_manifest(
    value: Mapping[str, Any] | None,
    *,
    require_resolved: bool = False,
) -> dict[str, Any]:
    if value is None:
        value = {
            "resolved": False,
            "runtime_version": (
                f"{sys.implementation.name}-{sys.version_info.major}."
                f"{sys.version_info.minor}.{sys.version_info.micro}"
            ),
            "image_digest": None,
            "git_revision": None,
            "executor_policy_digest": None,
        }
    if set(value) != {
        "resolved",
        "runtime_version",
        "image_digest",
        "git_revision",
        "executor_policy_digest",
    }:
        raise BundleError("environment manifest fields are invalid")
    resolved = value.get("resolved")
    runtime_version = value.get("runtime_version")
    image_digest = value.get("image_digest")
    git_revision = value.get("git_revision")
    executor_policy_digest = value.get("executor_policy_digest")
    if not isinstance(resolved, bool):
        raise BundleError("environment manifest resolved marker must be boolean")
    if not isinstance(runtime_version, str) or not runtime_version.strip():
        raise BundleError("environment runtime_version must be non-empty")
    if image_digest is not None and not _valid_digest(image_digest):
        raise BundleError("environment image_digest is invalid")
    if git_revision is not None and not (
        isinstance(git_revision, str)
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", git_revision)
    ):
        raise BundleError("environment git_revision must be an exact revision")
    if executor_policy_digest is not None and not _valid_digest(executor_policy_digest):
        raise BundleError("environment executor_policy_digest is invalid")
    if resolved and (
        image_digest is None or git_revision is None or executor_policy_digest is None
    ):
        raise BundleError(
            "resolved environment requires immutable artifact and executor policy digests and an exact revision"
        )
    if require_resolved and not resolved:
        raise BundleError("external execution requires a resolved environment manifest")
    return {
        "resolved": resolved,
        "runtime_version": runtime_version,
        "image_digest": image_digest,
        "git_revision": git_revision,
        "executor_policy_digest": executor_policy_digest,
    }


def validate_track_agent_manifest(
    track: str, agent_manifest: Mapping[str, Any]
) -> None:
    if track not in {"fixed_harness", "open_team"}:
        raise BundleError("track must be fixed_harness or open_team")
    roles = agent_manifest.get("roles")
    if track == "fixed_harness" and (
        not isinstance(roles, Mapping) or len(set(roles.values())) != 1
    ):
        raise BundleError("fixed_harness requires one model configuration")


@dataclass(slots=True)
class RunResult:
    run_id: str
    world_id: str
    track: str
    status: str
    tool_calls: int = 0
    turns: int = 0
    retries: int = 0
    latency_ms: int = 0
    cost_minor_units: int | None = None
    invalid_actions: int = 0
    error_count: int = 0
    tokens: int | None = None
    errors: list[str] = field(default_factory=list)
    model_metadata: dict[str, Any] = field(default_factory=dict)
    state_hash: str | None = None
    output_dir: Path | None = None
    trace_path: Path | None = None
    manifest_path: Path | None = None
    diagnostic_replay: bool = False

    def to_dict(self) -> dict[str, Any]:
        token_usage = self.model_metadata.get("token_usage")
        tokens = (
            sum(
                int(value)
                for value in token_usage.values()
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            )
            if isinstance(token_usage, Mapping)
            else self.tokens
        )
        unavailable = [
            key
            for key in ("model_latency_ms", "token_usage", "cost_minor_units")
            if key not in self.model_metadata
        ]
        return {
            "run_id": self.run_id,
            "world_id": self.world_id,
            "track": self.track,
            "status": self.status,
            "resource_usage": {
                "tool_calls": self.tool_calls,
                "turns": self.turns,
                "retries": self.retries,
                "latency_ms": self.latency_ms,
                "cost_minor_units": self.cost_minor_units,
                "invalid_actions": self.invalid_actions,
                "errors": self.error_count,
                "tokens": tokens,
                "metric_availability": {
                    "cost_minor_units": self.cost_minor_units is not None,
                    "tokens": tokens is not None,
                },
                "model_metadata": dict(self.model_metadata),
                "unavailable_metrics": unavailable,
            },
            "errors": list(self.errors),
            "state_hash": self.state_hash,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "trace_path": str(self.trace_path) if self.trace_path else None,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "diagnostic_replay": self.diagnostic_replay,
        }


def _resolve_bundle_file(
    root: Path, value: str | Path, *, required: bool = True
) -> Path | None:
    if not isinstance(value, (str, Path)):
        raise BundleError(f"bundle file path must be a string: {value!r}")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise BundleError(f"bundle file path escapes bundle root: {value!r}")
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise BundleError(f"cannot resolve bundle file path {value!r}: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BundleError(f"bundle file path escapes bundle root: {value!r}") from exc
    if not resolved.is_file():
        if required:
            raise BundleError(f"bundle file is missing: {value!r}")
        return None
    return resolved


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read JSON file {path}: {exc}") from exc


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BundleError(f"cannot read JSONL file {path}: {exc}") from exc
    result: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleError(f"invalid JSONL at {path}:{number}: {exc.msg}") from exc
        if not isinstance(value, Mapping):
            raise BundleError(f"JSONL row at {path}:{number} is not an object")
        result.append(dict(value))
    return result


def _bundle_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_file() and candidate.name == "manifest.json":
        return candidate.parent
    if candidate.is_dir() and (candidate / "manifest.json").is_file():
        return candidate
    raise BundleError(f"world bundle manifest.json not found at {candidate}")


def load_world_bundle(path: str | Path, allow_private: bool = False) -> WorldBundle:
    root = _bundle_path(path).resolve()
    manifest_path = _resolve_bundle_file(root, "manifest.json")
    assert manifest_path is not None
    manifest = _json(manifest_path)
    if not isinstance(manifest, Mapping) or not manifest.get("world_id"):
        raise BundleError("world manifest must contain world_id")
    release_visibility = manifest.get("release_visibility")
    if release_visibility not in {"public", "private"}:
        raise BundleError("world manifest must contain valid release_visibility")
    private = release_visibility == "private"
    if private and not allow_private:
        raise BundleError("private world bundles require allow_private=True")
    events_path = _resolve_bundle_file(root, "events.jsonl")
    artifacts_path = _resolve_bundle_file(root, "artifacts.jsonl")
    assert events_path is not None and artifacts_path is not None
    events = tuple(_jsonl(events_path))
    artifacts = tuple(_jsonl(artifacts_path))
    for row in artifacts:
        artifact_path = row.get("path")
        if artifact_path is not None:
            _resolve_bundle_file(root, artifact_path)
    actor_file = _resolve_bundle_file(root, "actors.jsonl", required=False)
    checkpoint_file = _resolve_bundle_file(root, "checkpoints.jsonl", required=False)
    actor_rows = tuple(_jsonl(actor_file)) if actor_file is not None else ()
    checkpoint_rows = (
        tuple(_jsonl(checkpoint_file)) if checkpoint_file is not None else ()
    )
    rubric_path = _resolve_bundle_file(root, "rubric.json")
    assert rubric_path is not None
    oracle_path = _resolve_bundle_file(root, "oracle.json", required=False)
    return WorldBundle(
        root,
        dict(manifest),
        events,
        artifacts,
        rubric_path,
        oracle_path,
        private,
        actor_rows,
        checkpoint_rows,
    )


def validate_world_bundle(
    path: str | Path, allow_private: bool = False
) -> dict[str, Any]:
    bundle = load_world_bundle(path, allow_private=allow_private)
    manifest = bundle.manifest
    errors: list[str] = []
    if bundle.path.name != bundle.world_id:
        errors.append("bundle directory name does not match manifest world_id")
    required = ("world_id", "vertical", "split", "release_visibility")
    errors.extend(f"manifest missing {key}" for key in required if key not in manifest)
    synthetic = manifest.get(
        "synthetic", (manifest.get("provenance") or {}).get("synthetic_only")
    )
    if synthetic is not True:
        errors.append("manifest synthetic must be true")
    artifact_total = int(manifest.get("artifact_total", len(bundle.artifacts)))
    if len(bundle.artifacts) != artifact_total:
        errors.append("artifact_total does not match artifacts.jsonl")
    artifact_paths: set[str] = set()
    available: list[str] = []
    for row in bundle.artifacts:
        artifact_path = row.get("path")
        source_uri = (
            (row.get("content") or {}).get("source_uri")
            if isinstance(row.get("content"), Mapping)
            else None
        )
        path_value = artifact_path if artifact_path is not None else source_uri
        if path_value is not None:
            if (
                not isinstance(path_value, str)
                or Path(path_value).is_absolute()
                or ".." in Path(path_value).parts
            ):
                errors.append(f"invalid artifact path {path_value!r}")
                continue
            artifact_paths.add(path_value)
            if not (bundle.path / path_value).is_file():
                errors.append(f"missing artifact file {path_value}")
        available_at = row.get("available_at")
        if isinstance(available_at, str):
            available.append(available_at)
    for event in bundle.events:
        if not event.get("event_id") or not event.get("available_at"):
            errors.append("event is missing event_id or available_at")
    if bundle.private and bundle.split != "blind":
        errors.append("private bundle must use the blind split")
    if not (bundle.path / "hidden_events.jsonl").is_file():
        errors.append("world bundle is missing hidden_events.jsonl")
    result = {
        "valid": not errors,
        "world_id": bundle.world_id,
        "split": bundle.split,
        "vertical": manifest.get("vertical"),
        "schema_version": manifest.get("schema_version"),
        "dataset_version": manifest.get(
            "dataset_version", manifest.get("schema_version")
        ),
        "seed": manifest.get("seed"),
        "private": bundle.private,
        "event_count": len(bundle.events),
        "artifact_count": len(bundle.artifacts),
        "checkpoint_count": len(bundle.checkpoint_rows)
        or len(manifest.get("checkpoint_ids", ())),
        "duration_days": manifest.get("duration_days"),
        "artifact_paths": len(artifact_paths),
        "available_at_min": min(available) if available else None,
        "available_at_max": max(available) if available else None,
        "errors": errors,
    }
    if bundle.private:
        result["pair_id"] = manifest.get("pair_id")
    return result


def _dataset_sidecar(root: Path, relative: str) -> Path | None:
    for parent in (root, *root.parents):
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    return None


def _dataset_observed_extras(
    root: Path, worlds: Sequence[Mapping[str, Any]]
) -> tuple[int | None, int | None, list[str]]:
    shared_path = _dataset_sidecar(root, "authoring/shared_documents.jsonl")
    shared_count = len(_jsonl(shared_path)) if shared_path is not None else None
    pair_path = _dataset_sidecar(root, "authoring/worlds.jsonl")
    errors: list[str] = []
    observed_ids = {
        str(row["world_id"]) for row in worlds if row.get("world_id") is not None
    }
    public_ids = {
        str(row["world_id"])
        for row in worlds
        if not row.get("private") and row.get("world_id") is not None
    }
    visible_pairs: dict[str, str] = {}
    if pair_path is not None:
        pair_rows = _jsonl(pair_path)
        rows_by_world: dict[str, list[Mapping[str, Any]]] = {}
        for row in pair_rows:
            if row.get("world_id") is not None:
                rows_by_world.setdefault(str(row["world_id"]), []).append(row)
        for world_id in sorted(public_ids):
            matches = rows_by_world.get(world_id, [])
            if len(matches) != 1:
                errors.append(
                    f"public authoring identity is missing or duplicate for {world_id}"
                )
                continue
            if matches[0].get("pair_id"):
                visible_pairs[world_id] = str(matches[0]["pair_id"])
    elif public_ids:
        errors.append("public authoring identities are missing")
    private_rows = [row for row in worlds if row.get("private")]
    validation_path = _dataset_sidecar(
        root,
        "private/validation.json" if private_rows else "authoring/validation.json",
    )
    validation = _json(validation_path) if validation_path is not None else {}
    pair_count = (
        int(validation["pair_count"])
        if isinstance(validation, Mapping)
        and isinstance(validation.get("pair_count"), int)
        and not isinstance(validation.get("pair_count"), bool)
        else None
    )
    pair_diffs = (
        validation.get("pair_diffs", ()) if isinstance(validation, Mapping) else ()
    )
    pair_members: dict[str, set[str]] = {}
    world_pairs: dict[str, str] = {}
    seen_pair_ids: set[str] = set()
    expected_differences = {
        "opaque_public_identity_projection",
        "declared_intervention",
        "causal_descendants_after_intervention",
    }
    if not isinstance(pair_diffs, Sequence) or isinstance(pair_diffs, (str, bytes)):
        pair_diffs = ()
    for item in pair_diffs:
        if not isinstance(item, Mapping) or not item.get("pair_id"):
            errors.append("pair identity is invalid")
            continue
        members = item.get("world_ids")
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            errors.append(f"pair {item['pair_id']} world_ids are invalid")
            continue
        member_ids = {str(value) for value in members}
        if not member_ids & observed_ids:
            continue
        pair_id = str(item["pair_id"])
        if pair_id in seen_pair_ids:
            errors.append(f"duplicate pair identity: {pair_id}")
        seen_pair_ids.add(pair_id)
        if item.get("base_facts_equal") is not True:
            errors.append(f"pair {pair_id} base facts are not equal")
        if item.get("pre_intervention_artifacts_equal") is not True:
            errors.append(f"pair {pair_id} pre-intervention artifacts are not equal")
        artifact_differences = item.get("post_intervention_artifact_differences")
        if (
            not isinstance(artifact_differences, int)
            or isinstance(artifact_differences, bool)
            or artifact_differences < 1
        ):
            errors.append(
                f"pair {pair_id} has no post-intervention artifact differences"
            )
        allowed_differences = item.get("allowed_differences")
        if (
            not isinstance(allowed_differences, Sequence)
            or isinstance(allowed_differences, (str, bytes))
            or len(allowed_differences) != len(expected_differences)
            or {str(value) for value in allowed_differences} != expected_differences
        ):
            errors.append(f"pair {pair_id} allowed differences are invalid")
        for value in members:
            world_id = str(value)
            if world_id in world_pairs and world_pairs[world_id] != pair_id:
                errors.append(f"world {world_id} belongs to multiple pairs")
            world_pairs[world_id] = pair_id
            pair_members.setdefault(pair_id, set()).add(world_id)
    for pair_id, members in pair_members.items():
        if len(members) != 2:
            errors.append(f"pair {pair_id} does not contain exactly two worlds")
            continue
        paired_worlds = [row for row in worlds if str(row.get("world_id")) in members]
        if len({row.get("split") for row in paired_worlds}) != 1:
            errors.append(f"pair {pair_id} spans multiple splits")
        if len({row.get("vertical") for row in paired_worlds}) != 1:
            errors.append(f"pair {pair_id} spans multiple verticals")
    if set(world_pairs) != observed_ids:
        errors.append("pair identities do not cover observed worlds")
    expected_pair_count = len(observed_ids) // 2
    if len(pair_members) != expected_pair_count:
        errors.append(
            f"observed dataset must contain {expected_pair_count} unique pair identities"
        )
    if len(observed_ids) == 72 and len(pair_members) != 36:
        errors.append("full dataset must contain 36 unique pair identities")
    for world_id, pair_id in visible_pairs.items():
        if world_pairs.get(world_id) != pair_id:
            errors.append(f"public pair identity mismatch for {world_id}")
    for blind_row in private_rows:
        world_id = str(blind_row.get("world_id", ""))
        blind_pair_id = blind_row.get("pair_id")
        if not blind_pair_id:
            errors.append(f"private pair identity is missing for {world_id}")
        elif world_pairs.get(world_id) != str(blind_pair_id):
            errors.append(f"private pair identity mismatch for {world_id}")
    observed_pair_count = len(pair_members) if pair_members else pair_count
    if observed_pair_count is None:
        errors.append("dataset pair count is missing")
    return shared_count, observed_pair_count, errors


AGGREGATE_FIELDS = frozenset(
    {
        "dataset_version",
        "seed",
        "world_count",
        "artifact_total",
        "artifact_count_per_world",
        "shared_documents",
        "splits",
        "verticals",
        "validation",
    }
)
VALIDATION_FIELDS = frozenset(
    {
        "valid",
        "world_count",
        "artifact_total",
        "artifact_count_per_world",
        "blind_included",
        "split_counts",
        "vertical_counts",
        "checkpoint_min",
        "checkpoint_max",
        "duration_min",
        "duration_max",
        "shared_document_count",
        "pair_count",
        "errors",
    }
)


def _observed_dataset(
    root: Path,
    worlds: Sequence[Mapping[str, Any]],
    base_errors: Sequence[str],
) -> dict[str, Any]:
    split_counts = {
        split: sum(row.get("split") == split for row in worlds)
        for split in ("blind", "dev", "train")
    }
    vertical_counts: dict[str, int] = {}
    for row in worlds:
        vertical = str(row.get("vertical"))
        vertical_counts[vertical] = vertical_counts.get(vertical, 0) + 1
    versions = {
        str(row["dataset_version"])
        for row in worlds
        if row.get("dataset_version") is not None
    }
    seeds = {
        int(row["seed"])
        for row in worlds
        if isinstance(row.get("seed"), int) and not isinstance(row.get("seed"), bool)
    }
    artifact_counts = {int(row.get("artifact_count", 0)) for row in worlds}
    checkpoint_counts = [int(row.get("checkpoint_count", 0)) for row in worlds]
    durations = [
        int(row["duration_days"])
        for row in worlds
        if row.get("duration_days") is not None
    ]
    shared_count, pair_count, pair_errors = _dataset_observed_extras(root, worlds)
    observed_errors = [*base_errors, *pair_errors]
    validation = {
        "valid": not observed_errors,
        "world_count": len(worlds),
        "artifact_total": sum(int(row.get("artifact_count", 0)) for row in worlds),
        "artifact_count_per_world": next(iter(artifact_counts))
        if len(artifact_counts) == 1
        else None,
        "blind_included": split_counts["blind"] > 0,
        "split_counts": split_counts,
        "vertical_counts": vertical_counts,
        "checkpoint_min": min(checkpoint_counts) if checkpoint_counts else None,
        "checkpoint_max": max(checkpoint_counts) if checkpoint_counts else None,
        "duration_min": min(durations) if durations else None,
        "duration_max": max(durations) if durations else None,
        "shared_document_count": shared_count,
        "pair_count": pair_count,
        "errors": observed_errors,
    }
    return {
        "dataset_version": next(iter(versions)) if len(versions) == 1 else None,
        "seed": next(iter(seeds)) if len(seeds) == 1 else None,
        "world_count": validation["world_count"],
        "artifact_total": validation["artifact_total"],
        "artifact_count_per_world": validation["artifact_count_per_world"],
        "shared_documents": shared_count,
        "splits": {
            key: split_counts[key]
            for key in (
                ("train", "dev", "blind") if split_counts["blind"] else ("train", "dev")
            )
        },
        "verticals": sorted(vertical_counts),
        "validation": validation,
    }


def _validation_manifest_errors(
    declared: Any, observed: Mapping[str, Any], label: str
) -> list[str]:
    if not isinstance(declared, Mapping):
        return [f"{label} validation summary is missing"]
    errors = [
        f"{label} validation field {key} is missing"
        for key in sorted(VALIDATION_FIELDS - set(declared))
    ]
    for key in VALIDATION_FIELDS & set(declared):
        if declared[key] != observed[key]:
            errors.append(f"{label} validation {key} does not match observed bundles")
    return errors


def _aggregate_manifest_errors(
    root: Path,
    manifest: Mapping[str, Any],
    worlds: Sequence[Mapping[str, Any]],
    base_errors: Sequence[str],
) -> list[str]:
    observed = _observed_dataset(root, worlds, base_errors)
    errors = [
        f"aggregate manifest {key} is missing"
        for key in sorted(AGGREGATE_FIELDS - set(manifest))
    ]
    for key in (AGGREGATE_FIELDS - {"validation", "seed"}) & set(manifest):
        expected = manifest[key]
        actual = observed[key]
        if key in {"splits", "verticals"}:
            expected = (
                {str(name): int(value) for name, value in expected.items()}
                if key == "splits" and isinstance(expected, Mapping)
                else sorted(str(value) for value in expected)
                if isinstance(expected, Sequence) and not isinstance(expected, str)
                else expected
            )
        if expected != actual:
            errors.append(f"aggregate manifest {key} does not match observed bundles")
    seed = manifest.get("seed")
    if "seed" in manifest and (
        not isinstance(seed, int) or isinstance(seed, bool) or seed < 0
    ):
        errors.append("aggregate manifest seed must be a non-negative integer")
    elif observed["seed"] is not None and seed != observed["seed"]:
        errors.append("aggregate manifest seed does not match observed bundles")
    errors.extend(
        _validation_manifest_errors(
            manifest.get("validation"),
            cast(Mapping[str, Any], observed["validation"]),
            "aggregate",
        )
    )
    return errors


def validate_dataset(path: str | Path, allow_private: bool = False) -> dict[str, Any]:
    root = Path(path)
    aggregate_manifest: Mapping[str, Any] | None = None
    aggregate_root: Path | None = None
    if root.is_file() and root.name == "manifest.json":
        candidate = _json(root)
        if isinstance(candidate, Mapping) and not candidate.get("world_id"):
            aggregate_manifest = candidate
            root = root.parent
            aggregate_root = root
    elif root.is_dir() and (root / "manifest.json").is_file():
        candidate = _json(root / "manifest.json")
        if isinstance(candidate, Mapping) and candidate.get("world_id"):
            return validate_world_bundle(root, allow_private=allow_private)
        if isinstance(candidate, Mapping):
            aggregate_manifest = candidate
            aggregate_root = root
    if aggregate_manifest is None and (
        root.is_file() or (root / "manifest.json").is_file()
    ):
        return validate_world_bundle(root, allow_private=allow_private)
    if aggregate_manifest is None and root.is_dir():
        candidates = [root / "output" / "manifest.json"]
        if root.name == "public":
            candidates.append(root.parent / "manifest.json")
        for candidate_path in candidates:
            if candidate_path.is_file():
                candidate = _json(candidate_path)
                if isinstance(candidate, Mapping) and not candidate.get("world_id"):
                    aggregate_manifest = candidate
                    aggregate_root = candidate_path.parent
                    break
    public_worlds: list[dict[str, Any]] = []
    requested_splits: tuple[str, ...]
    if root.name in {"train", "dev", "blind"} and root.parent.name == "public":
        public_root = root.parent
        requested_splits = (root.name,)
    elif root.name == "public":
        public_root = root
        requested_splits = ("train", "dev", "blind")
    elif (root.name == "output" or aggregate_manifest is not None) and (
        root / "public"
    ).is_dir():
        public_root = root / "public"
        requested_splits = ("train", "dev", "blind")
    else:
        public_root = root / "output" / "public"
        requested_splits = ("train", "dev", "blind")
    if public_root.is_dir():
        for split in requested_splits:
            public_worlds.extend(
                validate_world_bundle(item, allow_private=False)
                for item in sorted((public_root / split).glob("*/manifest.json"))
            )
    private_root = next(
        (
            candidate
            for candidate in (
                root / "private" / "blind",
                root.parent / "private" / "blind",
                root.parent.parent / "private" / "blind",
            )
            if candidate.is_dir()
        ),
        root / "private" / "blind",
    )
    private_worlds: list[dict[str, Any]] = []
    if allow_private and private_root.is_dir():
        private_worlds.extend(
            validate_world_bundle(item, allow_private=True)
            for item in sorted(private_root.glob("*/manifest.json"))
        )
    worlds = public_worlds + private_worlds
    if not worlds:
        raise BundleError(f"no world bundles found under {root}")
    bundle_errors = [
        f"{row['world_id']}: {error}" for row in worlds for error in row["errors"]
    ]
    world_id_counts: dict[str, int] = {}
    for row in worlds:
        world_id = str(row["world_id"])
        world_id_counts[world_id] = world_id_counts.get(world_id, 0) + 1
    identity_errors = [
        f"duplicate world_id across splits: {world_id}"
        for world_id, count in sorted(world_id_counts.items())
        if count > 1
    ]
    _, _, pair_errors = _dataset_observed_extras(root, worlds)
    errors = [*bundle_errors, *identity_errors, *pair_errors]
    if aggregate_manifest is not None:
        public_errors = [
            f"{row['world_id']}: {error}"
            for row in public_worlds
            for error in row["errors"]
        ]
        errors.extend(
            _aggregate_manifest_errors(
                aggregate_root or root,
                aggregate_manifest,
                public_worlds,
                public_errors,
            )
        )
    elif len(requested_splits) > 1:
        errors.append("aggregate manifest is missing")
    full_pack = (root / "output").is_dir()
    if full_pack:
        expected_pack_count = len(public_worlds) + len(private_worlds)
        if expected_pack_count != 72:
            errors.append("dataset does not contain 72 worlds")
    if allow_private and private_worlds:
        private_validation_path = _dataset_sidecar(root, "private/validation.json")
        if private_validation_path is None:
            errors.append("private validation summary is missing")
        else:
            private_validation = _json(private_validation_path)
            observed = _observed_dataset(root, worlds, bundle_errors)
            errors.extend(
                _validation_manifest_errors(
                    private_validation,
                    cast(Mapping[str, Any], observed["validation"]),
                    "private",
                )
            )
    summary = {
        "valid": not errors,
        "world_count": len(worlds),
        "errors": errors,
        "worlds": worlds,
    }
    if aggregate_manifest is not None:
        summary["dataset_version"] = aggregate_manifest.get("dataset_version")
    return summary


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("timestamp must be an RFC3339 string with a timezone")
    text = value.strip()
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", text
    ):
        raise ValueError("timestamp must be an RFC3339 string with a timezone")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return text


def _date_value(value: str) -> date:
    return datetime.fromisoformat(_timestamp(value)).date()


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_bundle(bundle: WorldBundle) -> str:
    payload = {
        "manifest": bundle.manifest,
        "events": bundle.events,
        "artifacts": bundle.artifacts,
        "actors": bundle.actor_rows,
        "checkpoints": bundle.checkpoint_rows,
    }
    return _sha256(to_json(payload).encode("utf-8"))


def _artifact_content(
    bundle: WorldBundle, row: Mapping[str, Any]
) -> tuple[Mapping[str, Any], str]:
    relative_value = row.get("path")
    content_value = row.get("content")
    if relative_value is None and isinstance(content_value, Mapping):
        content = dict(content_value)
        body = content.get("body")
        if isinstance(body, str):
            return content, body
        return content, to_json(content)
    path = _resolve_bundle_file(bundle.path, str(relative_value))
    assert path is not None
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = _json(path)
        content = dict(value) if isinstance(value, Mapping) else {"value": value}
    else:
        content = {"text": raw}
    return content, raw


def _structured_content(content: Mapping[str, Any]) -> Mapping[str, Any]:
    body = content.get("body")
    if (
        isinstance(body, str)
        and str(content.get("mime_type", "")).lower() == "application/json"
    ):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return content
        if isinstance(parsed, Mapping):
            return parsed
    return content


def _visible_roles(raw: Mapping[str, Any], visibility: Any) -> tuple[str, ...]:
    values = raw.get("visible_roles", ())
    if isinstance(values, Sequence) and not isinstance(values, str):
        result = tuple(str(item) for item in values)
    else:
        result = ()
    return result


def _actor_models(bundle: WorldBundle) -> tuple[Actor, ...]:
    actors: list[Actor] = []
    source_rows = bundle.actor_rows or tuple(
        item for item in bundle.manifest.get("actors", ()) if isinstance(item, Mapping)
    )
    for raw in source_rows:
        if not isinstance(raw, Mapping):
            continue
        if (
            raw.get("organization_id") is not None
            and raw.get("display_name") is not None
        ):
            actor_data = dict(raw)
            actor_data.setdefault("kind", "external")
            actor_data.setdefault("organization_id", "synthetic")
            actor_data.setdefault("role_tags", ())
            actor_data.setdefault(
                "active_from",
                bundle.manifest.get(
                    "start_at",
                    bundle.manifest.get("start_date", "1970-01-01T00:00:00+00:00"),
                ),
            )
            actor_data.setdefault("visibility", "public")
            actor_data.setdefault("attributes", {})
            if actor_data["visibility"] == "role_scoped":
                actor_data["visibility"] = "internal_role_scoped"
            actor_data["visible_roles"] = _visible_roles(
                actor_data, actor_data["visibility"]
            )
            actors.append(Actor.from_dict(actor_data))
            continue
        visibility = raw.get("visibility", ())
        actor_visibility = (
            "internal_role_scoped" if visibility == "role_scoped" else visibility
        )
        actor = Actor.from_dict(
            {
                "actor_id": raw.get("actor_id"),
                "kind": raw.get("kind", "external"),
                "display_name": raw.get("name", raw.get("label", raw.get("actor_id"))),
                "organization_id": raw.get("organization", "synthetic"),
                "role_tags": (raw.get("role"), raw.get("label")),
                "active_from": _timestamp(
                    bundle.manifest.get(
                        "start_at",
                        bundle.manifest.get("start_date", "1970-01-01T00:00:00+00:00"),
                    )
                ),
                "visibility": ",".join(str(item) for item in visibility)
                if isinstance(visibility, Sequence) and not isinstance(visibility, str)
                else str(actor_visibility),
                "email": raw.get("email"),
                "phone": raw.get("phone"),
                "attributes": {"synthetic": True, "source_role": raw.get("role")},
                "visible_roles": _visible_roles(raw, actor_visibility),
            }
        )
        actors.append(actor)
    return tuple(actors)


def _event_models(bundle: WorldBundle) -> tuple[Event, ...]:
    result: list[Event] = []
    for sequence, raw in enumerate(bundle.events):
        payload = raw.get("payload", {})
        actor_ids: list[str] = (
            [str(item) for item in raw.get("actor_ids", ())]
            if isinstance(raw.get("actor_ids"), Sequence)
            and not isinstance(raw.get("actor_ids"), str)
            else []
        )
        if isinstance(payload, Mapping):
            for key in ("actor_id", "source_actor_id", "recipient_actor_id"):
                if payload.get(key) and str(payload[key]) not in actor_ids:
                    actor_ids.append(str(payload[key]))
        result.append(
            Event.from_dict(
                {
                    "event_id": raw.get("event_id", f"event-{sequence:04d}"),
                    "world_id": bundle.world_id,
                    "sequence": int(raw.get("sequence", sequence)),
                    "kind": raw.get("kind", "event"),
                    "effective_at": _timestamp(
                        raw.get("effective_at", raw.get("available_at"))
                    ),
                    "recorded_at": _timestamp(
                        raw.get("recorded_at", raw.get("available_at"))
                    ),
                    "available_at": _timestamp(raw.get("available_at")),
                    "actor_ids": actor_ids,
                    "visibility": raw.get("visibility", "agent_visible"),
                    "payload": dict(payload)
                    if isinstance(payload, Mapping)
                    else {"value": payload},
                    "artifact_ids": tuple(
                        str(item) for item in raw.get("artifact_ids", ())
                    )
                    if isinstance(raw.get("artifact_ids"), Sequence)
                    and not isinstance(raw.get("artifact_ids"), str)
                    else (
                        (str(raw.get("artifact_id")),) if raw.get("artifact_id") else ()
                    ),
                    "channel": raw.get("channel"),
                    "causal_parent_ids": raw.get(
                        "causal_parent_ids", raw.get("causal_parent_event_ids", ())
                    ),
                    "visible_roles": _visible_roles(
                        raw, raw.get("visibility", "agent_visible")
                    ),
                }
            )
        )
    return tuple(result)


def _artifact_models(bundle: WorldBundle) -> tuple[Artifact, ...]:
    result: list[Artifact] = []
    for row in bundle.artifacts:
        content, raw = _artifact_content(bundle, row)
        result.append(
            Artifact.from_dict(
                {
                    "artifact_id": row.get("artifact_id"),
                    "world_id": bundle.world_id,
                    "kind": row.get(
                        "kind", row.get("artifact_type", row.get("channel", "document"))
                    ),
                    "title": row.get("title", row.get("artifact_id")),
                    "created_at": _timestamp(
                        row.get(
                            "created_at",
                            row.get("effective_at", row.get("available_at")),
                        )
                    ),
                    "available_at": _timestamp(row.get("available_at")),
                    "visibility": row.get("visibility", "agent_visible"),
                    "content": content,
                    "checksum": row.get("checksum", _sha256(raw.encode("utf-8"))),
                    "provenance": row.get(
                        "provenance",
                        {
                            "path": row.get("path"),
                            "synthetic": bool(row.get("synthetic", True)),
                            "source_event_id": row.get("source_event_id"),
                        },
                    ),
                    "source_actor_ids": row.get(
                        "source_actor_ids",
                        (row.get("source_actor_id"),)
                        if row.get("source_actor_id")
                        else (),
                    ),
                    "recipient_actor_ids": row.get(
                        "recipient_actor_ids",
                        (row.get("recipient_actor_id"),)
                        if row.get("recipient_actor_id")
                        else (),
                    ),
                    "thread_id": row.get("thread_id", row.get("pair_id")),
                    "record_id": row.get("record_id", row.get("deal_id")),
                    "version": row.get("version", 1),
                    "visible_roles": _visible_roles(
                        row, row.get("visibility", "agent_visible")
                    ),
                }
            )
        )
    return tuple(result)


def _checkpoint_models(bundle: WorldBundle) -> tuple[Checkpoint, ...]:
    if bundle.checkpoint_rows:
        result: list[Checkpoint] = []
        for fallback_index, raw in enumerate(
            sorted(
                bundle.checkpoint_rows, key=lambda item: int(item.get("sequence", 0))
            )
        ):
            sequence = int(raw.get("sequence", fallback_index))
            result.append(
                Checkpoint(
                    checkpoint_id=str(raw.get("checkpoint_id")),
                    world_id=bundle.world_id,
                    sequence=sequence,
                    available_at=_timestamp(raw.get("available_at")),
                    window_start=_timestamp(
                        raw.get("window_start", raw.get("available_at"))
                    ),
                    window_end=_timestamp(
                        raw.get("window_end", raw.get("available_at"))
                    ),
                    status=str(raw.get("status", "pending")),
                    objective_ids=tuple(
                        str(item) for item in raw.get("objective_ids", ())
                    ),
                    visible_artifact_ids=tuple(
                        str(item) for item in raw.get("visible_artifact_ids", ())
                    ),
                    required_roles=tuple(
                        str(item) for item in raw.get("required_roles", ROLE_ORDER)
                    ),
                    terminal=bool(raw.get("terminal", False)),
                    released_event_ids=tuple(
                        str(item) for item in raw.get("released_event_ids", ())
                    ),
                )
            )
        return tuple(result)
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for artifact in bundle.artifacts:
        checkpoint = str(artifact.get("checkpoint_id", "cp-01"))
        rows.setdefault(checkpoint, []).append(artifact)
    ids = list(bundle.manifest.get("checkpoint_ids", ()))
    if not ids:
        ids = sorted(rows)
    start = _date_value(
        str(
            bundle.manifest.get(
                "start_at",
                bundle.manifest.get("start_date", "1970-01-01T00:00:00+00:00"),
            )
        )
    )
    end = start + timedelta(days=int(bundle.manifest.get("duration_days", 180)))
    fallback_result: list[Checkpoint] = []
    for index, checkpoint_id in enumerate(ids):
        items = rows.get(str(checkpoint_id), [])
        dates = [
            _date_value(str(item.get("available_at")))
            for item in items
            if item.get("available_at")
        ]
        available = (
            min(dates)
            if dates
            else start + (end - start) * index // max(1, len(ids) - 1)
        )
        next_available = start + (end - start) * (index + 1) // max(1, len(ids))
        fallback_result.append(
            Checkpoint(
                checkpoint_id=str(checkpoint_id),
                world_id=bundle.world_id,
                sequence=index,
                available_at=f"{available.isoformat()}T00:00:00+00:00",
                window_start=f"{available.isoformat()}T00:00:00+00:00",
                window_end=f"{next_available.isoformat()}T00:00:00+00:00",
                status="pending",
                objective_ids=(f"objective-{checkpoint_id}",),
                visible_artifact_ids=tuple(
                    str(item.get("artifact_id"))
                    for item in items
                    if item.get("artifact_id")
                ),
                required_roles=ROLE_ORDER,
                terminal=index == len(ids) - 1,
                released_event_ids=tuple(
                    str(item.get("event_id"))
                    for item in bundle.events
                    if item.get("checkpoint_id") == checkpoint_id
                ),
            )
        )
    return tuple(fallback_result)


def _role_grants() -> tuple[RoleGrant, ...]:
    permissions = {
        "account_executive": (
            "crm.read",
            "crm.write",
            "communications.read",
            "communications.send_external",
            "communications.send_internal",
            "calendar.read",
            "calendar.write",
            "documents.read",
            "documents.write",
            "approvals.read",
            "approvals.request",
            "web.read",
            "team.read",
            "team.send",
            "run.read",
            "run.complete_checkpoint",
        ),
        "domain_specialist": (
            "crm.read",
            "communications.read",
            "communications.send_external",
            "communications.send_internal",
            "calendar.read",
            "calendar.write",
            "documents.read",
            "documents.write",
            "approvals.read",
            "approvals.request",
            "web.read",
            "team.read",
            "team.send",
            "run.read",
            "run.complete_checkpoint",
        ),
        "sales_manager": (
            "crm.read",
            "crm.write",
            "communications.read",
            "communications.send_internal",
            "calendar.read",
            "calendar.write",
            "documents.read",
            "documents.write",
            "approvals.read",
            "approvals.request",
            "approvals.decide",
            "web.read",
            "team.read",
            "team.send",
            "run.read",
            "run.complete_checkpoint",
        ),
        "revops": (
            "crm.read",
            "crm.write",
            "crm.merge",
            "communications.read",
            "communications.send_internal",
            "calendar.read",
            "calendar.write",
            "documents.read",
            "documents.write",
            "approvals.read",
            "web.read",
            "team.read",
            "team.send",
            "run.read",
            "run.complete_checkpoint",
        ),
    }
    return tuple(
        RoleGrant.from_dict(
            {
                "grant_id": f"grant-{role}",
                "principal_id": role,
                "role": role,
                "permissions": values,
                "resource_scopes": ("current_world",),
                "can_contact_external": role
                in {"account_executive", "domain_specialist"},
                "can_write_crm": role
                in {"account_executive", "sales_manager", "revops"},
                "can_approve_commercial": role == "sales_manager",
                "can_request_approval": role
                in {"account_executive", "domain_specialist", "sales_manager"},
                "approval_limit_minor_units": 100000
                if role == "sales_manager"
                else None,
            }
        )
        for role, values in permissions.items()
    )


def _manifest_values(
    bundle: WorldBundle,
    run_id: str,
    track: str,
    team_id: str,
    seed: int,
    limits: RunLimits,
    agent_manifest: Mapping[str, Any],
    environment_manifest: Mapping[str, Any],
    stakeholder_seeds: Sequence[int],
    stakeholder_model_digest: str | None,
    stakeholder_prompt_hash: str | None,
    stakeholder_timeout_seconds: float | None,
    stakeholder_seed: int | None = None,
) -> RunManifest:
    source = bundle.manifest
    digest = _digest_bundle(bundle)
    rubric_hash = stable_hash(_json(bundle.rubric_path))
    oracle_hash = stable_hash(_json(bundle.oracle_path)) if bundle.oracle_path else None
    return RunManifest.from_dict(
        {
            "run_id": run_id,
            "benchmark_version": str(
                source.get("dataset_version", source.get("schema_version", "v1.0.0"))
            ),
            "world_id": bundle.world_id,
            "track": track,
            "team_id": team_id,
            "protocol_version": PROTOCOL_VERSION,
            "tool_schema_version": "v1.0.0",
            "scenario_hash": digest,
            "rubric_hash": rubric_hash,
            "oracle_hash": oracle_hash,
            "seed": seed,
            "agent_manifest": dict(agent_manifest),
            "stakeholder_manifest": {
                "model_id": "subprocess"
                if stakeholder_model_digest
                else "deterministic",
                "model_digest": stakeholder_model_digest or _sha256(b"deterministic"),
                "prompt_hash": stakeholder_prompt_hash
                or _sha256(b"edlb-v1-stakeholders"),
                "seed": stakeholder_seeds[0]
                if stakeholder_seed is None
                else stakeholder_seed,
                "official_seeds": list(stakeholder_seeds),
                "timeout_seconds": stakeholder_timeout_seconds,
            },
            "limits": {
                "tool_calls_per_checkpoint": limits.tool_calls_per_checkpoint,
                "turns_per_checkpoint": limits.turns_per_checkpoint,
                "timeout_seconds": limits.timeout_seconds,
                "retries": limits.retries,
            },
            "environment": dict(environment_manifest),
            "started_at": _timestamp(
                source.get(
                    "start_at", source.get("start_date", "1970-01-01T00:00:00+00:00")
                )
            ),
            "status": "created",
        }
    )


def _scenario_model(bundle: WorldBundle) -> ScenarioManifest:
    source = bundle.manifest
    start_text = str(
        source.get("start_at", source.get("start_date", "1970-01-01T00:00:00+00:00"))
    )
    end_text = str(source.get("end_at", ""))
    start = _date_value(start_text)
    duration_days = int(
        source.get(
            "duration_days",
            max(
                1,
                (_date_value(end_text).toordinal() - start.toordinal())
                if end_text
                else 180,
            ),
        )
    )
    end = _date_value(end_text) if end_text else start + timedelta(days=duration_days)
    values: dict[str, Any] = {
        "world_id": bundle.world_id,
        "release_visibility": source.get("release_visibility"),
        "split": source.get("split"),
        "vertical": source.get("vertical"),
        "seller_org_id": source.get("seller_org_id", source.get("seller_id")),
        "buyer_org_id": source.get("buyer_org_id", source.get("buyer_name")),
        "title": source.get("title", source.get("motion")),
        "description": source.get("description"),
        "start_at": _timestamp(start_text),
        "end_at": _timestamp(end_text)
        if end_text
        else f"{end.isoformat()}T00:00:00+00:00",
        "duration_days": duration_days,
        "checkpoint_ids": tuple(source.get("checkpoint_ids", ())),
        "actor_ids": tuple(
            str(item.get("actor_id"))
            for item in source.get("actors", ())
            if isinstance(item, Mapping) and item.get("actor_id")
        ),
        "event_ids": tuple(
            str(item.get("event_id")) for item in bundle.events if item.get("event_id")
        ),
        "artifact_ids": tuple(
            str(item.get("artifact_id"))
            for item in bundle.artifacts
            if item.get("artifact_id")
        ),
        "required_channels": tuple(
            str(item)
            for item in source.get(
                "required_channels", source.get("artifact_channels", ())
            )
        ),
        "license": source.get("license"),
        "provenance": source.get("provenance"),
    }
    for key in (
        "pair_id",
        "counterfactual_variant",
        "causal_skeleton",
        "terminal_outcome",
        "seed",
        "outcome_reason",
    ):
        if source.get(key) is not None:
            values[key] = source[key]
    return ScenarioManifest.from_dict(
        {key: value for key, value in values.items() if value is not None}
    )


def _seed_systems(
    engine: RunEngine, bundle: WorldBundle, until: str | None = None
) -> None:
    seeded: set[str] = getattr(engine, "_edlb_seeded_artifacts", set())
    cutoff = _timestamp(until) if until is not None else None
    rows = sorted(
        bundle.artifacts,
        key=lambda row: (
            _timestamp(row.get("available_at")),
            str(row.get("artifact_id", "")),
        ),
    )
    for row in rows:
        artifact_id = str(row.get("artifact_id"))
        if artifact_id in seeded:
            continue
        content, raw = _artifact_content(bundle, row)
        kind = str(
            row.get("kind", row.get("artifact_type", row.get("channel", "document")))
        )
        available = _timestamp(row.get("available_at"))
        if cutoff is not None and available > cutoff:
            continue
        visibility_label = str(row.get("visibility", "agent_visible"))
        visibility = (
            _visible_roles(row, visibility_label)
            if visibility_label in {"role_scoped", "internal_role_scoped", "restricted"}
            else ROLE_ORDER
        )
        if visibility_label == "oracle_only":
            continue
        if (
            visibility_label in {"role_scoped", "internal_role_scoped", "restricted"}
            and not visibility
        ):
            raise BundleError(f"scoped artifact {artifact_id!r} has no visible_roles")
        if kind in {"crm", "crm_record"}:
            record = _structured_content(content)
            record_value = (
                record.get("record") if kind == "crm" and "record" in record else record
            )
            if isinstance(record_value, Mapping):
                record_id = str(
                    row.get(
                        "record_id",
                        record_value.get(
                            "record_id", record_value.get("deal_id", artifact_id)
                        ),
                    )
                )
                engine.seed_crm_record(
                    record_id, {**record_value, "visibility": visibility}, available
                )
        elif kind in {"email", "transcript", "call_transcript"}:
            engine.seed_communication(
                artifact_id,
                {
                    "message_id": artifact_id,
                    "channel": "transcript" if kind == "call_transcript" else kind,
                    "direction": "inbound",
                    "sender_role": "external",
                    "recipients": (),
                    "subject": str(row.get("title", artifact_id)),
                    "body": raw,
                    "created_at": available,
                    "available_at": available,
                    "visibility": visibility,
                    "metadata": {"artifact_id": artifact_id},
                },
            )
        elif kind == "internal_chat":
            engine.seed_team_message(
                artifact_id,
                {
                    "message_id": artifact_id,
                    "sender_role": "system",
                    "recipients": ROLE_ORDER,
                    "body": raw,
                    "created_at": available,
                    "available_at": available,
                    "visibility": visibility,
                    "metadata": {"artifact_id": artifact_id},
                },
            )
        elif kind in {"calendar", "calendar_event"}:
            event = _structured_content(content)
            event_value: Any = event.get("event") if "event" in event else event
            calendar_value = (
                dict(event_value)
                if isinstance(event_value, Mapping)
                else {"content": event_value}
            )
            calendar_value.setdefault("subject", str(row.get("title", artifact_id)))
            calendar_value.setdefault("start_at", available)
            calendar_value.setdefault("end_at", available)
            calendar_value.setdefault("participants", ())
            engine.seed_calendar_event(
                artifact_id,
                {
                    "calendar_id": artifact_id,
                    **calendar_value,
                    "available_at": available,
                    "visibility": visibility,
                    "artifact_id": artifact_id,
                },
            )
        elif kind in {
            "document",
            "proposal",
            "quote",
            "contract",
            "diligence_document",
        }:
            engine.seed_document(
                artifact_id,
                {
                    "document_id": artifact_id,
                    "title": str(row.get("title", artifact_id)),
                    "content": raw,
                    "kind": kind,
                    "version": int(row.get("version", 1)),
                    "created_at": _timestamp(row.get("created_at", available)),
                    "available_at": available,
                    "visibility": visibility,
                    "metadata": {"artifact_id": artifact_id},
                },
            )
        elif kind in {"web_news", "web_page", "news_item"}:
            engine.seed_web_record(
                artifact_id,
                {
                    "record_id": artifact_id,
                    "title": str(row.get("title", artifact_id)),
                    "content": raw,
                    "created_at": available,
                    "available_at": available,
                    "visibility": visibility,
                    "metadata": {"artifact_id": artifact_id},
                },
            )
        seeded.add(artifact_id)
    engine._edlb_seeded_artifacts = seeded


def _release_sources(engine: RunEngine) -> None:
    bundle = getattr(engine, "_edlb_bundle", None)
    if isinstance(bundle, WorldBundle):
        _seed_systems(engine, bundle, engine.current_time)


def open_world(
    bundle: str | Path | WorldBundle,
    *,
    run_id: str | None = None,
    track: str = "open_team",
    team_id: str = "reference",
    seed: int | None = None,
    stakeholder_seeds: Sequence[int] | None = None,
    limits: RunLimits | None = None,
    agent_manifest: Mapping[str, Any] | None = None,
    environment_manifest: Mapping[str, Any] | None = None,
    db_path: str | Path = ":memory:",
    trace_path: str | Path | None = None,
    allow_private: bool = False,
    stakeholder_realizer_command: Sequence[str] | None = None,
    stakeholder_model_digest: str | None = None,
    stakeholder_prompt_hash: str | None = None,
    stakeholder_timeout_seconds: float | None = None,
    stakeholder_seed: int | None = None,
) -> RunEngine:
    world = (
        bundle
        if isinstance(bundle, WorldBundle)
        else load_world_bundle(bundle, allow_private=allow_private)
    )
    actual_limits = limits or RunLimits()
    actual_run_id = (
        run_id
        or f"run-{hashlib.sha256(world.world_id.encode('utf-8')).hexdigest()[:24]}"
    )
    actual_seed = int(seed if seed is not None else world.manifest.get("seed", 0))
    actual_agent_manifest = normalize_agent_manifest(agent_manifest)
    validate_track_agent_manifest(track, actual_agent_manifest)
    actual_environment_manifest = normalize_environment_manifest(environment_manifest)
    official_seeds = normalize_official_seeds(stakeholder_seeds, actual_seed)
    if stakeholder_realizer_command is not None and (
        stakeholder_model_digest is None or stakeholder_prompt_hash is None
    ):
        raise BundleError(
            "subprocess stakeholder realizer requires model and prompt digests"
        )
    manifest = _manifest_values(
        world,
        actual_run_id,
        track,
        team_id,
        actual_seed,
        actual_limits,
        actual_agent_manifest,
        actual_environment_manifest,
        official_seeds,
        stakeholder_model_digest,
        stakeholder_prompt_hash,
        stakeholder_timeout_seconds,
        stakeholder_seed,
    )
    scenario = _scenario_model(world)
    engine = RunEngine(
        db_path=db_path,
        manifest=manifest,
        scenario=scenario,
        actors=_actor_models(world),
        events=_event_models(world),
        artifacts=_artifact_models(world),
        checkpoints=_checkpoint_models(world),
        grants=_role_grants(),
        trace_path=trace_path,
        stakeholder_realizer_command=stakeholder_realizer_command,
        stakeholder_timeout_seconds=stakeholder_timeout_seconds,
    )
    engine._edlb_bundle = world
    _seed_systems(engine, world, engine.current_time)
    return engine


def _wire(message: Message, allow_system: bool = False) -> str:
    return json.dumps(
        message.to_dict(allow_system=allow_system),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_protocol_line(raw_line: bytes) -> Message:
    if len(raw_line) > MAX_PROTOCOL_MESSAGE_BYTES:
        raise ProtocolViolation(
            f"JSONL message exceeds the {MAX_PROTOCOL_MESSAGE_BYTES}-byte transport ceiling"
        )
    try:
        return decode(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, ProtocolError) as exc:
        raise ProtocolViolation(str(exc)) from exc


def _message(
    *,
    run_id: str,
    sequence: int,
    occurred_at: str,
    kind: str,
    role: str,
    message_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    tool_name: str | None = None,
    arguments: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    call_id: str | None = None,
    ok: bool | None = None,
    result: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    recipient_role: str | None = None,
    checkpoint_id: str | None = None,
    summary: str | None = None,
    status: str | None = None,
    reason: str | None = None,
    observation_token: str | None = None,
) -> Message:
    return Message(
        protocol_version=PROTOCOL_VERSION,
        run_id=run_id,
        sequence=sequence,
        message_id=message_id or f"message-{sequence:06d}",
        occurred_at=occurred_at,
        kind=kind,
        role=role,
        payload=payload,
        tool_name=tool_name,
        arguments=arguments,
        idempotency_key=idempotency_key,
        call_id=call_id,
        ok=ok,
        result=result,
        error=error,
        recipient_role=recipient_role,
        checkpoint_id=checkpoint_id,
        summary=summary,
        status=status,
        reason=reason,
        observation_token=observation_token,
    )


def _error(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {
            "code": str(value.get("code", "tool_error")),
            "message": str(value.get("message", "tool failed")),
        }
    text = str(value)
    return {"code": "tool_error", "message": text or "tool failed"}


def _result_value(
    value: Any,
) -> tuple[bool, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    if isinstance(value, ToolResult):
        return (
            bool(value.ok),
            value.result,
            _error(value.error) if value.error is not None else None,
        )
    ok = bool(getattr(value, "ok", True))
    result = getattr(value, "result", getattr(value, "data", None))
    error = getattr(value, "error", None)
    if isinstance(result, Mapping):
        result_value: Mapping[str, Any] | None = dict(result)
    else:
        result_value = {"value": result}
    return ok, result_value if ok else None, None if ok else _error(error or result)


def _collect_model_metadata(result: RunResult, message: Message) -> None:
    payload = message.payload if isinstance(message.payload, Mapping) else {}
    sources = [payload.get("usage"), payload.get("metadata"), payload.get("model")]
    models = result.model_metadata.setdefault("models", {})
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        model_key = str(
            source.get("model_id") or source.get("model_digest") or "unknown"
        )
        model = models.setdefault(model_key, {})
        for key in ("model_id", "model_digest", "prompt_hash", "model_settings"):
            if key in source:
                model[key] = source[key]
                result.model_metadata.setdefault(key, source[key])
        latency = source.get("model_latency_ms")
        if (
            isinstance(latency, (int, float))
            and not isinstance(latency, bool)
            and math.isfinite(latency)
            and latency >= 0
        ):
            value = int(latency)
            model["model_latency_ms"] = int(model.get("model_latency_ms", 0)) + value
            result.model_metadata["model_latency_ms"] = (
                int(result.model_metadata.get("model_latency_ms", 0)) + value
            )
        usage = source.get("token_usage")
        if isinstance(usage, Mapping) and all(
            isinstance(usage.get(key), int)
            and not isinstance(usage.get(key), bool)
            and usage[key] >= 0
            for key in ("input", "output")
        ):
            aggregate = result.model_metadata.setdefault("token_usage", {})
            model_usage = model.setdefault("token_usage", {})
            for key in ("input", "output"):
                value = int(usage[key])
                model_usage[key] = int(model_usage.get(key, 0)) + value
                aggregate[key] = int(aggregate.get(key, 0)) + value
        cost = source.get("cost_minor_units")
        if (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
        ):
            value = int(cost)
            model["cost_minor_units"] = int(model.get("cost_minor_units", 0)) + value
            result.cost_minor_units = (result.cost_minor_units or 0) + value
            result.model_metadata["cost_minor_units"] = result.cost_minor_units


def _protocol_trace_payload(message: Message) -> dict[str, Any]:
    value = {
        "protocol_version": message.protocol_version,
        "run_id": message.run_id,
        "sequence": message.sequence,
        "message_id": message.message_id,
        "occurred_at": message.occurred_at,
        "kind": message.kind,
        "role": message.role,
    }
    if message.kind in {"start", "observation", "team_message"}:
        value["payload"] = dict(message.payload or {})
    if message.kind == "team_message":
        value["recipient_role"] = message.recipient_role
    elif message.kind == "yield" and message.reason is not None:
        value["reason"] = message.reason
    payload: dict[str, Any] = {"protocol_message": value}
    if message.kind == "team_message":
        payload.update(
            {
                "recipient_role": message.recipient_role,
                "payload": dict(message.payload or {}),
                "body": str(
                    (message.payload or {}).get(
                        "body", (message.payload or {}).get("text", "")
                    )
                ),
            }
        )
    elif message.kind == "yield":
        payload["reason"] = message.reason
    return payload


def _context_message(message: Message) -> dict[str, Any]:
    if message.kind != "team_message":
        return message.to_dict(allow_system=message.role == "system")
    return {
        "protocol_version": message.protocol_version,
        "run_id": message.run_id,
        "sequence": message.sequence,
        "message_id": message.message_id,
        "occurred_at": message.occurred_at,
        "kind": message.kind,
        "role": message.role,
        "recipient_role": message.recipient_role,
        "payload": dict(message.payload or {}),
    }


def _apply_team_message(
    engine: RunEngine, message: Message, trace: bool = True
) -> None:
    if message.recipient_role not in ROLE_ORDER or not message.payload:
        raise ProtocolViolation("invalid team message")
    body = str(message.payload.get("body", message.payload.get("text", "")))
    if not body:
        raise ProtocolViolation("team message body is required")
    engine.team_send(
        message.role,
        [message.recipient_role],
        body,
        f"team-{message.message_id}",
        ROLE_ORDER,
        message.payload.get("metadata")
        if isinstance(message.payload.get("metadata"), Mapping)
        else {"protocol_message_id": message.message_id},
    )
    if trace:
        engine._trace(
            "team_message",
            message.role,
            _protocol_trace_payload(message),
            message_id=message.message_id,
        )


def _trace_yield(engine: RunEngine, message: Message, trace: bool = True) -> None:
    if trace:
        engine._trace(
            "yield",
            message.role,
            _protocol_trace_payload(message),
            message_id=message.message_id,
        )


def tool_schemas(
    engine: RunEngine | None = None, dispatcher: Any = None
) -> tuple[dict[str, Any], ...]:
    candidate = dispatcher
    if candidate is None and engine is not None:
        try:
            from .tools import ToolDispatcher

            candidate = ToolDispatcher(engine)
        except ImportError:
            candidate = None
    if candidate is not None and hasattr(candidate, "schemas"):
        try:
            values = tuple(dict(item) for item in candidate.schemas())
            flattened = {
                f"{item.get('tool')}.{action}"
                for item in values
                for action in item.get("actions", ())
            }
            if NORMALIZED_ACTIONS.issubset(flattened):
                return values
        except TypeError, ValueError:
            pass
    return TOOLS


def _direct_tool(
    engine: RunEngine,
    role: str,
    tool: str,
    action: str,
    args: Mapping[str, Any],
    key: str | None,
) -> Any:
    if tool == "run" and action == "advance":
        raise RunnerError("agents cannot advance the virtual clock")
    if tool == "crm":
        if action == "search":
            return engine.crm_search(
                role, str(args.get("query", "")), cast(int | None, args.get("limit"))
            )
        if action == "read":
            return engine.crm_read(role, str(args["record_id"]))
        if action == "history":
            return engine.crm_history(role, str(args["record_id"]))
        if action == "update":
            return engine.crm_update(
                role, str(args["record_id"]), dict(args.get("changes") or {}), key
            )
        if action == "merge":
            return engine.crm_merge(
                role, str(args["source_id"]), str(args["target_id"]), key
            )
    if tool == "communications":
        if action == "search":
            return engine.communications_search(
                role,
                str(args.get("query", "")),
                args.get("channel"),
                cast(int | None, args.get("limit")),
            )
        if action == "read":
            return engine.communications_read(role, str(args["message_id"]))
        if action == "send":
            return engine.communications_send(
                role,
                str(args.get("channel", "email")),
                args.get("recipients", ()),
                str(args.get("subject", "")),
                str(args.get("body", "")),
                key,
                args.get("visibility", ROLE_ORDER),
                args.get("metadata"),
                args.get("semantic_envelope"),
            )
    if tool == "calendar":
        if action == "list":
            return engine.calendar_list(role, cast(int | None, args.get("limit")))
        if action == "schedule":
            return engine.calendar_schedule(
                role,
                str(args["subject"]),
                str(args["start_at"]),
                str(args["end_at"]),
                args.get("participants", ()),
                str(args.get("description", "")),
                key,
                args.get("visibility", ROLE_ORDER),
                args.get("semantic_envelope"),
            )
        if action == "reschedule":
            return engine.calendar_reschedule(
                role,
                str(args["calendar_id"]),
                str(args["start_at"]),
                str(args["end_at"]),
                args["semantic_envelope"],
                args.get("participants"),
                args.get("subject"),
                args.get("description"),
                key,
            )
        if action == "cancel":
            return engine.calendar_cancel(
                role,
                str(args["calendar_id"]),
                args["semantic_envelope"],
                str(args.get("reason", "")),
                key,
            )
    if tool == "documents":
        if action == "search":
            return engine.documents_search(
                role, str(args.get("query", "")), cast(int | None, args.get("limit"))
            )
        if action == "read":
            return engine.documents_read(role, str(args["document_id"]))
        if action == "create":
            return engine.documents_create(
                role,
                str(args["title"]),
                str(args.get("content", "")),
                str(args.get("kind", "document")),
                key,
                args.get("visibility", ROLE_ORDER),
                args.get("metadata"),
            )
        if action == "revise":
            return engine.documents_revise(
                role,
                str(args["document_id"]),
                str(args.get("content", "")),
                args.get("metadata"),
                key,
            )
        return engine.documents_attach(
            role,
            str(args["document_id"]),
            str(args["related_type"]),
            str(args["related_id"]),
            key,
        )
    if tool == "approvals":
        if action == "list":
            return engine.approvals_list(
                role, args.get("status"), cast(int | None, args.get("limit"))
            )
        if action == "request":
            return engine.approvals_request(
                role,
                str(args["approver_role"]),
                str(args["purpose"]),
                dict(args.get("details") or {}),
                key,
                args.get("visibility", ROLE_ORDER),
            )
        if action == "approve":
            return engine.approvals_approve(
                role, str(args["approval_id"]), str(args.get("note", "")), key
            )
        return engine.approvals_reject(
            role, str(args["approval_id"]), str(args.get("note", "")), key
        )
    if tool == "web":
        if action == "search":
            return engine.web_search(
                role, str(args.get("query", "")), cast(int | None, args.get("limit"))
            )
        return engine.web_open(role, str(args["record_id"]))
    if tool == "team":
        if action == "inbox":
            return engine.team_inbox(role, cast(int | None, args.get("limit")))
        if action == "search":
            return engine.team_search(
                role, str(args.get("query", "")), cast(int | None, args.get("limit"))
            )
        return engine.team_send(
            role,
            args.get("recipients", ()),
            str(args.get("body", "")),
            key,
            args.get("visibility", ROLE_ORDER),
            args.get("metadata"),
        )
    if tool == "run" and action == "status":
        return engine.run_status(role)
    if tool == "run" and action == "yield":
        return engine.run_yield(role)
    if tool == "run" and action == "complete_checkpoint":
        return _complete_checkpoint(
            engine,
            role,
            str(args["checkpoint_id"]),
            str(args.get("summary", "")),
            key or "",
        )
    raise RunnerError(f"unsupported tool action {tool}.{action}")


def dispatch_tool(
    engine: RunEngine, role: str, message: Message, dispatcher: Any = None
) -> tuple[bool, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    if message.kind != "tool_call" or not message.tool_name:
        raise ProtocolViolation("expected tool_call")
    if role != message.role or role not in ROLE_ORDER:
        raise ProtocolViolation("tool role does not match the active role")
    tool, action = message.tool_name.split(".", 1)
    if not any(
        item["tool"] == tool and action in item["actions"]
        for item in tool_schemas(engine, dispatcher)
    ):
        raise ProtocolViolation(f"unsupported tool {message.tool_name}")
    if action not in READ_ACTIONS and not message.idempotency_key:
        raise ProtocolViolation("write tool calls require an idempotency key")
    try:
        if dispatcher is None:
            try:
                from .tools import ToolDispatcher

                dispatcher = ToolDispatcher(engine)
            except ImportError:
                dispatcher = None
        if dispatcher is not None:
            call = ToolCall(
                message.message_id,
                message.tool_name,
                role,
                message.arguments or {},
                message.idempotency_key,
            )
            return _result_value(dispatcher.dispatch(call))
        return (
            True,
            _direct_tool(
                engine,
                role,
                tool,
                action,
                message.arguments or {},
                message.idempotency_key,
            ),
            None,
        )
    except (
        KeyError,
        OSError,
        ProtocolError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return False, None, _error(f"{type(exc).__name__}: {exc}")


def _checkpoint(engine: RunEngine) -> Mapping[str, Any] | None:
    value = engine.current_checkpoint()
    return dict(value) if isinstance(value, Mapping) else None


def _activate_first(engine: RunEngine) -> None:
    if getattr(engine, "current_checkpoint_index", -1) < 0 and engine.checkpoints():
        try:
            _advance(engine, False, "activate-first")
        except TypeError:
            engine.advance_checkpoint(
                budget_exhausted=False, idempotency_key="activate-first"
            )
            _release_sources(engine)


def _advance(engine: RunEngine, budget_exhausted: bool, key: str) -> Mapping[str, Any]:
    try:
        value = engine.advance_checkpoint(budget_exhausted, key)
    except TypeError:
        value = engine.advance_checkpoint(
            budget_exhausted=budget_exhausted, idempotency_key=key
        )
    if not isinstance(value, Mapping):
        raise RunnerError("engine advance did not return an object")
    _release_sources(engine)
    return value


def _complete_checkpoint(
    engine: RunEngine, role: str, checkpoint_id: str, summary: str, key: str
) -> Mapping[str, Any]:
    try:
        value = engine.complete_checkpoint(role, checkpoint_id, summary, key)
    except TypeError:
        value = engine.complete_checkpoint(
            role=role, checkpoint_id=checkpoint_id, summary=summary, idempotency_key=key
        )
    if not isinstance(value, Mapping):
        raise RunnerError("engine checkpoint completion did not return an object")
    return value


def _complete_run(
    engine: RunEngine,
    status: str,
    result: Mapping[str, Any] | None,
    reason: str | None,
    key: str,
) -> Mapping[str, Any]:
    try:
        value = engine.run_complete(status, result, reason, key)
    except TypeError:
        value = engine.run_complete(
            status=status, result=result, reason=reason, idempotency_key=key
        )
    if not isinstance(value, Mapping):
        raise RunnerError("engine completion did not return an object")
    return value


def _start_payload(engine: RunEngine, limits: RunLimits) -> dict[str, Any]:
    checkpoint = _checkpoint(engine)
    tool_cap, turn_cap = _run_caps(limits)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "track": engine.manifest.track,
        "roles": list(ROLE_ORDER),
        "tool_schemas": list(tool_schemas(engine)),
        "current_time": engine.current_time,
        "checkpoint": {
            key: value
            for key, value in (checkpoint or {}).items()
            if key not in {"world_id", "objective_ids", "released_event_ids"}
        },
        "limits": {
            "tool_calls_per_checkpoint": tool_cap,
            "turns_per_checkpoint": turn_cap,
            "timeout_seconds": limits.timeout_seconds,
            "retries": limits.retries,
        },
    }


def _run_caps(limits: RunLimits) -> tuple[int | None, int | None]:
    return limits.tool_calls_per_checkpoint, limits.turns_per_checkpoint


def _limit_exceeded(used: int, limit: int | None) -> bool:
    return limit is not None and used > limit


def _limit_reached(used: int, limit: int | None) -> bool:
    return limit is not None and used >= limit


def _observation(
    engine: RunEngine,
    role: str,
    sequence: int,
    limits: RunLimits,
    message_id: str,
    observation_token: str,
) -> Message:
    checkpoint = _checkpoint(engine) or {}
    tool_cap, turn_cap = _run_caps(limits)
    payload = {
        "current_time": engine.current_time,
        "checkpoint": {
            key: value
            for key, value in checkpoint.items()
            if key
            not in {
                "world_id",
                "objective_ids",
                "released_event_ids",
                "visible_artifact_ids",
            }
        },
        "budget": {
            "tool_calls_remaining": tool_cap,
            "turns_remaining": turn_cap,
        },
        "tool_schemas": list(tool_schemas(engine)),
    }
    return _message(
        run_id=engine.manifest.run_id,
        sequence=sequence,
        occurred_at=engine.current_time,
        kind="observation",
        role=role,
        message_id=message_id,
        payload=payload,
        observation_token=observation_token,
    )


def _write_outputs(
    engine: RunEngine, result: RunResult, output_dir: str | Path | None
) -> RunResult:
    failed_results = [
        event
        for event in engine.trace_events()
        if event.kind == "tool_result" and event.payload.get("ok") is False
    ]
    result.invalid_actions += sum(
        isinstance(event.payload.get("error"), Mapping)
        and event.payload["error"].get("code")
        in {
            "not_authorized",
            "protocol_error",
            "idempotency_error",
            "tool_error",
            "invalid_action",
        }
        for event in failed_results
    )
    result.error_count = len(result.errors) + len(failed_results)
    usage = result.model_metadata.get("token_usage")
    if isinstance(usage, Mapping):
        result.tokens = sum(
            int(value)
            for value in usage.values()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        )
    engine.persist_resource_usage(result.to_dict()["resource_usage"])
    if output_dir is None:
        result.state_hash = engine.state_hash()
        return result
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    trace_path = directory / "trace.jsonl"
    engine.dump_trace(trace_path)
    manifest_path = directory / "run-manifest.json"
    value = engine.manifest.to_dict()
    value.update({"status": result.status, "ended_at": engine._meta("ended_at")})
    manifest_path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (directory / "state.json").write_text(
        json.dumps(
            engine.state_snapshot(), ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    snapshots = engine.snapshots()
    with (directory / "snapshots.jsonl").open("w", encoding="utf-8") as stream:
        for snapshot in snapshots:
            stream.write(to_json(snapshot) + "\n")
    with (directory / "state-diffs.jsonl").open("w", encoding="utf-8") as stream:
        for snapshot in snapshots:
            stream.write(
                to_json(
                    {
                        "sequence": snapshot["sequence"],
                        "previous_state_hash": snapshot["previous_state_hash"],
                        "state_hash": snapshot["state_hash"],
                        "state_diff": snapshot["state_diff"],
                    }
                )
                + "\n"
            )
    result.output_dir = directory
    result.trace_path = trace_path
    result.manifest_path = manifest_path
    result.state_hash = engine.state_hash()
    (directory / "result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return result


class OpenTeamRunner:
    def __init__(
        self,
        engine: RunEngine,
        command: str | Sequence[str],
        limits: RunLimits | None = None,
        output_dir: str | Path | None = None,
        dispatcher: Any = None,
    ) -> None:
        if engine.manifest.track != "open_team":
            raise BundleError("OpenTeamRunner requires an open_team manifest")
        normalize_agent_manifest(engine.manifest.agent_manifest, require_resolved=True)
        validate_track_agent_manifest(
            engine.manifest.track, engine.manifest.agent_manifest
        )
        normalize_environment_manifest(
            engine.manifest.environment, require_resolved=True
        )
        self.engine = engine
        self.command = tuple(
            shlex.split(command) if isinstance(command, str) else command
        )
        self.limits = _bound_run_limits(engine, limits)
        self.output_dir = output_dir
        self.dispatcher = dispatcher
        self.sequence = 0
        self.protocol_sequence = -1
        self.completed_roles: set[str] = set()
        self.calls_this_checkpoint = 0
        self.turns_this_checkpoint = 0
        self.observation_token = secrets.token_urlsafe(24)
        self.response_deadline: float | None = None
        self.result = RunResult(
            engine.manifest.run_id, engine.manifest.world_id, "open_team", "running"
        )

    def _reset_response_deadline(self) -> None:
        self.response_deadline = (
            None
            if self.limits.timeout_seconds is None
            else time.monotonic() + self.limits.timeout_seconds
        )

    def _response_timeout(self) -> float | None:
        return (
            None
            if self.response_deadline is None
            else max(0.0, self.response_deadline - time.monotonic())
        )

    def _send(self, stream: Any, message: Message, allow_system: bool = False) -> None:
        value = _wire(message, allow_system=allow_system) + "\n"
        if self.limits.timeout_seconds is None:
            try:
                stream.write(value)
            except TypeError:
                stream.write(value.encode("utf-8"))
            stream.flush()
            return
        payload = memoryview(value.encode("utf-8"))
        file_descriptor = stream.fileno()
        sent = 0
        with selectors.DefaultSelector() as writer:
            writer.register(file_descriptor, selectors.EVENT_WRITE)
            while sent < len(payload):
                try:
                    written = os.write(file_descriptor, payload[sent:])
                except BlockingIOError:
                    written = 0
                if written:
                    sent += written
                    continue
                timeout = self._response_timeout()
                if timeout == 0.0 or not writer.select(timeout):
                    raise AgentProcessError("agent response timed out")

    def _next_system(
        self, kind: str, payload: Mapping[str, Any], role: str = "system"
    ) -> Message:
        self.sequence += 1
        return _message(
            run_id=self.engine.manifest.run_id,
            sequence=self.sequence,
            occurred_at=self.engine.current_time,
            kind=kind,
            role=role,
            payload=payload,
        )

    def _tool_result(
        self,
        message: Message,
        ok: bool,
        value: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None,
    ) -> Message:
        self.sequence += 1
        return _message(
            run_id=self.engine.manifest.run_id,
            sequence=self.sequence,
            occurred_at=self.engine.current_time,
            kind="tool_result",
            role=message.role,
            message_id=f"{message.message_id}.result",
            call_id=message.message_id,
            ok=ok,
            result=value,
            error=error,
        )

    def _advance_if_ready(self, stream: Any) -> bool:
        checkpoint = _checkpoint(self.engine)
        if checkpoint is None:
            return False
        required = set(checkpoint.get("required_roles", ROLE_ORDER))
        if not required.issubset(self.completed_roles):
            return False
        if checkpoint.get("terminal"):
            _complete_run(
                self.engine,
                "completed",
                {"terminal": True},
                None,
                f"run-complete-{self.engine.manifest.run_id}",
            )
            return True
        _advance(
            self.engine, False, f"advance-{self.engine.current_checkpoint_index + 1}"
        )
        self.observation_token = secrets.token_urlsafe(24)
        self.completed_roles.clear()
        self.calls_this_checkpoint = 0
        self.turns_this_checkpoint = 0
        for role in ROLE_ORDER:
            self._send(
                stream,
                _observation(
                    self.engine,
                    role,
                    self.sequence + 1,
                    self.limits,
                    f"observation-{self.sequence + 1}",
                    self.observation_token,
                ),
            )
            self.sequence += 1
        return False

    def _fail(self, reason: str) -> None:
        self.result.errors.append(reason)
        self.result.status = "failed"
        try:
            _complete_run(
                self.engine,
                "failed",
                None,
                reason,
                f"run-failed-{self.engine.manifest.run_id}-{len(self.result.errors)}",
            )
        except KeyError, OSError, RuntimeError, TypeError, ValueError:
            pass

    def _budget_advance(self, stream: Any) -> None:
        checkpoint = _checkpoint(self.engine)
        if checkpoint is None:
            self._fail("budget exhausted without an active checkpoint")
            return
        if checkpoint.get("terminal"):
            self._fail("terminal checkpoint exhausted its budget")
            return
        _advance(
            self.engine,
            True,
            f"advance-budget-{self.engine.current_checkpoint_index + 1}",
        )
        self.observation_token = secrets.token_urlsafe(24)
        self.completed_roles.clear()
        self.calls_this_checkpoint = 0
        self.turns_this_checkpoint = 0
        for role in ROLE_ORDER:
            self._send(
                stream,
                _observation(
                    self.engine,
                    role,
                    self.sequence + 1,
                    self.limits,
                    f"observation-{self.sequence + 1}",
                    self.observation_token,
                ),
            )
            self.sequence += 1

    def run(self) -> RunResult:
        started = time.monotonic()
        process: subprocess.Popen[Any] | None = None
        selector: selectors.BaseSelector | None = None
        stdout_buffer = b""
        try:
            if not self.command:
                raise AgentProcessError("agent command is empty")
            _activate_first(self.engine)
            process_error: Exception | None = None
            for attempt in range(self.limits.retries + 1):
                try:
                    process = subprocess.Popen(
                        self.command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=False,
                        bufsize=0,
                    )
                    process_error = None
                    break
                except OSError as exc:
                    process_error = exc
                    if attempt < self.limits.retries:
                        self.result.retries += 1
            if process is None:
                raise AgentProcessError(
                    str(process_error)
                    if process_error
                    else "agent process could not start"
                )
            assert process.stdin is not None and process.stdout is not None
            if self.limits.timeout_seconds is not None:
                os.set_blocking(process.stdin.fileno(), False)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            self._reset_response_deadline()
            self._send(
                process.stdin,
                self._next_system("start", _start_payload(self.engine, self.limits)),
                allow_system=True,
            )
            for role in ROLE_ORDER:
                self._send(
                    process.stdin,
                    _observation(
                        self.engine,
                        role,
                        self.sequence + 1,
                        self.limits,
                        f"observation-{self.sequence + 1}",
                        self.observation_token,
                    ),
                )
                self.sequence += 1
            while self.result.status == "running":
                timeout = self._response_timeout()
                if timeout == 0.0:
                    raise AgentProcessError("agent response timed out")
                ready = selector.select(timeout)
                if not ready:
                    if timeout is None:
                        continue
                    raise AgentProcessError("agent response timed out")
                for key, _ in ready:
                    fileobj = key.fileobj
                    fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
                    observation_epoch = self.engine.current_time
                    chunk = os.read(
                        fd,
                        min(65536, MAX_PROTOCOL_MESSAGE_BYTES + 1 - len(stdout_buffer)),
                    )
                    if not chunk:
                        remaining = self._response_timeout()
                        try:
                            process.wait(timeout=remaining)
                        except subprocess.TimeoutExpired as exc:
                            raise AgentProcessError("agent response timed out") from exc
                        raise AgentProcessError("agent process exited before run_end")
                    stdout_buffer += chunk
                    while b"\n" in stdout_buffer:
                        raw_line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                        if not raw_line.strip():
                            continue
                        message = _decode_protocol_line(raw_line)
                        if message.run_id != self.engine.manifest.run_id:
                            raise ProtocolViolation(
                                "message run_id does not match the active run"
                            )
                        if message.observation_token != self.observation_token:
                            self.result.invalid_actions += 1
                            if message.kind == "tool_call":
                                self._send(
                                    process.stdin,
                                    self._tool_result(
                                        message,
                                        False,
                                        None,
                                        {
                                            "code": "stale_observation",
                                            "message": "message does not match the current observation token",
                                        },
                                    ),
                                )
                            raise ProtocolViolation(
                                "message does not match the current observation token"
                            )
                        if message.sequence <= self.protocol_sequence:
                            raise ProtocolViolation(
                                "agent message sequence is not increasing"
                            )
                        self.protocol_sequence = message.sequence
                        if (
                            observation_epoch != self.engine.current_time
                            or message.occurred_at != observation_epoch
                        ):
                            self.result.invalid_actions += 1
                            if message.kind == "tool_call":
                                self._send(
                                    process.stdin,
                                    self._tool_result(
                                        message,
                                        False,
                                        None,
                                        {
                                            "code": "stale_observation",
                                            "message": "message belongs to an earlier observation epoch",
                                        },
                                    ),
                                )
                            raise ProtocolViolation(
                                "message belongs to an earlier observation epoch"
                            )
                        response_window_reset = False
                        self.result.turns += 1
                        self.turns_this_checkpoint += 1
                        call_cap, turn_cap = _run_caps(self.limits)
                        if _limit_exceeded(self.turns_this_checkpoint, turn_cap):
                            self._reset_response_deadline()
                            self._budget_advance(process.stdin)
                            continue
                        checkpoint_index = self.engine.current_checkpoint_index
                        if message.kind == "tool_call":
                            self.result.tool_calls += 1
                            self.calls_this_checkpoint += 1
                            if _limit_exceeded(self.calls_this_checkpoint, call_cap):
                                self.result.invalid_actions += 1
                                self._reset_response_deadline()
                                self._send(
                                    process.stdin,
                                    self._tool_result(
                                        message,
                                        False,
                                        None,
                                        {
                                            "code": "budget_exceeded",
                                            "message": "tool-call budget exhausted for this checkpoint",
                                        },
                                    ),
                                )
                                self._budget_advance(process.stdin)
                                continue
                            ok, value, error = dispatch_tool(
                                self.engine, message.role, message, self.dispatcher
                            )
                            self._reset_response_deadline()
                            response_window_reset = True
                            self._send(
                                process.stdin,
                                self._tool_result(message, ok, value, error),
                            )
                            if ok and message.tool_name == "run.complete_checkpoint":
                                self.completed_roles.add(message.role)
                                if self._advance_if_ready(process.stdin):
                                    self.result.status = "completed"
                                elif (
                                    self.engine.current_checkpoint_index
                                    != checkpoint_index
                                ):
                                    break
                        elif message.kind == "checkpoint_complete":
                            if (
                                message.role not in ROLE_ORDER
                                or message.checkpoint_id is None
                                or message.summary is None
                            ):
                                raise ProtocolViolation("invalid checkpoint completion")
                            _complete_checkpoint(
                                self.engine,
                                message.role,
                                message.checkpoint_id,
                                message.summary,
                                f"complete-{message.checkpoint_id}-{message.role}",
                            )
                            self.completed_roles.add(message.role)
                            self._reset_response_deadline()
                            response_window_reset = True
                            if self._advance_if_ready(process.stdin):
                                self.result.status = "completed"
                        elif message.kind == "team_message":
                            _apply_team_message(self.engine, message)
                        elif message.kind == "run_end":
                            if message.status == "completed" and not (
                                _checkpoint(self.engine) or {}
                            ).get("terminal"):
                                raise ProtocolViolation(
                                    "completed run_end requires terminal checkpoint"
                                )
                            self.result.status = str(message.status)
                        elif message.kind == "yield":
                            _trace_yield(self.engine, message)
                        else:
                            raise ProtocolViolation(
                                f"agent message kind {message.kind} is not accepted"
                            )
                        if (
                            self.result.status == "running"
                            and self.engine.status == "running"
                            and self.engine.current_checkpoint_index == checkpoint_index
                            and (
                                _limit_reached(self.calls_this_checkpoint, call_cap)
                                or _limit_reached(self.turns_this_checkpoint, turn_cap)
                            )
                        ):
                            if not response_window_reset:
                                self._reset_response_deadline()
                                response_window_reset = True
                            self._budget_advance(process.stdin)
                        if not response_window_reset:
                            self._reset_response_deadline()
                        if self.result.status != "running":
                            break
                    if len(stdout_buffer) > MAX_PROTOCOL_MESSAGE_BYTES:
                        raise ProtocolViolation(
                            f"JSONL message exceeds the {MAX_PROTOCOL_MESSAGE_BYTES}-byte transport ceiling"
                        )
            if self.result.status == "running":
                raise AgentProcessError("agent process ended without run_end")
        except (
            KeyError,
            OSError,
            ProtocolError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            if selector is not None:
                selector.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        self.result.latency_ms = int((time.monotonic() - started) * 1000)
        return _write_outputs(self.engine, self.result, self.output_dir)


class FixedHarnessScheduler:
    def __init__(
        self,
        engine: RunEngine,
        adapter_command: str | Sequence[str],
        limits: RunLimits | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        if engine.manifest.track != "fixed_harness":
            raise BundleError("FixedHarnessScheduler requires a fixed_harness manifest")
        normalize_agent_manifest(engine.manifest.agent_manifest, require_resolved=True)
        validate_track_agent_manifest(
            engine.manifest.track, engine.manifest.agent_manifest
        )
        normalize_environment_manifest(
            engine.manifest.environment, require_resolved=True
        )
        self.engine = engine
        self.adapter_command = tuple(
            shlex.split(adapter_command)
            if isinstance(adapter_command, str)
            else adapter_command
        )
        self.limits = _bound_run_limits(engine, limits)
        self.output_dir = output_dir
        self.contexts: dict[str, list[dict[str, Any]]] = {
            role: [] for role in ROLE_ORDER
        }
        self.last_sequences: dict[str, int] = {role: -1 for role in ROLE_ORDER}
        self.seen_alerts: dict[str, set[str]] = {role: set() for role in ROLE_ORDER}
        self.seen_team_messages: dict[str, set[str]] = {
            role: set() for role in ROLE_ORDER
        }
        self.sequence = 0
        self.calls_this_checkpoint = 0
        self.turns_this_checkpoint = 0
        self.result = RunResult(
            engine.manifest.run_id, engine.manifest.world_id, "fixed_harness", "running"
        )

    def _activation(
        self, role: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        checkpoint = _checkpoint(self.engine) or {}
        released_ids = {str(item) for item in checkpoint.get("released_event_ids", ())}
        events = self.engine.events(role)
        if released_ids:
            events = [
                item for item in events if str(item.get("event_id")) in released_ids
            ]
        alerts = [
            item
            for item in events
            if str(item.get("event_id")) not in self.seen_alerts[role]
        ]
        self.seen_alerts[role].update(
            str(item.get("event_id")) for item in alerts if item.get("event_id")
        )
        team_messages = self.engine.team_inbox(role)
        unread = [
            item
            for item in team_messages
            if str(item.get("message_id")) not in self.seen_team_messages[role]
        ]
        self.seen_team_messages[role].update(
            str(item.get("message_id")) for item in unread if item.get("message_id")
        )
        return alerts, unread

    def _request(self, role: str) -> list[Message]:
        alerts, unread_team_messages = self._activation(role)
        call_cap, turn_cap = _run_caps(self.limits)
        observation_token = secrets.token_urlsafe(24)
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "adapter_request",
            "run_id": self.engine.manifest.run_id,
            "role": role,
            "occurred_at": self.engine.current_time,
            "observation_token": observation_token,
            "checkpoint": {
                key: value
                for key, value in (_checkpoint(self.engine) or {}).items()
                if key != "world_id"
            },
            "tool_schemas": list(tool_schemas(self.engine)),
            "messages": list(self.contexts[role]),
            "alerts": alerts,
            "unread_team_messages": unread_team_messages,
            "budget": {
                "tool_calls_per_checkpoint": call_cap,
                "turns_per_checkpoint": turn_cap,
            },
        }
        self.contexts[role].append(
            {
                "kind": "observation",
                "role": role,
                "occurred_at": self.engine.current_time,
                "checkpoint": {
                    key: value
                    for key, value in (_checkpoint(self.engine) or {}).items()
                    if key != "world_id"
                },
            }
        )
        last_error: Exception | None = None
        for attempt in range(self.limits.retries + 1):
            started = time.monotonic()
            process: subprocess.Popen[bytes] | None = None
            selector: selectors.BaseSelector | None = None
            try:
                with tempfile.TemporaryFile() as request_stream:
                    request_stream.write(
                        (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                    )
                    request_stream.seek(0)
                    process = subprocess.Popen(
                        self.adapter_command,
                        stdin=request_stream,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    assert process.stdout is not None
                    selector = selectors.DefaultSelector()
                    selector.register(process.stdout, selectors.EVENT_READ)
                    deadline = (
                        None
                        if self.limits.timeout_seconds is None
                        else started + self.limits.timeout_seconds
                    )
                    stdout_buffer = b""
                    messages: list[Message] = []
                    while True:
                        timeout = (
                            None
                            if deadline is None
                            else max(0.0, deadline - time.monotonic())
                        )
                        if timeout == 0.0:
                            raise subprocess.TimeoutExpired(self.adapter_command, 0.0)
                        if not selector.select(timeout):
                            if timeout is None:
                                continue
                            raise subprocess.TimeoutExpired(
                                self.adapter_command, timeout
                            )
                        chunk = os.read(
                            process.stdout.fileno(),
                            min(
                                65536,
                                MAX_PROTOCOL_MESSAGE_BYTES + 1 - len(stdout_buffer),
                            ),
                        )
                        if not chunk:
                            break
                        stdout_buffer += chunk
                        while b"\n" in stdout_buffer:
                            raw_line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                            if raw_line.strip():
                                messages.append(_decode_protocol_line(raw_line))
                        if len(stdout_buffer) > MAX_PROTOCOL_MESSAGE_BYTES:
                            raise ProtocolViolation(
                                f"JSONL message exceeds the {MAX_PROTOCOL_MESSAGE_BYTES}-byte transport ceiling"
                            )
                    if stdout_buffer.strip():
                        messages.append(_decode_protocol_line(stdout_buffer))
                    timeout = (
                        None
                        if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                    returncode = process.wait(timeout=timeout)
                self.result.latency_ms += int((time.monotonic() - started) * 1000)
                if returncode != 0:
                    raise AgentProcessError(f"adapter exited {returncode}")
                if not messages:
                    return []
                for message in messages:
                    if (
                        message.run_id != self.engine.manifest.run_id
                        or message.role != role
                    ):
                        raise ProtocolViolation(
                            "adapter response has the wrong run or role"
                        )
                    if message.observation_token != observation_token:
                        raise ProtocolViolation(
                            "adapter response does not match the activation token"
                        )
                    if message.sequence <= self.last_sequences[role]:
                        raise ProtocolViolation(
                            "adapter message sequence is not increasing"
                        )
                    self.last_sequences[role] = message.sequence
                    _collect_model_metadata(self.result, message)
                return messages
            except (
                KeyError,
                OSError,
                ProtocolError,
                RuntimeError,
                subprocess.TimeoutExpired,
                TypeError,
                ValueError,
            ) as exc:
                last_error = exc
                if attempt < self.limits.retries:
                    self.result.retries += 1
            finally:
                if selector is not None:
                    selector.close()
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    if process.stdout is not None:
                        process.stdout.close()
        raise AgentProcessError(str(last_error) if last_error else "adapter failed")

    def _append(self, role: str, message: Mapping[str, Any]) -> None:
        self.contexts[role].append(dict(message))

    def _process(
        self, role: str, messages: Iterable[Message], completed_roles: set[str]
    ) -> bool:
        call_cap, turn_cap = _run_caps(self.limits)
        for message in messages:
            checkpoint_index = self.engine.current_checkpoint_index
            self._append(role, _context_message(message))
            if message.kind == "tool_call":
                self.result.tool_calls += 1
                self.calls_this_checkpoint += 1
                if _limit_exceeded(self.calls_this_checkpoint, call_cap):
                    result_message = _message(
                        run_id=self.engine.manifest.run_id,
                        sequence=self.sequence + 1,
                        occurred_at=self.engine.current_time,
                        kind="tool_result",
                        role=role,
                        message_id=f"{message.message_id}.result",
                        call_id=message.message_id,
                        ok=False,
                        error={
                            "code": "budget_exceeded",
                            "message": "tool-call budget exhausted for this checkpoint",
                        },
                    )
                    self.sequence += 1
                    self._append(role, result_message.to_dict())
                    return True
                ok, value, error = dispatch_tool(self.engine, role, message)
                result_message = _message(
                    run_id=self.engine.manifest.run_id,
                    sequence=self.sequence + 1,
                    occurred_at=self.engine.current_time,
                    kind="tool_result",
                    role=role,
                    message_id=f"{message.message_id}.result",
                    call_id=message.message_id,
                    ok=ok,
                    result=value,
                    error=error,
                )
                self.sequence += 1
                self._append(role, result_message.to_dict())
                if ok and message.tool_name == "run.complete_checkpoint":
                    completed_roles.add(role)
            elif message.kind == "checkpoint_complete":
                if not message.checkpoint_id or not message.summary:
                    raise ProtocolViolation("invalid checkpoint completion")
                _complete_checkpoint(
                    self.engine,
                    role,
                    message.checkpoint_id,
                    message.summary,
                    f"complete-{message.checkpoint_id}-{role}",
                )
                completed_roles.add(role)
            elif message.kind == "team_message":
                recipient_role = message.recipient_role
                payload = message.payload
                if recipient_role is None or payload is None:
                    raise ProtocolViolation("invalid team message")
                _apply_team_message(self.engine, message)
                self._append(
                    recipient_role,
                    {
                        "kind": "team_message",
                        "from": role,
                        "payload": dict(payload),
                    },
                )
            elif message.kind in {"yield", "run_end"}:
                if message.kind == "yield":
                    _trace_yield(self.engine, message)
            else:
                raise ProtocolViolation(
                    f"adapter emitted unsupported message kind {message.kind}"
                )
            checkpoint = _checkpoint(self.engine) or {}
            required = set(checkpoint.get("required_roles", ROLE_ORDER))
            if (
                required.issubset(completed_roles)
                or self.engine.current_checkpoint_index != checkpoint_index
                or self.engine.status != "running"
            ):
                return False
            if _limit_reached(self.calls_this_checkpoint, call_cap):
                return True
        return _limit_reached(self.turns_this_checkpoint, turn_cap)

    def run(self) -> RunResult:
        started = time.monotonic()
        try:
            if not self.adapter_command:
                raise AgentProcessError("adapter command is empty")
            _activate_first(self.engine)
            while self.result.status == "running":
                checkpoint = _checkpoint(self.engine)
                if checkpoint is None:
                    raise RunnerError("world has no active checkpoint")
                call_cap, turn_cap = _run_caps(self.limits)
                completed_roles: set[str] = set()
                self.calls_this_checkpoint = 0
                self.turns_this_checkpoint = 0
                budget_exhausted = False
                required = set(checkpoint.get("required_roles", ROLE_ORDER))
                checkpoint_index = self.engine.current_checkpoint_index
                while (
                    not required.issubset(completed_roles)
                    and self.engine.status == "running"
                    and self.engine.current_checkpoint_index == checkpoint_index
                    and not budget_exhausted
                ):
                    for role in ROLE_ORDER:
                        if role in completed_roles:
                            continue
                        while role not in completed_roles:
                            if _limit_reached(
                                self.calls_this_checkpoint, call_cap
                            ) or _limit_reached(self.turns_this_checkpoint, turn_cap):
                                budget_exhausted = True
                                break
                            self.result.turns += 1
                            self.turns_this_checkpoint += 1
                            messages = self._request(role)
                            if not messages:
                                budget_exhausted = _limit_reached(
                                    self.turns_this_checkpoint, turn_cap
                                )
                                break
                            before = len(completed_roles)
                            if self._process(role, messages, completed_roles):
                                budget_exhausted = True
                                break
                            if len(completed_roles) == before and all(
                                message.kind in {"tool_call", "team_message"}
                                for message in messages
                            ):
                                continue
                            break
                        if (
                            budget_exhausted
                            or required.issubset(completed_roles)
                            or self.engine.status != "running"
                            or self.engine.current_checkpoint_index != checkpoint_index
                        ):
                            break
                if required.issubset(completed_roles):
                    if checkpoint.get("terminal"):
                        _complete_run(
                            self.engine,
                            "completed",
                            {"terminal": True},
                            None,
                            f"run-complete-{self.engine.manifest.run_id}",
                        )
                        self.result.status = "completed"
                        break
                    _advance(
                        self.engine,
                        False,
                        f"advance-{self.engine.current_checkpoint_index + 1}",
                    )
                else:
                    _advance(
                        self.engine,
                        budget_exhausted
                        or _limit_reached(self.calls_this_checkpoint, call_cap)
                        or _limit_reached(self.turns_this_checkpoint, turn_cap),
                        f"advance-budget-{self.engine.current_checkpoint_index + 1}",
                    )
                if self.engine.status != "running":
                    self.result.status = self.engine.status
        except (
            KeyError,
            OSError,
            ProtocolError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            self.result.errors.append(f"{type(exc).__name__}: {exc}")
            self.result.status = "failed"
            try:
                _complete_run(
                    self.engine,
                    "failed",
                    None,
                    self.result.errors[-1],
                    f"run-failed-{self.engine.manifest.run_id}",
                )
            except KeyError, OSError, RuntimeError, TypeError, ValueError:
                pass
        self.result.latency_ms += int((time.monotonic() - started) * 1000)
        return _write_outputs(self.engine, self.result, self.output_dir)


def _validate_replay_rows(
    engine: RunEngine, rows: Sequence[Mapping[str, Any]]
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    if not rows:
        return "protocol", [], None
    trace_mode = any("payload_hash" in row or "actor_role" in row for row in rows)
    if trace_mode:
        previous_sequence: int | None = None
        validated: list[dict[str, Any]] = []
        source_manifest: dict[str, Any] | None = None
        allowed = {
            "run_id",
            "sequence",
            "message_id",
            "occurred_at",
            "kind",
            "actor_role",
            "payload_hash",
            "payload",
            "idempotency_key",
            "latency_ms",
            "token_usage",
            "cost_minor_units",
        }
        for row in rows:
            if set(row) - allowed:
                raise ProtocolViolation("trace row contains unknown fields")
            try:
                event = TraceEvent.from_dict(row)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProtocolViolation(f"invalid trace row: {exc}") from exc
            if event.run_id != engine.manifest.run_id:
                raise ProtocolViolation(
                    "trace row run_id does not match the active run"
                )
            if event.kind not in TRACE_KINDS:
                raise ProtocolViolation(f"invalid trace event kind: {event.kind}")
            if event.actor_role not in ROLE_ORDER and event.actor_role != "system":
                raise ProtocolViolation("trace row actor role is invalid")
            if not isinstance(event.payload, Mapping) or not event.payload:
                raise ProtocolViolation("trace row payload must be a non-empty object")
            if (
                previous_sequence is not None
                and event.sequence != previous_sequence + 1
            ):
                raise ProtocolViolation("trace row sequence is not contiguous")
            expected_hash = (
                "sha256:"
                + hashlib.sha256(to_json(event.payload).encode("utf-8")).hexdigest()
            )
            if event.payload_hash != expected_hash:
                raise ProtocolViolation("trace row payload_hash does not match payload")
            if event.kind == "start":
                if source_manifest is not None:
                    raise ProtocolViolation("trace contains multiple start events")
                expected_manifest = engine.trace_manifest()
                expected_fields = set(expected_manifest) | {"manifest_fingerprint"}
                if set(event.payload) != expected_fields:
                    raise ProtocolViolation(
                        "trace start does not contain the required manifest fields"
                    )
                source = {key: event.payload[key] for key in expected_manifest}
                fingerprint = event.payload.get("manifest_fingerprint")
                if not isinstance(fingerprint, str):
                    raise ProtocolViolation(
                        "trace start manifest fingerprint is missing"
                    )
                if fingerprint != stable_hash(source):
                    raise ProtocolViolation(
                        "trace start manifest fingerprint is invalid"
                    )
                source_environment = source.get("environment")
                if not isinstance(source_environment, Mapping):
                    raise ProtocolViolation("trace source environment is invalid")
                try:
                    normalize_environment_manifest(source_environment)
                except BundleError as exc:
                    raise ProtocolViolation(
                        f"trace source environment is invalid: {exc}"
                    ) from exc
                if source != expected_manifest:
                    raise ProtocolViolation(
                        "trace source manifest does not match the active run"
                    )
                source_manifest = {
                    **source,
                    "manifest_fingerprint": fingerprint,
                }
            validated.append(dict(row))
            previous_sequence = event.sequence
        if source_manifest is None:
            raise ProtocolViolation("trace does not contain a manifest-bound start")
        return "trace", validated, source_manifest
    previous_sequence = None
    validated_messages: list[dict[str, Any]] = []
    source_manifest = None
    source_run_id: str | None = None
    for row in rows:
        try:
            message = Message.from_dict(row, allow_system=True)
        except (KeyError, TypeError, ValueError, ProtocolError) as exc:
            raise ProtocolViolation(f"invalid protocol row: {exc}") from exc
        if source_run_id is None:
            source_run_id = message.run_id
        elif message.run_id != source_run_id:
            raise ProtocolViolation("protocol rows contain multiple run_id values")
        if previous_sequence is not None and message.sequence <= previous_sequence:
            raise ProtocolViolation("protocol row sequence is not increasing")
        if message.kind == "start":
            payload = message.payload or {}
            world_id = payload.get("world_id")
            track = payload.get("track")
            if world_id is not None and str(world_id) != engine.manifest.world_id:
                raise ProtocolViolation(
                    "protocol source world_id does not match the active world"
                )
            if track is not None and str(track) != engine.manifest.track:
                raise ProtocolViolation(
                    "protocol source track does not match the active track"
                )
            configuration_fields = {
                "agent_manifest",
                "environment",
                "limits",
                "configuration_hash",
            }
            if configuration_fields & payload.keys():
                agent_manifest = payload.get("agent_manifest")
                environment = payload.get("environment")
                limits = payload.get("limits")
                if not isinstance(agent_manifest, Mapping) or not isinstance(
                    limits, Mapping
                ):
                    raise ProtocolViolation(
                        "protocol source configuration is incomplete"
                    )
                try:
                    normalized_agent = normalize_agent_manifest(
                        agent_manifest, require_resolved=True
                    )
                    normalized_environment = (
                        normalize_environment_manifest(environment)
                        if isinstance(environment, Mapping)
                        else None
                    )
                    RunLimits(
                        limits.get("tool_calls_per_checkpoint"),
                        limits.get("turns_per_checkpoint"),
                        limits.get("timeout_seconds"),
                        limits.get("retries", 0),
                    )
                except (BundleError, ValueError) as exc:
                    raise ProtocolViolation(
                        f"invalid protocol source configuration: {exc}"
                    ) from exc
                source_configuration = {
                    "agent_manifest": normalized_agent,
                    "limits": dict(limits),
                }
                if normalized_environment is not None:
                    source_configuration["environment"] = normalized_environment
                expected_hash = stable_hash(source_configuration)
                if payload.get("configuration_hash") != expected_hash:
                    raise ProtocolViolation(
                        "protocol source configuration hash is invalid"
                    )
                if (
                    normalized_agent != engine.manifest.agent_manifest
                    or dict(limits) != dict(engine.manifest.limits)
                    or (
                        normalized_environment is not None
                        and normalized_environment != engine.manifest.environment
                    )
                ):
                    raise ProtocolViolation(
                        "protocol source configuration does not match the active run"
                    )
            source_manifest = {
                "run_id": message.run_id,
                "world_id": world_id,
                "track": track,
                "protocol_version": message.protocol_version,
                **{key: payload[key] for key in configuration_fields if key in payload},
            }
        validated_messages.append(dict(row))
        previous_sequence = message.sequence
    return "protocol", validated_messages, source_manifest


def replay_trace(
    engine: RunEngine, trace: str | Path | Iterable[Mapping[str, Any]]
) -> RunResult:
    rows = (
        _jsonl(Path(trace))
        if isinstance(trace, (str, Path))
        else [dict(item) for item in trace]
    )
    mode, rows, source_manifest = _validate_replay_rows(engine, rows)
    stakeholder_manifest = (
        source_manifest.get("stakeholder_manifest")
        if isinstance(source_manifest, Mapping)
        else None
    )
    if (
        mode == "trace"
        and isinstance(stakeholder_manifest, Mapping)
        and stakeholder_manifest.get("model_id") == "subprocess"
    ):
        raise ProtocolViolation(
            "model-backed trace replay requires recorded stakeholder realizations"
        )
    if source_manifest is not None:
        engine._set_meta("source_manifest", to_json(source_manifest))
    _activate_first(engine)
    result = RunResult(
        engine.manifest.run_id,
        engine.manifest.world_id,
        engine.manifest.track,
        "running",
        diagnostic_replay=True,
    )
    completed_roles: set[str] = set()
    derived_checkpoint_keys = {
        str(row.get("idempotency_key"))
        for row in rows
        if mode == "trace"
        and row.get("kind") == "tool_call"
        and isinstance(row.get("payload"), Mapping)
        and str(row["payload"].get("tool_name", "")).endswith("run.complete_checkpoint")
        and row.get("idempotency_key")
    }
    for row in rows:
        if mode == "trace" and row.get("kind") == "tool_result":
            continue
        if row.get("kind") == "tool_call":
            payload = row.get("payload", row)
            tool_name = (
                payload.get("tool_name")
                or row.get("tool_name")
                or (
                    f"{payload.get('tool')}.{payload.get('action')}"
                    if payload.get("tool") and payload.get("action")
                    else None
                )
            )
            if not tool_name:
                continue
            arguments = dict(payload.get("arguments", row.get("arguments", {})) or {})
            if (
                tool_name == "crm.search"
                and "query" not in arguments
                and "deal_id" in arguments
            ):
                arguments = {"query": str(arguments["deal_id"]), "limit": 100}
            message = _message(
                run_id=engine.manifest.run_id,
                sequence=int(row.get("sequence", result.turns)),
                occurred_at=str(row.get("occurred_at", engine.current_time)),
                kind="tool_call",
                role=str(row.get("role", row.get("actor_role", "account_executive"))),
                message_id=str(
                    payload.get(
                        "call_id", row.get("message_id", f"call-{result.turns}")
                    )
                ),
                tool_name=tool_name,
                arguments=arguments,
                idempotency_key=payload.get(
                    "idempotency_key", row.get("idempotency_key")
                ),
            )
            ok, _, _ = dispatch_tool(engine, message.role, message, None)
            result.tool_calls += 1
            if ok and tool_name == "run.complete_checkpoint":
                completed_roles.add(message.role)
                checkpoint = _checkpoint(engine)
                required = (
                    set(checkpoint.get("required_roles", ROLE_ORDER))
                    if checkpoint
                    else set(ROLE_ORDER)
                )
                if checkpoint and required.issubset(completed_roles):
                    if checkpoint.get("terminal"):
                        _complete_run(
                            engine,
                            "completed",
                            {"replayed": True},
                            None,
                            f"replay-complete-{engine.manifest.run_id}",
                        )
                        result.status = "completed"
                    else:
                        _advance(
                            engine,
                            False,
                            f"replay-advance-{engine.current_checkpoint_index + 1}",
                        )
                    completed_roles.clear()
        elif row.get("kind") == "team_message":
            payload = row.get("payload", row)
            protocol = (
                payload.get("protocol_message")
                if isinstance(payload, Mapping)
                else None
            )
            if isinstance(protocol, Mapping) and {
                "protocol_version",
                "run_id",
                "sequence",
                "message_id",
                "occurred_at",
                "kind",
                "role",
            } <= set(protocol):
                message = Message.from_dict(protocol, allow_system=True)
            else:
                message = _message(
                    run_id=engine.manifest.run_id,
                    sequence=int(row.get("sequence", result.turns)),
                    occurred_at=str(row.get("occurred_at", engine.current_time)),
                    kind="team_message",
                    role=str(
                        row.get("actor_role", row.get("role", "account_executive"))
                    ),
                    message_id=str(row.get("message_id", f"team-{result.turns}")),
                    recipient_role=str(
                        payload.get("recipient_role", row.get("recipient_role", ""))
                    ),
                    payload=(
                        payload.get("payload", payload)
                        if isinstance(payload, Mapping)
                        else {}
                    ),
                )
            _apply_team_message(engine, message, trace=mode == "protocol")
        elif row.get("kind") == "yield":
            payload = row.get("payload", row)
            protocol = (
                payload.get("protocol_message")
                if isinstance(payload, Mapping)
                else None
            )
            if isinstance(protocol, Mapping) and {
                "protocol_version",
                "run_id",
                "sequence",
                "message_id",
                "occurred_at",
                "kind",
                "role",
            } <= set(protocol):
                message = Message.from_dict(protocol, allow_system=True)
            else:
                message = _message(
                    run_id=engine.manifest.run_id,
                    sequence=int(row.get("sequence", result.turns)),
                    occurred_at=str(row.get("occurred_at", engine.current_time)),
                    kind="yield",
                    role=str(
                        row.get("actor_role", row.get("role", "account_executive"))
                    ),
                    message_id=str(row.get("message_id", f"yield-{result.turns}")),
                    reason=str(payload.get("reason", ""))
                    if isinstance(payload, Mapping)
                    and payload.get("reason") is not None
                    else None,
                )
            _trace_yield(engine, message, trace=mode == "protocol")
        elif row.get("kind") == "checkpoint_complete":
            if mode == "trace" and (
                str(row.get("idempotency_key")) in derived_checkpoint_keys
            ):
                continue
            payload = row.get("payload", row)
            role = str(
                row.get(
                    "role",
                    row.get("actor_role", payload.get("role", "account_executive")),
                )
            )
            checkpoint_id = payload.get("checkpoint_id", row.get("checkpoint_id"))
            if checkpoint_id:
                summary = str(payload.get("summary", payload.get("action", "replayed")))
                _complete_checkpoint(
                    engine,
                    role,
                    str(checkpoint_id),
                    summary,
                    f"replay-{checkpoint_id}-{role}",
                )
                completed_roles.add(role)
                checkpoint = _checkpoint(engine)
                required = (
                    set(checkpoint.get("required_roles", ROLE_ORDER))
                    if checkpoint
                    else set(ROLE_ORDER)
                )
                if (
                    checkpoint
                    and str(checkpoint.get("checkpoint_id")) == str(checkpoint_id)
                    and required.issubset(completed_roles)
                ):
                    if checkpoint.get("terminal"):
                        _complete_run(
                            engine,
                            "completed",
                            {"replayed": True},
                            None,
                            f"replay-complete-{engine.manifest.run_id}",
                        )
                        result.status = "completed"
                    else:
                        _advance(
                            engine,
                            False,
                            f"replay-advance-{engine.current_checkpoint_index + 1}",
                        )
                    completed_roles.clear()
        elif row.get("kind") == "observation":
            if mode == "trace":
                continue
            payload = row.get("payload", {})
            advance = (
                payload.get("checkpoint_advanced")
                if isinstance(payload, Mapping)
                else None
            )
            if isinstance(advance, Mapping) and result.status == "running":
                expected = (
                    int(
                        advance.get("checkpoint", {}).get(
                            "sequence", engine.current_checkpoint_index + 1
                        )
                    )
                    if isinstance(advance.get("checkpoint"), Mapping)
                    else engine.current_checkpoint_index + 1
                )
                if expected > engine.current_checkpoint_index:
                    _advance(
                        engine,
                        bool(advance.get("budget_exhausted", False)),
                        f"replay-budget-{expected}",
                    )
                    completed_roles.clear()
        elif row.get("kind") == "run_end":
            if engine.status != "running":
                result.status = engine.status
    if mode == "trace":
        engine.connection.execute("DELETE FROM trace")
        for row in rows:
            sequence = int(row.get("sequence", 0))
            engine.connection.execute(
                "INSERT INTO trace(sequence, raw) VALUES (?, ?)",
                (sequence, to_json(row)),
            )
    if result.status == "running":
        result.status = engine.status
    if result.status == "completed":
        engine.persist_resource_usage(result.to_dict()["resource_usage"])
    result.state_hash = engine.state_hash()
    return result


def run_open_team(
    engine: RunEngine, command: str | Sequence[str], **kwargs: Any
) -> RunResult:
    return OpenTeamRunner(engine, command, **kwargs).run()


def run_fixed_harness(
    engine: RunEngine, adapter_command: str | Sequence[str], **kwargs: Any
) -> RunResult:
    return FixedHarnessScheduler(engine, adapter_command, **kwargs).run()


def run_replicates(
    bundle: str | Path | WorldBundle,
    command: str | Sequence[str],
    *,
    track: str = "open_team",
    trials: int = 3,
    team_id: str = "reference",
    seed: int | None = None,
    stakeholder_seeds: Sequence[int] | None = None,
    limits: RunLimits | None = None,
    agent_manifest: Mapping[str, Any] | None = None,
    environment_manifest: Mapping[str, Any] | None = None,
    output_dir: str | Path | None = None,
    allow_private: bool = False,
) -> tuple[RunResult, ...]:
    if trials < 1:
        raise ValueError("trials must be positive")
    if track not in {"open_team", "fixed_harness"}:
        raise ValueError("track must be open_team or fixed_harness")
    actual_agent_manifest = normalize_agent_manifest(
        agent_manifest, require_resolved=True
    )
    validate_track_agent_manifest(track, actual_agent_manifest)
    actual_environment_manifest = normalize_environment_manifest(
        environment_manifest, require_resolved=True
    )
    world = (
        bundle
        if isinstance(bundle, WorldBundle)
        else load_world_bundle(bundle, allow_private=allow_private)
    )
    actual_limits = limits or RunLimits()
    base_seed = int(seed if seed is not None else world.manifest.get("seed", 0))
    official_seeds = normalize_official_seeds(stakeholder_seeds, base_seed)
    replicate_seeds = official_seeds + tuple(
        max(official_seeds) + index for index in range(1, max(0, trials - 3) + 1)
    )
    root = Path(output_dir) if output_dir is not None else None
    results: list[RunResult] = []
    for trial in range(1, trials + 1):
        trial_digest = hashlib.sha256(
            f"{world.world_id}:{track}:{team_id}:{base_seed}:{trial}".encode()
        ).hexdigest()[:24]
        run_id = f"replicate-{trial_digest}"
        trial_dir = root / f"trial-{trial}" if root is not None else None
        if trial_dir is not None:
            trial_dir.mkdir(parents=True, exist_ok=True)
        db_path = trial_dir / "run.sqlite" if trial_dir is not None else ":memory:"
        trace_path = trial_dir / "trace.jsonl" if trial_dir is not None else None
        engine = open_world(
            world,
            run_id=run_id,
            track=track,
            team_id=team_id,
            seed=replicate_seeds[trial - 1],
            limits=actual_limits,
            agent_manifest=actual_agent_manifest,
            environment_manifest=actual_environment_manifest,
            db_path=db_path,
            trace_path=trace_path,
            allow_private=allow_private,
            stakeholder_seeds=official_seeds,
            stakeholder_seed=official_seeds[(trial - 1) % len(official_seeds)],
        )
        try:
            if track == "fixed_harness":
                result = run_fixed_harness(
                    engine, command, limits=actual_limits, output_dir=trial_dir
                )
            else:
                result = run_open_team(
                    engine, command, limits=actual_limits, output_dir=trial_dir
                )
            results.append(result)
        finally:
            engine.close()
    return tuple(results)


__all__ = [
    "ROLE_ORDER",
    "TOOLS",
    "AgentProcessError",
    "BundleError",
    "FixedHarnessScheduler",
    "OpenTeamRunner",
    "ProtocolViolation",
    "RunLimits",
    "RunResult",
    "RunnerError",
    "WorldBundle",
    "deterministic_agent_manifest",
    "dispatch_tool",
    "load_world_bundle",
    "normalize_agent_manifest",
    "normalize_environment_manifest",
    "open_world",
    "replay_trace",
    "run_fixed_harness",
    "run_open_team",
    "run_replicates",
    "tool_schemas",
    "validate_dataset",
    "validate_track_agent_manifest",
    "validate_world_bundle",
]
