from compchem_tools.tools.poller import _auto_retry_enabled


def test_campaign_tag_disables_background_retry(monkeypatch):
    monkeypatch.delenv("MAGNOLIA_AUTO_RETRY", raising=False)
    assert _auto_retry_enabled({"system_tags": ["campaign"]}) is False
    assert _auto_retry_enabled({"system_tags": ["other"]}) is True


def test_global_retry_disable_still_applies(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_AUTO_RETRY", "0")
    assert _auto_retry_enabled({"system_tags": ["other"]}) is False
