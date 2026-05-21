"""Pure-function tests for the save_draft tweet parsing helpers."""

from scripts.x_threads.save_draft import _check_tweet_lengths, _parse_tweets


def test_parse_tweets_splits_on_separator() -> None:
    """Tweets file split by '---' lines produces one tweet per block."""
    raw = (
        "Lead tweet.\nTwo lines in one tweet.\n---\nSecond tweet.\n---\nThird tweet.\n"
    )
    tweets = _parse_tweets(raw)
    assert tweets == [
        "Lead tweet.\nTwo lines in one tweet.",
        "Second tweet.",
        "Third tweet.",
    ]


def test_parse_tweets_ignores_blank_blocks() -> None:
    """Trailing separators and empty blocks don't yield empty tweets."""
    raw = "Only one.\n---\n---\n\n"
    tweets = _parse_tweets(raw)
    assert tweets == ["Only one."]


def test_check_tweet_lengths_flags_long_tweets() -> None:
    """Tweets over 280 chars surface as warnings with the index."""
    long_text = "x" * 281
    warnings = _check_tweet_lengths(["ok", long_text, "ok"])
    assert len(warnings) == 1
    assert warnings[0].startswith("tweet 2:")
    assert "281" in warnings[0]


def test_check_tweet_lengths_passes_short_tweets() -> None:
    """Short tweets produce no warnings."""
    warnings = _check_tweet_lengths(["a", "b" * 280])
    assert warnings == []
