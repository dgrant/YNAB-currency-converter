import json
import os
import tempfile
import uuid
from pathlib import Path


class ConversionStore:
    """Persists the list of configured conversions as a single JSON file."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "conversions.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path) as f:
            return json.load(f)

    def get(self, conversion_id: str) -> dict | None:
        for conversion in self.load():
            if conversion["id"] == conversion_id:
                return conversion
        return None

    def add(self, conversion: dict) -> dict:
        conversion = {"id": uuid.uuid4().hex[:8], **conversion}
        conversions = self.load()
        conversions.append(conversion)
        self._save(conversions)
        return conversion

    def update(self, conversion_id: str, fields: dict) -> dict | None:
        """Merge fields into an existing conversion; None if it doesn't exist."""
        conversions = self.load()
        for i, existing in enumerate(conversions):
            if existing["id"] == conversion_id:
                updated = {**existing, **fields, "id": conversion_id}
                conversions[i] = updated
                self._save(conversions)
                return updated
        return None

    def delete(self, conversion_id: str) -> None:
        conversions = [c for c in self.load() if c["id"] != conversion_id]
        self._save(conversions)

    def _save(self, conversions: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(conversions, f, indent=2)
            os.replace(tmp_path, self.path)
        except BaseException:
            os.unlink(tmp_path)
            raise
