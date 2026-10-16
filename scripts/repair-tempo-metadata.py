import argparse
import json
import os
import shutil
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID


def scan_blocks(root: Path, apply: bool, prune_expired: bool = False) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    quarantine = root / "quarantine" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {"checked": 0, "expired": 0, "corrupt": 0, "quarantined": 0, "pruned": 0}
    for storage in (root / "blocks", root / "wal" / "blocks"):
        if not storage.is_dir():
            continue
        for tenant in storage.iterdir():
            if not tenant.is_dir() or tenant.is_symlink():
                continue
            with ExitStack() as resources:
                entries = resources.enter_context(os.scandir(tenant))
                deletion_manifest = None
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        UUID(entry.name)
                    except ValueError:
                        continue
                    block = Path(entry.path)
                    metadata = block / "meta.json"
                    if not metadata.exists():
                        metadata = block / "meta.compacted.json"
                        if not metadata.exists():
                            continue
                    summary["checked"] += 1
                    try:
                        with metadata.open(encoding="utf-8") as source:
                            document = json.load(source)
                        if not isinstance(document, dict):
                            raise ValueError("metadata is not a JSON object")
                        end = datetime.fromisoformat(document["endTime"].replace("Z", "+00:00"))
                        if end < cutoff:
                            summary["expired"] += 1
                            if apply and prune_expired:
                                if deletion_manifest is None:
                                    quarantine.mkdir(parents=True, exist_ok=True)
                                    deletion_manifest = resources.enter_context(
                                        (quarantine / "expired-blocks.jsonl").open("a", encoding="utf-8")
                                    )
                                deletion_manifest.write(json.dumps({"block": str(block), "endTime": document["endTime"]}) + "\n")
                                shutil.rmtree(block)
                                summary["pruned"] += 1
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
                        summary["corrupt"] += 1
                        record = {"block": str(block), "error": str(error)}
                        if apply:
                            destination = quarantine / block.relative_to(root)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            block.rename(destination)
                            summary["quarantined"] += 1
                            record["quarantine"] = str(destination)
                            with (quarantine / "manifest.jsonl").open("a", encoding="utf-8") as manifest:
                                manifest.write(json.dumps(record) + "\n")
                        print(json.dumps(record), flush=True)
                    if summary["checked"] % 100000 == 0:
                        print(json.dumps({"progress": summary}), flush=True)
    print(json.dumps({"summary": summary}), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan Tempo metadata; apply only while Tempo is stopped.")
    parser.add_argument("--root", type=Path, default=Path("/data/tempo"))
    parser.add_argument("--apply", action="store_true", help="Apply repairs while Tempo is stopped.")
    parser.add_argument("--prune-expired", action="store_true", help="Also delete validated blocks ending more than 14 days ago; dry-run unless --apply is set.")
    arguments = parser.parse_args()
    scan_blocks(arguments.root, arguments.apply, arguments.prune_expired)


if __name__ == "__main__":
    main()