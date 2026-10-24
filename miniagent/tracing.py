"""A plain JSONL event sink; credentials are never passed to this sink."""

import json
from pathlib import Path


class JsonlTrace:
    def __init__(self, directory: Path = Path('runs')):
        self.directory = Path(directory)
        self.last_path: Path | None = None

    def __call__(self, event: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last_path = self.directory / f"{event['run_id']}.jsonl"
        with self.last_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
