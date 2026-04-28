"""Add tracklist data to existing data/weeks/*.json without a full re-fetch.

Only Metal Archives album pages give us a structured tracklist; non-MA-sourced
records get an empty list.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_releases import fetch_ma_tracklist  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    weeks_dir = DATA_DIR / "weeks"
    for path in sorted(weeks_dir.glob("*.json")):
        with path.open() as f:
            data = json.load(f)
        added = 0
        for r in data.get("releases", []):
            if r.get("tracklist"):
                continue
            url = r.get("source_url", "")
            if url.startswith("https://www.metal-archives.com/"):
                r["tracklist"] = fetch_ma_tracklist(url)
                if r["tracklist"]:
                    added += 1
            else:
                r["tracklist"] = []
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"{path.name}: added tracklists to {added} releases", file=sys.stderr)


if __name__ == "__main__":
    main()
