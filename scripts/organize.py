#!/usr/bin/env python3
"""tidy: 安全なディレクトリ整理ツール（標準ライブラリのみ）。

設計ポリシー:
  - 削除しない: 不要ファイルは隔離ディレクトリ(_捨て)へ移すだけ
  - 上書きしない: 宛先が衝突したらサフィックスを付けてリネーム
  - 全部記録する: すべての移動を manifest に残し、いつでも追跡・復元できる
  - 実行前に確認: preview(dry-run) で計画を提示してから apply する

サブコマンド:
  assess   ディレクトリを診断し、skill向きか手動向きか（あるいは触らない方がよいか）を判定
  scan     1つ以上のディレクトリを走査して scan.json を出力（移動しない）
  preview  plan.json を検証し、from→to をツリー表示（移動しない / 取り残し検出）
  apply    plan.json に従って移動を実行し、manifest を記録（1件ごとに記録＝中断耐性あり）
  undo     最新 manifest を逆再生して元の状態に完全復元

2つの使い方:
  - その場整理: 1つのフォルダを中で分類する（plan の from は相対パス）
  - 集約+重複排除: 複数の場所から関連ファイル（例: 講義資料）を1か所に集め、
    同一内容の重複は1つだけ残して残りを _捨て へ（plan の from は絶対パス、
    to は宛先ルートからの相対パス）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

TRASH_DIR = "_捨て"          # 不要ファイルの隔離先（削除はしない）
LOG_DIR = "_整理ログ"         # manifest の保存先
HASH_LIMIT = 100 * 1024 * 1024  # これ以下のサイズのみハッシュ計算（重複検出用）
SNIPPET_DEFAULT = 500

# 明らかに不要なファイル（Claude が plan で最終判断するが、scan で候補として印を付ける）
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r"}
JUNK_SUFFIXES = (
    ".tmp", ".temp", ".swp", ".swo", ".bak", ".orig",
    ".pyc", ".pyo", ".crdownload", ".part", ".download",
)

# 講義・授業資料らしさのヒント（パス/ファイル名に含まれていれば course_hint=True）。
# あくまで Claude の判断を助ける目印で、最終決定は内容を見て行う。
COURSE_KEYWORDS = (
    "講義", "授業", "演習", "ゼミ", "資料", "レジュメ", "スライド", "シラバス",
    "課題", "レポート", "試験", "過去問", "板書", "教材", "第", "回",
    "lecture", "lec", "slide", "syllabus", "seminar", "assignment",
    "homework", "hw", "exam", "midterm", "final", "quiz", "course", "week",
)

# assess（診断）用。これらが見つかったらファイル移動は破壊的になりやすいので手動推奨。
PROJECT_DIR_MARKERS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".terraform", ".next", ".idea", ".gradle",
}
PROJECT_FILE_MARKERS = {
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "Makefile", "CMakeLists.txt", "tsconfig.json",
    "requirements.txt", "Gemfile", "setup.py", "composer.json",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _is_reserved(name: str) -> bool:
    return name in (TRASH_DIR, LOG_DIR)


def _looks_binary(chunk: bytes) -> bool:
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    # UTF-8 として解釈できればテキスト（日本語などのマルチバイトも正しく扱う）。
    # 末尾でマルチバイト文字が途中で切れている場合は許容する。
    try:
        chunk.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        if e.start >= len(chunk) - 3:
            return False
    # UTF-8 でない場合は制御文字の割合で判定（latin-1 等のテキストは高位バイトを許容）。
    ctrl = sum(1 for b in chunk if b < 9 or 13 < b < 32)
    return ctrl / len(chunk) > 0.30


def _junk_reason(p: Path, size: int) -> str | None:
    if p.name in JUNK_NAMES:
        return f"システム生成ファイル ({p.name})"
    if p.name.startswith("~$"):
        return "Office の一時ロックファイル"
    if p.suffix.lower() in JUNK_SUFFIXES:
        return f"一時/バックアップ拡張子 ({p.suffix})"
    if size == 0:
        return "0バイトの空ファイル"
    return None


def _course_hint(rel_path: str) -> bool:
    low = rel_path.lower()
    return any(k in rel_path or k.lower() in low for k in COURSE_KEYWORDS)


def _hash_file(p: Path) -> str | None:
    try:
        if p.stat().st_size > HASH_LIMIT:
            return None
        h = sha1()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _read_snippet(p: Path, binary: bool, limit: int) -> str:
    if binary or limit <= 0:
        return ""
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(limit).replace("\x00", "")
    except OSError:
        return ""


# --------------------------------------------------------------------------- scan
def cmd_scan(args: argparse.Namespace) -> int:
    roots: list[Path] = []
    for d in args.dirs:
        r = Path(d).expanduser().resolve()
        if not r.is_dir():
            print(f"エラー: ディレクトリが見つかりません: {r}", file=sys.stderr)
            return 2
        roots.append(r)
    roots = _prune_nested_roots(roots)

    files: list[dict] = []
    empty_dirs: list[str] = []           # 絶対パス
    by_hash: dict[str, list[str]] = {}   # sha1 -> 絶対パスのリスト（場所をまたいだ重複検出）

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            # 隔離/ログ用ディレクトリは触らない
            dirnames[:] = [x for x in dirnames if not _is_reserved(x)]
            rel_dir = d.relative_to(root)
            depth = 0 if str(rel_dir) == "." else len(rel_dir.parts)
            if args.depth is not None and depth >= args.depth:
                dirnames[:] = []

            if not dirnames and not filenames and str(rel_dir) != ".":
                empty_dirs.append(str(d))

            for name in filenames:
                p = d / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                size = st.st_size
                with p.open("rb") as f:
                    head = f.read(8192)
                binary = _looks_binary(head)
                digest = _hash_file(p) if not args.no_hash else None
                rel = str(p.relative_to(root))
                if digest:
                    by_hash.setdefault(digest, []).append(str(p))
                files.append({
                    "abspath": str(p),
                    "path": rel,
                    "root": str(root),
                    "size": size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "ext": p.suffix.lower(),
                    "binary": binary,
                    "junk": _junk_reason(p, size) is not None,
                    "junk_reason": _junk_reason(p, size),
                    "course_hint": _course_hint(rel),
                    "sha1": digest,
                    "snippet": _read_snippet(p, binary, args.max_snippet),
                })

    duplicates = [sorted(v) for v in by_hash.values() if len(v) > 1]
    report = {
        "roots": [str(r) for r in roots],
        "root": str(roots[0]) if len(roots) == 1 else None,
        "scanned_at": _now(),
        "depth": args.depth,
        "trash_dir": TRASH_DIR,
        "log_dir": LOG_DIR,
        "file_count": len(files),
        "files": sorted(files, key=lambda x: x["abspath"]),
        "empty_dirs": sorted(empty_dirs),
        "duplicates": duplicates,
    }
    out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"スキャン完了: {len(files)} ファイル / 重複 {len(duplicates)} 組 / "
              f"空dir {len(empty_dirs)} 個 / 対象 {len(roots)} ディレクトリ")
        print(f"  → {args.out} に書き出しました")
    else:
        print(out)
    return 0


# ------------------------------------------------------------------ plan / moves
def _load_plan(plan_path: str):
    data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    moves = data.get("moves", [])
    if not isinstance(moves, list):
        raise ValueError("plan.json の moves は配列である必要があります")
    return data, moves


def _safe_join(root: Path, rel: str) -> Path:
    """root の外に出る相対パスを拒否する（移動先の安全確保に使う）。"""
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"root の外を指すパスは許可されません: {rel}")
    return target


def _resolve_src(root: Path, src: str) -> Path:
    """移動元を解決する。絶対パスはそのまま（複数の場所から集約するため）、
    相対パスは root 配下に限定する（その場整理での `../` 脱出を防ぐ）。"""
    p = Path(src).expanduser()
    if p.is_absolute():
        return p.resolve()
    return _safe_join(root, src)


def _prune_nested_roots(roots: list[Path]) -> list[Path]:
    """親子関係にある root を間引き、二重走査を防ぐ。"""
    uniq = sorted(set(roots), key=lambda p: len(p.parts))
    kept: list[Path] = []
    for r in uniq:
        if any(k == r or k in r.parents for k in kept):
            continue
        kept.append(r)
    return kept


def _dedupe_dest(dst: Path, taken: set[Path]) -> Path:
    if dst not in taken and not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    i = 1
    while True:
        cand = dst.with_name(f"{stem} ({i}){suffix}")
        if cand not in taken and not cand.exists():
            return cand
        i += 1


def _current_files(root: Path) -> set[str]:
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [x for x in dirnames if not _is_reserved(x)]
        for name in filenames:
            out.add(str((Path(dirpath) / name).relative_to(root)))
    return out


# --------------------------------------------------------------------------- preview
def cmd_preview(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    data, moves = _load_plan(args.infile)

    errors: list[str] = []
    grouped: dict[str, list[tuple[str, str]]] = {}
    seen_rel: set[str] = set()   # root 配下から動かす元ファイル（取り残し検出用）
    dest_count: dict[Path, int] = {}

    for m in moves:
        src_rel, dst_rel = m.get("from"), m.get("to")
        if not src_rel or not dst_rel:
            errors.append(f"from/to が欠けています: {m}")
            continue
        try:
            src = _resolve_src(root, src_rel)
            dst = _safe_join(root, dst_rel)
        except ValueError as e:
            errors.append(str(e))
            continue
        if not src.is_file():
            errors.append(f"存在しないファイル: {src_rel}")
        try:  # root 配下の元ファイルだけ取り残し判定に含める（外部からの集約は対象外）
            seen_rel.add(str(src.relative_to(root)))
        except ValueError:
            pass
        dest_count[dst] = dest_count.get(dst, 0) + 1
        top = Path(dst_rel).parts[0] if Path(dst_rel).parts else dst_rel
        grouped.setdefault(top, []).append((src_rel, dst_rel))

    collisions = [str(d.relative_to(root)) for d, c in dest_count.items() if c > 1]

    # 取り残し検出: root 配下に現在あるのに plan に出てこないファイル（「どこいった」防止）
    present = _current_files(root)
    uncovered = sorted(present - seen_rel)

    print(f"== 整理プレビュー（dry-run・移動しません） ==")
    print(f"対象: {root}")
    print(f"移動予定: {len(moves)} 件\n")
    for top in sorted(grouped):
        pairs = grouped[top]
        print(f"[{top}/]  ({len(pairs)} 件)")
        for src_rel, dst_rel in sorted(pairs)[: args.limit]:
            print(f"    {src_rel}\n      → {dst_rel}")
        if len(pairs) > args.limit:
            print(f"    … 他 {len(pairs) - args.limit} 件")
        print()

    if collisions:
        print("⚠ 宛先が重複しています（apply 時に自動リネームで回避します）:")
        for c in collisions:
            print(f"    {c}")
        print()
    if uncovered:
        print(f"⚠ plan に含まれていないファイルが {len(uncovered)} 件あります（移動されず元の場所に残ります）:")
        for u in uncovered[: args.limit]:
            print(f"    {u}")
        if len(uncovered) > args.limit:
            print(f"    … 他 {len(uncovered) - args.limit} 件")
        print()
    if errors:
        print("✕ エラー（apply はできません。plan を修正してください）:")
        for e in errors:
            print(f"    {e}")
        return 1

    print("OK: この計画は apply 可能です。")
    if uncovered:
        print("   ※ 取り残しを無くしたい場合は plan に追記してください。")
    return 0


# --------------------------------------------------------------------------- apply
def cmd_apply(args: argparse.Namespace) -> int:
    import shutil

    root = Path(args.dir).expanduser().resolve()
    data, moves = _load_plan(args.infile)

    # 事前検証（preview と同じチェック。NG なら何も動かさない）
    planned: list[tuple[Path, str, str]] = []
    for m in moves:
        src_rel, dst_rel = m.get("from"), m.get("to")
        if not src_rel or not dst_rel:
            print(f"エラー: from/to 欠落: {m}", file=sys.stderr)
            return 1
        try:
            src = _resolve_src(root, src_rel)
            _safe_join(root, dst_rel)  # to が root 外なら弾く
        except ValueError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        if not src.is_file():
            print(f"エラー: 存在しないファイル: {src_rel}", file=sys.stderr)
            return 1
        planned.append((src, dst_rel, m.get("reason", "")))

    if not args.yes:
        print(f"{len(planned)} 件を移動します。確認のうえ --yes を付けて実行してください。")
        return 1

    log_dir = root / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = log_dir / f"manifest-{_ts()}.json"
    records: list[dict] = []

    def _flush(complete: bool) -> None:
        # 各移動の直後に書き出すことで、途中で中断/クラッシュしても
        # それまでの移動を undo で戻せるようにする（中断耐性）。
        manifest = {"root": str(root), "applied_at": _now(),
                    "complete": complete, "moves": records}
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, manifest_path)  # アトミックに置き換え

    _flush(False)  # 移動前に空のジャーナルを作成
    taken: set[Path] = set()
    try:
        for src, dst_rel, reason in planned:
            dst = _safe_join(root, dst_rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = _dedupe_dest(dst, taken)
            taken.add(final)
            shutil.move(str(src), str(final))
            records.append({
                "from": str(src),
                "to": str(final),
                "reason": reason,
                "moved_at": _now(),
            })
            _flush(False)  # 1件ごとにジャーナル更新（中断耐性）
    except OSError as e:
        _flush(False)
        print(f"エラー: 移動中に失敗しました（{e}）。{len(records)} 件は完了済みで記録されています。",
              file=sys.stderr)
        print(f"元に戻す: python3 organize.py undo \"{root}\"", file=sys.stderr)
        return 1
    _flush(True)  # 全件完了

    moved_trash = sum(1 for r in records if f"/{TRASH_DIR}/" in r["to"])
    print(f"完了: {len(records)} 件を移動しました（うち {moved_trash} 件を {TRASH_DIR}/ へ隔離）。")
    print(f"記録: {manifest_path}")
    print(f"元に戻す: python3 organize.py undo \"{root}\"")
    return 0


# --------------------------------------------------------------------------- undo
def _latest_manifest(root: Path) -> Path | None:
    log_dir = root / LOG_DIR
    if not log_dir.is_dir():
        return None
    cands = sorted(p for p in log_dir.glob("manifest-*.json")
                   if not p.name.endswith(".undone.json"))  # 使用済みは除外
    return cands[-1] if cands else None


def cmd_undo(args: argparse.Namespace) -> int:
    import shutil

    root = Path(args.dir).expanduser().resolve()
    mpath = Path(args.manifest) if args.manifest else _latest_manifest(root)
    if not mpath or not mpath.is_file():
        print("エラー: manifest が見つかりません。", file=sys.stderr)
        return 2

    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    moves = manifest.get("moves", [])
    restored, skipped = 0, 0
    dest_parents: set[Path] = set()

    for r in reversed(moves):
        cur = Path(r["to"])
        orig = Path(r["from"])
        anc = cur.parent
        while anc != root and root in anc.parents:  # root 直下まで祖先を集める
            dest_parents.add(anc)
            anc = anc.parent
        if not cur.exists():
            print(f"  スキップ（移動先が見つかりません）: {cur}")
            skipped += 1
            continue
        orig.parent.mkdir(parents=True, exist_ok=True)
        final = _dedupe_dest(orig, set())
        shutil.move(str(cur), str(final))
        restored += 1

    # apply で作られて空になったディレクトリ（カテゴリ folder・_捨て 等）を片付け
    for d in sorted(dest_parents, key=lambda x: len(x.parts), reverse=True):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    consumed = mpath.with_suffix(".undone.json")
    mpath.rename(consumed)
    print(f"復元完了: {restored} 件を元の場所に戻しました（スキップ {skipped} 件）。")
    print(f"使用済み manifest: {consumed}")
    return 0


# --------------------------------------------------------------------------- assess
def cmd_assess(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"エラー: ディレクトリが見つかりません: {root}", file=sys.stderr)
        return 2

    total_files = loose_files = junk = symlinks = max_depth = 0
    by_hash: dict[str, int] = {}
    dir_markers: set[str] = set()
    type_exts: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel = d.relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        max_depth = max(max_depth, depth)
        for dn in dirnames:
            if dn in PROJECT_DIR_MARKERS:
                dir_markers.add(dn)
            if (d / dn).is_symlink():
                symlinks += 1
        # マーカー/予約ディレクトリの中には入らない（巨大化と誤整理を避ける）
        dirnames[:] = [x for x in dirnames
                       if x not in PROJECT_DIR_MARKERS and not _is_reserved(x)]
        for name in filenames:
            p = d / name
            if p.is_symlink():
                symlinks += 1
            try:
                st = p.stat()
            except OSError:
                continue
            total_files += 1
            if depth == 0:
                loose_files += 1
            if p.suffix:
                type_exts.add(p.suffix.lower())
            if _junk_reason(p, st.st_size):
                junk += 1
            if not args.no_hash:
                dig = _hash_file(p)
                if dig:
                    by_hash[dig] = by_hash.get(dig, 0) + 1
    dup_groups = sum(1 for c in by_hash.values() if c > 1)

    try:
        root_entries = set(os.listdir(root))
    except OSError:
        root_entries = set()
    file_markers = sorted(root_entries & PROJECT_FILE_MARKERS)

    home = Path.home()
    # 「/」は全パスの祖先なので祖先判定に入れない（root==/ のときだけ該当させる）。
    sensitive = [Path("/System"), Path("/usr"), Path("/bin"),
                 Path("/Library"), home / "Library", Path("/Applications")]
    in_sensitive = (root in (home, Path("/"))
                    or any(root == s or s in root.parents for s in sensitive))

    reasons: list[str] = []
    if dir_markers or file_markers:
        verdict, label = "NG", "skill非推奨（手動 or 対象を絞る）"
        if dir_markers & {".git", ".hg", ".svn"}:
            reasons.append("バージョン管理リポジトリ（.git 等）。ファイル移動は履歴・管理を壊します。")
        if file_markers:
            reasons.append(f"プロジェクト設定ファイル {file_markers} を検出。コードプロジェクトの可能性大。")
        if dir_markers & {"node_modules", "__pycache__", ".venv", "venv", ".gradle"}:
            reasons.append("依存・ビルド成果物フォルダ（node_modules/.venv 等）。構造に意味があり再生成物です。")
        reasons.append("整理したいサブフォルダだけを対象にするか、手動で動かしてください。")
    elif in_sensitive:
        verdict, label = "NG", "触らない方がよい（重要な場所）"
        reasons.append(f"ホーム直下またはシステム/ライブラリ配下です（{root}）。広範囲の移動は危険。")
        reasons.append("Downloads など具体的な散らかりフォルダを指定してください。")
    elif total_files == 0:
        verdict, label = "SKIP", "対象なし（空ディレクトリ）"
        reasons.append("ファイルがありません。")
    elif total_files < 5:
        verdict, label = "MANUAL", "手動でOK（量が少ない）"
        reasons.append(f"ファイルが {total_files} 件だけ。自動化の手間に見合いません。")
    else:
        clutter = 0
        clutter += 2 if loose_files >= 10 else 0
        clutter += 1 if junk > 0 else 0
        clutter += 1 if dup_groups > 0 else 0
        clutter += 1 if len(type_exts) >= 4 else 0
        clutter += 1 if max_depth == 0 else 0
        if clutter >= 3:
            verdict, label = "SKILL", "skill向き（自動整理が有効）"
        else:
            verdict, label = "MAYBE", "どちらでも（内容を見て判断）"
        if loose_files >= 10:
            reasons.append(f"直下にファイルが {loose_files} 件、ゆるく散らかっています。")
        if junk:
            reasons.append(f"ゴミ候補が {junk} 件（.DS_Store/.tmp 等）。")
        if dup_groups:
            reasons.append(f"同一内容の重複が {dup_groups} 組ありそうです。")
        if len(type_exts) >= 4:
            reasons.append(f"ファイル種別が多様（{len(type_exts)} 種）で分類のメリットあり。")
        if max_depth >= 3 and clutter < 3:
            reasons.append("すでにある程度フォルダ分けされています。")

    print("== ディレクトリ診断 ==")
    print(f"対象: {root}")
    print(f"ファイル合計: {total_files} / 直下のゆるいファイル: {loose_files} / 最大階層: {max_depth}")
    print(f"ゴミ候補: {junk} / 重複: {dup_groups}組 / シンボリックリンク: {symlinks}")
    if dir_markers:
        print(f"検出マーカー(dir): {sorted(dir_markers)}")
    if file_markers:
        print(f"検出マーカー(file): {file_markers}")
    print()
    print(f"判定 [{verdict}]: {label}")
    for r in reasons:
        print(f"  - {r}")
    if symlinks and verdict in ("SKILL", "MAYBE", "MANUAL"):
        print(f"  - 注意: シンボリックリンクが {symlinks} 個。移動するとリンク参照が壊れることがあります。")
    return 0


# --------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="organize.py", description="安全なディレクトリ整理ツール")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="1つ以上のディレクトリを走査して scan.json を出力")
    s.add_argument("dirs", nargs="+", metavar="dir", help="走査するディレクトリ（複数指定可）")
    s.add_argument("--depth", type=int, default=None, help="走査する最大階層")
    s.add_argument("--out", default=None, help="出力先 JSON（省略時は標準出力）")
    s.add_argument("--max-snippet", type=int, default=SNIPPET_DEFAULT, help="内容スニペットの最大文字数")
    s.add_argument("--no-hash", action="store_true", help="重複検出用のハッシュ計算を省略")
    s.set_defaults(func=cmd_scan)

    pv = sub.add_parser("preview", help="plan.json を検証して dry-run 表示")
    pv.add_argument("dir", help="宛先ルート（_捨て/_整理ログ を置く場所。全 to はこの中に入る）")
    pv.add_argument("--in", dest="infile", required=True, help="plan.json")
    pv.add_argument("--limit", type=int, default=20, help="宛先ごとの表示件数上限")
    pv.set_defaults(func=cmd_preview)

    ap = sub.add_parser("apply", help="plan.json に従って移動を実行")
    ap.add_argument("dir", help="宛先ルート（_捨て/_整理ログ を置く場所。全 to はこの中に入る）")
    ap.add_argument("--in", dest="infile", required=True, help="plan.json")
    ap.add_argument("--yes", action="store_true", help="確認をスキップして実行")
    ap.set_defaults(func=cmd_apply)

    ud = sub.add_parser("undo", help="最新 manifest を逆再生して復元")
    ud.add_argument("dir", help="apply 時に指定した宛先ルート")
    ud.add_argument("--manifest", default=None, help="使用する manifest（省略時は最新）")
    ud.set_defaults(func=cmd_undo)

    asm = sub.add_parser("assess", help="ディレクトリを診断し skill向き/手動向き を判定")
    asm.add_argument("dir")
    asm.add_argument("--no-hash", action="store_true", help="重複検出のハッシュ計算を省略（高速）")
    asm.set_defaults(func=cmd_assess)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
