"""Distiller admission gate — R1–R4 of the distill-admission plan (2026-09-30).

Sits between extraction and staging-save. Cheap-local stages first:

  Stage 0 (R3, free): idle gate — a session whose transcript shows no error,
  parameter-change, or novel-reference signal never reaches an LLM.
  Stage 1 (R4, free): duplicate kill — a candidate whose title matches a
  recently-admitted title (token conjunction >= 0.6) is rejected before the
  pool forms.
  Stage 2 (R1/R2, one LLM call per slice's survivors): the admission judge.
  R1 admits only checkable operational content (parameter value+replaces+why,
  error symptoms+fix, failure modes, durable target references). R2 rejects
  session narration, workflow logs, success announcements, and — in
  OPERATIONS stores (the default profile) — one-off findings. Stores flagged
  `findings_keep` (e.g. communication) keep findings.

Every decision appends to <store>/admission-log.jsonl (audit trail). The
admitted-title ledger for R4 lives in <store>/admission-ledger.jsonl.
Provenance (R1 clause): admitted candidates are stamped with the opencode
session id by staging_io.save_candidate.

Failure model: if the judge LLM call fails, the gate FAILS OPEN (admits,
reason='judge_unavailable') — a dead judge must not silently drop a session's
whole output; the volume guard degrades to baseline, never to zero.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_WORD_RE = re.compile(r"[a-z0-9]+")

# R3 idle-gate signal patterns: a session is worth distilling iff it shows at
# least one error / parameter-change / novel-reference marker.
_IDLE_SIGNAL_RES: list[tuple[str, re.Pattern[str]]] = [
    ("error", re.compile(
        r"\b(error|failed|failure|traceback|exception|crash|oom|killed|"
        r"exit code|nonzero|fatal)\b", re.I)),
    ("parameter", re.compile(
        r"\b(parameter|flag|option|setting|sampling|ncores|walltime|max_tokens|"
        r"exhaustiveness|restraint)s?\b|--[a-z][a-z0-9-]{2,}", re.I)),
    ("reference", re.compile(
        r"\b(arxiv|doi|10\.\d{4,}|pdb [0-9a-z]{4}|commit [0-9a-f]{7,}|"
        r"https?://|residues? \d+|[A-Z]\d{2,3}\b)", re.I)),
]

PROFILES = ("operations", "findings_keep")
RECENT_WINDOW = 200

ADMISSION_JUDGE_PROMPT = """You are the ADMISSION GATE for a memory system's distillation output.
Candidates below were extracted from ONE session of a computational-chemistry
agent. Decide for EACH whether it deserves a permanent slot in the project's
memory. Volume is the enemy: the store already holds hundreds of entries that
are never retrieved; admit only what a future session will actually need.

ADMIT (checkable operational content — R1):
- parameter guidance: a concrete value choice with what it replaces and why
- error resolution: symptoms + cause + fix
- failure pattern: a confirmed approach that does NOT work
- durable target reference: stable facts about the system under study
  (residues, scores, binding modes) with provenance to named runs/analyses
Every admitted claim must be anchored (numbers, residues, file names, run
names) — a claim with no anchor cannot be re-verified and must be rejected
with reason "unanchored".

REJECT (R2):
- session narration ("we ran X then Y"), workflow logs, status updates
- success announcements without a transferable lesson
- one-off findings in OPERATIONS-profile stores (profile: {profile})
- anything about the memory system's own plumbing (consolidation, staging,
  distillation mechanics) rather than the science or the tools

Recently admitted titles (do not admit a near-duplicate of these):
{recent}

Return ONLY a JSON array: [{{"title": <as given>, "decision": "admit"|"reject",
"reason": "<=12 words", "class": "parameter|error|failure|reference|finding|narration"}}]"""


def _tokens(title: str) -> set[str]:
    toks = set(_WORD_RE.findall((title or "").lower()))
    return {t for t in toks if len(t) >= 3 and not t.isdigit()}


def is_same_claim(a: str, b: str, threshold: float = 0.6) -> bool:
    """Title-token conjunction, mirroring runner.is_duplicate / second-pass.

    v6 guard: the shared set must contain at least one CONTENT-BEARING token
    (>= 5 chars). Shared short/generic tokens ("job", "run", "oom") or shared
    identifiers alone do not make two titles the same claim — the bake-off
    diagnostic showed workhorses killed on identifier-led conjunctions
    ("job 614716" pairing unrelated lessons).
    """
    ta = _tokens(a)
    if not ta:
        return False
    tb = _tokens(b)
    if not tb:
        return False
    shared = ta & tb
    if len(shared) < 2 or len(shared) / min(len(ta), len(tb)) < threshold:
        return False
    return any(len(t) >= 5 for t in shared)


def session_has_signal(transcript: str) -> tuple[bool, list[str]]:
    """R3 idle gate: (worth_distilling, matched_signal_kinds)."""
    kinds = [name for name, rx in _IDLE_SIGNAL_RES if rx.search(transcript or "")]
    return (bool(kinds), kinds)


def admission_enabled() -> bool:
    return str(os.environ.get("MAGNOLIA_ADMISSION_GATE", "")).lower() in (
        "1", "true", "yes", "on")


def load_profile(store: Path | str) -> str:
    p = Path(store) / ".admission-profile"
    try:
        val = p.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "operations"
    return val if val in PROFILES else "operations"


def recent_admitted_titles(store: Path | str, window: int = RECENT_WINDOW) -> list[str]:
    ledger = Path(store) / "admission-ledger.jsonl"
    titles: list[str] = []
    try:
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("decision") == "admit":
                titles.append(row.get("title", ""))
    except OSError:
        pass
    return [t for t in titles[-window:] if t]


@dataclass
class AdmissionResult:
    admitted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # {candidate, stage, reason}
    session: str | None = None
    idle_skipped: bool = False
    judge_available: bool = True


class AdmissionGate:
    """Stateful gate for one store. The harness points it at a per-run store
    (out_dir); production points it at the project's .magnolia store."""

    def __init__(
        self,
        store: Path | str,
        *,
        profile: str | None = None,
        llm_json: Callable[..., Any] | None = None,
        idle_gate: bool = True,
    ):
        from pathlib import Path as _P
        self.store = _P(store)
        self.store.mkdir(parents=True, exist_ok=True)
        self.profile = profile or load_profile(self.store)
        self.idle_gate = idle_gate
        if llm_json is None:
            from compchem_memory.llm import call_llm_json as _cl
            llm_json = _cl
        self._llm_json = llm_json

    # ── stage 2 ────────────────────────────────────────────────────────────
    def _judge(self, candidates: list[dict], recent: list[str]) -> list[dict] | None:
        prompt = ADMISSION_JUDGE_PROMPT.format(
            profile=self.profile,
            recent="\n".join(f"- {t}" for t in recent) or "- (none)",
        )
        payload = json.dumps(
            [{"type": c.get("type"), "title": c.get("title"),
              "content": (c.get("content") or "")[:600]}
             for c in candidates],
            ensure_ascii=False, indent=1,
        )
        try:
            result = self._llm_json(prompt, payload, max_tokens=4000,
                                    disable_thinking=True)
        except Exception:  # noqa: BLE001 - judge failure = fail-open, logged
            return None
        if not isinstance(result, list):
            return None
        out = []
        for r in result:
            if isinstance(r, dict) and "title" in r:
                out.append(r)
        return out or None

    def admit(
        self,
        candidates: list[dict],
        *,
        transcript: str | None = None,
        session: str | None = None,
    ) -> AdmissionResult:
        res = AdmissionResult(session=session)
        now = datetime.now(timezone.utc).isoformat()

        # Stage 0 — R3 idle gate
        if self.idle_gate and transcript is not None:
            worth, kinds = session_has_signal(transcript)
            if not worth:
                res.idle_skipped = True
                self._log(now, session, None, "reject", f"idle_no_signal")
                return res

        # Stage 1 — R4 duplicate kill against recently admitted titles
        recent = recent_admitted_titles(self.store)
        pending: list[dict] = []
        for c in candidates or []:
            title = c.get("title", "")
            dup = next((t for t in recent if is_same_claim(title, t)), None)
            if dup:
                res.rejected.append(
                    {"candidate": c, "stage": "r4_dup", "reason": f"duplicate of: {dup}"})
                self._log(now, session, title, "reject", "r4_duplicate")
            else:
                pending.append(c)
        if not pending:
            return res

        # Stage 2 — R1/R2 judge (one batched call)
        verdicts = self._judge(pending, recent)
        by_title: dict[str, dict] = {}
        if verdicts is not None:
            res.judge_available = True
            for v in verdicts:
                by_title.setdefault(str(v.get("title", "")), v)
        else:
            res.judge_available = False

        for c in pending:
            title = c.get("title", "")
            if verdicts is None:
                res.admitted.append(c)  # fail-open
                self._ledger(now, session, title, "admit", "judge_unavailable_fail_open")
                continue
            v = by_title.get(title)
            if v and str(v.get("decision", "")).lower() == "admit":
                res.admitted.append(c)
                self._ledger(now, session, title, "admit",
                             f"class={v.get('class', '?')}: {v.get('reason', '')}")
            else:
                reason = (v or {}).get("reason", "judge_rejected")
                res.rejected.append({"candidate": c, "stage": "judge", "reason": reason})
                self._log(now, session, title, "reject", str(reason))
        return res

    # ── audit ──────────────────────────────────────────────────────────────
    def _log(self, now, session, title, decision, reason) -> None:
        self._append("admission-log.jsonl",
                     {"ts": now, "session": session, "title": title,
                      "decision": decision, "reason": reason})

    def _ledger(self, now, session, title, decision, reason) -> None:
        self._append("admission-ledger.jsonl",
                     {"ts": now, "session": session, "title": title,
                      "decision": decision, "reason": reason})
        self._log(now, session, title, decision, reason)

    def _append(self, name: str, row: dict) -> None:
        try:
            with open(self.store / name, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
