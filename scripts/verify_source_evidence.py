import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def source_records(
    registry_path: Path, manifest_path: Path
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources = {row["source_id"]: row for row in registry["sources"]}
    evidence = {row["source_id"]: row for row in manifest["evidence"]}
    if len(sources) != len(registry["sources"]) or len(evidence) != len(
        manifest["evidence"]
    ):
        raise ValueError("source evidence identifiers must be unique")
    if sources.keys() != evidence.keys():
        raise ValueError("source registry and evidence manifest must be a bijection")
    for source_id in sorted(sources):
        source = sources[source_id]
        row = evidence[source_id]
        if (
            source["url"] != row["source_url"]
            or source["retrieval_bytes"] != row["bytes"]
            or source["retrieval_sha256"].removeprefix("sha256:") != row["sha256"]
            or source["retrieval_method"] != "verified_official_hash_only"
            or row["retrieval_status"] != "verified_official_hash_only"
            or "path" in row
            or "evidence_path" in source
        ):
            raise ValueError(f"source evidence metadata mismatch: {source_id}")
        yield source, row


def fetch_digest(
    url: str,
    timeout: float | None,
    opener: Callable[..., Any] | None = None,
) -> tuple[int, str]:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
        },
    )
    digest = hashlib.sha256()
    size = 0
    options = {} if timeout is None else {"timeout": timeout}
    with closing((opener or urlopen)(request, **options)) as response:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        type=Path,
        default=root / "src/edlb/resources/source_registry.json",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "src/edlb/resources/source_evidence/manifest.json",
    )
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args(argv)
    selected = set(args.source_id)
    try:
        records = list(source_records(args.registry, args.manifest))
        if args.timeout is not None and args.timeout <= 0:
            raise ValueError("timeout must be positive")
        known = {str(source["source_id"]) for source, _ in records}
        if selected - known:
            raise ValueError(
                "unknown source identifiers: " + ", ".join(sorted(selected - known))
            )
        for source, row in records:
            source_id = str(source["source_id"])
            if selected and source_id not in selected:
                continue
            size, digest = fetch_digest(str(source["url"]), args.timeout)
            if size != row["bytes"] or digest != row["sha256"]:
                raise ValueError(f"source bytes mismatch: {source_id} {size} {digest}")
            print(f"{source_id} {size} {digest}")
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
