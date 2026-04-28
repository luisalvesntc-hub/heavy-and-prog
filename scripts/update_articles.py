"""Augment existing data/weeks/*.json with journalism articles.

Re-runs the journalism-pool fetch + per-release matching without the slow MA/MB/Wiki
discovery pass. Use after changing the article-matching logic.
"""

import json
import sys
from pathlib import Path

# Reuse functions from fetch_releases.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_releases import fetch_journalism_pool, match_articles  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    pool = fetch_journalism_pool()
    weeks_dir = DATA_DIR / "weeks"
    for path in sorted(weeks_dir.glob("*.json")):
        with path.open() as f:
            data = json.load(f)
        for r in data.get("releases", []):
            r["articles"] = match_articles(r, pool)
        matched = sum(1 for r in data["releases"] if r.get("articles"))
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"{path.name}: matched articles to {matched}/{len(data['releases'])} releases",
              file=sys.stderr)


if __name__ == "__main__":
    main()
