"""Cluster registry — where the generic Slurm backend learns about a specific site.

``ssh_slurm`` knows how to drive *a* Slurm cluster over ssh. It should not know
which one. Everything site-specific — the ssh alias, the scratch layout, the
account you are charged against, whether reaching the login node needs a VPN
tunnel first — is data, loaded from here.

**Layering.** Later sources win, key by key, so a site profile can ship a
partition while the person running it supplies their own account:

1. ``clusters.yaml`` packaged beside this module — site facts for clusters the
   project has been used on. Public, committed, no per-person values.
2. ``~/.config/magnolia/clusters.yaml`` — per-user overrides and private sites.
3. ``$MAGNOLIA_CLUSTERS_FILE`` — an explicit path, for tests and one-offs.

**Which cluster, when the caller does not say.** ``$MAGNOLIA_CLUSTER``, else the
profile marked ``default: true``, else the only configured profile. With several
profiles and no default, resolution fails and names them: guessing would submit
someone's job, and someone's compute budget, to the wrong machine.

Nothing here reaches the network — this module only decides *what* the backend
should talk to.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

CLUSTERS_FILE_ENV = "MAGNOLIA_CLUSTERS_FILE"
CLUSTER_ENV = "MAGNOLIA_CLUSTER"

PACKAGED_CLUSTERS = Path(__file__).with_name("clusters.yaml")
USER_CLUSTERS = Path.home() / ".config" / "magnolia" / "clusters.yaml"

# Every key the backend reads, with a value that is true of a plain Slurm site.
# An empty string means "this site does not have one" and the corresponding step
# is skipped — no tunnel script, no --account, no `module use`. Azzurra needs all
# of them; a bare university cluster needs none, and must not be made to pretend.
CLUSTER_DEFAULTS: dict[str, Any] = {
    "ssh_host": "",
    "scratch_root": "/scratch/{user}/magnolia",
    "default_user": "",
    "default_account": "",
    "default_qos": "",
    "default_partition": "",
    "tunnel_script": "",
    "modulefiles_use": "",
    "requires_control_master": True,
    "default": False,
}


class ClusterError(RuntimeError):
    """Raised when no cluster can be resolved, or a resolved one is unusable."""


def _read(path: Path) -> dict[str, dict[str, Any]]:
    """Parse one clusters file. A missing file is not an error; a broken one is."""
    if not path.is_file():
        return {}
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ClusterError(f"{path}: not valid YAML: {exc}") from exc
    clusters = doc.get("clusters", doc)
    if not isinstance(clusters, dict):
        raise ClusterError(f"{path}: expected a mapping of cluster name -> settings")
    return {str(k): dict(v or {}) for k, v in clusters.items()}


def _sources() -> list[Path]:
    paths = [PACKAGED_CLUSTERS, USER_CLUSTERS]
    explicit = os.environ.get(CLUSTERS_FILE_ENV)
    if explicit:
        paths.append(Path(explicit).expanduser())
    return paths


def load() -> dict[str, dict[str, Any]]:
    """Merge every source into ``{name: settings}``, defaults filled in.

    Merging is per key, not per profile: overriding ``default_account`` for a
    packaged site must not silently drop its ``tunnel_script``.
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in _sources():
        for name, settings in _read(path).items():
            merged.setdefault(name, dict(CLUSTER_DEFAULTS)).update(settings)
    for settings in merged.values():
        if not settings.get("default_user"):
            settings["default_user"] = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return merged


def resolve(cluster: str | None = None,
            registry: dict[str, dict[str, Any]] | None = None) -> str:
    """Return the cluster name to use, or raise ``ClusterError`` naming the options.

    ``registry`` lets a caller resolve against an already-loaded snapshot instead
    of re-reading the files on every ssh call.
    """
    registry = load() if registry is None else registry
    if not registry:
        raise ClusterError(
            "no clusters configured. Add one to "
            f"{USER_CLUSTERS} (see {PACKAGED_CLUSTERS.name} for the shape)"
        )
    if cluster:
        if cluster not in registry:
            raise ClusterError(
                f"unknown cluster: {cluster} (configured: {', '.join(sorted(registry))})"
            )
        return cluster
    from_env = os.environ.get(CLUSTER_ENV)
    if from_env:
        if from_env not in registry:
            raise ClusterError(
                f"${CLUSTER_ENV}={from_env} is not configured "
                f"(configured: {', '.join(sorted(registry))})"
            )
        return from_env
    flagged = [name for name, s in registry.items() if s.get("default")]
    if len(flagged) == 1:
        return flagged[0]
    if len(registry) == 1:
        return next(iter(registry))
    raise ClusterError(
        f"several clusters configured ({', '.join(sorted(registry))}) and no default. "
        f"Pass cluster=..., set ${CLUSTER_ENV}, or mark one 'default: true'"
    )


def get(cluster: str | None = None,
        registry: dict[str, dict[str, Any]] | None = None) -> tuple[str, dict[str, Any]]:
    """Resolve a cluster and return ``(name, settings)``, validated."""
    registry = load() if registry is None else registry
    name = resolve(cluster, registry)
    settings = registry[name]
    if not settings.get("ssh_host"):
        raise ClusterError(f"cluster {name!r} has no ssh_host; set one in {USER_CLUSTERS}")
    return name, settings


def remote_scratch(settings: dict[str, Any]) -> str:
    """Expand ``scratch_root``'s ``{user}`` placeholder."""
    user = settings.get("default_user") or ""
    if "{user}" in settings["scratch_root"] and not user:
        raise ClusterError(
            "scratch_root references {user} but no user is known; set default_user "
            f"in {USER_CLUSTERS} or export USER"
        )
    return settings["scratch_root"].format(user=user)
