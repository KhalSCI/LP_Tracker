"""Tests for the scouting reports as HTML pages."""
import json
from collections import Counter
from datetime import datetime

import pytest

import scouting
import scouting_web
from champions import champions
from config import TIMEZONE
from test_scouting import account, player, report_with, games, summarize, NOW


@pytest.fixture(autouse=True)
def champion_icons(monkeypatch):
    monkeypatch.setattr(champions, "version", "16.21.1")
    monkeypatch.setattr(champions, "id_by_name", {"Camille": "Camille", "Wukong": "MonkeyKing"})
    monkeypatch.setattr(champions, "by_id", {"Camille": "Camille", "MonkeyKing": "Wukong"})


def sample_report(**fields) -> dict:
    euw, eune = account("Sredni#pit", tier="MASTER"), account("Lumino#INT", "eun1", "DIAMOND")
    jungle = player(summarize(games("Wukong", 5, 1, role="Jungle") + games("Camille", 1, 1, queue=3130, role="Jungle"),
                              NOW, 6, {"wukong": {"presence": 0.33, "wr": 0.53}}), euw, eune)
    top = player(summarize(games("Camille", 2, 4), NOW, 6, {}), account("<b>Bad</b>#EUW"))
    history = {"games": 2, "wins": 1, "players": Counter(), "picks": Counter({"Camille": 2}), "bans": Counter()}
    defaults = {
        "team": "Średni & Łobuzy", "summary": "• banujcie **Wukonga**\n• uważać na <jungle>", "history": history,
        "pro_meta": {"wukong": {"champion": "Wukong", "picks": 10, "bans": 23, "presence": 0.33, "wr": 0.53}},
    }
    return report_with([top, jungle, {"riot_id": "X#1", "error": "Nie znaleziono konta"}], **{**defaults, **fields})


class TestRender:
    """Test cases for the report page."""

    def test_page(self):
        page = scouting_web.render_report(sample_report(), index_url="./")

        assert page.startswith("<!doctype html>") and '<meta name="robots" content="noindex,nofollow">' in page
        assert "Scouting: <span>Średni &amp; Łobuzy</span>" in page
        assert "<li>banujcie <strong>Wukonga</strong></li>" in page and "uważać na &lt;jungle&gt;" in page
        assert "&lt;b&gt;Bad&lt;/b&gt;#EUW" in page and "<b>Bad</b>" not in page
        assert "https://ddragon.leagueoflegends.com/cdn/16.21.1/img/champion/MonkeyKing.png" in page
        assert "2 konta: jeden gracz" in page and "SoloQ nie liczone" in page
        assert "Nie znaleziono konta" in page
        assert 'class="wr good">83%' in page  # Wukong 5-1
        assert "Przeciwko nam" in page and "Camille ×2" in page
        assert "Meta pro" in page and "33% P+B · 53% WR" in page

    def test_draft_plan_sections(self):
        html = scouting_web.render_summary("### Bany – faza 1\n• **Viktor** — komfort\n• Wukong\n### Plan\nNa blue: <B1>")
        assert html == ("<h3>Bany – faza 1</h3><ul><li><strong>Viktor</strong> — komfort</li><li>Wukong</li></ul>"
                        "<h3>Plan</h3><p>Na blue: &lt;B1&gt;</p>")

    def test_answers_and_tags(self):
        report = sample_report()
        report["bans"][0]["tags"] = ["komfort", "kontruje nas"]
        report["bans"][0]["counter"] = "Syndra ma 45% (600 gier)"
        report["answers"] = [{"role": "Mid", "player": "Maly", "our_players": ["iym"], "picks": [
            {"champion": "Wukong", "tourney": 3, "soloq": 10, "answers": [
                {"champion": "Camille", "wr": 0.62, "games": 900, "ours": {"scrims": (2, 1), "soloq": (0, 0)}}],
             "general": [{"champion": "Taliyah", "wr": 0.54, "games": 994, "ours": {"scrims": (0, 0), "soloq": (0, 0)}}]},
            {"champion": "Camille", "tourney": 0, "soloq": 4, "answers": [], "general": []},
        ]}]

        page = scouting_web.render_report(report)

        assert '<span class="tag comfort">🎯 komfort</span><span class="tag counter">⚔ kontruje nas</span>' in page
        assert "Syndra ma 45% (600 gier)" in page
        assert "<h2>Nasze odpowiedzi</h2>" in page and "3 turn. · 10 SoloQ" in page
        assert 'class="wr good">62%</span><small>900 gier · scrimy 1-1</small>' in page
        assert "brak danych o matchupach z naszą pulą" in page
        assert ('<span class="lbl">ogólnie</span><div class="chips general"><span class="chip answer">' in page
                and "<b>Taliyah</b>" in page and "<small>994 gry</small>" in page)

    def test_unknown_champion_gets_an_empty_icon(self):
        assert scouting_web.icon("Nowy Champion") == '<span class="icon"></span>'

    def test_slug(self):
        assert scouting_web.slugify("Średni & Łobuzy") == "sredni-lobuzy"
        assert scouting_web.slugify("!!!") == "druzyna"


class TestPublish:
    """Test cases for writing the pages."""

    def test_report_latest_page_and_index(self, tmp_path):
        first = sample_report(team="Cockpit", created=datetime(2026, 10, 30, 18, 0, tzinfo=TIMEZONE))
        second = sample_report(team="Cockpit", created=datetime(2026, 10, 31, 9, 30, tzinfo=TIMEZONE))
        other = sample_report(team="Francja", created=datetime(2026, 10, 30, 20, 0, tzinfo=TIMEZONE))

        urls = [scouting_web.publish_report(r, str(tmp_path), "https://example.com/scouting/abc") for r in (first, other, second)]

        assert urls[2] == "https://example.com/scouting/abc/cockpit-20261031-0930.html"
        assert {p.name for p in tmp_path.iterdir()} == {
            "cockpit-20261030-1800.html", "cockpit-20261031-0930.html", "cockpit.html",
            "francja-20261030-2000.html", "francja.html", "index.html", "reports.json"}
        assert (tmp_path / "cockpit.html").read_text() == (tmp_path / "cockpit-20261031-0930.html").read_text()
        assert len(json.loads((tmp_path / "reports.json").read_text())) == 3
        index = (tmp_path / "index.html").read_text()
        assert index.index("Cockpit") < index.index("Francja")  # newest team first
        assert 'href="cockpit.html"' in index and 'href="cockpit-20261030-1800.html">30.10 18:00' in index
        assert oct((tmp_path / "cockpit.html").stat().st_mode & 0o777) == "0o644"

    def test_republishing_the_same_report_keeps_one_entry(self, tmp_path):
        report = sample_report(team="Cockpit")
        scouting_web.publish_report(report, str(tmp_path), "https://example.com/s")
        scouting_web.publish_report(report, str(tmp_path), "https://example.com/s")
        assert len(json.loads((tmp_path / "reports.json").read_text())) == 1

    def test_off_without_settings(self, tmp_path):
        assert scouting_web.publish_report(sample_report(), "", "") is None

    def test_embed_links_the_page(self):
        embed = scouting.build_embed(sample_report(), "https://example.com/s/cockpit.html")
        assert embed.url == "https://example.com/s/cockpit.html"
        assert embed.fields[-1].name == "📄 Pełny raport" and embed.fields[-1].value == "https://example.com/s/cockpit.html"
