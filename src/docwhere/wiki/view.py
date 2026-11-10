"""The reader page for wiki hits: the same layout as for local documents.

Each hit shows its section (neighbours folded) with the terms and the answer marked, and each
quote passed in is found in the pages read and framed, with the sticky evidence list on the
side. Pages are not copied anywhere: the page is built from what the search just returned.
"""

from __future__ import annotations

import json
import time

from docwhere.extractors.html import html_chunks
from docwhere.model import norm
from docwhere.viewer import (
    PAGE_TEMPLATE,
    RAIL_QUOTE_CHARS,
    RESULT_BODY,
    STATIC_CSP,
    STYLE,
    esc,
    quote_probe,
    section_html,
)
from docwhere.wiki import Page


def build_wiki_html(
    hits: list[dict],
    pages: dict[tuple[str, str], Page],
    terms: list[str],
    context: int = 1,
    quotes: list[str] = (),
    answer: str = "",
    title: str = "",
) -> str:
    """``hits`` are WikiHit dicts, ``pages`` the pages they came from keyed by (wiki, page id)."""
    quotes = [q for q in (norm(q) for q in quotes) if q]
    sections: dict[tuple[str, str], list[dict]] = {}
    for key, page in pages.items():
        sections[key] = [
            {"id": i, "crumb": c.crumb, "title": c.title, "body": "\n".join(c.lines),
             "anchors": json.dumps(c.anchors), "fragment": next((a["fragment"] for a in c.anchors if a.get("fragment")), "")}
            for i, c in enumerate(html_chunks(page.html, page.title, page.root))
        ]

    def own_section(hit: dict) -> int | None:
        rows = sections.get((hit["wiki"], hit["page_id"])) or []
        text = (hit.get("anchor") or {}).get("text", "")
        for row in rows:
            if row["crumb"] == hit["crumb"] and (not text or text in row["body"].split("\n")):
                return row["id"]
        return next((row["id"] for row in rows if row["crumb"] == hit["crumb"]), 0 if rows else None)

    owns = {hit["n"]: own_section(hit) for hit in hits}
    evidence = [_locate(q, hits, sections, owns) for q in quotes]
    shown_title = title or " ".join(terms)
    answer_html = _answer(answer, evidence)
    rail = _rail(bool(answer_html), evidence, hits)
    cards = [_card(hit, pages.get((hit["wiki"], hit["page_id"])), sections.get((hit["wiki"], hit["page_id"])) or [],
                   owns[hit["n"]], context, quotes, {ev["section_id"] for ev in evidence if ev["hit"] is hit}) for hit in hits]
    body = RESULT_BODY.format(
        layout="layout has-rail" if rail else "layout",
        rail=rail,
        title=esc(shown_title),
        query=f'<span>社内 wiki</span><span>検索語 {esc(" ".join(terms))}</span><span>{esc(time.strftime("%Y-%m-%d %H:%M"))}</span>',
        answer=answer_html,
        cards=(f'<h2 class="region">ページ {len(hits)} 件</h2>' + "".join(cards)) if cards
        else '<p class="empty">一致するページはありませんでした。言い方を変えて検索し直してください。</p>',
    )
    return PAGE_TEMPLATE.format(csp=STATIC_CSP, title=esc(shown_title), style=STYLE, body=body)


def _target(hit: dict, section_id: int | None) -> str:
    return f"hit{hit['n']}" if section_id is None else f"w{hit['n']}-s{section_id}"


def _locate(quote: str, hits: list[dict], sections: dict, owns: dict) -> dict:
    """The hit and section whose text holds ``quote`` (its start, as for local files); the
    hit's own section is preferred, then the rest of its page, then the other pages."""
    probe = quote_probe(quote).lower()
    candidates = []
    for hit in hits:
        rows = sections.get((hit["wiki"], hit["page_id"])) or []
        own = owns[hit["n"]]
        ordered = sorted(rows, key=lambda r: (r["id"] != own, abs(r["id"] - (own or 0))))
        candidates += [(hit, row) for row in ordered]
    for hit, row in candidates:
        if probe and probe in row["body"].lower():
            target = _target(hit, row["id"])
            return {"quote": quote, "hit": hit, "section": row["crumb"], "section_id": row["id"], "target": target,
                    "fragment": row["fragment"]}
    return {"quote": quote, "hit": None, "section": "", "section_id": None, "target": "", "fragment": ""}


def _url(hit: dict, fragment: str) -> str:
    base = hit["url"].split("#", 1)[0]
    from urllib.parse import quote

    return base + (f"#{quote(fragment)}" if fragment else "")


def _readable(url: str) -> str:
    """The address as people read it (page names in their own letters); links keep it encoded."""
    from urllib.parse import unquote

    from docwhere.model import norm

    return norm(unquote(url))


def _open_controls(url: str) -> str:
    """In the window a button (the bridge opens the page in the browser); in a browser tab,
    where the page has no script, a plain link."""
    return (f'<button type="button" class="open-wiki" data-live hidden data-open-url="{esc(url)}">ブラウザで開く</button>'
            f'<a class="open-wiki" data-static href="{esc(url)}" target="_blank" rel="noopener noreferrer">wiki で開く</a>')


def _citation(ev: dict) -> str:
    hit = ev["hit"]
    where = [f"社内 wiki「{hit['title']}」"]
    section = ev["section"].split(" > ", 1)[1] if " > " in ev["section"] else ""
    if section:
        where.append(section)
    return f"「{ev['quote']}」（{'、'.join(where)}）"


def _answer(answer: str, evidence: list[dict]) -> str:
    if not (answer or evidence):
        return ""
    items = []
    for ev in evidence:
        hit = ev["hit"]
        if hit is None:
            items.append('<div class="evidence"><p class="missing">読んだページの中で位置を特定できませんでした。</p>'
                         f'<blockquote>{esc(ev["quote"])}</blockquote></div>')
            continue
        section = ev["section"].split(" > ", 1)[1] if " > " in ev["section"] else ""
        parts = [f'<span class="file">{esc(hit["title"])}</span>']
        if section:
            parts.append(f'<a href="#{ev["target"]}">{esc(section)}</a>')
        button = f'<button type="button" class="cite" data-live hidden data-cite="{esc(_citation(ev))}">引用をコピー</button>'
        source = f'<p class="source">{"".join(parts)}{_open_controls(_url(hit, ev["fragment"]))}{button}</p>'
        shown = f'<a class="quote" href="#{ev["target"]}"><blockquote>{esc(ev["quote"])}</blockquote></a>'
        items.append(f'<div class="evidence">{source}{shown}</div>')
    head = f'<h2 class="evidence-head">根拠 {len(evidence)} 件</h2>' if len(evidence) > 1 else ""
    text = answer or evidence[0]["quote"]
    return (f'<section class="answer" id="answer" aria-label="回答"><p class="answer-text"><span>{esc(text)}</span></p>'
            f'{head}{"".join(items)}</section>')


def _rail(has_answer: bool, evidence: list[dict], hits: list[dict]) -> str:
    if not evidence and len(hits) < 2:
        return ""
    parts = ['<a class="to-answer" href="#answer">回答へ戻る</a>'] if has_answer else []
    if evidence:
        items = []
        for ev in evidence:
            quote = ev["quote"] if len(ev["quote"]) <= RAIL_QUOTE_CHARS else ev["quote"][:RAIL_QUOTE_CHARS] + "…"
            if ev["hit"] is None:
                items.append(f'<li><span class="item unplaced"><span class="pg">位置不明</span><span class="q">{esc(quote)}</span></span></li>')
                continue
            items.append(f'<li><a class="item" href="#{ev["target"]}"><span class="pg">{esc(ev["hit"]["title"])}</span>'
                         f'<span class="q">{esc(quote)}</span><span class="sec">{esc(ev["section"].split(" > ")[-1])}</span></a></li>')
        parts.append(f'<h2>根拠</h2><ol class="evidence-list">{"".join(items)}</ol>')
    if len(hits) > 1:
        items = "".join(
            f'<li><a class="item" href="#hit{h["n"]}"><span class="pg">{esc(h["wiki"])}</span>'
            f'<span class="q">{esc(h["title"])}</span><span class="sec">{esc(h["crumb"].split(" > ")[-1])}</span></a></li>'
            for h in hits
        )
        parts.append(f'<h2>ページ {len(hits)} 件</h2><ol class="candidate-list">{items}</ol>')
    return f'<nav class="rail" aria-label="根拠と候補">{"".join(parts)}</nav>'


def _card(hit: dict, page: Page | None, rows: list[dict], own: int | None, context: int, quotes: list[str],
          evidence_sections: set) -> str:
    out = []
    if own is None or page is None:
        out.append('<p class="empty">このページを読めませんでした。wiki で開いて確かめてください。</p>')
    else:
        near = [r for r in rows if abs(r["id"] - own) <= context or r["id"] in evidence_sections]
        for row in near:
            mine = row["id"] == own
            body = section_html(row, hit["terms"], (hit.get("anchor") or {}).get("text") if mine else None, quotes)
            heading = esc(row["crumb"] or row["title"] or "見出しなし")
            sid = f' id="{_target(hit, row["id"])}"'
            if mine or row["id"] in evidence_sections:
                out.append(f'<section class="own"{sid}><h3>{heading}</h3>{body}</section>')
            else:
                out.append(f'<details class="section"{sid}><summary>{heading}</summary>{body}</details>')
    facts = [f"<span>社内 wiki {esc(hit['wiki'])}</span>"]
    if hit.get("space"):
        facts.append(f"<span>スペース {esc(hit['space'])}</span>")
    if hit.get("updated"):
        facts.append(f"<span>更新 {esc(hit['updated'][:10])}</span>")
    fragment = next((r["fragment"] for r in rows if r["id"] == own), "") if own is not None else ""
    return f"""
<article class="hit" id="hit{hit['n']}">
  <header>
    <h2>{esc(hit['title'])}</h2>
    <p class="crumb">{esc(hit['crumb'])}</p>
    <p class="facts">{''.join(facts)}</p>
    <p class="actions">{_open_controls(_url(hit, fragment))}</p>
    <p class="path"><span class="copy" title="クリックで全体を選択">{esc(_readable(hit['url']))}</span><span class="hint">クリックで選択して Ctrl+C でコピー</span></p>
  </header>
  <div class="extract">{''.join(out)}</div>
</article>"""
