"""Registry of task packs: manifests, protocol hashes and adapters.

The always-on core knows a pack only by its id, kind, protocol hash and adapter.
Manifests are data; adapters are allowlisted Python entry points. A retired pack
keeps its protocol hash so old receipts stay readable, but has no adapter and is
never executed.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import RouterError

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_ROOT = REPO_ROOT / "router_runner" / "taskpacks"
DEFAULT_ALLOWLIST = frozenset({"demo-taskpack-v0.1", "memory-dreaming-v0.1", "nk2r-challenge-v0.1"})
_ALLOWED_FIELDS = frozenset({
    "schema", "id", "kind", "status", "protocol_file", "protocol_hash",
    "adapter", "entry", "scope", "budget",
})
_ALLOWED_STATUS = frozenset({"active", "retired"})


class TaskPack(Protocol):
    def run(
        self, work_item: Mapping[str, Any], mandate: Mapping[str, Any],
        out_root: Path | None, attempt: int = 1,
    ) -> dict[str, Any]: ...

    def reconcile(
        self, work_item: Mapping[str, Any], mandate: Mapping[str, Any],
        attempt: int, out_root: Path | None,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class TaskPackManifest:
    id: str
    kind: str
    status: str
    protocol_file: Path | None
    protocol_hash: str | None
    adapter: str | None
    entry: str | None
    scope: str
    budget: Mapping[str, Any]


@dataclass(frozen=True)
class LoadedTaskPack:
    manifest: TaskPackManifest
    protocol_hash: str
    adapter: TaskPack | None


def _fail(message: str) -> RouterError:
    return RouterError(f"task-pack registry: {message}")


def _load_manifest(path: Path) -> TaskPackManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _fail(f"{path.name}: cannot read manifest: {error}") from error
    if not isinstance(raw, dict):
        raise _fail(f"{path.name}: manifest must be an object")
    unknown = sorted(set(raw) - _ALLOWED_FIELDS)
    if unknown:
        raise _fail(f"{path.name}: unknown fields: {', '.join(unknown)}")
    if raw.get("schema") != 1:
        raise _fail(f"{path.name}: unsupported schema {raw.get('schema')!r}")

    pack_id = raw.get("id")
    if not isinstance(pack_id, str) or not pack_id:
        raise _fail(f"{path.name}: id must be a non-empty string")
    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        raise _fail(f"{path.name}: kind must be a non-empty string")
    status = raw.get("status")
    if status not in _ALLOWED_STATUS:
        raise _fail(f"{path.name}: status must be active or retired")
    scope = raw.get("scope")
    if not isinstance(scope, str) or not scope:
        raise _fail(f"{path.name}: scope must be a non-empty string")
    budget = raw.get("budget")
    if not isinstance(budget, dict):
        raise _fail(f"{path.name}: budget must be an object")

    adapter = raw.get("adapter")
    entry = raw.get("entry")
    protocol_file = raw.get("protocol_file")
    protocol_hash = raw.get("protocol_hash")
    if entry is not None:
        raise _fail(f"{path.name}: subprocess entries are not enabled; adapter only")

    if status == "active":
        if not isinstance(adapter, str) or ":" not in adapter:
            raise _fail(f"{path.name}: active pack needs an adapter entry point 'module:Class'")
        if not isinstance(protocol_file, str) or not protocol_file:
            raise _fail(f"{path.name}: active pack needs protocol_file")
        if protocol_hash is not None:
            raise _fail(f"{path.name}: active pack must not set protocol_hash; it is computed")
    else:
        if adapter is not None:
            raise _fail(f"{path.name}: retired pack must not have an adapter")
        if protocol_file is not None:
            raise _fail(f"{path.name}: retired pack must not have a protocol_file")
        if not isinstance(protocol_hash, str) or len(protocol_hash) != 64:
            raise _fail(f"{path.name}: retired pack needs a 64-character protocol_hash")

    return TaskPackManifest(
        id=pack_id, kind=kind, status=status,
        protocol_file=Path(protocol_file) if protocol_file is not None else None,
        protocol_hash=protocol_hash, adapter=adapter, entry=entry,
        scope=scope, budget=budget,
    )


def _resolve_protocol(manifest: TaskPackManifest, registry_root: Path, manifest_path: Path) -> Path:
    protocol = (registry_root / str(manifest.protocol_file)).resolve()
    if not protocol.is_relative_to(registry_root.resolve()):
        raise _fail(f"{manifest_path.name}: protocol_file escapes the registry root")
    if not protocol.is_file():
        raise _fail(f"{manifest_path.name}: protocol_file does not exist: {manifest.protocol_file}")
    return protocol


def _load_adapter(entry: str, manifest: TaskPackManifest) -> TaskPack:
    module_name, _, class_name = entry.partition(":")
    if not module_name or not class_name:
        raise _fail(f"{manifest.id}: adapter must be 'module:Class'")
    try:
        module = importlib.import_module(module_name)
        adapter_class = getattr(module, class_name)
    except (ImportError, AttributeError) as error:
        raise _fail(f"{manifest.id}: cannot import adapter {entry}: {error}") from error
    adapter = adapter_class(manifest)
    if not callable(getattr(adapter, "run", None)) or not callable(getattr(adapter, "reconcile", None)):
        raise _fail(f"{manifest.id}: adapter {entry} does not implement run/reconcile")
    return adapter


def load_task_packs(
    registry_root: Path = REGISTRY_ROOT, allowed_ids: frozenset[str] | None = None,
) -> dict[str, LoadedTaskPack]:
    """Load and validate every manifest; unlisted packs are skipped, never imported."""
    root = Path(registry_root).resolve()
    if not root.is_dir():
        raise _fail(f"registry directory does not exist: {root}")

    packs: dict[str, LoadedTaskPack] = {}
    for path in sorted(root.glob("*.json")):
        manifest = _load_manifest(path)
        if manifest.id in packs:
            raise _fail(f"duplicate task pack id: {manifest.id}")
        if allowed_ids is not None and manifest.id not in allowed_ids:
            continue
        if manifest.status == "active":
            protocol = _resolve_protocol(manifest, root, path)
            protocol_hash = hashlib.sha256(protocol.read_bytes()).hexdigest()
            manifest = replace(manifest, protocol_file=protocol)
            adapter = _load_adapter(str(manifest.adapter), manifest)
        else:
            protocol_hash = str(manifest.protocol_hash)
            adapter = None
        packs[manifest.id] = LoadedTaskPack(manifest, protocol_hash, adapter)

    if not packs:
        raise _fail(f"no task packs loaded from {root}")
    return packs


@lru_cache(maxsize=1)
def default_registry() -> dict[str, LoadedTaskPack]:
    return load_task_packs(REGISTRY_ROOT, DEFAULT_ALLOWLIST)
