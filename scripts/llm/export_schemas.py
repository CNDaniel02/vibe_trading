from __future__ import annotations

import json
from pathlib import Path

from scripts.llm.schemas import AGENT_INPUT_SCHEMA, CHALLENGE_OUTPUT_SCHEMA, DECISION_OUTPUT_SCHEMA, NEWS_OUTPUT_SCHEMA


def export(root: str | Path) -> list[Path]:
    destination = Path(root) / "schemas"
    destination.mkdir(parents=True, exist_ok=True)
    schemas = {
        "agent_input.schema.json": AGENT_INPUT_SCHEMA,
        "news_agent_output.schema.json": NEWS_OUTPUT_SCHEMA,
        "challenge_agent_output.schema.json": CHALLENGE_OUTPUT_SCHEMA,
        "decision_manager_output.schema.json": DECISION_OUTPUT_SCHEMA,
    }
    paths = []
    for name, schema in schemas.items():
        path = destination / name
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


if __name__ == "__main__":
    export(Path(__file__).resolve().parents[2])
