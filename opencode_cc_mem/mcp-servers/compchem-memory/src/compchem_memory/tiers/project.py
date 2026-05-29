"""Project tier: durable, human-readable notes scoped to a project directory."""

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

from compchem_memory.atomic_io import atomic_write_text
from compchem_memory.storage import backup_file

ENTRY_TYPES = (
    "success_pattern",
    "error_resolution",
    "failure_pattern",
    "parameter_guidance",
    "workflow_note",
    "note",
)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `patch` into `base`. Patch values override; nested
    dicts are merged element-wise; non-dict values replace whole.
    """
    out = dict(base)
    for key, value in patch.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _format_index_value(value: Any) -> str:
    """Render a single index field for the flow-style row.

    Goals: human-readable, greppable, round-trips through yaml.safe_load.
    - None -> 'null'
    - bool -> 'true'/'false'
    - int/float -> str(value)
    - all-digit strings get single-quoted so they round-trip as str (e.g. job_id)
    - other strings emit bare unless they contain flow-context terminators
      (`,` `{` `}` `[` `]`) or leading/trailing whitespace, in which case
      they get single-quoted.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s == "":
        return "''"
    needs_quote = (
        s.isdigit()  # preserve as string (e.g. job_id)
        or s != s.strip()
        or any(ch in s for ch in (",", "{", "}", "[", "]", "'", '"', "\n"))
        or s[0] in ("&", "*", "!", "|", ">", "%", "@", "`", "#")
    )
    if needs_quote:
        return "'" + s.replace("'", "''") + "'"
    return s


def _format_index_row(row: dict[str, Any]) -> str:
    """Render one index row in flow-style YAML on a single line."""
    parts = [f"{k}: {_format_index_value(v)}" for k, v in row.items()]
    return "{" + ", ".join(parts) + "}"


class ProjectManager:
    def __init__(self, global_base: Path):
        self.global_base = global_base

    def _project_store(self, project_dir: str) -> Path:
        local = Path(project_dir) / ".magnolia"
        if local.is_symlink():
            target = local.resolve()
            if target.exists():
                return target
        return local

    def _entries_dir(self, project_dir: str) -> Path:
        d = self._project_store(project_dir) / "entries"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _runs_dir(self, project_dir: str) -> Path:
        d = self._project_store(project_dir) / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _staging_dir(self, project_dir: str) -> Path:
        d = self._project_store(project_dir) / "staging"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self, project_dir: str) -> Path:
        return self._entries_dir(project_dir) / "INDEX.md"

    def _runs_index_path(self, project_dir: str) -> Path:
        return self._runs_dir(project_dir) / "INDEX.yaml"

    def create_entry(
        self,
        project_dir: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        source: str = "auto",
        staging: bool = False,
        entry_type: str = "note",
        tools: list[str] | None = None,
        confidence: float = 0.5,
        related_entries: list[str] | None = None,
        references: list[str] | None = None,
        notebook_section: str | None = None,
    ) -> str:
        if entry_type not in ENTRY_TYPES:
            entry_type = "note"
        if staging:
            base = self._staging_dir(project_dir)
        else:
            base = self._entries_dir(project_dir)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:60].strip("_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        fname = f"{ts}_{slug}.md"
        fpath = base / fname
        now_iso = datetime.now(timezone.utc).isoformat()
        now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        frontmatter = {
            "id": ts,
            "type": entry_type,
            "title": title,
            "description": content[:200],
            "tools": tools or [],
            "tags": tags or [],
            "created": now_iso,
            "updated": now_iso,
            "date": now_date,
            "last_verified": now_date,
            "author": source,
            "source": source,
            "observation_count": 1,
            "confidence": confidence,
        }
        if related_entries:
            frontmatter["related_entries"] = related_entries
        if references:
            frontmatter["references"] = references
        if notebook_section:
            frontmatter["notebook_section"] = notebook_section
        fm_str = "---\n" + yaml.dump(frontmatter, default_flow_style=False) + "---\n"
        fpath.write_text(fm_str + "\n" + content + "\n")
        if not staging:
            self._update_index(project_dir)
            self._update_related_links(project_dir, frontmatter["id"], fpath.name, tags or [], title)
        return str(fpath)

    def get_entry(self, project_dir: str, name: str) -> str | None:
        entries_dir = self._entries_dir(project_dir)
        normalized = re.sub(r"[^a-zA-Z0-9]+", "_", name).lower()
        for f in entries_dir.glob("*.md"):
            fn_norm = re.sub(r"[^a-zA-Z0-9]+", "_", f.stem).lower()
            if f.name == name or f.stem == name or normalized in fn_norm:
                return f.read_text()
        return None

    def list_entries(self, project_dir: str) -> list[dict[str, Any]]:
        entries_dir = self._entries_dir(project_dir)
        results: list[dict[str, Any]] = []
        md_files = sorted(entries_dir.glob("*.md"))
        for f in md_files:
            if f.name == "INDEX.md":
                continue
            text = f.read_text()
            meta = self._parse_frontmatter(text)
            results.append(
                {
                    "name": f.name,
                    "title": meta.get("title", f.stem),
                    "type": meta.get("type", "note"),
                    "date": meta.get("date", ""),
                    "tags": meta.get("tags", []),
                    "tools": meta.get("tools", []),
                    "confidence": meta.get("confidence", 0.5),
                    "observation_count": meta.get("observation_count", 0),
                    "source": meta.get("source", ""),
                    "path": str(f),
                }
            )
        return results

    def search_entries(
        self, project_dir: str, keyword: str = "", tags: list[str] | None = None
    ) -> list[dict[str, Any]]:
        entries = self.list_entries(project_dir)
        results: list[dict[str, Any]] = []
        for entry in entries:
            text_content = self.get_entry(project_dir, entry["name"]) or ""
            match = True
            if keyword:
                match = keyword.lower() in (text_content + str(entry)).lower()
            if match and tags:
                entry_tags_lower = [t.lower() for t in entry.get("tags", [])]
                if not any(t.lower() in entry_tags_lower for t in tags):
                    match = False
            if match:
                entry["tier"] = "project"
                results.append(entry)
        return results

    def promote_to_skill(
        self, project_dir: str, entry_name: str, skills_dir: str
    ) -> str:
        entries_dir = self._entries_dir(project_dir)
        source = None
        for f in entries_dir.glob("*.md"):
            if f.name == entry_name or entry_name in f.name:
                source = f
                break
        if not source:
            raise FileNotFoundError(f"Entry not found: {entry_name}")
        backup_file(source, project_dir)
        skills_path = Path(skills_dir)
        dest = skills_path / source.name
        dest.write_text(source.read_text())
        source.unlink()
        self._update_index(project_dir)
        return str(dest)

    def confirm_staging(self, project_dir: str, entry_name: str) -> str:
        staging = self._staging_dir(project_dir)
        entries = self._entries_dir(project_dir)
        source = None
        for f in staging.glob("*.md"):
            if f.name == entry_name or entry_name in f.name or entry_name in f.stem:
                source = f
                break
        if not source:
            raise FileNotFoundError(f"Staging entry not found: {entry_name}")
        backup_file(source, project_dir)
        dest = entries / source.name
        dest.write_text(source.read_text())
        source.unlink()
        self._update_index(project_dir)
        return str(dest)

    def auto_promote_staging(self, project_dir: str) -> list[str]:
        """Auto-promote staging entries with observation_count >= 2, confidence > 0.85,
        AND observed_in_sessions containing >= 2 distinct session_ids.

        Closes cybernetics §3.4 (single-session auto-promotion risk).
        """
        staging = self._staging_dir(project_dir)
        promoted = []
        for f in list(staging.glob("*.md")):
            text = f.read_text()
            meta = self._parse_frontmatter(text)
            obs = meta.get("observation_count", 0)
            conf = meta.get("confidence", 0.5)
            sessions = meta.get("observed_in_sessions", []) or []
            distinct_sessions = len(set(sessions))
            if obs >= 2 and conf > 0.85 and distinct_sessions >= 2:
                try:
                    dest = self.confirm_staging(project_dir, f.stem)
                    promoted.append(f.name)
                except FileNotFoundError:
                    pass
        return promoted

    def bump_observation_count(
        self, project_dir: str, entry_name: str, session_id: str | None = None
    ) -> bool:
        """Increment observation_count on a staging entry; record session_id if provided.
        Returns True if found."""
        staging = self._staging_dir(project_dir)
        for f in staging.glob("*.md"):
            if f.name == entry_name or entry_name in f.name or entry_name in f.stem:
                meta = self._parse_frontmatter(f.read_text())
                new_count = meta.get("observation_count", 0) + 1
                sessions = list(meta.get("observed_in_sessions", []) or [])
                if session_id and session_id not in sessions:
                    sessions.append(session_id)
                self._update_entry_frontmatter(f, "observation_count", new_count)
                self._update_entry_frontmatter(f, "observed_in_sessions", sessions)
                return True
        return False

    def find_similar_staging(
        self, project_dir: str, title: str, tags: list[str]
    ) -> str | None:
        """Find a staging entry with similar title or shared tags. Returns filename or None."""
        staging = self._staging_dir(project_dir)
        title_lower = title.lower()
        tags_set = set(t.lower() for t in tags)
        best_match = None
        best_score = 0
        for f in staging.glob("*.md"):
            meta = self._parse_frontmatter(f.read_text())
            existing_title = meta.get("title", "").lower()
            existing_tags = set(t.lower() for t in meta.get("tags", []))
            score = len(tags_set & existing_tags)
            title_words = set(title_lower.split())
            existing_words = set(existing_title.split())
            score += len(title_words & existing_words) * 2
            if score > best_score and score >= 2:
                best_score = score
                best_match = f.name
        return best_match

    def record_run(
        self,
        project_dir: str,
        run_id: str,
        tool: str,
        status: str | None,
        metrics: dict[str, Any] | None = None,
        quality_flags: list[str] | None = None,
        errors_solved: list[str] | None = None,
        *,
        lifecycle: str | None = None,
        remote: dict[str, Any] | None = None,
    ) -> str:
        runs_dir = self._runs_dir(project_dir)
        record: dict[str, Any] = {
            "run_id": run_id,
            "tool": tool,
            "status": status,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "metrics": metrics or {},
            "quality_flags": quality_flags or [],
            "errors_solved": errors_solved or [],
        }
        if lifecycle is not None:
            record["lifecycle"] = lifecycle
        if remote is not None:
            record["remote"] = remote
        ts = datetime.now(timezone.utc).strftime("%Y%m%d")
        fname = f"{ts}_{run_id}.yaml"
        fpath = runs_dir / fname
        atomic_write_text(fpath, yaml.dump(record, default_flow_style=False, sort_keys=False))
        self._update_runs_index(project_dir)
        return str(fpath)

    def update_run(
        self,
        project_dir: str,
        run_id: str,
        patch: dict[str, Any],
    ) -> str:
        """Load runs/<date>_<run_id>.yaml, deep-merge `patch`, write back,
        refresh INDEX.yaml. Returns the YAML path.

        Raises FileNotFoundError if the run record doesn't exist.
        """
        runs_dir = self._runs_dir(project_dir)
        matches = list(runs_dir.glob(f"*_{run_id}.yaml"))
        if not matches:
            raise FileNotFoundError(
                f"No run record for run_id={run_id!r} in {runs_dir}"
            )
        fpath = matches[0]
        existing = yaml.safe_load(fpath.read_text()) or {}
        merged = _deep_merge(existing, patch)
        atomic_write_text(fpath, yaml.dump(merged, default_flow_style=False, sort_keys=False))
        self._update_runs_index(project_dir)
        return str(fpath)

    def get_run_history(self, project_dir: str) -> list[dict[str, Any]]:
        runs_dir = self._runs_dir(project_dir)
        results: list[dict[str, Any]] = []
        for f in sorted(runs_dir.glob("*.yaml")):
            if f.name == "INDEX.yaml":
                continue
            record = yaml.safe_load(f.read_text())
            record["path"] = str(f)
            results.append(record)
        return results

    def _parse_frontmatter(self, text: str) -> dict[str, Any]:
        if not text.startswith("---"):
            return {}
        end = text.find("---", 3)
        if end == -1:
            return {}
        fm = text[3:end].strip()
        try:
            return yaml.safe_load(fm) or {}
        except yaml.YAMLError:
            return {}

    def _update_index(self, project_dir: str) -> None:
        entries = self.list_entries(project_dir)
        index_path = self._index_path(project_dir)
        lines = ["# Project-Tier Entry Index\n\n"]
        lines.append(
            f"**Last updated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        )
        if not entries:
            lines.append("_No entries yet._\n")
        else:
            # Group by type
            by_type: dict[str, list[dict[str, Any]]] = {}
            for e in entries:
                t = e.get("type", "note")
                if t not in by_type:
                    by_type[t] = []
                by_type[t].append(e)

            lines.append(f"**Total entries:** {len(entries)}\n\n")

            type_labels = {
                "success_pattern": "Success Patterns",
                "error_resolution": "Error Resolutions",
                "failure_pattern": "Failure Patterns",
                "parameter_guidance": "Parameter Guidance",
                "workflow_note": "Workflow Notes",
                "note": "Notes & Annotations",
            }

            for entry_type in ENTRY_TYPES:
                group = by_type.get(entry_type, [])
                if not group:
                    continue
                heading = type_labels.get(entry_type, entry_type.title())
                lines.append(f"## {heading}\n\n")
                for e in group:
                    tags = ", ".join(e.get("tags", []))
                    date = e.get("date", "")
                    title = e["title"]
                    stem = e["name"].replace(".md", "")
                    lines.append(
                        f"- [[{stem}]] **{title}** [{entry_type}] — {date} [{tags}]\n"
                    )
                lines.append("\n")

        atomic_write_text(index_path, "".join(lines))

    def _update_runs_index(self, project_dir: str) -> None:
        """Scan all runs/*_*.yaml files, emit a one-line-per-record INDEX.yaml.

        Output is flow-style YAML: each record renders on its own line, which
        makes the file `grep`-friendly (any grep hit returns a complete record
        with all identifying columns).
        """
        runs_dir = self._runs_dir(project_dir)
        index_path = self._runs_index_path(project_dir)

        # Backward compat: remove old INDEX.md if it exists
        old_index = runs_dir / "INDEX.md"
        if old_index.exists():
            old_index.unlink()

        records: list[dict[str, Any]] = []
        for yfile in sorted(runs_dir.glob("*.yaml")):
            if yfile.name == "INDEX.yaml":
                continue
            try:
                data = yaml.safe_load(yfile.read_text()) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(data, dict):
                continue
            remote = data.get("remote") or {}
            slurm = remote.get("slurm") or {}
            row = {
                "run_id": data.get("run_id"),
                "tool": data.get("tool"),
                "date": data.get("date"),
                "lifecycle": data.get("lifecycle"),
                "status": data.get("status"),
                "cluster": remote.get("cluster"),
                "job_id": remote.get("job_id"),
                "slurm_state": slurm.get("state"),
                "elapsed": slurm.get("elapsed"),
                "node": slurm.get("node_list"),
            }
            records.append(row)
        if not records:
            atomic_write_text(index_path, "[]\n")
            return
        lines = ["# runs/INDEX.yaml — auto-maintained by ProjectManager._update_runs_index()"]
        for row in records:
            lines.append(f"- {_format_index_row(row)}")
        atomic_write_text(index_path, "\n".join(lines) + "\n")

    def _update_related_links(
        self,
        project_dir: str,
        new_id: str,
        new_filename: str,
        new_tags: list[str],
        new_title: str,
    ) -> None:
        entries_dir = self._entries_dir(project_dir)
        new_tags_set = set(t.lower() for t in new_tags)
        new_title_lower = new_title.lower()
        for f in entries_dir.glob("*.md"):
            if f.name == new_filename or f.name == "INDEX.md":
                continue
            text = f.read_text()
            meta = self._parse_frontmatter(text)
            existing_tags = set(t.lower() for t in meta.get("tags", []))
            existing_title = meta.get("title", "").lower()
            shared_tags = new_tags_set & existing_tags
            title_overlap = (
                len(set(new_title_lower.split()) & set(existing_title.split())) >= 2
            )
            if len(shared_tags) >= 2 or title_overlap:
                related = meta.get("related_entries", [])
                if new_id not in related:
                    related.append(new_id)
                    self._update_entry_frontmatter(f, "related_entries", related)

    def _update_entry_frontmatter(
        self, fpath: Path, key: str, value: Any
    ) -> None:
        text = fpath.read_text()
        if not text.startswith("---"):
            return
        end = text.find("---", 3)
        if end == -1:
            return
        fm = text[3:end].strip()
        try:
            meta = yaml.safe_load(fm) or {}
        except yaml.YAMLError:
            return
        meta[key] = value
        meta["updated"] = datetime.now(timezone.utc).isoformat()
        new_fm = yaml.dump(meta, default_flow_style=False)
        body = text[end + 3:].lstrip("\n")
        fpath.write_text(f"---\n{new_fm}---\n\n{body}")

    # ── Goal management ──────────────────────────────────────────────────

    def decrement_confidence_for_tool(
        self, project_dir: str, tool: str, delta: float = 0.05
    ) -> int:
        """Decrement confidence on success_pattern entries matching a failed tool.

        Only applies to entries not recently verified (older than 7 days).
        Returns count of entries adjusted.
        """
        entries_dir = self._entries_dir(project_dir)
        adjusted = 0
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date()
        tool_lower = tool.lower()
        for f in entries_dir.glob("*.md"):
            if f.name == "INDEX.md":
                continue
            text = f.read_text()
            meta = self._parse_frontmatter(text)
            if meta.get("type") != "success_pattern":
                continue
            entry_tools = [t.lower() for t in meta.get("tools", [])]
            if tool_lower not in entry_tools:
                continue
            # Skip recently verified entries
            verified_str = meta.get("last_verified", meta.get("date", ""))
            if verified_str:
                try:
                    verified_date = datetime.fromisoformat(verified_str).date()
                    if verified_date >= seven_days_ago:
                        continue
                except (ValueError, TypeError):
                    pass
            old_conf = meta.get("confidence", 0.5)
            new_conf = max(0.0, round(old_conf - delta, 2))
            if new_conf != old_conf:
                self._update_entry_frontmatter(f, "confidence", new_conf)
                adjusted += 1
        return adjusted

    def get_goal(self, project_dir: str) -> str | None:
        """Read the project goal. Returns content or None."""
        goal_path = self._project_store(project_dir) / "GOAL.md"
        if goal_path.exists():
            return goal_path.read_text()
        return None

    def set_goal(self, project_dir: str, content: str) -> str:
        """Write or update the project goal. Returns path."""
        goal_path = self._project_store(project_dir) / "GOAL.md"
        goal_path.write_text(content)
        return str(goal_path)
