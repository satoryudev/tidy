#!/usr/bin/env python3
"""dir-organizer の総合テスト。

一時的な「仮想ディレクトリ」(sandbox) を作り、考えられるケースを片っ端から検証する。
organize.py を実際に CLI として呼び出す結合テスト（黒箱）。標準ライブラリのみで動く。

使い方:
    python3 tests/run_tests.py          # 全テスト実行
失敗が1つでもあれば終了コード 1。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "organize.py"
PY = sys.executable

_passed = 0
_failed = 0


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(SCRIPT), *args],
                          capture_output=True, text=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def snapshot(root: Path, exclude=("_整理ログ", "_捨て")) -> dict[str, str]:
    """root 配下の {相対パス: sha1}。指定トップフォルダは除外。"""
    out: dict[str, str] = {}
    for dp, dirnames, files in os.walk(root):
        d = Path(dp)
        rel = d.relative_to(root)
        if rel.parts and rel.parts[0] in exclude:
            dirnames[:] = []
            continue
        for f in files:
            fp = d / f
            out[str(fp.relative_to(root))] = sha(fp)
    return out


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def verdict_of(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("判定 ["):
            return line.split("[", 1)[1].split("]", 1)[0]
    return "?"


# --------------------------------------------------------------------------- cases
def t_inplace_roundtrip(box: Path):
    r = box / "inplace"
    (r / "sub").mkdir(parents=True)
    (r / "spec.md").write_text("# 仕様\n本文\n", encoding="utf-8")
    (r / "run.py").write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
    (r / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (r / "sub" / "メモ.txt").write_text("memo\n", encoding="utf-8")
    (r / "junk.tmp").write_text("x", encoding="utf-8")
    base = snapshot(r)

    plan = box / "inplace.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "spec.md", "to": "ドキュメント/spec.md"},
        {"from": "run.py", "to": "コード/run.py"},
        {"from": "data.csv", "to": "データ/data.csv"},
        {"from": "sub/メモ.txt", "to": "ドキュメント/メモ.txt"},
        {"from": "junk.tmp", "to": "_捨て/junk.tmp"},
    ]})
    pv = run("preview", str(r), "--in", str(plan))
    check("inplace: preview OK", pv.returncode == 0 and "取り残し" not in pv.stdout, pv.stdout)
    ap = run("apply", str(r), "--in", str(plan), "--yes")
    check("inplace: apply rc=0", ap.returncode == 0, ap.stderr)
    check("inplace: 構造ができた", (r / "ドキュメント" / "spec.md").is_file()
          and (r / "_捨て" / "junk.tmp").is_file())
    ud = run("undo", str(r))
    check("inplace: undo rc=0", ud.returncode == 0, ud.stderr)
    check("inplace: undo で完全復元（ハッシュ一致）", snapshot(r) == base,
          f"{base} != {snapshot(r)}")


def t_junk_and_types(box: Path):
    r = box / "types"
    r.mkdir()
    (r / ".DS_Store").write_bytes(b"")
    (r / "a.tmp").write_text("t", encoding="utf-8")
    (r / "~$lock.docx").write_text("l", encoding="utf-8")
    (r / "m.pyc").write_text("c", encoding="utf-8")
    (r / "empty.dat").write_bytes(b"")
    (r / "日本語.md").write_text("# 見出し\n日本語の本文です\n", encoding="utf-8")
    (r / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00IHDR")
    sc = run("scan", str(r), "--out", str(box / "s.json"))
    check("scan rc=0", sc.returncode == 0, sc.stderr)
    d = json.loads((box / "s.json").read_text(encoding="utf-8"))
    by = {f["path"]: f for f in d["files"]}
    junk_ok = all(by[n]["junk"] for n in [".DS_Store", "a.tmp", "~$lock.docx", "m.pyc", "empty.dat"])
    check("junk検出: ゴミに印が付く", junk_ok)
    check("日本語md はテキスト扱い + snippet あり",
          by["日本語.md"]["binary"] is False and "日本語" in by["日本語.md"]["snippet"],
          str(by["日本語.md"]))
    check("PNG はバイナリ扱い", by["pic.png"]["binary"] is True)


def t_consolidate_dedupe(box: Path):
    dl, desk, docs = box / "dl", box / "desk", box / "docs" / "old"
    for d in (dl, desk, docs):
        d.mkdir(parents=True)
    (dl / "第3回_線形代数.pdf").write_text("LA week3\n", encoding="utf-8")
    (desk / "線形代数_第3回.pdf").write_text("LA week3\n", encoding="utf-8")  # 重複
    (docs / "micro_lec05.pdf").write_text("micro 5\n", encoding="utf-8")
    (dl / "個人メモ.txt").write_text("private\n", encoding="utf-8")  # 無関係
    (desk / ".DS_Store").write_bytes(b"")
    src_base = {**snapshot(dl, ()), **snapshot(desk, ()), **snapshot(docs, ())}

    sc = run("scan", str(dl), str(desk), str(docs), "--out", str(box / "cs.json"))
    d = json.loads((box / "cs.json").read_text(encoding="utf-8"))
    check("consolidate: 3 root 走査", len(d["roots"]) == 3, str(d["roots"]))
    check("consolidate: 場所をまたいだ重複1組", len(d["duplicates"]) == 1, str(d["duplicates"]))
    hints = {Path(f["abspath"]).name: f["course_hint"] for f in d["files"]}
    check("consolidate: 講義hint 正しい",
          hints["第3回_線形代数.pdf"] and not hints["個人メモ.txt"], str(hints))

    target = box / "講義資料"
    target.mkdir()
    plan = box / "cplan.json"
    write_json(plan, {"root": str(target), "trash_dir": "_捨て", "moves": [
        {"from": str(dl / "第3回_線形代数.pdf"), "to": "線形代数/第3回.pdf"},
        {"from": str(desk / "線形代数_第3回.pdf"), "to": "_捨て/線形代数_第3回.pdf"},
        {"from": str(docs / "micro_lec05.pdf"), "to": "ミクロ経済/lec05.pdf"},
        {"from": str(desk / ".DS_Store"), "to": "_捨て/.DS_Store"},
    ]})
    pv = run("preview", str(target), "--in", str(plan))
    check("consolidate: 無関係ファイルは取り残し警告に出ない",
          pv.returncode == 0 and "取り残し" not in pv.stdout, pv.stdout)
    ap = run("apply", str(target), "--in", str(plan), "--yes")
    check("consolidate: apply rc=0", ap.returncode == 0, ap.stderr)
    check("consolidate: 講義フォルダに集約", (target / "線形代数" / "第3回.pdf").is_file()
          and (target / "ミクロ経済" / "lec05.pdf").is_file())
    check("consolidate: 重複と.DS_Storeを隔離", (target / "_捨て" / "線形代数_第3回.pdf").is_file()
          and (target / "_捨て" / ".DS_Store").is_file())
    check("consolidate: 無関係ファイルは元のまま", (dl / "個人メモ.txt").is_file())
    ud = run("undo", str(target))
    after = {**snapshot(dl, ()), **snapshot(desk, ()), **snapshot(docs, ())}
    check("consolidate: undo で元の散らばった場所へ完全復元",
          ud.returncode == 0 and after == src_base, f"{src_base} != {after}")


def t_collision(box: Path):
    r = box / "collide"
    r.mkdir()
    (r / "a.txt").write_text("A", encoding="utf-8")
    (r / "b.txt").write_text("B", encoding="utf-8")
    base = snapshot(r)
    plan = box / "collision.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "a.txt", "to": "out/merged.txt"},
        {"from": "b.txt", "to": "out/merged.txt"},
    ]})
    run("apply", str(r), "--in", str(plan), "--yes")
    files = sorted(p.name for p in (r / "out").iterdir())
    check("collision: 上書きせずリネーム", files == ["merged (1).txt", "merged.txt"], str(files))
    contents = {(r / "out" / f).read_text(encoding="utf-8") for f in files}
    check("collision: 両方の中身が残る", contents == {"A", "B"}, str(contents))
    run("undo", str(r))
    check("collision: undo で復元", snapshot(r) == base)


def t_preview_uncovered(box: Path):
    r = box / "uncov"
    r.mkdir()
    (r / "x.txt").write_text("x", encoding="utf-8")
    (r / "y.txt").write_text("y", encoding="utf-8")  # plan に入れない
    plan = box / "uncov.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て",
                      "moves": [{"from": "x.txt", "to": "d/x.txt"}]})
    pv = run("preview", str(r), "--in", str(plan))
    check("uncovered: 取り残しを警告", pv.returncode == 0 and "y.txt" in pv.stdout
          and "含まれていない" in pv.stdout, pv.stdout)


def t_safety_escape(box: Path):
    r = box / "escape"
    r.mkdir()
    (r / "f.txt").write_text("data", encoding="utf-8")
    outside = box / "escape_target.txt"
    plan = box / "escape.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て",
                      "moves": [{"from": "f.txt", "to": "../escape_target.txt"}]})
    pv = run("preview", str(r), "--in", str(plan))
    check("safety: root外への to は preview でエラー", pv.returncode == 1, pv.stdout)
    ap = run("apply", str(r), "--in", str(plan), "--yes")
    check("safety: apply も拒否し移動しない",
          ap.returncode != 0 and not outside.exists() and (r / "f.txt").is_file(), ap.stderr)


def t_requires_yes(box: Path):
    r = box / "needyes"
    r.mkdir()
    (r / "f.txt").write_text("d", encoding="utf-8")
    plan = box / "needyes.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て",
                      "moves": [{"from": "f.txt", "to": "d/f.txt"}]})
    ap = run("apply", str(r), "--in", str(plan))  # --yes なし
    check("--yes なしでは移動しない", ap.returncode == 1 and (r / "f.txt").is_file(), ap.stdout)


def t_interrupt_resilience(box: Path):
    r = box / "interrupt"
    r.mkdir()
    (r / "a.txt").write_text("A", encoding="utf-8")
    (r / "b.txt").write_text("B", encoding="utf-8")
    (r / "c.txt").write_text("C", encoding="utf-8")
    (r / "BLOCK").write_text("i am a file", encoding="utf-8")  # ここを親にすると mkdir 失敗
    plan = box / "interrupt.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "a.txt", "to": "g1/a.txt"},
        {"from": "b.txt", "to": "g2/b.txt"},
        {"from": "c.txt", "to": "BLOCK/sub/c.txt"},  # 失敗する（BLOCK はファイル）
    ]})
    ap = run("apply", str(r), "--in", str(plan), "--yes")
    check("interrupt: 途中失敗で rc!=0", ap.returncode != 0, ap.stdout)
    logs = list((r / "_整理ログ").glob("manifest-*.json"))
    check("interrupt: manifest が残っている（中断耐性）", len(logs) == 1, str(logs))
    man = json.loads(logs[0].read_text(encoding="utf-8")) if logs else {"moves": []}
    check("interrupt: 完了分だけ記録（2件）", len(man.get("moves", [])) == 2, str(man.get("moves")))
    ud = run("undo", str(r))
    check("interrupt: undo で完了分を復元", ud.returncode == 0
          and (r / "a.txt").is_file() and (r / "b.txt").is_file()
          and (r / "c.txt").is_file(), ud.stderr)


def t_undo_twice(box: Path):
    r = box / "twice"
    r.mkdir()
    (r / "f.txt").write_text("d", encoding="utf-8")
    base = snapshot(r)
    plan = box / "twice.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て",
                      "moves": [{"from": "f.txt", "to": "d/f.txt"}]})
    run("apply", str(r), "--in", str(plan), "--yes")
    run("undo", str(r))
    snap1 = snapshot(r)
    ud2 = run("undo", str(r))  # 2回目：消費済みなので対象なし
    check("undo二重実行: 2回目は対象なし(rc=2)", ud2.returncode == 2, ud2.stdout + ud2.stderr)
    check("undo二重実行: ファイルは変化しない", snapshot(r) == snap1 == base)


def t_assess_project(box: Path):
    r = box / "proj"
    (r / ".git").mkdir(parents=True)
    (r / "src").mkdir()
    (r / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (r / "src" / "index.js").write_text("console.log(1)", encoding="utf-8")
    a = run("assess", str(r))
    check("assess: gitプロジェクトは NG", verdict_of(a.stdout) == "NG", a.stdout)


def t_assess_clutter(box: Path):
    r = box / "clutter"
    r.mkdir()
    for i in range(12):
        ext = [".txt", ".pdf", ".png", ".csv", ".md"][i % 5]
        (r / f"file{i}{ext}").write_text(f"content {i}\n", encoding="utf-8")
    (r / ".DS_Store").write_bytes(b"")
    (r / "dupA.txt").write_text("same\n", encoding="utf-8")
    (r / "dupB.txt").write_text("same\n", encoding="utf-8")
    a = run("assess", str(r))
    check("assess: 散らかりは SKILL向き", verdict_of(a.stdout) == "SKILL", a.stdout)


def t_assess_small(box: Path):
    r = box / "small"
    r.mkdir()
    (r / "a.txt").write_text("a", encoding="utf-8")
    (r / "b.txt").write_text("b", encoding="utf-8")
    a = run("assess", str(r))
    check("assess: 少量は MANUAL", verdict_of(a.stdout) == "MANUAL", a.stdout)


def t_assess_organized(box: Path):
    r = box / "organized"
    (r / "docs" / "a" / "b").mkdir(parents=True)
    for i in range(6):
        (r / "docs" / "a" / "b" / f"n{i}.txt").write_text(str(i), encoding="utf-8")
    (r / "readme.txt").write_text("r", encoding="utf-8")
    (r / "notes.txt").write_text("n", encoding="utf-8")
    a = run("assess", str(r))
    check("assess: 整理済みは SKILL ではない", verdict_of(a.stdout) != "SKILL", a.stdout)


CASES = [
    t_inplace_roundtrip, t_junk_and_types, t_consolidate_dedupe, t_collision,
    t_preview_uncovered, t_safety_escape, t_requires_yes, t_interrupt_resilience,
    t_undo_twice, t_assess_project, t_assess_clutter, t_assess_small, t_assess_organized,
]


def main() -> int:
    box = Path(tempfile.mkdtemp(prefix="dir-organizer-test-"))
    print(f"sandbox: {box}\n")
    try:
        for case in CASES:
            print(f"[{case.__name__}]")
            case(box)
    finally:
        shutil.rmtree(box, ignore_errors=True)
    print(f"\n結果: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
