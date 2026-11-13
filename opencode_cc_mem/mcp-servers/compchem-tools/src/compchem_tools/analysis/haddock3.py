"""HADDOCK3-specific run analysis: capri metrics plus interchain contact and
per-residue contribution analysis.

Kept in compchem-tools because it is domain analysis, not memory policy. The
memory promotion gate only reads the resulting run record.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any

CONTACT_THRESHOLD = 0.5  # ca-ca contact probability considered a real contact


def analyze(run_path: Path) -> dict[str, Any]:
    metrics = _capri_metrics(run_path)
    contacts = _contact_analysis(run_path)
    if contacts:
        metrics["contacts"] = contacts
    return {
        "metrics": metrics,
        "quality_flags": _quality_flags(metrics),
    }


def _capri_metrics(run_path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    capri_files = list(run_path.glob("output/*_caprieval/capri_clt.tsv"))
    if not capri_files:
        capri_files = list(run_path.glob("**/*_caprieval/capri_clt.tsv"))

    if capri_files:
        last_capri = sorted(capri_files)[-1]
        try:
            lines = last_capri.read_text().strip().split("\n")
            if len(lines) >= 2:
                header = lines[0].split("\t")
                best = lines[1].split("\t")
                for i, h in enumerate(header):
                    if i < len(best):
                        metrics[h.strip()] = best[i].strip()
        except Exception:
            pass

    clust_files = list(run_path.glob("output/*_clustfcc/clustfcc.txt"))
    if not clust_files:
        clust_files = list(run_path.glob("**/*_clustfcc/clustfcc.txt"))
    if clust_files:
        try:
            text = clust_files[-1].read_text()
            metrics["cluster_count"] = text.count("cluster")
        except Exception:
            pass
    return metrics


def _quality_flags(metrics: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    score = metrics.get("score", metrics.get("best_score"))
    if score is not None:
        try:
            if float(score) > 0:
                flags.append("positive_haddock_score")
        except (ValueError, TypeError):
            pass
    cluster_count = metrics.get("cluster_count")
    if cluster_count is not None and cluster_count == 0:
        flags.append("no_clusters")
    fnat = metrics.get("fnat")
    if fnat is not None:
        try:
            if float(fnat) < 0.1:
                flags.append("very_low_fnat")
        except (ValueError, TypeError):
            pass
    return flags


def _residue_key(key: str) -> str:
    """Normalize 'A-428-PHE-CD1' or 'A-80-ASP' to residue-level 'A-428-PHE'."""
    parts = key.split("-")
    return "-".join(parts[:3]) if len(parts) >= 3 else key


def _parse_residue(key: str) -> tuple[str, int | str, str]:
    """'A-80-ASP' -> (chain, residue_id, residue_name)."""
    parts = key.split("-")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return key, "", ""


def _read_contacts(path: Path) -> list[tuple[str, str, float]]:
    """Read one interchain_contacts.tsv into (res1, res2, ca_ca_probability)."""
    rows: list[tuple[str, str, float]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    header_seen = False
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if not header_seen:
            header_seen = True  # column header row
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        try:
            prob = float(fields[2])
        except ValueError:
            continue
        rows.append((fields[0], fields[1], prob))
    return rows


def _contact_analysis(run_path: Path) -> dict[str, Any]:
    files = sorted(run_path.glob("output/13_contactmap/cluster*_interchain_contacts.tsv"))
    if not files:
        files = sorted(run_path.glob("**/cluster*_interchain_contacts.tsv"))
    if not files:
        return {}

    clusters: dict[str, Any] = {}
    receptor_scores: dict[str, float] = defaultdict(float)
    ligand_scores: dict[str, float] = defaultdict(float)

    for path in files:
        cluster = path.name.split("_")[0]
        rows = _read_contacts(path)
        n_contacts = 0
        scored_pairs: list[tuple[float, str, str]] = []
        for res1, res2, prob in rows:
            res1 = _residue_key(res1)
            res2 = _residue_key(res2)
            chain1 = _parse_residue(res1)[0]
            chain2 = _parse_residue(res2)[0]
            if prob >= CONTACT_THRESHOLD:
                n_contacts += 1
            scored_pairs.append((prob, res1, res2))
            if chain1 == "A":
                receptor_scores[res1] += prob
                ligand_scores[res2] += prob
            elif chain2 == "A":
                receptor_scores[res2] += prob
                ligand_scores[res1] += prob
            else:
                # No chain A: keep both as ligand-side contributions.
                ligand_scores[res1] += prob
                ligand_scores[res2] += prob
        scored_pairs.sort(reverse=True)
        clusters[cluster] = {
            "n_contacts": n_contacts,
            "top_pairs": [
                {"res1": r1, "res2": r2, "probability": round(p, 3)}
                for p, r1, r2 in scored_pairs[:5]
            ],
        }

    def _hotspots(scores: dict[str, float], n: int = 10) -> list[dict[str, Any]]:
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:n]
        out = []
        for key, score in ranked:
            chain, resid, resname = _parse_residue(key)
            out.append({
                "residue": key,
                "chain": chain,
                "residue_id": resid,
                "residue_name": resname,
                "score": round(score, 3),
            })
        return out

    return {
        "clusters": clusters,
        "receptor_hotspots": _hotspots(receptor_scores),
        "ligand_hotspots": _hotspots(ligand_scores),
        "receptor_chain": "A",
        "contact_threshold": CONTACT_THRESHOLD,
    }
