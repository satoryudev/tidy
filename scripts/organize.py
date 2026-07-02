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
  suggest  scan.json から baseline plan.json を自動生成（拡張子・内容・重複群から判断）
  preview  plan.json を検証し、from→to をツリー表示（移動しない / 取り残し検出）
  apply    plan.json に従って移動を実行し、manifest を記録（1件ごとに記録＝中断耐性あり）
  verify   apply 直後の整合性チェック（from が消え、to が存在することを確認）
  undo     最新 manifest を逆再生して元の状態に完全復元
  redo     直前の undo を取り消し、apply 後の状態へ戻す
  review   _捨て/ の中身を一覧 / 復元 / 物理削除する（隔離ファイルの後片付け）
  history  全ての場所で行った操作を ~/.tidy に集約し、1本のタイムラインで表示

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
import re
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

# 依存関係スキャン対象。コードファイル + マークアップ。
DEP_EXTS = {
    ".py",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".html", ".htm",
    ".css", ".scss", ".sass",
    ".md", ".markdown",
    ".sh", ".bash",
}
# import 抽出のために読む最大バイト数（snippet とは別に、ファイル先頭をもう少し広く見る）。
DEP_READ_BYTES = 32 * 1024

# 候補解決時に試す拡張子（js の `./foo` → `foo.js` `foo/index.ts` などへ）。
DEP_RESOLVE_EXTS = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".html", ".css", ".scss", ".sass", ".sh")

# 言語別 import 抽出パターン。group(1) が import 先の文字列。
# 「local かどうか」（外部パッケージ vs 同梱）の判定はこの後 _resolve_dep_target で行う。
_RE_PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.+[\w.]*|[\w.]+)[ \t]+import\b", re.MULTILINE)
_RE_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)\b", re.MULTILINE)
_RE_JS_FROM = re.compile(r"""(?:^|[;\s])(?:import|export)[^'"`;\n]*?from[ \t]+['"]([^'"]+)['"]""", re.MULTILINE)
_RE_JS_IMPORT_BARE = re.compile(r"""(?:^|[;\s])import[ \t]+['"]([^'"]+)['"]""", re.MULTILINE)
_RE_JS_REQUIRE = re.compile(r"""\brequire[ \t]*\([ \t]*['"]([^'"]+)['"][ \t]*\)""", re.MULTILINE)
_RE_JS_DYN = re.compile(r"""\bimport[ \t]*\([ \t]*['"]([^'"]+)['"][ \t]*\)""", re.MULTILINE)
_RE_HTML_SRC_HREF = re.compile(r"""\b(?:src|href)[ \t]*=[ \t]*['"]([^'"]+)['"]""", re.IGNORECASE)
_RE_CSS_IMPORT = re.compile(r"""@import[ \t]+(?:url[ \t]*\([ \t]*)?['"]([^'"]+)['"]""")
_RE_CSS_URL = re.compile(r"""\burl\([ \t]*['"]?([^'")]+)['"]?[ \t]*\)""")
_RE_MD_LINK = re.compile(r"""!?\[[^\]]*\]\(([^)\s]+)""")
_RE_SH_SOURCE = re.compile(r"""^[ \t]*(?:source|\.)[ \t]+([^\s;|&]+)""", re.MULTILINE)


def _extract_import_targets(text: str, ext: str) -> list[str]:
    """ファイル本文（先頭 ~32KB）から import 先文字列を抜き出す。

    重複あり・順序保持で返す。外部 URL（http://, data:）は除外するが、外部パッケージ名
    （`react`, `os` 等）は **含めて** 返す。local かどうかの最終判定は呼び出し側で
    `_resolve_dep_target` がパス解決の成否で行う（同名パッケージとローカルモジュールの
    衝突は実際のファイル存在で解決する設計）。
    """
    out: list[str] = []

    def add(s: str | None):
        if not s:
            return
        s = s.strip()
        if not s:
            return
        if "://" in s or s.startswith("data:") or s.startswith("#") or s.startswith("mailto:"):
            return
        out.append(s)

    if ext == ".py":
        for m in _RE_PY_FROM.finditer(text):
            add(m.group(1))
        for m in _RE_PY_IMPORT.finditer(text):
            # `import a, b.c` のようにカンマで複数並ぶケース
            for part in m.group(1).split(","):
                add(part.strip())
        return out

    if ext in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        for pat in (_RE_JS_FROM, _RE_JS_IMPORT_BARE, _RE_JS_REQUIRE, _RE_JS_DYN):
            for m in pat.finditer(text):
                add(m.group(1))
        return out

    if ext in {".html", ".htm"}:
        for m in _RE_HTML_SRC_HREF.finditer(text):
            add(m.group(1))
        return out

    if ext in {".css", ".scss", ".sass"}:
        for pat in (_RE_CSS_IMPORT, _RE_CSS_URL):
            for m in pat.finditer(text):
                add(m.group(1))
        return out

    if ext in {".md", ".markdown"}:
        for m in _RE_MD_LINK.finditer(text):
            add(m.group(1))
        return out

    if ext in {".sh", ".bash"}:
        for m in _RE_SH_SOURCE.finditer(text):
            add(m.group(1))
        return out

    return out


def _read_for_deps(p: Path) -> str:
    """import 解析用に先頭 ~32KB をテキストとして読み出す。UTF-8 以外でも errors=replace で進める。"""
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(DEP_READ_BYTES).replace("\x00", "")
    except OSError:
        return ""


def _resolve_dep_target(target: str, source_path: Path, by_path: dict[str, dict]) -> str | None:
    """target 文字列をスキャン済みファイル群の中の絶対パスに解決する。

    対応:
      - Python: `.foo` / `..foo` などのドット相対 → ソース親dir基準で foo.py / foo/__init__.py
      - JS/TS/CSS/HTML/MD: `./foo` / `../bar/foo` などのパス相対 → 拡張子を順に試す
      - 拡張子なし・ディレクトリ指定 → `/index.{js,ts,...}` も試す

    解決できなければ None（= ローカル依存ではなく外部依存とみなす）。
    """
    if not target:
        return None
    src_dir = source_path.parent

    # Python のドット相対: `.foo`, `..foo.bar`, `.` (= 同パッケージ)
    if target.startswith(".") and not target.startswith("./") and not target.startswith("../"):
        dots = len(target) - len(target.lstrip("."))
        rest = target[dots:].replace(".", "/") if target[dots:] else ""
        base = src_dir
        for _ in range(dots - 1):
            base = base.parent
        cands: list[Path] = []
        if rest:
            cands.append(base / f"{rest}.py")
            cands.append(base / rest / "__init__.py")
        else:
            cands.append(base / "__init__.py")
        for c in cands:
            try:
                r = str(c.resolve())
            except OSError:
                continue
            if r in by_path and r != str(source_path):
                return r
        return None

    # それ以外はパス相対っぽいかどうかで判断
    is_pathlike = (
        target.startswith("./")
        or target.startswith("../")
        or "/" in target
        or target.startswith(".")  # ".env" のようなドット始まりファイル
    )
    if not is_pathlike:
        # Python の `from helper import X` のように、同階層にファイルがあれば慣習として local。
        # JS/TS では `react` のような bare specifier は必ず外部なので試さない。
        if source_path.suffix.lower() == ".py":
            mod_path = target.replace(".", "/")
            cands_py: list[Path] = [
                src_dir / f"{mod_path}.py",
                src_dir / mod_path / "__init__.py",
            ]
            for c in cands_py:
                try:
                    r = str(c.resolve())
                except OSError:
                    continue
                if r in by_path and r != str(source_path):
                    return r
        # 解決できない → 外部依存とみなしてスキップ
        return None

    cands_paths: list[Path] = []
    base = (src_dir / target)
    cands_paths.append(base)
    # 拡張子補完
    if not base.suffix:
        for ext_try in DEP_RESOLVE_EXTS:
            cands_paths.append(Path(str(base) + ext_try))
    # ディレクトリ index
    for ext_try in DEP_RESOLVE_EXTS:
        cands_paths.append(base / f"index{ext_try}")

    for c in cands_paths:
        try:
            r = str(c.resolve())
        except OSError:
            continue
        if r in by_path and r != str(source_path):
            return r
    return None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _tidy_home() -> Path:
    """全体の操作履歴を集約するホーム。既定は ~/.tidy。

    テストや隔離実行のために環境変数 TIDY_HOME で差し替えできる。
    「散らかりの整理」はローカル（同一ボリューム内の rename）で行い、
    ここに置くのは **来歴の索引だけ**。実ファイルは動かさない設計。
    """
    override = os.environ.get("TIDY_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tidy"


def _record_history(action: str, target: Path, **fields) -> None:
    """操作の1行を TIDY_HOME/history.jsonl に追記する（ベストエフォート）。

    apply / undo / redo / review-restore / review-purge の成功時に呼ぶ。
    どの場所を整理しても履歴は1本のタイムラインに集まり、`history` で横断できる。
    失敗しても本処理は絶対に壊さない（履歴は補助機能なので握りつぶす）。
    """
    try:
        home = _tidy_home()
        home.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _now(), "action": action, "target": str(target)}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with (home / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # 履歴が書けなくても整理自体は成功として扱う


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

    raw_import_targets: dict[str, list[str]] = {}  # abspath -> import 文字列の配列（未解決）

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
                ext = p.suffix.lower()
                if digest:
                    by_hash.setdefault(digest, []).append(str(p))
                # 依存抽出用にもう少し広く読む（snippet とは別。コードファイルだけ）
                if not args.no_deps and not binary and ext in DEP_EXTS:
                    targets = _extract_import_targets(_read_for_deps(p), ext)
                    if targets:
                        raw_import_targets[str(p)] = targets
                files.append({
                    "abspath": str(p),
                    "path": rel,
                    "root": str(root),
                    "size": size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "ext": ext,
                    "binary": binary,
                    "junk": _junk_reason(p, size) is not None,
                    "junk_reason": _junk_reason(p, size),
                    "course_hint": _course_hint(rel),
                    "sha1": digest,
                    "snippet": _read_snippet(p, binary, args.max_snippet),
                })

    # 依存解決＆クラスタ生成（全 root スキャン後にまとめて）
    by_path = {f["abspath"]: f for f in files}
    edges: list[dict] = []                     # {"from": abs, "to": abs}
    per_file_imports: dict[str, list[str]] = {}
    per_file_imported_by: dict[str, list[str]] = {}
    for src_abs, targets in raw_import_targets.items():
        src_path = Path(src_abs)
        resolved: set[str] = set()
        for t in targets:
            r = _resolve_dep_target(t, src_path, by_path)
            if r:
                resolved.add(r)
        if not resolved:
            continue
        per_file_imports[src_abs] = sorted(resolved)
        for r in resolved:
            per_file_imported_by.setdefault(r, []).append(src_abs)
            edges.append({"from": src_abs, "to": r})

    # 連結成分（無向）を計算 → コードクラスタ
    adj: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for src_abs, deps_list in per_file_imports.items():
        nodes.add(src_abs)
        adj.setdefault(src_abs, set())
        for d in deps_list:
            nodes.add(d)
            adj.setdefault(d, set())
            adj[src_abs].add(d)
            adj[d].add(src_abs)
    visited: set[str] = set()
    clusters: list[list[str]] = []
    for n in sorted(nodes):
        if n in visited:
            continue
        stack = [n]
        comp: list[str] = []
        while stack:
            x = stack.pop()
            if x in visited:
                continue
            visited.add(x)
            comp.append(x)
            stack.extend(adj.get(x, ()))
        if len(comp) > 1:
            clusters.append(sorted(comp))

    # クラスタ名: 最も多くを import している（出次数が高い）ファイルの basename を使う。
    # 同点ならアルファベット順で安定化。クラスタ ID も付与しておくとレポート時に便利。
    cluster_records: list[dict] = []
    for ci, members in enumerate(clusters):
        # 出次数 = 自分が import しているファイル数（per_file_imports に登録された数）
        def out_deg(p: str) -> int:
            return len(per_file_imports.get(p, []))
        leader = sorted(members, key=lambda p: (-out_deg(p), Path(p).name))[0]
        name = Path(leader).stem
        cluster_records.append({
            "id": f"c{ci:02d}",
            "name": name,
            "leader": leader,
            "members": members,
        })
    # 逆引きマップ: abspath -> cluster id
    member_to_cluster: dict[str, str] = {}
    for c in cluster_records:
        for m in c["members"]:
            member_to_cluster[m] = c["id"]

    # 各 file に imports / imported_by / cluster を埋める
    for f in files:
        ap = f["abspath"]
        f["imports"] = per_file_imports.get(ap, [])
        f["imported_by"] = sorted(per_file_imported_by.get(ap, []))
        f["cluster"] = member_to_cluster.get(ap)

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
        "code_dependencies": {
            "edges": sorted(edges, key=lambda e: (e["from"], e["to"])),
            "clusters": cluster_records,
        },
    }
    out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"スキャン完了: {len(files)} ファイル / 重複 {len(duplicates)} 組 / "
              f"空dir {len(empty_dirs)} 個 / 対象 {len(roots)} ディレクトリ")
        if cluster_records:
            print(f"  コード依存クラスタ: {len(cluster_records)} 組 "
                  f"({sum(len(c['members']) for c in cluster_records)} ファイル)")
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
def _humanize_bytes(n: int) -> str:
    """人間が読みやすいバイト表記。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n} B"  # 到達しないが型のため


def cmd_preview(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    data, moves = _load_plan(args.infile)

    errors: list[str] = []
    grouped: dict[str, list[tuple[str, str]]] = {}
    seen_rel: set[str] = set()   # root 配下から動かす元ファイル（取り残し検出用）
    dest_count: dict[Path, int] = {}
    total_bytes = 0
    trash_bytes = 0

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
        elif src.resolve() == dst.resolve():
            # from == to は意味のない move（apply で SameFileError になる）→ 早期に弾く
            errors.append(f"移動元と移動先が同じです: {src_rel}")
        else:
            # サイズ集計は実在ファイルだけで（エラー時は当然 0 扱い）
            try:
                size = src.stat().st_size
                total_bytes += size
                if dst_rel.startswith(f"{TRASH_DIR}/") or f"/{TRASH_DIR}/" in dst_rel:
                    trash_bytes += size
            except OSError:
                pass
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
    print(f"移動予定: {len(moves)} 件 / 総サイズ {_humanize_bytes(total_bytes)}"
          + (f" (うち {TRASH_DIR}/ へ {_humanize_bytes(trash_bytes)})" if trash_bytes else "")
          + "\n")

    # サマリヘッダ: トップフォルダごとの件数を1行で並べる。「Documents/ 12件 / 画像/ 3件」のように
    # 100件の plan でも「どこに何件行くか」を一目で把握できる（v5 追加）。
    if grouped:
        summary_parts = [f"{top}/ {len(pairs)}件" for top, pairs in sorted(grouped.items())]
        print(f"サマリ: {' / '.join(summary_parts)}\n")

    for top in sorted(grouped):
        pairs = grouped[top]
        print(f"[{top}/]  ({len(pairs)} 件)")
        for src_rel, dst_rel in sorted(pairs)[: args.limit]:
            print(f"    {src_rel}\n      → {dst_rel}")
        if len(pairs) > args.limit:
            print(f"    … 他 {len(pairs) - args.limit} 件")
        print()

    # 依存クラスタの分断検出: plan に cluster 情報があるとき、それぞれのクラスタの member が
    # 同じ宛先 dir に揃っているかをチェックする。揃っていなければ「import が壊れる可能性」を警告。
    plan_clusters = data.get("clusters") or []
    cluster_warnings: list[str] = []
    if plan_clusters:
        from_to: dict[str, str] = {m.get("from"): m.get("to") for m in moves
                                   if m.get("from") and m.get("to")}
        for c in plan_clusters:
            members = c.get("members") or []
            in_plan = [(m, from_to[m]) for m in members if m in from_to]
            missing = [m for m in members if m not in from_to]
            dest_parents = {str(Path(to).parent) for _, to in in_plan}
            label = f"{c.get('id', '?')}「{c.get('name', '?')}」 ({len(members)} 件)"
            if len(dest_parents) > 1:
                cluster_warnings.append(
                    f"分断: {label} のメンバーが {len(dest_parents)} つの異なる宛先に分かれています:\n"
                    + "\n".join(f"      {p}/" for p in sorted(dest_parents))
                )
            if missing and in_plan:
                # 部分的に plan に入っている = 一部だけ動いて他は残る → import が壊れる可能性大
                cluster_warnings.append(
                    f"未カバー: {label} のうち {len(missing)} 件が plan に含まれていません: "
                    + ", ".join(missing[: args.limit])
                )

    if collisions:
        print("⚠ 宛先が重複しています（apply 時に自動リネームで回避します）:")
        for c in collisions:
            print(f"    {c}")
        print()
    if cluster_warnings:
        print("⚠ コード依存クラスタの分断/取り残しがあります（import が壊れる可能性）:")
        for w in cluster_warnings:
            print(f"    {w}")
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
    import signal

    # --dry-run なら preview と等価な挙動にして安全側に倒す
    if getattr(args, "dry_run", False):
        # preview と同じ引数で委譲（limit はデフォルト 20）
        pv_args = argparse.Namespace(dir=args.dir, infile=args.infile, limit=20)
        return cmd_preview(pv_args)

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
            dst_check = _safe_join(root, dst_rel)  # to が root 外なら弾く
        except ValueError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        if not src.is_file():
            print(f"エラー: 存在しないファイル: {src_rel}", file=sys.stderr)
            return 1
        if src.resolve() == dst_check.resolve():
            print(f"エラー: 移動元と移動先が同じです: {src_rel}", file=sys.stderr)
            return 1
        planned.append((src, dst_rel, m.get("reason", "")))

    if not args.yes:
        print(f"{len(planned)} 件を移動します。確認のうえ --yes を付けて実行してください。")
        return 1

    log_dir = root / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = log_dir / f"manifest-{_ts()}.json"
    records: list[dict] = []
    interrupted = False

    def _flush(complete: bool) -> None:
        # 各移動の直後に書き出すことで、途中で中断/クラッシュしても
        # それまでの移動を undo で戻せるようにする（中断耐性）。
        manifest = {"root": str(root), "applied_at": _now(),
                    "complete": complete, "moves": records}
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, manifest_path)  # アトミックに置き換え

    # Ctrl-C を捕まえて、ジャーナルを残してから抜ける。途中までの移動は undo で戻せる。
    def _sigint(_signum, _frame):
        nonlocal interrupted
        interrupted = True
    prev_sigint = signal.signal(signal.SIGINT, _sigint)

    _flush(False)  # 移動前に空のジャーナルを作成
    taken: set[Path] = set()
    try:
        for src, dst_rel, reason in planned:
            if interrupted:
                break
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
    finally:
        signal.signal(signal.SIGINT, prev_sigint)

    if interrupted:
        _flush(False)
        print(f"中断: {len(records)} 件まで完了したところで Ctrl-C を受け取りました。",
              file=sys.stderr)
        print(f"記録: {manifest_path}", file=sys.stderr)
        print(f"完了分を戻すには: python3 organize.py undo \"{root}\"", file=sys.stderr)
        return 130  # シェル慣例: SIGINT 終了は 128+2

    _flush(True)  # 全件完了

    moved_trash = sum(1 for r in records if f"/{TRASH_DIR}/" in r["to"])
    _record_history("apply", root, moves=len(records), trashed=moved_trash,
                    manifest=str(manifest_path))
    print(f"完了: {len(records)} 件を移動しました（うち {moved_trash} 件を {TRASH_DIR}/ へ隔離）。")
    print(f"記録: {manifest_path}")
    print(f"確認: python3 organize.py verify \"{root}\"")
    print(f"元に戻す: python3 organize.py undo \"{root}\"")
    return 0


# --------------------------------------------------------------------------- undo
def _latest_manifest(root: Path) -> Path | None:
    log_dir = root / LOG_DIR
    if not log_dir.is_dir():
        return None
    # `.undone` 系（undo 済み / redo で消費済み）はすべて除外。stem を見れば一発で判定できる。
    cands = sorted(p for p in log_dir.glob("manifest-*.json")
                   if ".undone" not in p.stem)
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
    _record_history("undo", root, restored=restored, skipped=skipped,
                    manifest=str(consumed))
    print(f"復元完了: {restored} 件を元の場所に戻しました（スキップ {skipped} 件）。")
    print(f"使用済み manifest: {consumed}")
    return 0


# --------------------------------------------------------------------------- suggest
# 拡張子セット（マジックではなく辞書として束ねて、新しい拡張子が来てもここに足せばよい）
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".svg", ".bmp", ".tiff", ".tif"}
_VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv"}
_AUD_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs", ".java",
             ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala", ".php", ".lua"}
_SHELL_EXT = {".sh", ".bash", ".zsh", ".fish"}
_DATA_EXT = {".csv", ".tsv", ".jsonl", ".parquet", ".sqlite", ".db"}
_STRUCT_EXT = {".json", ".yaml", ".yml", ".xml", ".toml"}
_ARCHIVE_EXT = {".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".tgz", ".tbz2"}
_INSTALLER_EXT = {".dmg", ".pkg", ".exe", ".msi", ".deb", ".rpm"}
_DOC_OFFICE_EXT = {".docx", ".doc", ".pages", ".odt", ".rtf"}
_DOC_SHEET_EXT = {".xlsx", ".xls", ".numbers", ".ods"}
_DOC_SLIDE_EXT = {".pptx", ".ppt", ".key", ".odp"}
_TEXT_EXT = {".md", ".markdown", ".txt", ".rst", ".adoc"}

_CONFIG_NAMES = {
    "package.json", "tsconfig.json", "pyproject.toml", "Cargo.toml",
    ".eslintrc.json", ".prettierrc", "Gemfile", "go.mod", "requirements.txt",
    "Makefile", "Dockerfile", "docker-compose.yml", "vite.config.ts",
    "next.config.js", "next.config.ts", ".gitignore", ".gitattributes",
}

_COPY_MARKERS = ("copy", "コピー", "_final", "_copy", " - copy", "(1)", "(2)", "(3)", "(4)", "(5)")
_INVOICE_KW = ("請求書", "領収書", "invoice", "receipt", "bill")
_CONTRACT_KW = ("contract", "契約", "規約", "agreement")
_SPEC_KW = ("仕様", "spec", "design", "設計", "specification")


def _classify_dest(f: dict) -> tuple[str, str] | None:
    """1ファイルの行き先カテゴリパスと理由を返す。判定不能なら None（plan に含めない）。

    返す `to` は宛先ルートからの相対パス。ファイル名は src の basename をそのまま使う
    （rename しない原則）。
    """
    name = Path(f.get("abspath") or f.get("path", "")).name
    if not name:
        return None
    name_low = name.lower()
    ext = f.get("ext", "")
    binary = f.get("binary", False)
    snippet = f.get("snippet") or ""

    # 1. ゴミ候補 → 隔離（scan が junk フラグを付けたものはそのまま信用）
    if f.get("junk"):
        reason = f.get("junk_reason") or "不要候補"
        return (f"{TRASH_DIR}/{name}", reason)

    # 2. シェバン付きはスクリプト扱い（拡張子なしでも捕捉できる）
    if not binary and snippet.startswith("#!"):
        return (f"コード/{name}", "シェバン付きスクリプト")

    # 3. 名前で確定する設定ファイル（package.json 等）
    if name in _CONFIG_NAMES:
        return (f"コード/設定/{name}", "プロジェクト設定")

    # 4. 拡張子で確定するもの
    if ext in _IMG_EXT:
        if any(kw in name_low or kw in name for kw in ("screenshot", "screen shot", "スクリーンショット")):
            return (f"画像/スクリーンショット/{name}", "スクリーンショット")
        return (f"画像/{name}", "画像")
    if ext in _VID_EXT:
        return (f"動画・音声/録画/{name}", "動画")
    if ext in _AUD_EXT:
        return (f"動画・音声/音源/{name}", "音声")
    if ext in _SHELL_EXT:
        return (f"コード/shell/{name}", "シェルスクリプト")
    if ext in _CODE_EXT:
        return (f"コード/{name}", "ソースコード")
    if ext in _DATA_EXT:
        return (f"データ/{name}", "データファイル")
    if ext in {".html", ".htm", ".css", ".scss", ".sass"}:
        return (f"コード/{name}", "Web ファイル")
    if ext in _STRUCT_EXT:
        # 設定っぽい名前なら設定、それ以外はデータ
        if any(kw in name_low for kw in ("config", "settings", ".eslintrc", ".prettierrc", "tsconfig", ".babelrc")):
            return (f"コード/設定/{name}", "設定ファイル")
        return (f"データ/{name}", "構造化データ")
    if ext in _ARCHIVE_EXT:
        return (f"アーカイブ/{name}", "圧縮ファイル")
    if ext in _INSTALLER_EXT:
        return (f"インストーラ/{name}", "インストーラ")
    if ext == ".pdf":
        if any(kw in name_low or kw in name for kw in _INVOICE_KW):
            return (f"ドキュメント/請求書/{name}", "請求書/領収書")
        if any(kw in name_low or kw in name for kw in _CONTRACT_KW):
            return (f"ドキュメント/契約/{name}", "契約書")
        if f.get("course_hint"):
            return (f"ドキュメント/講義/{name}", "講義資料")
        return (f"ドキュメント/{name}", "PDF")
    if ext in _DOC_OFFICE_EXT:
        return (f"ドキュメント/{name}", "Word/Pages")
    if ext in _DOC_SHEET_EXT:
        return (f"ドキュメント/表計算/{name}", "表計算")
    if ext in _DOC_SLIDE_EXT:
        return (f"ドキュメント/スライド/{name}", "スライド")
    if ext in _TEXT_EXT:
        first_line = (snippet.split("\n", 1)[0] if snippet else "").strip()
        first_low = first_line.lower()
        if any(kw in first_line or kw in first_low for kw in _SPEC_KW):
            return (f"ドキュメント/仕様/{name}", "仕様書")
        if "readme" in name_low or first_low.startswith(("# readme", "## readme")):
            return (f"ドキュメント/{name}", "README")
        return (f"ドキュメント/メモ/{name}", "テキストメモ")

    # 拡張子なし・不明 + バイナリは触らない（安全側）。テキストならドキュメント扱い。
    if not binary:
        return (f"ドキュメント/{name}", "拡張子不明のテキスト")
    return None


def _dup_primary_score(f: dict) -> tuple:
    """重複群から「正本」を選ぶための並び順スコア。小さいほど primary 寄り。"""
    name = Path(f.get("abspath") or f.get("path", "")).name
    name_low = name.lower()
    parent_parts = len(Path(f.get("abspath") or f.get("path", "")).parent.parts)
    has_copy = any(m in name_low or m in name for m in _COPY_MARKERS)
    mtime_iso = f.get("mtime") or ""
    return (
        int(has_copy),         # 0: コピー印なし優先
        -parent_parts,         # 深い階層（=ちゃんと格納された場所）優先
        mtime_iso == "",       # mtime が無いものを後ろへ
        -ord(mtime_iso[0]) if mtime_iso else 0,  # 文字列比較で新しい mtime 優先
        len(name),             # 短い名前優先
    )


def cmd_suggest(args: argparse.Namespace) -> int:
    """scan.json を読んで baseline plan.json を組み立てる。

    使い方:
      - 単一 root を scan した結果 → そのまま「その場整理」プラン（from は相対パス）
      - 複数 root を scan した結果 → 「集約モード」プラン（from は絶対パス）。
        この場合 --target で集約先ルートを必ず指定する。
    """
    try:
        scan = json.loads(Path(args.infile).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"エラー: scan.json を読めません: {e}", file=sys.stderr)
        return 2

    files = scan.get("files", [])
    if not files:
        print("scan.json にファイルがありません。", file=sys.stderr)
        return 1
    roots = scan.get("roots") or ([scan["root"]] if scan.get("root") else [])
    multi_root = len(roots) > 1

    if multi_root and not args.target:
        print("エラー: 複数 root を scan した結果（集約モード）には --target が必要です。",
              file=sys.stderr)
        return 2

    if args.target:
        plan_root = Path(args.target).expanduser().resolve()
    else:
        plan_root = Path(roots[0]).resolve()

    # 「from を絶対パスで書く必要があるか」の判定。
    # 集約モード（複数 root）はもちろん、単一 root でも target が root と異なる場合
    # （例: scan ~/Downloads / --target ~/Documents）は from を絶対にしないと
    # apply が「target からの相対」と解釈して見つけられない。
    cross_target = multi_root or (args.target and Path(roots[0]).resolve() != plan_root)

    # 重複群: 正本を1つ選び、残りは _捨て へ
    by_path_files = {f["abspath"]: f for f in files}
    dup_decisions: dict[str, tuple[bool, str]] = {}   # abspath -> (is_primary, group_id)
    for gi, group in enumerate(scan.get("duplicates", [])):
        # group は abspath 文字列の配列。files から該当 dict を引き当てる
        group_files = [by_path_files[p] for p in group if p in by_path_files]
        if len(group_files) < 2:
            continue
        group_sorted = sorted(group_files, key=_dup_primary_score)
        primary = group_sorted[0]
        dup_decisions[primary["abspath"]] = (True, f"g{gi:02d}")
        for f in group_sorted[1:]:
            dup_decisions[f["abspath"]] = (False, f"g{gi:02d}")

    # コード依存クラスタ: 連結したファイル群は同じサブフォルダにまとめて配置する。
    # クラスタの宛先トップフォルダ（コード/ or ドキュメント/ など）はリーダーの分類から決める。
    code_deps = scan.get("code_dependencies") or {}
    clusters = code_deps.get("clusters") or []
    cluster_dest_dir: dict[str, str] = {}    # cluster_id -> "コード/main" 等の相対 dir
    cluster_label_for: dict[str, str] = {}   # cluster_id -> 表示名（reason 用）
    member_to_cluster_id: dict[str, str] = {}
    for c in clusters:
        cid = c.get("id") or f"c{len(cluster_dest_dir):02d}"
        leader_path = c.get("leader") or (c.get("members") or [None])[0]
        if not leader_path or leader_path not in by_path_files:
            continue
        leader_classified = _classify_dest(by_path_files[leader_path])
        # リーダーが junk 扱いされる稀ケースは無視（クラスタなし扱い）
        if not leader_classified or leader_classified[0].startswith(f"{TRASH_DIR}/"):
            continue
        top_folder = leader_classified[0].split("/", 1)[0]  # "コード" 等
        # 同名衝突を避けるため、リーダー名にクラスタ id をぶら下げる場合がある
        # （シンプルさ優先で id は付けず、後段の preview で衝突警告が出れば対応）
        cname = c.get("name") or Path(leader_path).stem or cid
        cluster_dest_dir[cid] = f"{top_folder}/{cname}"
        cluster_label_for[cid] = cname
        for m in c.get("members", []):
            member_to_cluster_id[m] = cid

    moves: list[dict] = []
    skipped: list[dict] = []
    for f in files:
        ap = f["abspath"]
        # 宛先ルート内のファイルは「動かさない側」として除外。
        # その場整理（target が指定されていない or target == 単一 root）では除外しない。
        if cross_target:
            try:
                Path(ap).resolve().relative_to(plan_root)
                # plan_root の中にあるファイルは「すでに整理済み」とみなしてスキップ
                skipped.append({"abspath": ap, "reason": "宛先ルート内（移動不要）"})
                continue
            except ValueError:
                pass

        dup = dup_decisions.get(ap)
        if dup and not dup[0]:
            # 重複の非 primary は _捨て へ
            name = Path(ap).name
            dest_rel = f"{TRASH_DIR}/{name}"
            from_val = ap if cross_target else f["path"]
            moves.append({
                "from": from_val,
                "to": dest_rel,
                "reason": f"重複（{dup[1]}）の非正本 → 隔離",
            })
            continue

        # コード依存クラスタに属するファイルは、リーダーの分類先サブフォルダにまとめて配置。
        # これで `main.py` と `helper.py` が別フォルダにバラけて import が壊れる事故を防ぐ。
        cid = member_to_cluster_id.get(ap)
        if cid and cid in cluster_dest_dir:
            # junk フラグは個別に尊重（.DS_Store がクラスタに紛れても隔離）
            if f.get("junk"):
                jreason = f.get("junk_reason") or "不要候補"
                dest_rel = f"{TRASH_DIR}/{Path(ap).name}"
                from_val = ap if cross_target else f["path"]
                moves.append({"from": from_val, "to": dest_rel, "reason": jreason})
                continue
            name = Path(ap).name
            dest_rel = f"{cluster_dest_dir[cid]}/{name}"
            from_val = ap if cross_target else f["path"]
            moves.append({
                "from": from_val,
                "to": dest_rel,
                "reason": f"クラスタ「{cluster_label_for[cid]}」({cid}) として一緒に配置",
            })
            continue

        classified = _classify_dest(f)
        if classified is None:
            skipped.append({"abspath": ap, "reason": "判定不能（拡張子・内容から行き先を決められず）"})
            continue
        dest_rel, why = classified
        from_val = ap if cross_target else f["path"]
        moves.append({"from": from_val, "to": dest_rel, "reason": why})

    # plan に含めるクラスタ情報。preview で「クラスタを分断する plan」を警告するために使う。
    # members は plan の from と直接突き合わせるため、suggest が生成した from 値で表現する。
    abs_to_from: dict[str, str] = {m["from"]: m["from"] for m in moves}
    abs_to_from.update({f["abspath"]: (f["abspath"] if cross_target else f["path"])
                        for f in files})
    plan_clusters: list[dict] = []
    for c in clusters:
        cid = c.get("id")
        members = [abs_to_from[m] for m in c.get("members", []) if m in abs_to_from]
        if len(members) < 2:
            continue
        plan_clusters.append({
            "id": cid,
            "name": c.get("name"),
            "members": sorted(members),
        })

    plan = {
        "root": str(plan_root),
        "trash_dir": TRASH_DIR,
        "generated_by": "suggest",
        "generated_at": _now(),
        "mode": "consolidate" if cross_target else "in-place",
        "moves": moves,
        "skipped": skipped,
        "clusters": plan_clusters,
    }

    out = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        n_trash = sum(1 for m in moves if m["to"].startswith(f"{TRASH_DIR}/"))
        print(f"baseline plan を作成: {len(moves)} 件の move（うち {n_trash} 件を {TRASH_DIR}/ へ）")
        print(f"判定不能で対象外: {len(skipped)} 件")
        print(f"  → {args.out}")
        print("次の手順: preview で確認 → 必要に応じて plan を編集 → apply")
    else:
        print(out)
    return 0


# --------------------------------------------------------------------------- verify
def cmd_verify(args: argparse.Namespace) -> int:
    """直近 apply の整合性を確認する。

    各 move について:
      - from のパスにファイルが残っていないこと（=ちゃんと移動された）
      - to のパスにファイルが存在すること
    を確かめる。manifest が無ければ assess と同じ exit 2。
    """
    root = Path(args.dir).expanduser().resolve()
    mpath = Path(args.manifest) if args.manifest else _latest_manifest(root)
    if not mpath or not mpath.is_file():
        print("エラー: manifest が見つかりません（apply されていない可能性）。", file=sys.stderr)
        return 2

    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    moves = manifest.get("moves", [])
    if not moves:
        print(f"manifest に moves がありません: {mpath}", file=sys.stderr)
        return 1

    issues: list[str] = []
    ok = 0
    for r in moves:
        src = Path(r["from"])
        dst = Path(r["to"])
        if src.exists():
            # apply 後に同名ファイルが再生成された可能性は残るが、これは「動かしたのに残ってる」
            # という追跡対象なので警告として出す。
            issues.append(f"残存: {src}（移動元にファイルが残っています）")
            continue
        if not dst.exists():
            issues.append(f"消失: {dst}（移動先が見当たりません）")
            continue
        ok += 1

    print(f"== 整合性チェック ==")
    print(f"manifest: {mpath}")
    print(f"OK: {ok} / {len(moves)} 件")
    if issues:
        print(f"問題: {len(issues)} 件")
        for s in issues:
            print(f"  - {s}")
        return 1
    print("すべての移動が manifest どおりです。")
    return 0


# --------------------------------------------------------------------------- redo
def _latest_undone(root: Path) -> Path | None:
    """直近の undo で消費された manifest（再適用候補）を返す。"""
    log_dir = root / LOG_DIR
    if not log_dir.is_dir():
        return None
    cands = sorted(p for p in log_dir.glob("manifest-*.undone.json")
                   if not p.name.endswith(".consumed.json"))
    return cands[-1] if cands else None


def cmd_redo(args: argparse.Namespace) -> int:
    """直前の undo を取り消して apply 後の状態に戻す。

    挙動:
      1. 最新の `manifest-*.undone.json` を見つける（undo で消費されたもの）
      2. その中の moves を再実行（from → to）
      3. 新しい manifest を timestamp 付きで作成（次の undo で戻せるように）
      4. 元の `.undone.json` を `.undone.consumed.json` にリネーム
         （同じ undo を二度 redo しないため）
    """
    import shutil

    root = Path(args.dir).expanduser().resolve()
    src_manifest = Path(args.manifest) if args.manifest else _latest_undone(root)
    if not src_manifest or not src_manifest.is_file():
        print("エラー: 取り消すべき undo が見つかりません（直前に undo を実行していないか、すでに redo 済み）。",
              file=sys.stderr)
        return 2

    data = json.loads(src_manifest.read_text(encoding="utf-8"))
    moves = data.get("moves", [])
    if not moves:
        print(f"対象 undo に moves がありません: {src_manifest}", file=sys.stderr)
        return 1

    log_dir = root / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    new_manifest = log_dir / f"manifest-{_ts()}.json"
    records: list[dict] = []

    def _flush(complete: bool) -> None:
        out = {"root": str(root), "applied_at": _now(),
               "complete": complete, "moves": records, "redo_of": str(src_manifest)}
        tmp = new_manifest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, new_manifest)

    _flush(False)
    restored, skipped = 0, 0
    try:
        for r in moves:
            # redo は apply 時のロジックを再現する: from が現在の場所、to が宛先。
            src = Path(r["from"])
            dst = Path(r["to"])
            if not src.exists():
                # undo で元に戻したはずの場所にファイルが無い（さらに動かされた可能性）。
                skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = _dedupe_dest(dst, set())
            shutil.move(str(src), str(final))
            records.append({
                "from": str(src),
                "to": str(final),
                "reason": r.get("reason", "") + " (redo)",
                "moved_at": _now(),
            })
            _flush(False)
    except OSError as e:
        _flush(False)
        print(f"エラー: redo 中に失敗しました（{e}）。{len(records)} 件まで完了。",
              file=sys.stderr)
        return 1
    _flush(True)

    consumed = src_manifest.with_name(src_manifest.name.replace(".undone.json", ".undone.consumed.json"))
    src_manifest.rename(consumed)

    _record_history("redo", root, moves=len(records), skipped=skipped,
                    manifest=str(new_manifest))
    print(f"再適用完了: {len(records)} 件（スキップ {skipped} 件）。")
    print(f"記録: {new_manifest}")
    print(f"消費済み undo: {consumed}")
    print(f"取り消し: python3 organize.py undo \"{root}\"")
    return 0


# --------------------------------------------------------------------------- review
def _scan_trash(root: Path) -> list[Path]:
    """root/_捨て/ を再帰的に走査し、隔離されているファイルの絶対パスを返す。"""
    trash = root / TRASH_DIR
    if not trash.is_dir():
        return []
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(trash):
        for n in filenames:
            out.append((Path(dirpath) / n).resolve())
    return sorted(out)


def _collect_trash_history(root: Path) -> dict[str, dict]:
    """manifest 群を横断して、現在 _捨て に居る各ファイルの来歴を辞書で返す。

    返り値: {現在の絶対パス: {"from": 元の絶対パス, "reason": ..., "moved_at": ISO, "manifest": "..."}}
    既に undo されたエントリ（.undone.json）は対象外。consumed.json も対象外。
    """
    log_dir = root / LOG_DIR
    out: dict[str, dict] = {}
    if not log_dir.is_dir():
        return out
    for p in sorted(log_dir.glob("manifest-*.json")):
        if ".undone" in p.stem:
            continue
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for rec in m.get("moves", []):
            to = rec.get("to")
            if not to:
                continue
            try:
                to_abs = str(Path(to).resolve())
            except OSError:
                continue
            # _捨て 配下のみ対象
            if f"/{TRASH_DIR}/" not in to_abs and not to_abs.endswith(f"/{TRASH_DIR}"):
                continue
            out[to_abs] = {
                "from": rec.get("from"),
                "reason": rec.get("reason", ""),
                "moved_at": rec.get("moved_at", ""),
                "manifest": str(p),
            }
    return out


def _age_days(iso_ts: str) -> float | None:
    if not iso_ts:
        return None
    try:
        # `_now()` は ISO with offset; `_ts()` は %Y%m%d-%H%M%S。前者だけ来る想定。
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        now = datetime.now(t.tzinfo)
        return (now - t).total_seconds() / 86400.0
    except ValueError:
        return None


def cmd_review(args: argparse.Namespace) -> int:
    """_捨て/ の中身を見渡し、必要に応じて復元 / 物理削除する（v5）。

    list（既定）:
      _捨て/ にあるファイルを、隔離日時・元の場所・理由つきで一覧表示。

    --restore <pattern>:
      glob で一致する隔離ファイルを元の場所（manifest の from）へ戻す。
      apply と同じく宛先衝突時はリネームで回避。

    --purge [--older-than DAYS]:
      物理削除（tidy の唯一の delete 経路）。明示の --yes が必須で、
      --older-than を指定したときはその日数より古い隔離ファイルだけが対象。
      manifest 自体は残し、purge した経緯を別ログ purge-<ts>.jsonl に追記する。
    """
    import fnmatch
    import shutil

    root = Path(args.dir).expanduser().resolve()
    trash_dir = root / TRASH_DIR
    if not trash_dir.is_dir():
        print(f"_捨て/ がありません: {trash_dir}", file=sys.stderr)
        return 2

    files = _scan_trash(root)
    history = _collect_trash_history(root)

    def _entry(p: Path) -> dict:
        h = history.get(str(p), {})
        return {
            "path": p,
            "rel": str(p.relative_to(trash_dir)),
            "size": p.stat().st_size if p.is_file() else 0,
            "from": h.get("from"),
            "reason": h.get("reason", ""),
            "moved_at": h.get("moved_at", ""),
            "age_days": _age_days(h.get("moved_at", "")),
            "tracked": bool(h),
        }

    entries = [_entry(p) for p in files]

    # ---- restore ----
    if args.restore:
        matches = [e for e in entries
                   if fnmatch.fnmatch(e["rel"], args.restore)
                   or fnmatch.fnmatch(e["path"].name, args.restore)]
        if not matches:
            print(f"パターンに一致する隔離ファイルがありません: {args.restore}", file=sys.stderr)
            return 1
        without_origin = [m for m in matches if not m["from"]]
        if without_origin:
            print("✕ 復元元が記録されていないファイルがあります（restore できません）:", file=sys.stderr)
            for m in without_origin:
                print(f"    {m['rel']}", file=sys.stderr)
            return 1
        if not args.yes:
            print(f"{len(matches)} 件を復元予定:")
            for m in matches[:20]:
                print(f"  {m['rel']}  →  {m['from']}")
            if len(matches) > 20:
                print(f"  … 他 {len(matches) - 20} 件")
            print("\n--yes を付けて再実行してください。")
            return 1
        restored = 0
        # 復元の journal は manifest と区別するため別ファイル名にする
        rlog = root / LOG_DIR / f"restore-{_ts()}.jsonl"
        rlog.parent.mkdir(parents=True, exist_ok=True)
        with rlog.open("a", encoding="utf-8") as jf:
            for m in matches:
                src = m["path"]
                dst = Path(m["from"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                final = _dedupe_dest(dst, set())
                shutil.move(str(src), str(final))
                jf.write(json.dumps({"ts": _now(), "from": str(src), "to": str(final),
                                     "reason": "review --restore"}, ensure_ascii=False) + "\n")
                restored += 1
        _record_history("review-restore", root, restored=restored,
                        pattern=args.restore, log=str(rlog))
        print(f"復元完了: {restored} 件を元の場所へ戻しました。")
        print(f"記録: {rlog}")
        return 0

    # ---- purge ----
    if args.purge:
        targets = list(entries)
        if args.older_than is not None:
            targets = [e for e in targets if (e["age_days"] is not None
                                              and e["age_days"] >= args.older_than)]
        if not targets:
            print("削除対象がありません。", file=sys.stderr)
            return 1
        if not args.yes:
            print(f"⚠ 物理削除予定: {len(targets)} 件（合計 {_humanize_bytes(sum(e['size'] for e in targets))}）")
            for e in targets[:20]:
                age = f"{e['age_days']:.0f}d" if e["age_days"] is not None else "?"
                print(f"  {e['rel']}  ({_humanize_bytes(e['size'])}, age {age})")
            if len(targets) > 20:
                print(f"  … 他 {len(targets) - 20} 件")
            print("\n削除は取り消せません。実行するには --yes を付けてください。")
            return 1
        plog = root / LOG_DIR / f"purge-{_ts()}.jsonl"
        plog.parent.mkdir(parents=True, exist_ok=True)
        purged = 0
        with plog.open("a", encoding="utf-8") as jf:
            for e in targets:
                try:
                    e["path"].unlink()
                except OSError as err:
                    print(f"削除失敗: {e['rel']} ({err})", file=sys.stderr)
                    continue
                jf.write(json.dumps({"ts": _now(), "path": str(e["path"]),
                                     "size": e["size"], "from": e["from"]},
                                    ensure_ascii=False) + "\n")
                purged += 1
        purged_bytes = sum(e["size"] for e in targets)
        _record_history("review-purge", root, purged=purged, bytes=purged_bytes,
                        older_than=args.older_than, log=str(plog))
        print(f"物理削除: {purged} 件 ({_humanize_bytes(purged_bytes)})")
        print(f"記録: {plog}")
        return 0

    # ---- list（既定） ----
    if not entries:
        print(f"_捨て/ は空です: {trash_dir}")
        return 0
    total = sum(e["size"] for e in entries)
    tracked = sum(1 for e in entries if e["tracked"])
    print(f"== _捨て の中身 ==")
    print(f"対象: {root}")
    print(f"ファイル数: {len(entries)} 件 / 合計 {_humanize_bytes(total)} / "
          f"manifest 追跡済み {tracked}/{len(entries)}\n")
    for e in entries[: args.limit]:
        age = f"{e['age_days']:.0f}d 前" if e["age_days"] is not None else "—"
        size = _humanize_bytes(e["size"])
        line = f"  {e['rel']}  ({size}, {age})"
        print(line)
        if e["reason"]:
            print(f"      理由: {e['reason']}")
        if e["from"]:
            print(f"      元の場所: {e['from']}")
        if not e["tracked"]:
            print(f"      ※ manifest 未追跡（手で置かれたか古いログ）")
    if len(entries) > args.limit:
        print(f"  … 他 {len(entries) - args.limit} 件")
    print()
    print("次の手順:")
    print("  復元（特定パターン）: review --restore '<glob>' --yes")
    print("  物理削除（30日より古い）: review --purge --older-than 30 --yes")
    return 0


# --------------------------------------------------------------------------- history
def _read_history() -> list[dict]:
    """TIDY_HOME/history.jsonl を読んで、記録の配列を返す（新しい順）。"""
    path = _tidy_home() / "history.jsonl"
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 壊れた行はスキップ（追記専用なので普通は起きない）
    out.reverse()  # 新しいものを上に
    return out


# action ごとの「1行サマリ」を作る小関数。history 表示の見やすさのためだけの整形。
def _history_line(rec: dict) -> str:
    action = rec.get("action", "?")
    if action == "apply":
        detail = f"{rec.get('moves', 0)} 件移動" + (
            f"（うち {rec.get('trashed', 0)} 件を隔離）" if rec.get("trashed") else "")
    elif action == "undo":
        detail = f"{rec.get('restored', 0)} 件を復元"
    elif action == "redo":
        detail = f"{rec.get('moves', 0)} 件を再適用"
    elif action == "review-restore":
        detail = f"{rec.get('restored', 0)} 件を _捨て から復元" + (
            f"（{rec['pattern']}）" if rec.get("pattern") else "")
    elif action == "review-purge":
        b = rec.get("bytes")
        detail = f"{rec.get('purged', 0)} 件を物理削除" + (
            f"（{_humanize_bytes(b)}）" if b else "")
    else:
        detail = ""
    return detail


# action → 表示ラベル（記号つきで一目で区別できるように）
_ACTION_LABEL = {
    "apply": "▸ 整理",
    "undo": "↩ 取り消し",
    "redo": "↪ やり直し",
    "review-restore": "⤴ 復元",
    "review-purge": "✕ 物理削除",
}


def cmd_history(args: argparse.Namespace) -> int:
    """tidy が今まで全ての場所で行った操作を、1本のタイムラインで表示する（v6）。

    どのディレクトリを整理しても、成功した mutating 操作（apply/undo/redo/
    review-restore/review-purge）は ~/.tidy/history.jsonl に集約されている。
    「先週 tidy で何やったっけ」を場所をまたいで振り返れる。
    --target で特定ディレクトリだけに絞れる。
    """
    records = _read_history()
    home = _tidy_home()

    if args.target:
        want = str(Path(args.target).expanduser().resolve())
        records = [r for r in records if r.get("target") == want]

    if not records:
        where = f"（対象: {args.target}）" if args.target else ""
        print(f"操作履歴はまだありません{where}。")
        print(f"（履歴の保存先: {home / 'history.jsonl'}）")
        return 0

    shown = records[: args.limit]
    print(f"== tidy 操作履歴 ==")
    print(f"保存先: {home / 'history.jsonl'}")
    if args.target:
        print(f"対象フィルタ: {args.target}")
    print(f"記録数: {len(records)} 件（最新 {len(shown)} 件を表示）\n")

    for r in shown:
        ts = r.get("ts", "")
        # ISO の "2026-06-24T17:24:54+09:00" を "2026-06-24 17:24" 程度に短縮
        ts_short = ts.replace("T", " ")[:16] if ts else "?"
        label = _ACTION_LABEL.get(r.get("action", ""), r.get("action", "?"))
        detail = _history_line(r)
        print(f"  {ts_short}  {label}  {detail}")
        if not args.target:
            print(f"      場所: {r.get('target', '?')}")
        manifest = r.get("manifest") or r.get("log")
        if manifest and args.verbose:
            print(f"      記録: {manifest}")

    if len(records) > args.limit:
        print(f"\n  … 他 {len(records) - args.limit} 件（--limit で増やせます）")
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
    s.add_argument("--no-deps", action="store_true",
                   help="コード依存（import）解析を省略（巨大ディレクトリの高速化用）")
    s.set_defaults(func=cmd_scan)

    sg = sub.add_parser("suggest", help="scan.json から baseline plan.json を自動生成")
    sg.add_argument("--in", dest="infile", required=True, help="入力 scan.json")
    sg.add_argument("--out", default=None, help="出力先 plan.json（省略時は標準出力）")
    sg.add_argument("--target", default=None,
                    help="集約モード（複数 root 走査時）の宛先ルート。単一 root では省略可")
    sg.set_defaults(func=cmd_suggest)

    pv = sub.add_parser("preview", help="plan.json を検証して dry-run 表示")
    pv.add_argument("dir", help="宛先ルート（_捨て/_整理ログ を置く場所。全 to はこの中に入る）")
    pv.add_argument("--in", dest="infile", required=True, help="plan.json")
    pv.add_argument("--limit", type=int, default=20, help="宛先ごとの表示件数上限")
    pv.set_defaults(func=cmd_preview)

    ap = sub.add_parser("apply", help="plan.json に従って移動を実行")
    ap.add_argument("dir", help="宛先ルート（_捨て/_整理ログ を置く場所。全 to はこの中に入る）")
    ap.add_argument("--in", dest="infile", required=True, help="plan.json")
    ap.add_argument("--yes", action="store_true", help="確認をスキップして実行")
    ap.add_argument("--dry-run", action="store_true", help="preview と等価（移動しない）")
    ap.set_defaults(func=cmd_apply)

    vf = sub.add_parser("verify", help="直近 apply の整合性を確認（from が消え、to が存在することを確認）")
    vf.add_argument("dir", help="apply 時に指定した宛先ルート")
    vf.add_argument("--manifest", default=None, help="検証する manifest（省略時は最新）")
    vf.set_defaults(func=cmd_verify)

    ud = sub.add_parser("undo", help="最新 manifest を逆再生して復元")
    ud.add_argument("dir", help="apply 時に指定した宛先ルート")
    ud.add_argument("--manifest", default=None, help="使用する manifest（省略時は最新）")
    ud.set_defaults(func=cmd_undo)

    rd = sub.add_parser("redo", help="直前の undo を取り消して apply 後の状態へ戻す")
    rd.add_argument("dir", help="apply 時に指定した宛先ルート")
    rd.add_argument("--manifest", default=None,
                    help="再適用する .undone.json（省略時は最新の消費未済 undo）")
    rd.set_defaults(func=cmd_redo)

    rv = sub.add_parser("review", help="_捨て/ の中身を一覧 / 復元 / 物理削除する（v5）")
    rv.add_argument("dir", help="apply 時に指定した宛先ルート")
    rv.add_argument("--limit", type=int, default=30, help="一覧表示の上限件数")
    rv.add_argument("--restore", default=None,
                    help="glob パターン一致の隔離ファイルを元の場所へ戻す（要 --yes）")
    rv.add_argument("--purge", action="store_true",
                    help="隔離ファイルを物理削除する（要 --yes、tidy 唯一の delete 経路）")
    rv.add_argument("--older-than", type=float, default=None,
                    metavar="DAYS",
                    help="--purge と併用。指定日数より古いものだけを対象にする")
    rv.add_argument("--yes", action="store_true", help="restore/purge の確認をスキップ")
    rv.set_defaults(func=cmd_review)

    hi = sub.add_parser("history",
                        help="tidy が全ての場所で行った操作を1本のタイムラインで表示する（v6）")
    hi.add_argument("--target", default=None,
                    help="特定ディレクトリの操作だけに絞る")
    hi.add_argument("--limit", type=int, default=30, help="表示する最大件数")
    hi.add_argument("--verbose", action="store_true", help="manifest/ログのパスも表示")
    hi.set_defaults(func=cmd_history)

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
