"""
Scouting reports as HTML pages, written to a folder a web server serves (khal-dev.com/scouting/<secret>/):
one page per scouting, a page per team that always shows its latest scouting, and an index of all of them.
"""
import json
import os
import re
import unicodedata
from datetime import datetime
from html import escape
from pathlib import Path

import scouting
from champions import champions
from config import SCOUTING_WEB_DIR, SCOUTING_WEB_URL
from roasts import games_count

MANIFEST = "reports.json"
# Champions per block on the page
PAGE_CHAMPIONS = 10
PRO_CHAMPIONS = 20
GOOD_WR, BAD_WR = 0.6, 0.4

STYLE = """
:root { --bg:#0b0e12; --surface:#12161c; --surface2:#171c23; --border:#232a33; --text:#e7ebef; --muted:#8b95a1;
        --accent:#3fd0b6; --ok:#4ade80; --bad:#f87171; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
a { color:var(--accent); }
main { max-width:1240px; margin:0 auto; padding:28px 20px 60px; }
h1 { margin:0 0 4px; font-size:28px; } h1 span { color:var(--accent); }
h2 { font-size:18px; margin:34px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--border); }
.meta { color:var(--muted); font-size:13px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
.summary ul { margin:0 0 6px; padding-left:18px; } .summary li { margin:4px 0; }
.summary h3 { font-size:15px; margin:14px 0 4px; color:var(--accent); } .summary h3:first-child { margin-top:0; }
.icon { width:28px; height:28px; border-radius:6px; vertical-align:middle; background:var(--surface2); flex:none; }
.icon.big { width:44px; height:44px; border-radius:8px; }
.bans { display:grid; grid-template-columns:repeat(auto-fill,minmax(225px,1fr)); gap:10px; }
.ban { display:flex; gap:10px; align-items:flex-start; padding:12px; } .ban .icon.big { width:38px; height:38px; }
.ban b { display:block; font-size:16px; } .who { color:var(--muted); font-size:13px; }
.line { font-size:13px; margin-top:3px; display:flex; gap:6px; align-items:baseline; white-space:nowrap; }
.line .lbl { color:var(--muted); min-width:54px; }
.player { margin-bottom:16px; padding:0; overflow:hidden; }
.player > header { padding:12px 16px; background:var(--surface2); border-bottom:1px solid var(--border); }
.player h3 { display:inline; font-size:18px; margin:0 8px 0 0; }
.role { display:inline-block; font:600 12px var(--mono); color:var(--bg); background:var(--accent); border-radius:4px;
        padding:2px 7px; margin-right:8px; vertical-align:2px; text-transform:uppercase; }
.accounts { margin-top:6px; display:flex; flex-wrap:wrap; gap:6px 14px; font-size:13px; color:var(--muted); }
.srv { font:600 11px var(--mono); border:1px solid var(--border); border-radius:4px; padding:0 5px; margin-right:4px; color:var(--text); }
.tag { display:inline-block; font-size:12px; border-radius:4px; padding:1px 7px; margin-left:4px; }
.tag.warn { background:rgba(248,113,113,.15); color:var(--bad); } .tag.note { background:rgba(139,149,161,.15); color:var(--muted); }
.tag.hint { background:rgba(250,204,21,.12); color:#facc15; }
.tags { margin:2px 0 4px; display:flex; flex-wrap:wrap; gap:4px; } .tags .tag { margin-left:0; }
.tag.comfort { background:rgba(63,208,182,.14); color:var(--accent); } .tag.counter { background:rgba(248,113,113,.15); color:var(--bad); }
.tag.meta { background:rgba(167,139,250,.16); color:#a78bfa; }
.answers { display:grid; gap:10px; } .answer-role { padding:0; overflow:hidden; }
.answer-role > header { padding:8px 14px; background:var(--surface2); border-bottom:1px solid var(--border); font-size:13px; color:var(--muted); }
.answer-role > header b { color:var(--text); font-size:14px; margin-right:6px; }
.pick { display:grid; grid-template-columns:minmax(150px,220px) 1fr; gap:10px; padding:8px 14px; border-top:1px solid var(--border); align-items:center; }
.pick:first-of-type { border-top:none; } .pick .them { display:flex; align-items:center; gap:8px; }
.pick .them small { display:block; color:var(--muted); font-size:12px; }
.pick .chips .chip small { color:var(--muted); margin-left:2px; }
@media (max-width:700px) { .pick { grid-template-columns:1fr; } }
.blocks { display:grid; grid-template-columns:repeat(3,1fr); gap:0; }
.block { padding:12px 16px; border-left:1px solid var(--border); } .block:first-child { border-left:none; }
.block h4 { margin:0 0 8px; font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
.block h4 small { text-transform:none; letter-spacing:0; margin-left:6px; }
.block .empty { color:var(--muted); font-size:13px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:right; color:var(--muted); font-weight:500; padding:2px 4px; } th:first-child { text-align:left; }
td { text-align:right; padding:3px 4px; border-top:1px solid var(--border); white-space:nowrap; font-family:var(--mono); }
td.champ { text-align:left; font-family:inherit; display:flex; align-items:center; gap:8px; }
.wr { display:inline-block; min-width:44px; padding:0 4px; border-radius:4px; }
.wr.good { color:var(--ok); background:rgba(74,222,128,.12); } .wr.bad { color:var(--bad); background:rgba(248,113,113,.12); }
.chips { display:flex; flex-wrap:wrap; gap:6px; } .chip { display:inline-flex; align-items:center; gap:6px; background:var(--surface2);
  border:1px solid var(--border); border-radius:20px; padding:2px 10px 2px 3px; font-size:13px; }
.chip .icon { width:22px; height:22px; border-radius:50%; }
.pro { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:8px; }
.pro .row { display:flex; align-items:center; gap:10px; }
.bar { flex:1; height:6px; background:var(--surface2); border-radius:3px; overflow:hidden; }
.bar i { display:block; height:100%; background:var(--accent); }
.num { font:12px var(--mono); color:var(--muted); min-width:84px; text-align:right; }
footer { margin-top:40px; color:var(--muted); font-size:12px; }
.teams { display:grid; gap:10px; } .team { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.team .older { font-size:13px; color:var(--muted); }
@media (max-width:900px) { .blocks { grid-template-columns:1fr; } .block { border-left:none; border-top:1px solid var(--border); }
  .block:first-child { border-top:none; } main { padding:18px 12px 40px; } }
"""


TAG_CLASSES = {"komfort": "comfort", "kontruje nas": "counter", "meta": "meta"}
TAG_LABELS = {"komfort": "🎯 komfort", "kontruje nas": "⚔ kontruje nas", "meta": "📈 meta"}


def slugify(name: str) -> str:
    """ASCII file name part of a team name: "Średni Kukaracza" -> "sredni-kukaracza"."""
    ascii_name = unicodedata.normalize("NFKD", name.replace("ł", "l").replace("Ł", "L")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "druzyna"


def decimal(value: float, digits: int = 2) -> str:
    """Polish decimal comma: 1.5 -> "1,50"."""
    return f"{value:.{digits}f}".replace(".", ",")


def wr_cell(wr: float | None) -> str:
    if wr is None:
        return ""
    kind = "good" if wr >= GOOD_WR else "bad" if wr <= BAD_WR else ""
    return f'<span class="wr {kind}">{scouting.pct(wr)}</span>'


def icon(champion: str, big: bool = False) -> str:
    url = champions.icon_url(champion)
    if not url:
        return f'<span class="icon{" big" if big else ""}"></span>'
    return f'<img class="icon{" big" if big else ""}" src="{escape(url)}" alt="" loading="lazy">'


def inline_markdown(text: str) -> str:
    """Escaped text with the model's **bold** as real bold."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escape(text))


def render_summary(summary: str) -> str:
    """The draft plan: "### " lines as headings, "• " lines as list items, the rest as paragraphs."""
    parts, items = [], []

    def close_list():
        if items:
            parts.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
            items.clear()

    for line in summary.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            close_list()
            parts.append(f"<h3>{inline_markdown(line.lstrip('#').strip())}</h3>")
        elif line[0] in "•-*" and line[1:2] == " ":
            items.append(inline_markdown(line[2:].strip()))
        else:
            close_list()
            parts.append(f"<p>{inline_markdown(line)}</p>")
    close_list()
    return "".join(parts)


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"pl\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex,nofollow\">"
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>"
    )


def champion_rows(table: list[dict]) -> str:
    """A champion block's table: champion, games, W-L, winrate, KDA, pro presence."""
    if not table:
        return '<p class="empty">brak gier</p>'
    rows = "".join(
        f"<tr><td class=\"champ\">{icon(c['champion'])}{escape(c['champion'])}</td><td>{c['games']}</td>"
        f"<td>{c['wins']}-{c['losses']}</td><td>{wr_cell(c['wr'])}</td><td>{decimal(c['kda'])}</td>"
        f"<td title=\"Pro WR: {scouting.pct(c['pro_wr']) or '-'}\">{scouting.pct(c['pro_presence'])}</td></tr>"
        for c in table[:PAGE_CHAMPIONS]
    )
    return f"<table><tr><th>Champion</th><th>Gry</th><th>W-L</th><th>WR</th><th>KDA</th><th>Pro P+B</th></tr>{rows}</table>"


def block(title: str, count: str, content: str) -> str:
    small = f"<small>{escape(count)}</small>" if count else ""
    return f'<div class="block"><h4>{escape(title)}{small}</h4>{content}</div>'


def player_card(player: dict) -> str:
    if player.get("error"):
        return f'<article class="card player"><header>{escape(player["riot_id"])} <span class="tag warn">{escape(player["error"])}</span></header></article>'
    s = player["summary"]
    merged = len(player["accounts"]) > 1
    accounts = "".join(
        f'<span><span class="srv">{escape(scouting.server_name(a["platform"]))}</span>{escape(a["riot_id"])} · '
        f'{escape(scouting.rank_text(a["ranks"].get("solo")))}'
        + (f'<span class="tag note">SoloQ nie liczone</span>' if merged and a["policy"]["skipped"] else "")
        + (f'<span class="tag hint">{escape(a["role_note"])}</span>' if a.get("role_note") else "")
        + "</span>"
        for a in player["accounts"]
    )
    tags = ""
    if merged:
        tags += f'<span class="tag note">{len(player["accounts"])} konta: jeden gracz</span>'
    if s["one_trick"]:
        tags += f'<span class="tag warn">one-trick: {escape(s["one_trick"]["champion"])} ({scouting.pct(s["one_trick"]["share"])} gier)</span>'

    if s["ranked_skipped"]:
        blocks = block(s["ranked_label"], "", f'<p class="empty">{escape(s["ranked_skipped"])}</p>')
    else:
        blocks = block(f"{s['ranked_label']} · {s['months']} mies.", games_count(s["ranked_games"]), champion_rows(s["ranked"]))
        if s["recent_months"]:
            blocks += block(f"{s['ranked_label']} · {s['recent_months']} mies.", games_count(s["recent_games"]),
                            champion_rows(s["recent"]))
    blocks += block(f"Turnieje · {s['tourney_months']} mies.", games_count(s["tournament_games"]), champion_rows(s["tournament"]))
    return (
        f'<article class="card player"><header><span class="role">{escape(s["role"] or "?")}</span>'
        f'<h3>{escape(player["name"])}</h3>{tags}<div class="accounts">{accounts}</div></header>'
        f'<div class="blocks">{blocks}</div></article>'
    )


def ban_card(ban: dict) -> str:
    tags = "".join(f'<span class="tag {TAG_CLASSES[t]}">{TAG_LABELS[t]}</span>' for t in ban["tags"])
    lines = ""
    if ban["ranked_games"]:
        lines += f'<div class="line"><span class="lbl">SoloQ</span>{games_count(ban["ranked_games"])} · {wr_cell(ban["ranked_wr"])}</div>'
    if ban["tourney_games"]:
        lines += f'<div class="line"><span class="lbl">Turnieje</span>{games_count(ban["tourney_games"])} · {wr_cell(ban["tourney_wr"])}</div>'
    if ban.get("counter"):
        lines += f'<div class="line"><span class="lbl">Kontra</span>{escape(ban["counter"])}</div>'
    if ban.get("pro_presence") is not None:
        turn = f' · tura bana ~{ban["pro_ban_turn"]:.0f}' if ban.get("pro_ban_turn") else ""
        lines += f'<div class="line"><span class="lbl">Pro P+B</span>{scouting.pct(ban["pro_presence"])}{turn}</div>'
    who = " · ".join(part for part in (ban["player"], ban["role"]) if part)
    return (
        f'<div class="card ban">{icon(ban["champion"], big=True)}<div><b>{escape(ban["champion"])}</b>'
        f'<span class="who">{escape(who)}</span><div class="tags">{tags}</div>{lines}</div></div>'
    )


def answers_section(answers: list[dict]) -> str:
    """Per role: the opponent's likeliest picks and our answers with matchup win rates."""
    roles = ""
    for role in answers:
        picks = ""
        for pick in role["picks"]:
            counts = " · ".join(part for part in (
                f"{pick['tourney']} turn." if pick["tourney"] else "", f"{pick['soloq']} SoloQ" if pick["soloq"] else "") if part)
            chips_html = "".join(
                f'<span class="chip">{icon(a["champion"])}{escape(a["champion"])} {wr_cell(a["wr"])}'
                f'<small>{a["games"]}'
                + (f' · scrimy {scouting.record_text(a["ours"]["scrims"])}' if a["ours"]["scrims"][0] else "")
                + (f' · SoloQ {scouting.record_text(a["ours"]["soloq"])}' if a["ours"]["soloq"][0] else "")
                + "</small></span>"
                for a in pick["answers"]
            ) or '<span class="meta">brak danych o matchupach z naszą pulą</span>'
            picks += (f'<div class="pick"><div class="them">{icon(pick["champion"])}<div>{escape(pick["champion"])}'
                      f'<small>{escape(counts)}</small></div></div><div class="chips">{chips_html}</div></div>')
        roles += (
            f'<article class="card answer-role"><header><span class="role">{escape(role["role"])}</span>'
            f'<b>{escape(role["player"])}</b> nasi: {escape(", ".join(role["our_players"]) or "-")}</header>{picks}</article>'
        )
    return f'<section class="answers">{roles}</section>'


def pro_timing(champion: dict) -> str:
    """"tura bana ~2 · runda picku 1,3": average pro ban turn and pick round."""
    parts = []
    if champion.get("ban_turn"):
        parts.append(f"tura bana ~{champion['ban_turn']:.0f}")
    if champion.get("pick_round"):
        parts.append(f"runda picku {decimal(champion['pick_round'], 1)}")
    return " · ".join(parts)


def pro_details(champion: dict) -> str:
    return f"{champion['picks']} picków, {champion['bans']} banów"


def chips(counter, label: str) -> str:
    items = "".join(
        f'<span class="chip">{icon(champion)}{escape(champion)}{f" ×{n}" if n > 1 else ""}</span>'
        for champion, n in counter.most_common()
    )
    return f'<div class="line"><span class="lbl">{escape(label)}</span></div><div class="chips">{items or "brak danych"}</div>'


def render_report(report: dict, index_url: str = "") -> str:
    """The whole scouting report as one HTML page."""
    title = f"Scouting: {report['team']}"
    meta = (
        f"{report['created']:%d.%m.%Y %H:%M} · SoloQ z {report['months']} mies. (konta spoza EUW od Mastera) · "
        f"turnieje z {report['tourney_months']} mies. · {scouting.pro_note(report)}"
    )
    if index_url:
        meta += f' · <a href="{escape(index_url)}">wszystkie raporty</a>'
    body = f'<h1>Scouting: <span>{escape(report["team"])}</span></h1><p class="meta">{meta}</p>'

    if report["summary"]:
        body += f'<h2>Najważniejsze</h2><section class="card summary">{render_summary(report["summary"])}</section>'

    body += "<h2>Bany</h2>"
    body += (f'<section class="bans">{"".join(ban_card(b) for b in report["bans"])}</section>' if report["bans"]
             else '<p class="meta">Za mało gier, żeby coś zasugerować.</p>')

    if report.get("answers"):
        body += ('<h2>Nasze odpowiedzi</h2><p class="meta">Ich najczęstsze picki na każdej roli (turnieje, potem SoloQ) i '
                 'najlepsze odpowiedzi z naszej puli na tej roli: WR matchupu na Emerald+ (OP.GG), liczba gier i nasze wyniki '
                 'w tym matchupie.</p>' + answers_section(report["answers"]))

    body += "<h2>Gracze</h2>" + "".join(player_card(p) for p in report["players"])

    history = report["history"]
    if history["games"]:
        body += (
            f'<h2>Przeciwko nam</h2><section class="card"><div>{scouting.maps_count(history["games"])}, nasz bilans '
            f'{history["wins"]}-{history["games"] - history["wins"]}</div>{chips(history["picks"], "Grali")}'
            f'{chips(history["bans"], "Banowali")}</section>'
        )

    if report["pro_meta"]:
        top = sorted(report["pro_meta"].values(), key=lambda c: -(c["presence"] or 0))[:PRO_CHAMPIONS]
        rows = "".join(
            f'<div class="row" title="{escape(pro_details(c))}">{icon(c["champion"])}<span style="min-width:96px">{escape(c["champion"])}'
            f'<br><small class="meta">{escape(scouting.role_list(c.get("roles") or {}))}</small></span>'
            f'<span class="bar"><i style="width:{round((c["presence"] or 0) * 100)}%"></i></span>'
            f'<span class="num">{scouting.pct(c["presence"])} P+B{" · " + scouting.pct(c["wr"]) + " WR" if c["wr"] is not None else ""}'
            f'<br>{escape(pro_timing(c))}</span></div>'
            for c in top
        )
        body += (f'<h2>Meta pro: obecność w draftach (pick + ban)</h2><p class="meta">gol.gg, patche '
                 f'{escape(", ".join(report["pro_patches"]))}, {report["pro_games"]} gier</p><section class="pro">{rows}</section>')

    body += "<footer>LP Tracker · dane: Riot API (SoloQ i gry z kodów turniejowych), gol.gg (gra profesjonalna)</footer>"
    return page(title, body)


def render_index(entries: list[dict]) -> str:
    """Index page: each team's latest report first, with links to its earlier ones."""
    teams = {}
    for entry in sorted(entries, key=lambda e: e["created"], reverse=True):
        teams.setdefault(entry["team"].lower(), []).append(entry)
    rows = ""
    for reports in teams.values():
        latest = reports[0]
        created = datetime.fromisoformat(latest["created"])
        older = " · ".join(
            f'<a href="{escape(r["file"])}">{datetime.fromisoformat(r["created"]):%d.%m %H:%M}</a>' for r in reports[1:6]
        )
        rows += (
            f'<div class="card team"><div><b><a href="{escape(latest["latest"])}">{escape(latest["team"])}</a></b>'
            f'<div class="meta">{created:%d.%m.%Y %H:%M} · SoloQ {latest["months"]} mies., turnieje {latest["tourney_months"]} mies.</div></div>'
            + (f'<div class="older">wcześniejsze: {older}</div>' if older else "") + "</div>"
        )
    body = '<h1>Scouting <span>PYRY</span></h1><p class="meta">Raporty przeciwników z bota LP Tracker</p>'
    body += f'<section class="teams">{rows or "<p class=meta>Brak raportów.</p>"}</section>'
    return page("Scouting PYRY", body)


def write_file(path: Path, text: str) -> None:
    """Write so the web server never serves a half-written file."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, path)


def publish_report(report: dict, directory: str = SCOUTING_WEB_DIR, base_url: str = SCOUTING_WEB_URL) -> str | None:
    """
    Write the report's page, its team's latest page and the index. Returns the report's URL,
    or None when publishing isn't configured.
    """
    if not directory or not base_url:
        return None
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    slug = slugify(report["team"])
    file = f"{slug}-{report['created']:%Y%m%d-%H%M}.html"
    latest = f"{slug}.html"
    html = render_report(report, index_url="./")
    write_file(folder / file, html)
    write_file(folder / latest, html)

    manifest = folder / MANIFEST
    entries = json.loads(manifest.read_text()) if manifest.exists() else []
    entries = [e for e in entries if e["file"] != file]
    entries.append({
        "team": report["team"], "file": file, "latest": latest, "created": report["created"].isoformat(),
        "months": report["months"], "tourney_months": report["tourney_months"],
    })
    write_file(manifest, json.dumps(entries, ensure_ascii=False, indent=1))
    write_file(folder / "index.html", render_index(entries))
    return f"{base_url}/{file}"
