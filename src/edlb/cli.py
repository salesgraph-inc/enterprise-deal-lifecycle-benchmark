from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .baselines import podman_command, run_baseline
from .generate import generate_dataset
from .grading import aggregate_scorecards, grade_run
from .reporting import scorecard_json, write_report
from .runner import (
    BundleError,
    FixedHarnessScheduler,
    OpenTeamRunner,
    RunLimits,
    _write_outputs,
    load_world_bundle,
    open_world,
    replay_trace,
    validate_dataset,
)


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _resolve_world(path: str | Path, allow_private: bool = False):
    candidate = Path(path)
    if candidate.is_dir() and (candidate / "manifest.json").is_file():
        return candidate
    if candidate.is_file() and candidate.name == "manifest.json":
        return candidate.parent
    for root in (candidate, candidate / "benchmarks" / "v1"):
        for location in (
            root / "output" / "public" / "train",
            root / "output" / "public" / "dev",
            root / "private" / "blind",
        ):
            if location.is_dir():
                matches = list(location.glob(f"{path}*/manifest.json"))
                if matches:
                    return matches[0].parent
    raise ValueError(f"world bundle not found: {path}")


def _output_path(args: argparse.Namespace, run_id: str, *, fresh: bool = False) -> Path:
    value = Path(args.output) if args.output else Path("runs") / run_id
    if fresh and value.exists() and (not value.is_dir() or any(value.iterdir())):
        raise ValueError(f"output path already exists and is not empty: {value}")
    value.mkdir(parents=True, exist_ok=True)
    return value


def _trace_start(path: Path) -> dict[str, Any]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict) and value.get("kind") == "start":
                return value
    raise ValueError("trace does not contain a start event")


def _trace_run_id(path: Path) -> str:
    value = _trace_start(path)
    if value.get("run_id"):
        return str(value["run_id"])
    raise ValueError("trace start does not contain a run_id")


def _trace_open_options(path: Path) -> dict[str, Any]:
    start = _trace_start(path)
    payload = start.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    if "manifest_fingerprint" not in payload:
        return {"track": payload.get("track", "open_team")}
    stakeholder = payload.get("stakeholder_manifest")
    stakeholder = stakeholder if isinstance(stakeholder, dict) else {}
    official = stakeholder.get("official_seeds")
    stakeholder_seeds = (
        tuple(int(seed) for seed in official)
        if isinstance(official, Sequence) and not isinstance(official, (str, bytes))
        else None
    )
    limits = payload.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    model_digest = (
        stakeholder.get("model_digest")
        if stakeholder.get("model_id") == "subprocess"
        else None
    )
    return {
        "track": payload.get("track", "open_team"),
        "team_id": payload.get("team_id", "reference"),
        "seed": int(payload["seed"]),
        "stakeholder_model_digest": model_digest,
        "stakeholder_prompt_hash": stakeholder.get("prompt_hash"),
        "stakeholder_seeds": stakeholder_seeds,
        "stakeholder_seed": int(stakeholder["seed"])
        if stakeholder.get("seed") is not None
        else None,
        "limits": RunLimits(
            int(limits.get("tool_calls_per_checkpoint", 64)),
            int(limits.get("turns_per_checkpoint", 128)),
            float(limits.get("timeout_seconds", 30.0)),
            int(limits.get("retries", 2)),
        ),
    }


def _limits(args: argparse.Namespace) -> RunLimits:
    return RunLimits(args.max_tool_calls, args.max_turns, args.timeout, args.retries)


def cmd_validate(args: argparse.Namespace) -> int:
    summary = validate_dataset(args.path, allow_private=args.allow_private)
    _json_print(summary)
    return 0 if summary.get("valid") else 1


def cmd_generate(args: argparse.Namespace) -> int:
    summary = generate_dataset(args.root, args.private_config, args.official)
    _json_print(summary)
    return 0 if summary.get("valid") else 1


def cmd_run(args: argparse.Namespace) -> int:
    world_path = _resolve_world(args.world, allow_private=args.allow_private)
    bundle = load_world_bundle(world_path, allow_private=args.allow_private)
    run_id = args.run_id
    output_key = (
        run_id
        or f"run-{hashlib.sha256(bundle.world_id.encode('utf-8')).hexdigest()[:24]}"
    )
    output = _output_path(args, output_key, fresh=True)
    limits = _limits(args)
    engine = open_world(
        bundle,
        run_id=run_id,
        track=args.track,
        team_id=args.team_id,
        seed=args.seed,
        limits=limits,
        db_path=output / "run.sqlite",
        trace_path=output / "trace.jsonl",
        allow_private=args.allow_private,
    )
    try:
        if args.baseline:
            result = run_baseline(engine, args.baseline, output)
        elif args.track == "fixed_harness":
            if not args.adapter_command:
                raise ValueError("--adapter-command is required for fixed_harness")
            result = FixedHarnessScheduler(
                engine, args.adapter_command, limits, output
            ).run()
        else:
            if not args.agent_command:
                raise ValueError("--agent-command is required for open_team")
            result = OpenTeamRunner(engine, args.agent_command, limits, output).run()
    finally:
        engine.close()
    _json_print(result.to_dict())
    return 0 if result.status == "completed" else 1


def cmd_replay(args: argparse.Namespace) -> int:
    world_path = _resolve_world(args.world, allow_private=args.allow_private)
    run_id = args.run_id or _trace_run_id(args.trace)
    options = _trace_open_options(args.trace)
    output = _output_path(args, run_id, fresh=True)
    engine = open_world(
        world_path,
        run_id=run_id,
        track=options.get("track", "open_team"),
        team_id=options.get("team_id", "reference"),
        seed=options.get("seed"),
        stakeholder_seeds=options.get("stakeholder_seeds"),
        stakeholder_seed=options.get("stakeholder_seed"),
        limits=options.get("limits"),
        db_path=output / "run.sqlite",
        trace_path=output / "trace.jsonl",
        allow_private=args.allow_private,
        stakeholder_model_digest=options.get("stakeholder_model_digest"),
        stakeholder_prompt_hash=options.get("stakeholder_prompt_hash"),
    )
    try:
        result = replay_trace(engine, args.trace)
        _write_outputs(engine, result, output)
    finally:
        engine.close()
    _json_print(result.to_dict())
    return 0 if result.status in {"completed", "running"} else 1


def _run_db(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "run.sqlite"
    if not candidate.is_file():
        raise ValueError(f"run database not found: {candidate}")
    return candidate


def cmd_grade(args: argparse.Namespace) -> int:
    database = _run_db(args.run)
    rubric = Path(args.rubric)
    oracle = Path(args.oracle) if args.oracle else None
    scorecard = grade_run(database, rubric, oracle=oracle)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(scorecard_json(scorecard), encoding="utf-8")
    _json_print(json.loads(scorecard_json(scorecard)))
    return 0 if scorecard.get("status") in {"valid", "agent_error"} else 1


def cmd_report(args: argparse.Namespace) -> int:
    source = Path(args.scorecard)
    value = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(value, list):
        value = aggregate_scorecards(value)
    destination = Path(args.output)
    markdown = Path(args.markdown) if args.markdown else None
    json_path, markdown_path = write_report(value, destination, markdown)
    _json_print({"json": str(json_path), "markdown": str(markdown_path)})
    return 0


def cmd_podman(args: argparse.Namespace) -> int:
    command = podman_command(args.image, args.command, args.world, args.output)
    _json_print({"command": command})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edlb")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("path", type=Path)
    validate.add_argument("--allow-private", action="store_true")
    validate.set_defaults(handler=cmd_validate)

    generate = commands.add_parser("generate")
    generate.add_argument("--root", type=Path)
    generate.add_argument("--private-config", type=Path)
    generate.add_argument("--official", action="store_true")
    generate.set_defaults(handler=cmd_generate)

    run = commands.add_parser("run")
    run.add_argument("world")
    run.add_argument(
        "--track", choices=("open_team", "fixed_harness"), default="open_team"
    )
    run.add_argument("--agent-command")
    run.add_argument("--adapter-command")
    run.add_argument(
        "--baseline", choices=("oracle", "scripted_oracle", "do_nothing", "flawed")
    )
    run.add_argument("--team-id", default="reference")
    run.add_argument("--run-id")
    run.add_argument("--seed", type=int)
    run.add_argument("--output", type=Path)
    run.add_argument("--max-tool-calls", type=int, default=64)
    run.add_argument("--max-turns", type=int, default=128)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--retries", type=int, default=2)
    run.add_argument("--allow-private", action="store_true")
    run.set_defaults(handler=cmd_run)

    replay = commands.add_parser("replay")
    replay.add_argument("world")
    replay.add_argument("trace", type=Path)
    replay.add_argument("--run-id")
    replay.add_argument("--output", type=Path)
    replay.add_argument("--allow-private", action="store_true")
    replay.set_defaults(handler=cmd_replay)

    grade = commands.add_parser("grade")
    grade.add_argument("run")
    grade.add_argument("--rubric", required=True, type=Path)
    grade.add_argument("--oracle", type=Path)
    grade.add_argument("--output", type=Path)
    grade.set_defaults(handler=cmd_grade)

    report = commands.add_parser("report")
    report.add_argument("scorecard", type=Path)
    report.add_argument("--output", required=True, type=Path)
    report.add_argument("--markdown", type=Path)
    report.set_defaults(handler=cmd_report)

    podman = commands.add_parser("podman")
    podman.add_argument("image")
    podman.add_argument("world", type=Path)
    podman.add_argument("output", type=Path)
    podman.add_argument("command", nargs=argparse.REMAINDER)
    podman.set_defaults(handler=cmd_podman)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (BundleError, ValueError, OSError, RuntimeError) as exc:
        print(f"edlb: {exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
