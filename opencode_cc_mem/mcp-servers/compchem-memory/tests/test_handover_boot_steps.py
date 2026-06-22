"""The boot worker runs the handover step between distillation and boot-context."""


def test_boot_steps_order_includes_handover(tmp_path):
    from compchem_memory import server

    steps = server._build_boot_steps(str(tmp_path), tmp_path)
    names = [name for name, _ in steps]
    assert names == ["startup_scan", "handover", "boot_context", "audit"]
