from compchem_memory.opencode_ingest import scrub_secrets


def test_scrub_secrets_handles_quoted_json_fields():
    body = (
        '{"api_key":"supersecretvalue",'
        '"password": "1234567890123456",'
        '"input_tokens": 12345678}'
    )
    redacted = scrub_secrets(body)
    assert "supersecretvalue" not in redacted
    assert "1234567890123456" not in redacted
    assert '"input_tokens": 12345678' in redacted


def test_scrub_secrets_preserves_hashes_and_run_ids():
    sha = "a" * 64
    body = (
        f"sha256={sha}\n"
        "run_id=haddock3_20260101_abcdef\n"
        "api_key=supersecretvalue\n"
        "Authorization: Bearer abcdefghijklmnop\n"
    )
    redacted = scrub_secrets(body)
    assert sha in redacted
    assert "haddock3_20260101_abcdef" in redacted
    assert "supersecretvalue" not in redacted
    assert "abcdefghijklmnop" not in redacted
