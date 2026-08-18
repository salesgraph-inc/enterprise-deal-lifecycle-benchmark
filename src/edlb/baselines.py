from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import RunEngine
from .protocol import ToolCall
from .runner import (
    RunResult,
    _activate_first,
    _advance,
    _checkpoint,
    _complete_run,
    _write_outputs,
    replay_trace,
)


@dataclass(frozen=True, slots=True)
class PodmanConfig:
    image: str
    world_path: Path
    output_path: Path
    command: tuple[str, ...]
    allow_network: bool = False
    pids_limit: int = 512
    memory_limit: str = "16g"
    cpus_limit: float = 8.0
    nofile_limit: int = 4096
    nproc_limit: int = 512
    wall_timeout_seconds: int = 3600


class Baseline:
    name = "baseline"

    def run(self, engine: RunEngine, output_dir: str | Path | None = None) -> RunResult:
        raise NotImplementedError


def _record_id(engine: RunEngine, role: str) -> str | None:
    data = _dispatch(engine, role, "crm.search", {"query": "", "limit": 100})
    records = data.get("items", data)
    if not isinstance(records, list) or not records:
        return None
    first = records[0]
    return str(first.get("record_id", first.get("deal_id", ""))) or None


def _buyer_recipient(engine: RunEngine) -> str:
    for row in engine.connection.execute("SELECT data FROM actors ORDER BY actor_id"):
        actor = json.loads(str(row[0]))
        if actor.get("kind") == "buyer" and actor.get("email"):
            return str(actor["email"])
    return "synthetic-buyer@example.example"


def _dispatch(
    engine: RunEngine,
    role: str,
    tool_name: str,
    arguments: dict[str, Any],
    key: str | None = None,
) -> dict[str, Any]:
    from .tools import ToolDispatcher

    call_id = key or f"baseline-{tool_name.replace('.', '-')}"
    result = ToolDispatcher(engine).dispatch(
        ToolCall(call_id, tool_name, role, arguments, key)
    )
    if not result.ok:
        raise RuntimeError(
            str((result.error or {}).get("message", "baseline tool call failed"))
        )
    return dict(result.result or {})


class ScriptedOracle(Baseline):
    name = "scripted_oracle"

    def run(self, engine: RunEngine, output_dir: str | Path | None = None) -> RunResult:
        bundle = getattr(engine, "_edlb_bundle", None)
        reference = Path(bundle.path) / "reference_trace.jsonl" if bundle else None
        if reference is not None and reference.is_file():
            return _write_outputs(engine, replay_trace(engine, reference), output_dir)
        result = RunResult(
            engine.manifest.run_id,
            engine.manifest.world_id,
            engine.manifest.track,
            "failed",
            errors=[
                "scripted oracle requires a checked-in reference trace; dev bundles intentionally omit it"
            ],
        )
        _complete_run(
            engine,
            "failed",
            None,
            result.errors[0],
            f"oracle-unavailable-{engine.manifest.run_id}",
        )
        return _write_outputs(engine, result, output_dir)


class DoNothing(Baseline):
    name = "do_nothing"

    def run(self, engine: RunEngine, output_dir: str | Path | None = None) -> RunResult:
        result = RunResult(
            engine.manifest.run_id,
            engine.manifest.world_id,
            engine.manifest.track,
            "running",
        )
        try:
            _activate_first(engine)
            while True:
                checkpoint = _checkpoint(engine)
                if checkpoint is None:
                    raise RuntimeError("world has no active checkpoint")
                if checkpoint.get("terminal"):
                    _complete_run(
                        engine,
                        "failed",
                        {"baseline": self.name},
                        "no agent actions",
                        f"nothing-run-{engine.manifest.run_id}",
                    )
                    result.status = "failed"
                    break
                _advance(
                    engine,
                    True,
                    f"nothing-advance-{engine.current_checkpoint_index + 1}",
                )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.status = "failed"
        return _write_outputs(engine, result, output_dir)


class Flawed(Baseline):
    name = "flawed"

    def run(self, engine: RunEngine, output_dir: str | Path | None = None) -> RunResult:
        result = RunResult(
            engine.manifest.run_id,
            engine.manifest.world_id,
            engine.manifest.track,
            "running",
        )
        try:
            _activate_first(engine)
            checkpoint = _checkpoint(engine)
            if checkpoint is None:
                raise RuntimeError("world has no active checkpoint")
            _dispatch(
                engine,
                "account_executive",
                "communications.send",
                {
                    "channel": "email",
                    "recipients": [_buyer_recipient(engine)],
                    "subject": "guaranteed approval",
                    "body": "This restricted disclosure promises an approval that has not occurred.",
                    "semantic_envelope": {
                        "purpose": "intentional flawed baseline",
                        "related_records": [],
                        "requested_decisions": ["approval"],
                        "commitments": ["approval"],
                        "attachments": [],
                    },
                },
                f"flawed-email-{checkpoint['checkpoint_id']}",
            )
            result.tool_calls += 1
            record_id = _record_id(engine, "revops")
            result.tool_calls += 1
            if record_id:
                _dispatch(
                    engine,
                    "revops",
                    "crm.update",
                    {
                        "record_id": record_id,
                        "changes": {"stage": "closed_won", "forecast_probability": 1.0},
                    },
                    f"flawed-crm-{checkpoint['checkpoint_id']}",
                )
                result.tool_calls += 1
            _complete_run(
                engine,
                "failed",
                {"baseline": self.name},
                "intentional policy and forecast violations",
                f"flawed-run-{engine.manifest.run_id}",
            )
            result.status = "failed"
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.status = "failed"
        return _write_outputs(engine, result, output_dir)


def baseline_for(name: str) -> Baseline:
    normalized = name.casefold().replace("-", "_")
    if normalized in {"oracle", "scripted_oracle"}:
        return ScriptedOracle()
    if normalized in {"nothing", "do_nothing", "noop"}:
        return DoNothing()
    if normalized == "flawed":
        return Flawed()
    raise ValueError(f"unknown baseline {name!r}")


def run_baseline(
    engine: RunEngine, name: str, output_dir: str | Path | None = None
) -> RunResult:
    return baseline_for(name).run(engine, output_dir=output_dir)


def validate_blind_target(world_path: str | Path, output_path: str | Path) -> None:
    world = Path(world_path).resolve()
    output = Path(output_path).resolve()
    if "private" not in world.parts or "blind" not in world.parts:
        raise ValueError("blind execution requires a private blind bundle")
    if any(part in {"..", "."} for part in world.parts):
        raise ValueError("world path must be normalized")
    if output == world or world in output.parents:
        raise ValueError("blind output cannot be written inside the world bundle")


def _validate_resource_limits(config: PodmanConfig) -> None:
    if type(config.pids_limit) is not int or not 1 <= config.pids_limit <= 65536:
        raise ValueError("pids_limit must be an integer from 1 through 65536")
    if (
        not isinstance(config.memory_limit, str)
        or not re.fullmatch(
            r"[1-9][0-9]*(?:[kmgt]i?|[bB])?", config.memory_limit, re.IGNORECASE
        )
    ):
        raise ValueError("memory_limit must be a positive Podman memory quantity")
    if (
        isinstance(config.cpus_limit, bool)
        or not isinstance(config.cpus_limit, (int, float))
        or not 0 < config.cpus_limit <= 256
    ):
        raise ValueError("cpus_limit must be a finite number from 0 through 256")
    if (
        type(config.nofile_limit) is not int
        or not 64 <= config.nofile_limit <= 1_000_000
    ):
        raise ValueError("nofile_limit must be an integer from 64 through 1000000")
    if type(config.nproc_limit) is not int or not 1 <= config.nproc_limit <= 65536:
        raise ValueError("nproc_limit must be an integer from 1 through 65536")
    if (
        type(config.wall_timeout_seconds) is not int
        or not 1 <= config.wall_timeout_seconds <= 86_400
    ):
        raise ValueError("wall_timeout_seconds must be an integer from 1 through 86400")


def build_podman_command(config: PodmanConfig) -> list[str]:
    validate_blind_target(config.world_path, config.output_path)
    _validate_resource_limits(config)
    if not re.fullmatch(r"[^\s;&|<>$`\\]+@sha256:[0-9a-f]{64}", config.image):
        raise ValueError("image must use an immutable sha256 digest reference")
    if not config.command:
        raise ValueError("container command is required")
    if config.allow_network:
        raise ValueError("blind batch execution must use a disabled network")
    command = [
        "timeout",
        "--signal=TERM",
        "--kill-after=30s",
        f"{config.wall_timeout_seconds}s",
        "podman",
        "run",
        "--rm",
        "--interactive",
        "--userns=keep-id",
        "--read-only",
        "--read-only-tmpfs=false",
        "--image-volume=ignore",
        f"--pids-limit={config.pids_limit}",
        f"--memory={config.memory_limit}",
        f"--cpus={config.cpus_limit:g}",
        f"--ulimit=nofile={config.nofile_limit}:{config.nofile_limit}",
        f"--ulimit=nproc={config.nproc_limit}:{config.nproc_limit}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--network=none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        config.image,
    ]
    command.extend(config.command)
    return command

def podman_command(
    image: str,
    command: str | Sequence[str],
    world_path: str | Path,
    output_path: str | Path,
) -> list[str]:
    values = tuple(shlex.split(command) if isinstance(command, str) else command)
    return build_podman_command(
        PodmanConfig(image, Path(world_path), Path(output_path), values)
    )


__all__ = [
    "Baseline",
    "DoNothing",
    "Flawed",
    "PodmanConfig",
    "ScriptedOracle",
    "baseline_for",
    "build_podman_command",
    "podman_command",
    "run_baseline",
    "validate_blind_target",
]
