# docwhere

フォルダ以下の PDF・Word・Excel・PowerPoint・テキストを**節単位**で全文検索し、該当箇所をそのまま開く CLI。
エージェントからは Agent Skills 形式のスキル `office-search`（[skills/office-search/SKILL.md](skills/office-search/SKILL.md)）経由で使う。

```
> docwhere search config 設定コマンド --in D:\docs
 1. 運用マニュアル.docx  3 コマンド > 3.2 config コマンド  (2026-08-01)
    config は設定ファイルを読み込み、値を検証する。
    D:\docs\運用マニュアル.docx
 2. 設計書_v1.pdf  2 CLI > 2.1 config  p.45  (2025-03-10)
    config コマンドは設定値を検証する
    D:\docs\旧版\設計書_v1.pdf
    ! newer version: 設計書_v2.docx
> docwhere show 1     # 節の全文
> docwhere open 1     # Word がその段落を選択した状態で開く
```

## インストール

```
uv tool install -e .
```

## コマンド

| コマンド | 内容 |
|---|---|
| `search TERM... [--in DIR] [--all] [--ext pdf,docx] [--json]` | 検索。実行前にフォルダの変更分だけインデックスを更新する |
| `show N [-C K]` | 直前の検索の N 件目の節を全文表示（`-C` で前後の節も） |
| `show --id ID [-C K]` | `search --json` の `id` で節を全文表示。複数の検索が並行していても番号がずれない |
| `open N` | N 件目を該当箇所で開く |
| `index DIR... [--rebuild]` | インデックスを明示的に作成・再作成 |
| `status [--errors]` | インデックス済みフォルダと、読めなかったファイル |

複数の TERM は OR（`--all` で AND）。見出しに語を含む節、複数の言い換えに当たる節が上位に来る。

## 対応形式と「該当箇所」

| 形式 | 検索単位 | 位置 | open |
|---|---|---|---|
| PDF | しおりがあれば章ごとのページ、なければ文字の大きさ・太字・「第N章」から推定した節 | p.N | Acrobat で該当ページ |
| docx / docm / doc | 見出しで区切った節 | 節の見出し | Word で該当段落を選択 |
| xlsx / xlsm / xls | シートの行（見出し行付き） | Sheet!D5 | Excel で該当セルを選択 |
| pptx / pptm / ppt | スライド（ノート含む） | slide N | PowerPoint で該当スライド |
| txt / md / csv / log など | md は見出し、他は一定量 | L行 | VS Code で該当行 |

- .doc / .xls / .ppt は Office（COM）で新形式に変換してから読む。変換中のマクロは強制無効。パスワード付きの .doc / .xls は読めないファイルとして扱う（.ppt は未検証）。
- `open` で開く文書のマクロは、ダブルクリックで開いたときと同じく Office のセキュリティ センターの設定に従う。
- 展開後のサイズが 1 GB を超える Office ファイル（zip 爆弾）は読まない。文書中の制御文字は取り除いてから保存・表示する。
- 画像だけのスキャン PDF は読めない。検索結果の JSON の `unreadable` と `status --errors` に毎回出る。
- ファイル名から版違い（`_v1.4` / `_第2版` / `_最終` / `- コピー`）を同じ文書とみなし、古い版には `newer_version` を付けて順位を下げる。日付は版とみなさない（議事録などを別物として扱うため）。

## 設計メモ

- インデックスは `%LOCALAPPDATA%\docwhere\index.db`（`DOCWHERE_DB` で変更可）。SQLite FTS5 の trigram トークナイザで、日本語も形態素解析なしで部分一致する。2 文字以下の語は LIKE で走査。
- 文字は NFKC 正規化して保存・検索するので、全角/半角の違いでは取りこぼさない。
- 更新判定は mtime + size。抽出処理を変えたら `extractors.EXTRACTOR_VERSION` を上げると、既存インデックスが自動で再抽出される。
- 新しい形式は `src/docwhere/extractors/` にモジュールを足し、`@register(".ext")` を付けて `_MODULES` に登録する。

## 開発

```
uv sync
uv run pytest
```

## ライセンス

MIT（[LICENSE](LICENSE)）。PDF の抽出に使う依存ライブラリ PyMuPDF は AGPL-3.0（または商用ライセンス）で提供されている。docwhere を PyMuPDF と一緒に再配布する場合は、そのライセンス条件にも従うこと。
