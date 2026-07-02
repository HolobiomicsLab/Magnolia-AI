"""Health check: audit knowledge base for staleness, gaps, and inconsistencies."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def run_health_check(
    project_dir: str,
    stale_days: int = 90,
    min_confidence: float = 0.3,
    fix: bool = False,
) -> dict[str, Any]:
    """Audit project-tier knowledge base for issues.

    Returns dict with: status, checks_run, issues_found, issues_fixed,
    report_markdown, details.
    """
    entries_dir = Path(project_dir) / ".magnolia" / "entries"
    staging_dir = Path(project_dir) / ".magnolia" / "staging"

    all_issues: list[dict] = []
    fixes_applied = 0

    # 1. Stale entries
    stale = _check_stale_entries(entries_dir, stale_days)
    for s in stale:
        all_issues.append({"check": "stale", **s})

    # 2. Low confidence
    low_conf = _check_low_confidence(entries_dir, min_confidence)
    for lc in low_conf:
        all_issues.append({"check": "low_confidence", **lc})

    # 3. Orphaned entries
    orphaned = _check_orphaned_entries(entries_dir)
    for o in orphaned:
        all_issues.append({"check": "orphaned", **o})

    # 4. Duplicates
    duplicates = _check_duplicates(entries_dir)
    for d in duplicates:
        all_issues.append({"check": "duplicate", **d})

    # 5. Broken references
    broken = _check_broken_refs(entries_dir)
    for b in broken:
        all_issues.append({"check": "broken_ref", **b})

    # 6. Stalled staging
    stalled = _check_stalled_staging(staging_dir)
    for s in stalled:
        all_issues.append({"check": "stalled_staging", **s})

    # Apply fixes if requested
    if fix:
        broken_fixes = _fix_broken_refs(entries_dir, broken)
        fixes_applied += broken_fixes

        stale_fixes = _fix_stale(entries_dir, stale)
        fixes_applied += stale_fixes

    report_md = _build_report(project_dir, all_issues, fixes_applied, fix)

    return {
        "status": "issues_found" if all_issues else "healthy",
        "checks_run": 6,
        "issues_found": len(all_issues),
        "issues_fixed": fixes_applied,
        "report_markdown": report_md,
        "details": all_issues,
    }


def _check_stale_entries(entries_dir: Path, stale_days: int) -> list[dict]:
    if not entries_dir.exists():
        return []
    now = datetime.now(timezone.utc)
    results = []
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        meta = _parse_frontmatter(f.read_text())
        last_verified = meta.get("last_verified", meta.get("date", ""))
        if last_verified:
            try:
                verified = datetime.strptime(last_verified, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                age = (now - verified).days
                if age > stale_days:
                    results.append({
                        "filename": f.name,
                        "title": meta.get("title", ""),
                        "age_days": age,
                        "last_verified": last_verified,
                    })
            except ValueError:
                pass
    return results


def _check_low_confidence(entries_dir: Path, threshold: float) -> list[dict]:
    if not entries_dir.exists():
        return []
    results = []
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        meta = _parse_frontmatter(f.read_text())
        conf = meta.get("confidence", 0.5)
        if conf < threshold:
            results.append({
                "filename": f.name,
                "title": meta.get("title", ""),
                "confidence": conf,
            })
    return results


def _check_orphaned_entries(entries_dir: Path) -> list[dict]:
    """Find entries with no related_entries and no tag overlap with any other entry."""
    if not entries_dir.exists():
        return []
    # Build tag index
    entries = []
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        meta = _parse_frontmatter(f.read_text())
        entries.append({
            "filename": f.name,
            "title": meta.get("title", ""),
            "tags": set(t.lower() for t in meta.get("tags", [])),
            "related_entries": meta.get("related_entries", []),
        })

    results = []
    for entry in entries:
        if entry["related_entries"]:
            continue
        # Check if any other entry shares 2+ tags
        has_connection = False
        for other in entries:
            if other["filename"] == entry["filename"]:
                continue
            if len(entry["tags"] & other["tags"]) >= 2:
                has_connection = True
                break
        if not has_connection and entry["tags"]:
            results.append({
                "filename": entry["filename"],
                "title": entry["title"],
                "tags": list(entry["tags"]),
            })
    return results


def _check_duplicates(entries_dir: Path) -> list[dict]:
    """Find entries with normalized identical titles but different content."""
    if not entries_dir.exists():
        return []
    titles: dict[str, list[dict]] = {}
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        meta = _parse_frontmatter(f.read_text())
        title = meta.get("title", "").lower().strip()
        if title not in titles:
            titles[title] = []
        titles[title].append({"filename": f.name, "title": meta.get("title", "")})

    results = []
    for title, files in titles.items():
        if len(files) > 1:
            results.append({
                "normalized_title": title,
                "files": files,
            })
    return results


def _check_broken_refs(entries_dir: Path) -> list[dict]:
    """Find entries whose related_entries reference IDs that no longer exist."""
    if not entries_dir.exists():
        return []
    # Collect all entry IDs
    all_ids = set()
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        meta = _parse_frontmatter(f.read_text())
        if "id" in meta:
            all_ids.add(meta["id"])

    results = []
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        meta = _parse_frontmatter(f.read_text())
        related = meta.get("related_entries", [])
        broken_ids = [rid for rid in related if rid not in all_ids]
        if broken_ids:
            results.append({
                "filename": f.name,
                "title": meta.get("title", ""),
                "broken_ids": broken_ids,
            })
    return results


def _check_stalled_staging(staging_dir: Path, stall_days: int = 30) -> list[dict]:
    """Find staging entries older than stall_days that haven't been promoted."""
    if not staging_dir.exists():
        return []
    now = datetime.now(timezone.utc)
    results = []
    for f in staging_dir.glob("*.md"):
        meta = _parse_frontmatter(f.read_text())
        created = meta.get("created", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created)
                age = (now - created_dt).days
                if age > stall_days:
                    results.append({
                        "filename": f.name,
                        "title": meta.get("title", ""),
                        "age_days": age,
                        "observation_count": meta.get("observation_count", 0),
                        "confidence": meta.get("confidence", 0.5),
                    })
            except ValueError:
                pass
    return results


def _fix_broken_refs(entries_dir: Path, broken: list[dict]) -> int:
    """Remove broken related_entries IDs from frontmatter."""
    fixes = 0
    for issue in broken:
        fpath = entries_dir / issue["filename"]
        if not fpath.exists():
            continue
        _update_frontmatter_value(
            fpath, "related_entries",
            lambda current: [r for r in (current or []) if r not in issue["broken_ids"]],
        )
        fixes += 1
    return fixes


def _fix_stale(entries_dir: Path, stale: list[dict]) -> int:
    """Mark stale entries with stale: true flag."""
    fixes = 0
    for issue in stale:
        fpath = entries_dir / issue["filename"]
        if not fpath.exists():
            continue
        _update_frontmatter_value(fpath, "stale", True)
        fixes += 1
    return fixes


def _update_frontmatter_value(fpath: Path, key: str, value_or_fn: Any) -> None:
    """Surgical frontmatter update. value_or_fn can be a callable receiving current value."""
    text = fpath.read_text()
    if not text.startswith("---"):
        return
    end = text.find("---", 3)
    if end == -1:
        return
    try:
        meta = yaml.safe_load(text[3:end].strip()) or {}
    except yaml.YAMLError:
        return
    if callable(value_or_fn):
        meta[key] = value_or_fn(meta.get(key))
    else:
        meta[key] = value_or_fn
    meta["updated"] = datetime.now(timezone.utc).isoformat()
    new_fm = yaml.dump(meta, default_flow_style=False)
    body = text[end + 3:].lstrip("\n")
    fpath.write_text(f"---\n{new_fm}---\n\n{body}")


def _build_report(
    project_dir: str,
    issues: list[dict],
    fixes: int,
    fix_mode: bool,
) -> str:
    lines = [
        f"# Memory Health Check Report\n\n",
        f"**Project:** {project_dir}\n",
        f"**Date:** {datetime.now(timezone.utc).isoformat()}\n",
        f"**Mode:** {'auto-fix' if fix_mode else 'dry-run'}\n\n",
        f"## Summary\n\n",
        f"- Checks run: 6\n",
        f"- Issues found: {len(issues)}\n",
        f"- Issues fixed: {fixes}\n\n",
    ]

    if not issues:
        lines.append("No issues found. Knowledge base is healthy.\n")
        return "".join(lines)

    # Group by check type
    by_check: dict[str, list[dict]] = {}
    for issue in issues:
        check = issue["check"]
        if check not in by_check:
            by_check[check] = []
        by_check[check].append(issue)

    sections = {
        "stale": ("Stale Entries", lambda i: f"- `{i['filename']}` — last verified {i['last_verified']}, {i['age_days']} days old"),
        "low_confidence": ("Low Confidence", lambda i: f"- `{i['filename']}` — confidence: {i['confidence']}"),
        "orphaned": ("Orphaned Entries", lambda i: f"- `{i['filename']}` — \"{i['title']}\" (tags: {', '.join(i['tags'])})"),
        "duplicate": ("Potential Duplicates", lambda i: f"- \"{i['normalized_title']}\" — {len(i['files'])} copies: {', '.join(f['filename'] for f in i['files'])}"),
        "broken_ref": ("Broken References", lambda i: f"- `{i['filename']}` — references missing IDs: {', '.join(i['broken_ids'])}"),
        "stalled_staging": ("Stalled Staging Entries", lambda i: f"- `{i['filename']}` — {i['age_days']} days old (obs: {i['observation_count']}, conf: {i['confidence']})"),
    }

    for check_key, (heading, formatter) in sections.items():
        group = by_check.get(check_key, [])
        if not group:
            continue
        lines.append(f"### {heading} ({len(group)})\n\n")
        for issue in group:
            lines.append(formatter(issue) + "\n")
        lines.append("\n")

    return "".join(lines)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end].strip()) or {}
    except yaml.YAMLError:
        return {}
