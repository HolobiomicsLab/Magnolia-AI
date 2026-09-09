"""Unit tests for compchem_tools.tools.clusters.

The registry exists so that ssh_slurm stops being an Azzurra client. What these
tests defend is that property: a second cluster can be added without touching
Python, a site profile can be adjusted one key at a time, and an ambiguous
request fails loudly instead of picking someone's cluster for them.
"""
from __future__ import annotations

import os

import pytest
import yaml

from compchem_tools.tools import clusters


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Point the registry at throwaway files so a real ~/.config cannot leak in."""
    packaged = tmp_path / "packaged.yaml"
    user = tmp_path / "user.yaml"
    monkeypatch.setattr(clusters, "PACKAGED_CLUSTERS", packaged)
    monkeypatch.setattr(clusters, "USER_CLUSTERS", user)
    monkeypatch.delenv(clusters.CLUSTERS_FILE_ENV, raising=False)
    monkeypatch.delenv(clusters.CLUSTER_ENV, raising=False)
    monkeypatch.setenv("USER", "someone")

    def write(path, **profiles):
        path.write_text(yaml.safe_dump({"clusters": profiles}))

    return packaged, user, write


# ── the packaged profile ────────────────────────────────────────────────────

def test_packaged_azzurra_keeps_its_site_facts():
    """Moved here from test_ssh_slurm: these are configuration now, not code.

    Read the PACKAGED file directly — a developer machine's user config
    (~/.config/magnolia/clusters.yaml) legitimately overrides the account.
    """
    azzurra = yaml.safe_load(clusters.PACKAGED_CLUSTERS.read_text())["clusters"]["azzurra"]

    assert azzurra["ssh_host"] == "azzurra"
    # No group account in the packaged (public) profile — that is per-user
    # config in ~/.config/magnolia/clusters.yaml.
    assert azzurra["default_account"] == ""
    # default_qos is intentionally empty (commit ad2aa09): Slurm auto-assigns QOS
    # from the association; an explicit --qos can trip QOSGrpCpuLimit on this site.
    assert azzurra["default_qos"] == ""
    assert azzurra["default_partition"] == "cpucourt"
    assert azzurra["tunnel_script"] == "hpc_tunnel.sh"
    assert azzurra["requires_control_master"] is True


def test_packaged_profiles_name_no_person():
    """A username is a fact about a person, not about a machine."""
    for name, profile in clusters.load().items():
        assert profile["default_user"] == os.environ.get("USER", ""), name


# ── layering ────────────────────────────────────────────────────────────────

def test_a_cluster_can_be_added_without_touching_python(isolated):
    packaged, user, write = isolated
    write(packaged, azzurra={"ssh_host": "azzurra"})
    write(user, jeanzay={"ssh_host": "jean-zay", "default_account": "abc@cpu"})

    registry = clusters.load()
    assert set(registry) == {"azzurra", "jeanzay"}
    assert registry["jeanzay"]["default_account"] == "abc@cpu"


def test_overriding_one_key_keeps_the_rest_of_the_profile(isolated):
    """Whole-profile replacement would silently drop Azzurra's tunnel script."""
    packaged, user, write = isolated
    write(packaged, azzurra={"ssh_host": "azzurra", "tunnel_script": "hpc_tunnel.sh",
                             "default_account": "testaccount"})
    write(user, azzurra={"default_account": "mine"})

    azzurra = clusters.load()["azzurra"]
    assert azzurra["default_account"] == "mine"
    assert azzurra["tunnel_script"] == "hpc_tunnel.sh"


def test_defaults_describe_a_plain_slurm_site(isolated):
    packaged, _user, write = isolated
    write(packaged, plain={"ssh_host": "login.example"})

    plain = clusters.load()["plain"]
    assert plain["default_account"] == ""      # no group account -> no --account
    assert plain["default_partition"] == ""    # no named partition -> no --partition
    assert plain["tunnel_script"] == ""        # reachable without a VPN dance
    assert plain["requires_control_master"] is True


def test_default_user_falls_back_to_the_environment(isolated, monkeypatch):
    packaged, _user, write = isolated
    write(packaged, plain={"ssh_host": "login.example"})
    monkeypatch.setenv("USER", "lfn")

    assert clusters.load()["plain"]["default_user"] == "lfn"


def test_explicit_file_overrides_everything(isolated, monkeypatch, tmp_path):
    packaged, _user, write = isolated
    write(packaged, plain={"ssh_host": "login.example"})
    override = tmp_path / "ci.yaml"
    write(override, plain={"ssh_host": "ci-login"})
    monkeypatch.setenv(clusters.CLUSTERS_FILE_ENV, str(override))

    assert clusters.load()["plain"]["ssh_host"] == "ci-login"


def test_a_broken_file_is_an_error_not_an_empty_registry(isolated):
    packaged, _user, _write = isolated
    packaged.write_text("clusters: [not, a, mapping]")

    with pytest.raises(clusters.ClusterError, match="mapping"):
        clusters.load()


# ── resolution ──────────────────────────────────────────────────────────────

def test_a_single_configured_cluster_needs_no_argument(isolated):
    packaged, _user, write = isolated
    write(packaged, only={"ssh_host": "h"})

    assert clusters.resolve() == "only"


def test_the_default_flag_picks_among_several(isolated):
    packaged, _user, write = isolated
    write(packaged, a={"ssh_host": "a"}, b={"ssh_host": "b", "default": True})

    assert clusters.resolve() == "b"


def test_the_environment_overrides_the_default_flag(isolated, monkeypatch):
    packaged, _user, write = isolated
    write(packaged, a={"ssh_host": "a"}, b={"ssh_host": "b", "default": True})
    monkeypatch.setenv(clusters.CLUSTER_ENV, "a")

    assert clusters.resolve() == "a"


def test_ambiguity_is_an_error_that_names_the_candidates(isolated):
    """Picking one would submit someone's job, and someone's budget, elsewhere."""
    packaged, _user, write = isolated
    write(packaged, a={"ssh_host": "a"}, b={"ssh_host": "b"})

    with pytest.raises(clusters.ClusterError) as exc:
        clusters.resolve()
    assert "a, b" in str(exc.value) and "no default" in str(exc.value)


def test_an_unknown_name_lists_what_is_configured(isolated):
    packaged, _user, write = isolated
    write(packaged, a={"ssh_host": "a"})

    with pytest.raises(clusters.ClusterError, match="configured: a"):
        clusters.resolve("typo")


def test_an_unconfigured_environment_variable_is_not_silently_ignored(isolated, monkeypatch):
    packaged, _user, write = isolated
    write(packaged, a={"ssh_host": "a"})
    monkeypatch.setenv(clusters.CLUSTER_ENV, "ghost")

    with pytest.raises(clusters.ClusterError, match="ghost"):
        clusters.resolve()


def test_an_empty_registry_says_where_to_put_a_cluster(isolated):
    with pytest.raises(clusters.ClusterError, match="no clusters configured"):
        clusters.resolve()


def test_get_rejects_a_profile_with_no_ssh_host(isolated):
    packaged, _user, write = isolated
    write(packaged, broken={"default_partition": "p"})

    with pytest.raises(clusters.ClusterError, match="ssh_host"):
        clusters.get()


# ── scratch paths ───────────────────────────────────────────────────────────

def test_remote_scratch_expands_the_user_placeholder():
    settings = {"scratch_root": "/workspace/{user}/magnolia", "default_user": "lfn"}
    assert clusters.remote_scratch(settings) == "/workspace/lfn/magnolia"


def test_remote_scratch_refuses_to_build_a_path_with_a_hole_in_it():
    settings = {"scratch_root": "/workspace/{user}/magnolia", "default_user": ""}
    with pytest.raises(clusters.ClusterError, match="no user is known"):
        clusters.remote_scratch(settings)


def test_a_scratch_root_without_a_placeholder_needs_no_user():
    settings = {"scratch_root": "/shared/magnolia", "default_user": ""}
    assert clusters.remote_scratch(settings) == "/shared/magnolia"
