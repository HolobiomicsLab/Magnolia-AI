"""Harness-neutral hook layer.

Kept import-free on purpose: the CLI imports the behaviour modules it needs
lazily, so loading this package never pulls in a client adapter.
"""
