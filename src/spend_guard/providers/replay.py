"""Record live provider calls once; replay them offline (CI) through the real ledger.

A cassette is JSONL: a `{"meta": ...}` line, then one line per call keyed by a hash of
(model, system, prompt, max_output_tokens). Replay is strict: an unseen request raises, it never
falls back to a live call.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from spend_guard.providers.base import Provider, ProviderFailed, ProviderResult


def request_hash(model: str, system: str, prompt: str, max_output_tokens: int) -> str:
    blob = json.dumps([model, system, prompt, max_output_tokens], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def load(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    meta: dict[str, Any] = {}
    rows: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if "meta" in row:
            meta = row["meta"]
        else:
            rows[row["request_hash"]] = row
    return meta, rows


class ReplayProvider:
    def __init__(self, path: Path) -> None:
        self.meta, self.rows = load(path)
        self.name = f"replay:{self.meta.get('provider', '?')}"
        self.misses: list[str] = []

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        h = request_hash(model, system, prompt, max_output_tokens)
        row = self.rows.get(h)
        if row is None:
            self.misses.append(prompt[:60])
            raise ProviderFailed("no recorded response for this request; re-record the cassette")
        r = row["result"]
        return ProviderResult(r["text"], int(r["input_tokens"]), int(r["output_tokens"]))


class RecordingProvider:
    def __init__(self, live: Provider, path: Path, meta: dict[str, Any]) -> None:
        self.live = live
        self.name = live.name
        self.path = Path(path)
        self.meta = {"provider": live.name, **meta}
        self.rows: dict[str, dict[str, Any]] = {}

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        result = self.live.complete(model, system, prompt, max_output_tokens)
        h = request_hash(model, system, prompt, max_output_tokens)
        self.rows[h] = {
            "request_hash": h,
            "model": model,
            "prompt": prompt,
            "max_output_tokens": max_output_tokens,
            "result": {
                "text": result.text,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        }
        return result

    def save(self) -> None:
        lines = [json.dumps({"meta": self.meta}, sort_keys=True)]
        lines += [
            json.dumps(r, sort_keys=True, ensure_ascii=False)
            for r in sorted(self.rows.values(), key=lambda r: r["prompt"])
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
