from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.core.models import utc_now


def inspect_jsonl(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    malformed: list[dict[str, Any]] = []
    valid = 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            malformed.append(
                {
                    "line_number": line_number,
                    "sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                    "error": str(exc),
                }
            )
            continue
        if isinstance(value, dict):
            valid += 1
        else:
            malformed.append(
                {
                    "line_number": line_number,
                    "sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                    "error": "JSONL record is not an object",
                }
            )
    return {
        "path": str(path.resolve()),
        "checked_at": utc_now(),
        "line_count": len(lines),
        "valid_record_count": valid,
        "malformed_record_count": len(malformed),
        "malformed_records": malformed,
        "rewritten": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--filename", default="audit.jsonl")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    report = inspect_jsonl(root / "logs" / args.filename)
    output = root / "logs" / f"{Path(args.filename).stem}_integrity_report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
