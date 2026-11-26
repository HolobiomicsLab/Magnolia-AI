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


def _norm_decimal(value: str) -> str:
    """Canonical form for comparison only; 0.10 and 0.1 are one number."""
    try:
        return format(float(value), ".12g")
    except ValueError:
        return value
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
    involving_tier: str | None = None,
) -> list[dict[str, Any]]:
    """Pairwise conflict-candidate scan over loaded entries.

    Returns candidate pairs (highest discriminative overlap first), capped at
    ``max_pairs``. With ``involving=<filename>`` only pairs containing that
    entry are kept (filter applied BEFORE the cap, so crowd-out cannot hide
    the requested side); ``involving_tier`` disambiguates a filename that
    exists in both tiers. Each pair dict carries both entries'
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
            # Parameter-context test: for a shared parameter term, the decimals
            # that BELONG to it must differ between the two entries. Values are
            # bound by syntax first (w_air = 0.1, 0.1*w_air); otherwise the
            # token's own clause supplies the candidates. Same parameter with
            # different values, not the same values with different example
            # scores (second live-sweep lesson).
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
        def _matches(side: dict[str, Any]) -> bool:
            return (side["filename"] == involving
                    and (involving_tier is None or side["tier"] == involving_tier))
        pairs = [p for p in pairs if _matches(p["a"]) or _matches(p["b"])]
    return pairs[:max_pairs]


_CLAUSE_RE = re.compile(r"\.(?!\d)\s|\n|[|;]")


def _clauses(text: str) -> list[str]:
    """Sentence- and table-cell-sized chunks.

    A period followed by a digit is a decimal point, not a sentence end, so
    "0.1. Next sentence" splits while "0.1" does not."""
    return _CLAUSE_RE.split(text)


def _is_filename_only(text: str, token: str) -> bool:
    """True when every occurrence of ``token`` is a path or filename component.

    ``weights_params`` inside "weights_params.json" names a file, not a
    parameter; comparing the decimals near it invents conflicts out of
    unrelated scores. One bare prose occurrence is enough to keep the token."""
    hits = list(re.finditer(re.escape(token), text))
    if not hits:
        return False
    for m in hits:
        if re.match(r"\.[a-zA-Z0-9]+", text[m.end():]):
            continue  # token.ext
        if m.start() > 0 and text[m.start() - 1] == "/":
            continue  # path component
        return False
    return True


def _bound_values(text: str, token: str) -> set[str]:
    """Decimals attached to the token by syntax: ``name = 0.1``, ``name: 0.1``,
    ``0.1 * name``. Only here does a value provably belong to the parameter;
    anything else is a guess about what a nearby number refers to."""
    tok = re.escape(token)
    vals: set[str] = set()
    for m in re.finditer(rf"(?<![\w]){tok}\s*(?:=|:|≈)\s*(\d+\.\d+)", text):
        vals.add(m.group(1))
    for m in re.finditer(rf"(\d+\.\d+)\s*[*×]\s*{tok}(?![\w])", text):
        vals.add(m.group(1))
    return vals


def _clause_values(text: str, token: str) -> set[str]:
    """Fallback when the token is never bound: decimals in the first clause
    that contains the token and at least one decimal.

    Clause-scoped, not character-windowed: a table row on the previous line
    cannot leak into a parameter mentioned in the next sentence. (The old
    +-60-character window did exactly that, which is how a cluster score and
    an iRMSD were once reported as two conflicting values of w_air.)"""
    for cl in _clauses(text):
        if not re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", cl):
            continue
        vals = _DECIMAL_RE.findall(cl)
        if vals:
            return set(vals)
    return set()


def _param_conflicts(text_a: str, text_b: str, signal: set[str]) -> list[dict[str, Any]]:
    out = []
    for t in sorted(signal):
        if _is_filename_only(text_a, t) and _is_filename_only(text_b, t):
            continue  # identifier, not a parameter
        ba = _bound_values(text_a, t)
        bb = _bound_values(text_b, t)
        a_vals = ba or _clause_values(text_a, t)
        b_vals = bb or _clause_values(text_b, t)
        if not a_vals or not b_vals:
            continue
        # Compare numerically: 0.10 and 0.1 are the same value. The raw
        # strings stay in a_values/b_values for the reviewer.
        na = {_norm_decimal(v) for v in a_vals}
        nb = {_norm_decimal(v) for v in b_vals}
        if na == nb:
            continue
        out.append({
            "param": t,
            "a_values": sorted(a_vals)[:6],
            "b_values": sorted(b_vals)[:6],
            # "explicit": both sides bind a number to the name by syntax AND the
            # value sets are disjoint. That disagreement is provable without any
            # model, so an adjudicator must never clear it. When the sets share
            # a value the disagreement is not proven (one entry may be listing
            # per-stage values) and it stays adjudicable as "proximity".
            "binding": "explicit" if (ba and bb and not (na & nb)) else "proximity",
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
        [source, *targets], max_pairs=len(targets),
        involving=source.get("filename", ""),
        involving_tier=source.get("tier", ""))


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
