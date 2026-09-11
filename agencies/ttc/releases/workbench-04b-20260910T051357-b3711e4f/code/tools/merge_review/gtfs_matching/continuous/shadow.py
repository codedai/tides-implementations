"""Evaluate saved old/new episode scores without rerunning ingestion or matching.

Inputs are the append-only shadow/{mode}-episodes.json saved with Experiment04.
Independent labels are optional. Missing labels yield descriptive statistics only.
"""

import argparse
import json
from pathlib import Path

from .evaluation import evaluate_shadow, load_verified_labels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", required=True, type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    episodes = json.loads(args.episodes.read_text())
    labels = load_verified_labels(args.labels) if args.labels else None
    report = evaluate_shadow(episodes["old"], episodes["new"], labels)
    # Never silently overwrite a previous audit result or label source.
    with args.output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(args.output)


if __name__ == "__main__":
    main()
