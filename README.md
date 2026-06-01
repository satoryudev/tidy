# claude-skills

[Claude Code](https://claude.com/claude-code) で使える自作スキル集。

## 収録スキル

### `dir-organizer` — 安全なディレクトリ整理

散らかったディレクトリを、**ファイルの中身を見て意味的に分類**し、安全に整理するスキル。

- **削除しない**: 不要ファイルはゴミ箱ではなく隔離ディレクトリ `_捨て/` へ移すだけ
- **集約 + 重複排除**: 講義資料などが複数の場所に散らばって重複しているのを、1か所に集めて
  同一内容（同 sha1）の重複を1つに絞る（残りは `_捨て/` へ隔離）
- **実行前に確認**: dry-run プレビューで計画を提示してから実行
- **全部追える**: すべての移動を manifest に記録。`undo` でいつでも完全復元（集めたファイルも
  元の散らばっていた場所へ戻る）
- **依存ゼロ**: Python 標準ライブラリのみ（Python 3.9+）

## インストール

各スキルのフォルダ（例: `dir-organizer/`）を `~/.claude/skills/` 配下に置けば使えます。

### 方法A: コピー
```bash
git clone https://github.com/<your-account>/claude-skills.git
cp -r claude-skills/dir-organizer ~/.claude/skills/
```

### 方法B: シンボリックリンク（リポジトリの更新が即反映される）
```bash
git clone https://github.com/<your-account>/claude-skills.git
ln -s "$(pwd)/claude-skills/dir-organizer" ~/.claude/skills/dir-organizer
```

インストール後、Claude Code で「このフォルダを整理して」などと頼むと自動で発火します。
`/dir-organizer` と打って明示的に呼ぶこともできます。

## dir-organizer の単体利用（スキルを使わず CLI として）

```bash
cd dir-organizer
# 1. 走査（複数ディレクトリを並べると集約モード用にまとめてスキャン）
python3 scripts/organize.py scan "<対象dir>" [<対象dir2> ...] --out /tmp/scan.json
# 2. plan.json を用意（手書き or Claude に作らせる）
# 3. プレビュー（移動しない）
python3 scripts/organize.py preview "<対象dir>" --in /tmp/plan.json
# 4. 実行
python3 scripts/organize.py apply "<対象dir>" --in /tmp/plan.json --yes
# 5. 元に戻す
python3 scripts/organize.py undo "<対象dir>"
```

## ライセンス

MIT License（`LICENSE` 参照）。
