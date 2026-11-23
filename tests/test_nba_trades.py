from datetime import datetime, timezone

from app.nba_trades import _article


def parse(title, url="https://www.espn.com/nba/story/_/id/123/example"):
    return _article(title, "", url, datetime(2026, 11, 23, tzinfo=timezone.utc), "ESPN", {"www.espn.com"})


def test_trade_filter_excludes_metaphors_and_offsite_articles():
    assert parse("Players trade shots over Hall of Fame voting") is None
    assert parse("NBA trade rumors", "https://example.com/nba") is None
    assert parse("NBA trade request")["kind"] == "Coverage"


def test_proposal_is_analysis():
    assert parse("NBA trade proposal for the deadline")["kind"] == "Analysis"
