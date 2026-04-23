"""Consolidation: merge duplicates, expire stale entries, trim to budget."""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def consolidate_tier(
    tier: str,
    base_dir: str,
    stale_days: int = 90,
    max_entries: int = 50,
    skills_dir: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "tier": tier,
        "merged": 0,
        "expired": 0,
        "remaining": 0,
        "actions": [],
    }

    if tier == "project":
        entries_dir = _resolve_entries_dir(base_dir)
        if not entries_dir.exists():
            return report

        entries = [e for e in entries_dir.glob("*.md") if e.name != "INDEX.md"]
        report["remaining"] = len(entries)

        merged = _merge_duplicates(entries, entries_dir)
        report["merged"] = merged
        report["actions"].append(f"Merged {merged} duplicate entries")

        expired = _expire_stale(entries_dir, stale_days)
        report["expired"] = expired
        report["actions"].append(f"Flagged {expired} stale entries")

        # Enforce max_entries cap — archive lowest-confidence entries
        archived = _archive_excess(entries_dir, base_dir, max_entries)
        if archived:
            report["archived"] = archived
            report["actions"].append(f"Archived {archived} excess entries (cap: {max_entries})")

        remaining = len([e for e in entries_dir.glob("*.md") if e.name != "INDEX.md"])
        report["remaining"] = remaining

        # Refresh INDEX.md whenever entries were modified
        if merged or expired or archived:
            _refresh_index(entries_dir)

    elif tier == "skill":
        if skills_dir:
            skill_path = Path(skills_dir)
            if skill_path.exists():
                files = list(skill_path.glob("*.md"))
                report["remaining"] = len(files)

    return report


def _resolve_entries_dir(base_dir: str) -> Path:
    local = Path(base_dir) / ".magnolia" / "entries"
    if local.is_symlink():
        target = local.resolve()
        if target.exists():
            return target
    if local.exists():
        return local
    return local


def _merge_duplicates(entries: list[Path], entries_dir: Path) -> int:
    titles: dict[str, list[Path]] = {}
    for e in entries:
        text = e.read_text()
        title = _get_title(text)
        normalized = title.lower().strip()
        if normalized not in titles:
            titles[normalized] = []
        titles[normalized].append(e)

    merged = 0
    for title, paths in titles.items():
        if len(paths) > 1:
            keeper = paths[0]
            keeper_text = keeper.read_text()
            keeper_meta = _parse_frontmatter(keeper_text)
            keeper_body = _extract_body(keeper_text)
            for dup in paths[1:]:
                dup_text = dup.read_text()
                dup_meta = _parse_frontmatter(dup_text)
                dup_body = _extract_body(dup_text)
                # Merge frontmatter: take max confidence, sum observations, union tags/tools
                if dup_meta.get("confidence", 0) > keeper_meta.get("confidence", 0):
                    keeper_meta["confidence"] = dup_meta["confidence"]
                keeper_meta["observation_count"] = (
                    keeper_meta.get("observation_count", 0) + dup_meta.get("observation_count", 0)
                )
                keeper_meta["tags"] = list(set(
                    keeper_meta.get("tags", []) + dup_meta.get("tags", [])
                ))
                keeper_meta["tools"] = list(set(
                    keeper_meta.get("tools", []) + dup_meta.get("tools", [])
                ))
                # Concatenate bodies
                if dup_body.strip():
                    keeper_body = keeper_body.rstrip("\n") + "\n\n---\n\n" + dup_body.lstrip("\n")
                merged += 1
            # Write merged entry with updated frontmatter FIRST (before unlink)
            fm_str = yaml.dump(keeper_meta, default_flow_style=False)
            keeper.write_text(f"---\n{fm_str}---\n\n{keeper_body}\n")
            # Now safe to delete duplicates
            for dup in paths[1:]:
                dup.unlink()
    return merged


def _extract_body(text: str) -> str:
    """Extract the body (content after frontmatter) from a markdown file."""
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return text
    return text[end + 3:].lstrip("\n")


def _expire_stale(entries_dir: Path, stale_days: int) -> int:
    now = datetime.now(timezone.utc)
    expired = 0
    for f in entries_dir.glob("*.md"):
        if f.name == "INDEX.md":
            continue
        text = f.read_text()
        meta = _parse_frontmatter(text)
        last_verified = meta.get("last_verified", meta.get("date", ""))
        if last_verified:
            try:
                verified_date = datetime.strptime(last_verified, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                age = (now - verified_date).days
                if age > stale_days and not meta.get("stale"):
                    _update_frontmatter_field(f, "stale", True)
                    _update_frontmatter_field(f, "stale_age_days", age)
                    expired += 1
            except ValueError:
                pass
    return expired


def _update_frontmatter_field(f: Path, key: str, value: Any) -> None:
    """Update a single frontmatter field without corrupting the rest."""
    text = f.read_text()
    if not text.startswith("---"):
        return
    end = text.find("---", 3)
    if end == -1:
        return
    try:
        meta = yaml.safe_load(text[3:end].strip()) or {}
    except yaml.YAMLError:
        return
    meta[key] = value
    meta["updated"] = datetime.now(timezone.utc).isoformat()
    new_fm = yaml.dump(meta, default_flow_style=False)
    body = text[end + 3:].lstrip("\n")
    f.write_text(f"---\n{new_fm}---\n\n{body}")


def _get_title(text: str) -> str:
    meta = _parse_frontmatter(text)
    if "title" in meta:
        return meta["title"]
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _archive_excess(entries_dir: Path, base_dir: str, max_entries: int) -> int:
    """Move lowest-confidence entries to archive/ when count exceeds max_entries."""
    entries = [e for e in entries_dir.glob("*.md") if e.name != "INDEX.md"]
    if len(entries) <= max_entries:
        return 0

    # Score each entry by confidence × observation_count, break ties by age (older first)
    scored: list[tuple[float, datetime, Path]] = []
    for e in entries:
        meta = _parse_frontmatter(e.read_text())
        confidence = meta.get("confidence", 0.5)
        observations = meta.get("observation_count", 0)
        score = confidence * (1 + observations * 0.1)
        date_str = meta.get("date", "2000-01-01")
        try:
            date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            date = datetime(2000, 1, 1, tzinfo=timezone.utc)
        scored.append((score, date, e))

    # Sort ascending: lowest score first, oldest first for ties
    scored.sort(key=lambda x: (x[0], x[1]))

    to_archive = len(entries) - max_entries
    archive_dir = Path(base_dir) / ".magnolia" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived = 0
    for _, _, entry in scored[:to_archive]:
        dest = archive_dir / entry.name
        # Don't overwrite existing archives
        if not dest.exists():
            entry.rename(dest)
            archived += 1
        else:
            # If archive already has this name, add timestamp suffix
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            dest = archive_dir / f"{entry.stem}_{ts}.md"
            entry.rename(dest)
            archived += 1

    return archived


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


def _refresh_index(entries_dir: Path) -> None:
    """Regenerate INDEX.md to remove references to archived/deleted entries."""
    from compchem_memory.tiers.project import ProjectManager

    index_path = entries_dir / "INDEX.md"
    if not index_path.exists():
        return
    # Find project root by checking if entries_dir's parent is .magnolia/
    # or if entries_dir is a resolved symlink under a differently-named dir.
    # Walk up: entries → parent → check if parent is .magnolia or parent/entries
    # exists as a child of a .magnolia dir.
    magnolia_dir = entries_dir.parent
    if magnolia_dir.name == ".magnolia":
        project_root = str(magnolia_dir.parent)
    else:
        # entries_dir may be a resolved symlink target. Check if there's a
        # .magnolia/entries path that resolves here, and use that project root.
        # Fallback: use magnolia_dir itself (entries_dir's parent).
        found = False
        # Check if any path in the chain has .magnolia -> entries
        for parent in entries_dir.parents:
            candidate = parent / ".magnolia" / "entries"
            try:
                if candidate.resolve() == entries_dir.resolve():
                    project_root = str(parent)
                    found = True
                    break
            except OSError:
                continue
        if not found:
            project_root = str(magnolia_dir)
    proj_m = ProjectManager(Path.home() / ".magnolia")
    proj_m._update_index(project_root)
