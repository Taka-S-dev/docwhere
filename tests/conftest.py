"""Build one small document of every supported kind, with the same command documented in each."""

from __future__ import annotations

from pathlib import Path

import pytest

from docwhere.index import Index


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    _word(root / "運用マニュアル.docx")
    _pdf(root / "sub" / "設計書.pdf")
    _excel(root / "コマンド一覧.xlsx")
    _powerpoint(root / "説明会.pptx")
    (root / "notes.md").write_text("# Intro\nhello\n\n## deploy コマンド\ndeploy は本番へ反映する。\n", encoding="utf-8")
    (root / "~$運用マニュアル.docx").write_bytes(b"lock file")  # must be skipped
    return root


@pytest.fixture
def index(tmp_path: Path) -> Index:
    idx = Index(tmp_path / "index" / "index.db")
    yield idx
    idx.close()


def _word(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("はじめに", 1)
    doc.add_paragraph("この文書は運用手順をまとめたものです。config の話は後述。")
    doc.add_heading("コマンド", 1)
    doc.add_heading("config コマンド", 2)
    doc.add_paragraph("config は設定ファイルを読み込み、値を検証する。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "オプション"
    table.cell(0, 1).text = "説明"
    table.cell(1, 0).text = "--force"
    table.cell(1, 1).text = "検証エラーを無視する"
    doc.add_heading("backup コマンド", 2)
    doc.add_paragraph("backup はデータを退避する。")
    doc.save(path)


def _pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    for text in ("1 Overview\nsystem outline", "2 CLI\nconfig command reads settings", "config --force skips checks"):
        doc.new_page().insert_text((72, 72), text)
    doc.set_toc([[1, "1 Overview", 1], [1, "2 CLI", 2], [2, "2.1 config", 2]])
    doc.save(path)


def _excel(path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "CLI"
    ws.append(["コマンド", "概要"])
    ws.append(["config", "設定を検証する"])
    ws.append(["backup", "データ退避"])
    wb.save(path)


def _powerpoint(path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    for title, body in (("概要", "システムの全体像"), ("config の使い方", "設定ファイルを指定して実行")):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
    slide.notes_slide.notes_text_frame.text = "質疑で --force の説明をする"
    prs.slides[0].shapes.add_textbox(Inches(1), Inches(5), Inches(3), Inches(1)).text_frame.text = "ﾊﾝｶｸ ｶﾅ"
    prs.save(path)
