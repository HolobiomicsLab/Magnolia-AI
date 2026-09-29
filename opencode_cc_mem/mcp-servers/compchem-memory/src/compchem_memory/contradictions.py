"""Claim-conflict detection between memory entries.

Two entries are CONFLICT CANDIDATES when they are about the same specific
topic AND state different numbers. The motivating case (found 2026-09 in a
production store): one project entry correctly says w_air = 0.01 for
rigidbody/it0 and 0.1 for flexref/emref/mdref, a later entry overgeneralizes
"w_air = 0.1 across all runs" — nothing flagged the disagreement and a
downstream replay test was misled by the wrong entry.

This module is deliberately deterministic and LLM-free: it finds CANDIDATE
pairs for a human (or a later LLM pass) to judge. Lexical similarity alone
cannot tell a contradiction from a scope difference — the motivating pair
shares ZERO significant title words ("scoring weights" vs "AIR-free
computation"), so matching must use title + tags + body tokens, and must
ignore tokens that are common across the project's corpus (every docking
entry mentions haddock3/peptide/docking).

A pair qualifies when ALL hold:
  1. at least 2 shared "discriminating" tokens — tokens appearing in at most
     a quarter of the scanned corpus (min floor 2 so tiny corpora still work);
  2. at least one of them is a letter-only compound token (contains ``_``,
     no digits) — parameter / file names like ``w_air``, ``weights_params``.
     Generic prose passes any rarity filter on a diverse corpus; compound
     parameter names are where quantitative claims live;
  3. both entries contain decimal numbers (regex ``\\d+\\.\\d+``);
  4. for at least one shared parameter term, the decimals appearing NEAR
     that term differ between the two entries — the same parameter with
     different values, not the same values with different example scores.

Pairs are ranked by the number of conflicting parameter terms first.
"""

import re
from pathlib import Path
from typing import Any

# Mirrors ProjectManager._STOPWORDS (tiers/project.py) — duplicated here so
# this module stays a leaf with no internal imports (project.py imports FROM
# this module; the reverse would be circular).
_STOPWORDS = frozenset({
    "a", "an", "the", "to", "of", "for", "and", "or", "is", "are", "be",
    "in", "on", "at", "by", "with", "from", "into", "as", "it", "its",
    "this", "that", "than", "then", "when", "not", "no", "same", "before",
    "after", "always", "must", "should", "via", "per", "but",
})

_DECIMAL_RE = re.compile(r"\d+\.\d+")
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


class ConflictCandidatesError(Exception):
    """Raised when a staging promotion would land next to a conflicting claim.

    Carries ``candidates``: the list of candidate-pair dicts (see
    ``find_conflict_candidates``) so the caller can show what conflicts.
    """

    def __init__(self, message: str, candidates: list[dict[str, Any]]):
        super().__init__(message)
        self.candidates = candidates


def significant_words(text: str) -> set[str]:
    """Topic-bearing tokens of any text: lowercase, stopwords, single
    characters, bare numbers and digit-initial tokens removed.

    Digit-initial tokens (``16_suite_peptide_s9000_full``, ``20260101_000000``,
    ``1abc``) are run-directory names, dates, entry ids and PDB codes quoted
    from provenance — on real corpora they are rare enough to pass the
    rarity filter yet carry no contradiction semantics, and they flood the
    candidate list (found in the first live sweep over a production store,
    2026-09)."""
    return {
        w
        for w in _TOKEN_RE.findall(text.lower())
        if w not in _STOPWORDS and len(w) > 1 and not w.isdigit() and not w[0].isdigit()
    }


def _entry_tokens(entry: dict[str, Any]) -> set[str]:
    parts = [entry.get("title", ""), " ".join(entry.get("tags", []) or []),
             entry.get("text", "")]
    return significant_words(" ".join(parts))


def _entry_numbers(entry: dict[str, Any]) -> set[str]:
    return set(_DECIMAL_RE.findall(entry.get("text", "")))


def load_entries(entries_dir: Path, staging_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load entries (and optionally staging entries) into plain dicts.

    Each dict: filename, tier ("project"/"staging"), id, title, tags, text.
    Files without a parsable title AND body are skipped. INDEX.md ignored.
    """
    out: list[dict[str, Any]] = []
    dirs = [(Path(entries_dir), "project")]
    if staging_dir is not None:
        dirs.append((Path(staging_dir), "staging"))
    for d, tier in dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "INDEX.md":
                continue
            entry = load_entry_file(f, tier)
            if entry is not None:
                out.append(entry)
    return out


def load_entry_file(path: Path, tier: str) -> dict[str, Any] | None:
    """Load one entry markdown file into the plain dict ``load_entries`` returns."""
    text = path.read_text()
    meta = _parse_frontmatter(text)
    title = meta.get("title", "")
    body = _body_after_frontmatter(text)
    if not title and not body.strip():
        return None
    return {
        "filename": path.name,
        "tier": tier,
        "id": str(meta.get("id", path.stem)),
        "title": title,
        "tags": list(meta.get("tags", []) or []),
        "text": f"{title}\n{body}",
    }


def find_conflict_candidates(
    entries: list[dict[str, Any]],
    max_pairs: int = 20,
    involving: str | None = None,
) -> list[dict[str, Any]]:
    """Pairwise conflict-candidate scan over loaded entries.

    Returns candidate pairs (highest discriminative overlap first), capped at
    ``max_pairs``. With ``involving=<filename>`` only pairs containing that
    entry are kept (filter applied BEFORE the cap, so crowd-out cannot hide
    the requested side). Each pair dict carries both entries'
    filename/tier/id/title, the shared discriminating tokens, and the decimal
    numbers unique to each side — the evidence a reviewer needs to judge the
    pair.
    """
    if len(entries) < 2:
        return []
    tokens = [_entry_tokens(e) for e in entries]
    numbers = [_entry_numbers(e) for e in entries]
    texts = [e.get("text", "") for e in entries]

    n = len(entries)
    df: dict[str, int] = {}
    for toks in tokens:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    df_cap = max(2, n // 4)

    pairs: list[dict[str, Any]] = []
    for i in range(n):
        for j in range(i + 1, n):
            if entries[i].get("tier") == "staging" and entries[j].get("tier") == "staging":
                continue  # staging-vs-staging is consolidation's job
            shared = tokens[i] & tokens[j]
            if len(shared) < 2:
                continue
            discriminating = {t for t in shared if df.get(t, 0) <= df_cap}
            if len(discriminating) < 2:
                continue
            # Signal tokens: letter-only compound tokens (parameter / file
            # names like w_air, weights_params). Prose words pass any rarity
            # filter on a diverse corpus — the first live sweep (a production
            # store, 482 entries) returned 252 shared "rare" tokens of generic
            # English for one pair while the real w_air pair had 9 and was
            # crowded out. Compounds with digits (peptide_s9000) are run-dir
            # fragments, not parameter names.
            signal = {
                t for t in discriminating
                if "_" in t and not any(c.isdigit() for c in t)
            }
            if not signal:
                continue
            nums_i, nums_j = numbers[i], numbers[j]
            if not nums_i or not nums_j or nums_i == nums_j:
                continue
            # Parameter-context test: for a shared parameter term, the
            # decimals appearing NEAR it must differ between the two entries.
            # Second live-sweep lesson (production store): entries that merely cite
            # the same weight vector with different example scores shared
            # w_air/w_vdw/... too; only the value IN CONTEXT distinguishes a
            # contradiction (w_air: {0.01, 0.1} vs {0.1}) from agreement.
            conflicts = _param_conflicts(texts[i], texts[j], signal)
            if not conflicts:
                continue
            pairs.append({
                "a": _side(entries[i]),
                "b": _side(entries[j]),
                "param_conflicts": conflicts,
                "signal_terms": sorted(signal)[:8],
                "overlap": len(discriminating),
                "signal_count": len(signal),
            })

    pairs.sort(
        key=lambda p: (-len(p["param_conflicts"]), -p["signal_count"],
                       -p["overlap"], p["a"]["filename"])
    )
    if involving is not None:
        pairs = [
            p for p in pairs
            if involving in (p["a"]["filename"], p["b"]["filename"])
        ]
    return pairs[:max_pairs]


_CONTEXT_WINDOW = 60


def _param_context_numbers(text: str, token: str) -> set[str]:
    """Decimals appearing within +-60 chars of any occurrence of ``token``."""
    vals: set[str] = set()
    for m in re.finditer(re.escape(token), text):
        lo, hi = max(0, m.start() - _CONTEXT_WINDOW), m.end() + _CONTEXT_WINDOW
        vals.update(_DECIMAL_RE.findall(text[lo:hi]))
    return vals


def _param_conflicts(text_a: str, text_b: str, signal: set[str]) -> list[dict[str, Any]]:
    out = []
    for t in sorted(signal):
        a_vals = _param_context_numbers(text_a, t)
        b_vals = _param_context_numbers(text_b, t)
        if a_vals and b_vals and a_vals != b_vals:
            out.append({
                "param": t,
                "a_values": sorted(a_vals)[:6],
                "b_values": sorted(b_vals)[:6],
            })
    return out


def candidates_against_entry(
    source: dict[str, Any],
    targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Conflict candidates between one incoming entry and existing entries.

    Corpus statistics (document frequency) are computed over targets plus the
    source, so rarity is judged against the project's memory as the incoming
    entry would join it. No cap — the gate shows everything it found.
    """
    return find_conflict_candidates(
        [source, *targets], max_pairs=len(targets), involving=source.get("filename", "")
    )


def _side(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": entry.get("filename", ""),
        "tier": entry.get("tier", "project"),
        "id": entry.get("id", ""),
        "title": entry.get("title", ""),
    }


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    try:
        import yaml

        return yaml.safe_load(text[3:end].strip()) or {}
    except Exception:
        return {}


def _body_after_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return ""
    return text[end + 3:]
