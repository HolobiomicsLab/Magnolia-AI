"""Deterministic claim verifier — receipts are claims; this checks them.

Given a textual claim with quantitative assertions ("w_air was -0.42 in the
flexref stage", "best score -108.2, n=4 clusters"), verify each labeled
number against evidence gathered from run artifacts (the run record's
metrics block, supplied evidence dicts). Purely deterministic — no LLM in
the loop, so the check cannot be talked out of a contradiction.

Fail-closed (verification-regime rule):
  * a labeled number with NO matching evidence key  -> unverified
  * a labeled number whose evidence disagrees       -> contradicted
  * text with no checkable quantitative claims      -> no_quantitative_claims
  (never "verified" — a vacuous pass would launder unverifiable prose)

The verdict names the offending numbers, so a planted wrong number trips
loudly rather than diffusely.
"""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

# label = number with '=' or ':' separator; label is an identifier path
# (w_air, capri.weight, stages.flexref.w_air, ...).
_CLAIM_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.]*)\s*[=:]\s*"
    r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def verify_claim(
    claim: str,
    evidence: dict[str, Any] | None = None,
    *,
    rel_tol: float = 0.01,
    abs_tol: float = 1e-3,
) -> dict[str, Any]:
    """Verify every labeled number in ``claim`` against ``evidence``.

    Returns a verdict dict:
      status: verified | contradicted | unverified | no_quantitative_claims
      checks: per-claim detail (label, claimed, observed, status, evidence_key)
    """
    claims = [
        (m.group(1), float(m.group(2))) for m in _CLAIM_RE.finditer(claim)
    ]
    unlabeled = len(_NUMBER_RE.findall(claim)) - len(claims)
    if not claims:
        return {
            "status": "no_quantitative_claims",
            "checks": [],
            "unlabeled_numbers_ignored": max(unlabeled, 0),
        }

    index = _flatten_evidence(evidence or {})
    checks = []
    for label, value in claims:
        candidates = _lookup(index, label)
        if not candidates:
            checks.append({
                "label": label, "claimed": value, "observed": None,
                "status": "unverified", "evidence_key": None,
                "reason": "no evidence key matches this label",
            })
            continue
        key, observed = min(
            candidates,
            key=lambda kv: abs(kv[1] - value),
        )
        if abs(observed - value) <= max(abs_tol, rel_tol * abs(value)):
            checks.append({
                "label": label, "claimed": value, "observed": observed,
                "status": "verified", "evidence_key": key,
            })
        else:
            checks.append({
                "label": label, "claimed": value, "observed": observed,
                "status": "contradicted", "evidence_key": key,
                "reason": "closest evidence value disagrees beyond tolerance",
            })

    if any(c["status"] == "contradicted" for c in checks):
        status = "contradicted"
    elif any(c["status"] == "unverified" for c in checks):
        status = "unverified"
    else:
        status = "verified"
    return {
        "status": status,
        "checks": checks,
        "unlabeled_numbers_ignored": max(unlabeled, 0),
    }


def _flatten_evidence(evidence: dict[str, Any], prefix: str = "") -> dict[str, list[float]]:
    """Flatten nested dicts into {dotted.key.path: [numbers found]}."""
    index: dict[str, list[float]] = {}
    for key, value in evidence.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            for k, nums in _flatten_evidence(value, path).items():
                index.setdefault(k, []).extend(nums)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            index.setdefault(path, []).append(float(value))
        elif isinstance(value, str):
            nums = [float(n) for n in _NUMBER_RE.findall(value)]
            if nums:
                index.setdefault(path, []).extend(nums)
    return index


def _lookup(index: dict[str, list[float]], label: str) -> list[tuple[str, float]]:
    """Evidence keys matching a claim label: exact, or path-suffixed
    (claim 'w_air' matches 'metrics.w_air' and 'stages.flexref.w_air')."""
    label = label.lower()
    out: list[tuple[str, float]] = []
    for key, nums in index.items():
        kl = key.lower()
        if kl == label or kl.endswith("." + label):
            for n in nums:
                out.append((key, n))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compchem-memory verify-claim",
        description="Verify the labeled numbers in a claim against a JSON "
        "evidence dict (e.g. a run record).",
    )
    parser.add_argument("claim")
    parser.add_argument("--evidence-json", default="{}",
                        help="JSON object used as evidence")
    args = parser.parse_args(argv)
    evidence = json.loads(args.evidence_json)
    print(json.dumps(verify_claim(args.claim, evidence), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
