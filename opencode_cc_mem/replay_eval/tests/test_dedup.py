from replay_eval.runner import is_duplicate


def test_obvious_duplicate():
    prior = ["Boot handover block is tail-sliced when budget exceeded"]
    assert is_duplicate("Boot handover block tail-sliced on overflow", prior)


def test_different_lesson_not_duplicate():
    prior = ["GPX4 curation stall was a project dormancy artifact"]
    assert not is_duplicate("Kimi judge model id must be k3 not kimi-k3-preview", prior)


def test_shared_two_tokens_but_low_ratio():
    # 2 shared significant tokens but ratio < 0.6 -> not a duplicate
    a = "handover merge skips unchanged sessions via sealed cursors"
    b = "handover timer interval default twenty minutes env tunable"
    assert not is_duplicate(a, [b])


def test_pure_digit_tokens_ignored():
    # Identical digit-heavy strings: digits are dropped, so only "slices"
    # survives (1 shared token < 2) -> NOT a duplicate. If digits were
    # counted this would be ratio 1.0 and flag as duplicate.
    assert not is_duplicate("21 slices 2026", ["2026 21 slices"])


def test_empty_title_never_duplicate():
    assert not is_duplicate("", ["anything at all here"])
