"""Corpus loading and validation.

The corpus is FROZEN: every change is a version bump with a reason recorded
in corpus/manifest.yaml, and a corpus is never extended after arm results on
it were seen (Goodhart guard — add NEW sessions as a new version instead).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Slice:
    name: str
    project: str
    session: str
    capture_version: str
    chars: int
    text: str
    gold: dict = field(default_factory=dict)


@dataclass
class Corpus:
    path: Path
    manifest: dict
    slices: list


def load_corpus(corpus_dir: Path) -> Corpus:
    corpus_dir = Path(corpus_dir)
    manifest_path = corpus_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"corpus manifest missing: {manifest_path}")

    import yaml

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("frozen"):
        raise ValueError("corpus manifest must set frozen: true")
    changes = manifest.get("changes") or []
    if not changes or not changes[-1].get("reason"):
        raise ValueError("every corpus version needs a reason in manifest.yaml changes")

    gold_dir = corpus_dir / "extraction_gold"
    slices_manifest = json.loads((gold_dir / "slices_manifest.json").read_text(encoding="utf-8"))
    default_capture = manifest.get("capture_version", "unspecified")
    slices = []
    for entry in slices_manifest:
        text_path = gold_dir / "slices" / f"{entry['name']}.txt"
        if not text_path.exists() or text_path.stat().st_size == 0:
            raise FileNotFoundError(f"corpus slice missing or empty: {text_path}")
        text = text_path.read_text(encoding="utf-8")
        slices.append(
            Slice(
                name=entry["name"],
                project=entry.get("project", ""),
                session=entry.get("session", ""),
                capture_version=entry.get("capture_version", default_capture),
                chars=entry.get("chars", len(text)),
                text=text,
                gold=entry.get("gold", {}),
            )
        )
    if not slices:
        raise ValueError("corpus has no slices")
    return Corpus(path=corpus_dir, manifest=manifest, slices=slices)


def load_incidents(corpus_dir: Path) -> list:
    """Incident fixtures (smoke-detector Phase 0 replay set). Each incident is
    a markdown file with Symptom / Expected-detection sections."""
    incidents = []
    for p in sorted((Path(corpus_dir) / "incidents").glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        incidents.append({"name": p.stem, "text": p.read_text(encoding="utf-8")})
    return incidents


def load_retrieval_pairs(corpus_dir: Path) -> list:
    """Query -> expected-memories pairs for the retrieval eval (recall@k, MRR).

    Each line of retrieval/pairs.jsonl:
      {"id": "...", "query": "...", "expected": ["<memory entry id or title>", ...]}
    """
    pairs_path = Path(corpus_dir) / "retrieval" / "pairs.jsonl"
    if not pairs_path.exists():
        return []
    pairs = []
    for line in pairs_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rec = json.loads(line)
        for req in ("id", "query", "expected"):
            if req not in rec:
                raise ValueError(f"{pairs_path}: record missing '{req}': {rec}")
        pairs.append(rec)
    return pairs
