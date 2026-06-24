# tidy

[Claude Code](https://claude.com/claude-code) で使える、**安全なディレクトリ整理スキル**。

散らかったディレクトリを、**ファイルの中身を見て意味的に分類**し、安全に整理する。

- **まず診断**: `assess` で「このフォルダは skill向きか / 手動向きか / 触らない方がよいか」を判定。
  コードプロジェクトやシステム配下を誤って整理する事故を防ぐ
- **baseline 自動生成**: `suggest` で scan 結果から plan を自動で組み立て、人間/Claude は差分だけ
  直せばよい状態にする（ゼロから手書きしない）
- **コード依存を見る**: Python / JS / TS / HTML / CSS / Markdown の import や `src=`/`href=` を解析。
  `main.py` + `helper.py` のように相互参照しているコード群は1クラスタとしてまとめ、`suggest` は
  同じ subfolder に配置する。手編集でクラスタを分断したときは preview が警告して import 破壊を防ぐ
- **削除しない**: 不要ファイルはゴミ箱ではなく隔離ディレクトリ `_捨て/` へ移すだけ
- **集約 + 重複排除**: 講義資料などが複数の場所に散らばって重複しているのを、1か所に集めて
  同一内容（同 sha1）の重複を1つに絞る（残りは `_捨て/` へ隔離）
- **実行前に確認**: dry-run プレビューで計画を提示。`preview` は総サイズと隔離サイズも併記する
- **中断に強い**: 移動は1件ごとに記録するので、Ctrl-C や障害で止まっても `undo` で戻せる
- **整合性検証**: `verify` で from が消えて to が存在することを apply 直後にチェック
- **追跡 + 復元 + やり直し**: すべての移動を manifest に記録。`undo` で完全復元（集めたファイルも
  元の散らばっていた場所へ戻る）。やっぱり apply 後がよかったら `redo`
- **`_捨て/` の見回り**: `review` で隔離ファイルを一覧（隔離日時・元の場所・理由つき）、
  `--restore` で復元、`--purge --older-than 30` で古いものだけ物理削除（tidy 唯一の delete 経路、
  必ず `--yes` ゲート）
- **テスト済み**: `tests/run_tests.py` に round-trip・重複排除・衝突・安全性・中断耐性・診断・
  suggest・verify・redo・自己移動拒否・サイズ表示・**コード依存解析**（py/js/html/css）・
  **クラスタ分断警告**・**cross-target 単一ソース** ・ **review (list/restore/purge)** など
  **105 項目** の総合テスト
- **依存ゼロ**: Python 標準ライブラリのみ（Python 3.9+）

## インストール

このリポジトリ自体が1つのスキルです。`~/.claude/skills/tidy` として置けば使えます。

### 方法A: コピー
```bash
git clone https://github.com/satoryudev/tidy.git
cp -r tidy ~/.claude/skills/tidy
```

### 方法B: シンボリックリンク（リポジトリの更新が即反映される）
```bash
git clone https://github.com/satoryudev/tidy.git
ln -s "$(pwd)/tidy" ~/.claude/skills/tidy
```

インストール後、Claude Code で「このフォルダを整理して」「講義資料をまとめて」などと頼むと
自動で発火します。`/tidy` と打って明示的に呼ぶこともできます。

## CLI として単体利用（スキルを使わず手動で）

```bash
# 0. 診断（skill向きか手動向きか）
python3 scripts/organize.py assess "<対象dir>"
# 1. 走査（複数ディレクトリを並べると集約モード用にまとめてスキャン。コード依存も自動解析）
python3 scripts/organize.py scan "<対象dir>" [<対象dir2> ...] --out /tmp/scan.json
#    --no-deps を付けると import 解析を省略（巨大ディレクトリ向け）
# 2. baseline plan を自動生成（集約モードなら --target で宛先ルート指定）
python3 scripts/organize.py suggest --in /tmp/scan.json --out /tmp/plan.json [--target ~/Documents/講義資料]
# 3. プレビュー（移動しない・総サイズ表示）
python3 scripts/organize.py preview "<宛先ルート>" --in /tmp/plan.json
# 4. 実行
python3 scripts/organize.py apply "<宛先ルート>" --in /tmp/plan.json --yes
#    （または `apply --dry-run` で preview と同じ表示）
# 5. 整合性チェック
python3 scripts/organize.py verify "<宛先ルート>"
# 6. 元に戻す / やり直す
python3 scripts/organize.py undo "<宛先ルート>"
python3 scripts/organize.py redo "<宛先ルート>"   # 直前の undo を取り消し
# 7. _捨て の見回り（v5）
python3 scripts/organize.py review "<宛先ルート>"                         # 一覧（隔離日時・元の場所・理由）
python3 scripts/organize.py review "<宛先ルート>" --restore "*.pdf" --yes  # pattern 復元
python3 scripts/organize.py review "<宛先ルート>" --purge --older-than 30 --yes  # 古いものを物理削除（不可逆）
```

## テスト

一時的な仮想ディレクトリを作って結合テストを実行する（標準ライブラリのみ）:
```bash
python3 tests/run_tests.py
```

## ライセンス

MIT License（`LICENSE` 参照）。
