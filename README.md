# dir-organizer

[Claude Code](https://claude.com/claude-code) で使える、**安全なディレクトリ整理スキル**。

散らかったディレクトリを、**ファイルの中身を見て意味的に分類**し、安全に整理する。

- **まず診断**: `assess` で「このフォルダは skill向きか / 手動向きか / 触らない方がよいか」を判定。
  コードプロジェクトやシステム配下を誤って整理する事故を防ぐ
- **削除しない**: 不要ファイルはゴミ箱ではなく隔離ディレクトリ `_捨て/` へ移すだけ
- **集約 + 重複排除**: 講義資料などが複数の場所に散らばって重複しているのを、1か所に集めて
  同一内容（同 sha1）の重複を1つに絞る（残りは `_捨て/` へ隔離）
- **実行前に確認**: dry-run プレビューで計画を提示してから実行
- **中断に強い**: 移動は1件ごとに記録するので、途中で止まっても `undo` で戻せる
- **全部追える**: すべての移動を manifest に記録。`undo` でいつでも完全復元（集めたファイルも
  元の散らばっていた場所へ戻る）
- **テスト済み**: `tests/run_tests.py` に round-trip・重複排除・衝突・安全性・中断耐性・診断など
  35 項目の総合テスト
- **依存ゼロ**: Python 標準ライブラリのみ（Python 3.9+）

## インストール

このリポジトリ自体が1つのスキルです。`~/.claude/skills/dir-organizer` として置けば使えます。

### 方法A: コピー
```bash
git clone https://github.com/satoryudev/dir-organizer.git
cp -r dir-organizer ~/.claude/skills/dir-organizer
```

### 方法B: シンボリックリンク（リポジトリの更新が即反映される）
```bash
git clone https://github.com/satoryudev/dir-organizer.git
ln -s "$(pwd)/dir-organizer" ~/.claude/skills/dir-organizer
```

インストール後、Claude Code で「このフォルダを整理して」「講義資料をまとめて」などと頼むと
自動で発火します。`/dir-organizer` と打って明示的に呼ぶこともできます。

## CLI として単体利用（スキルを使わず手動で）

```bash
# 0. 診断（skill向きか手動向きか）
python3 scripts/organize.py assess "<対象dir>"
# 1. 走査（複数ディレクトリを並べると集約モード用にまとめてスキャン）
python3 scripts/organize.py scan "<対象dir>" [<対象dir2> ...] --out /tmp/scan.json
# 2. plan.json を用意（手書き or Claude に作らせる）
# 3. プレビュー（移動しない）
python3 scripts/organize.py preview "<宛先ルート>" --in /tmp/plan.json
# 4. 実行
python3 scripts/organize.py apply "<宛先ルート>" --in /tmp/plan.json --yes
# 5. 元に戻す
python3 scripts/organize.py undo "<宛先ルート>"
```

## テスト

一時的な仮想ディレクトリを作って結合テストを実行する（標準ライブラリのみ）:
```bash
python3 tests/run_tests.py
```

## ライセンス

MIT License（`LICENSE` 参照）。
