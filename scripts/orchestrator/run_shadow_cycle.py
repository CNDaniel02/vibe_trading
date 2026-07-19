from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.agents.api_investment_team import ApiInvestmentTeam
from scripts.core.audit import append_jsonl
from scripts.core.config import assert_paper_mode, load_runtime_config
from scripts.llm import build_provider


def run_shadow_cycle(root: str | Path, snapshot: dict) -> dict:
    root = Path(root)
    config = load_runtime_config(root)
    assert_paper_mode(config)
    provider, tracker = build_provider(config["llm"], root)
    result = ApiInvestmentTeam(root, config, provider, tracker).run(snapshot).to_dict()
    append_jsonl(root, "shadow_decisions.jsonl", result)
    return {"shadow_decision": result, "usage": tracker.summary()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--snapshot-json", required=True)
    args = parser.parse_args()
    snapshot = json.loads(Path(args.snapshot_json).read_text(encoding="utf-8"))
    print(json.dumps(run_shadow_cycle(args.root, snapshot), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
