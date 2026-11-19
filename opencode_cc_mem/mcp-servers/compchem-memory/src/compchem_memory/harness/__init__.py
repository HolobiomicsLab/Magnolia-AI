"""Harness-neutral configuration and launch contracts."""

from compchem_memory.harness.protocol import RENDERERS, Launch, LaunchContext
from compchem_memory.harness import render_claude, render_codex, render_vibe

RENDERERS["claude"] = render_claude.render
RENDERERS["codex"] = render_codex.render
RENDERERS["vibe"] = render_vibe.render

__all__ = ["Launch", "LaunchContext", "RENDERERS"]
