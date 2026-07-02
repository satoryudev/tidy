#!/usr/bin/env python3
"""tidy の総合テスト。

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


def run(*args: str, tidy_home: str | None = None) -> subprocess.CompletedProcess:
    # TIDY_HOME を必ず与えて、実 ~/.tidy を汚さないようにする。個別テストで
    # 履歴の中身を検証したいときは tidy_home に専用の一時パスを渡す。
    env = dict(os.environ)
    if tidy_home is not None:
        env["TIDY_HOME"] = tidy_home
    return subprocess.run([PY, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env)


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


def t_suggest_inplace(box: Path):
    """suggest: 単一 root から baseline plan を作り、preview/apply まで走る。"""
    r = box / "sg_inplace"
    r.mkdir()
    (r / "仕様メモ.md").write_text("# 仕様\n本文\n", encoding="utf-8")
    (r / "run.py").write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
    (r / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (r / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (r / ".DS_Store").write_bytes(b"")
    (r / "dup.txt").write_text("dup\n", encoding="utf-8")
    (r / "dup のコピー.txt").write_text("dup\n", encoding="utf-8")

    scan_p = box / "sg_in.scan.json"
    plan_p = box / "sg_in.plan.json"
    sc = run("scan", str(r), "--out", str(scan_p))
    check("suggest/inplace: scan rc=0", sc.returncode == 0, sc.stderr)
    sg = run("suggest", "--in", str(scan_p), "--out", str(plan_p))
    check("suggest/inplace: rc=0", sg.returncode == 0, sg.stderr)

    plan = json.loads(plan_p.read_text(encoding="utf-8"))
    check("suggest/inplace: mode は in-place", plan["mode"] == "in-place", plan.get("mode"))
    by_from = {m["from"]: m["to"] for m in plan["moves"]}
    check("suggest/inplace: 仕様md は 仕様/ へ",
          by_from.get("仕様メモ.md", "").startswith("ドキュメント/仕様/"), str(by_from))
    check("suggest/inplace: シェバン py は コード/ へ",
          by_from.get("run.py", "").startswith("コード/"), str(by_from))
    check("suggest/inplace: png は 画像/ へ",
          by_from.get("photo.png", "").startswith("画像/"), str(by_from))
    check("suggest/inplace: csv は データ/ へ",
          by_from.get("data.csv", "").startswith("データ/"), str(by_from))
    check("suggest/inplace: .DS_Store は _捨て/ へ",
          by_from.get(".DS_Store", "").startswith("_捨て/"), str(by_from))
    # 重複の片方が _捨て へ、もう片方が通常分類へ
    dup_to = [by_from["dup.txt"], by_from["dup のコピー.txt"]]
    trash_count = sum(1 for t in dup_to if t.startswith("_捨て/"))
    check("suggest/inplace: 重複の片方だけ隔離", trash_count == 1, str(dup_to))

    # 生成した plan で apply まで通る
    pv = run("preview", str(r), "--in", str(plan_p))
    check("suggest/inplace: preview OK", pv.returncode == 0, pv.stdout)
    ap = run("apply", str(r), "--in", str(plan_p), "--yes")
    check("suggest/inplace: apply OK", ap.returncode == 0, ap.stderr)


def t_suggest_consolidate(box: Path):
    """suggest: 複数 root と --target で集約モード plan を作る。"""
    dl, desk, target = box / "sg_dl", box / "sg_desk", box / "sg_target"
    dl.mkdir(); desk.mkdir(); target.mkdir()
    (dl / "第3回_線形代数.pdf").write_text("LA week3\n", encoding="utf-8")
    (desk / "線形代数_第3回.pdf").write_text("LA week3\n", encoding="utf-8")  # 重複
    (dl / "個人メモ.txt").write_text("private\n", encoding="utf-8")

    scan_p = box / "sg_con.scan.json"
    plan_p = box / "sg_con.plan.json"
    run("scan", str(dl), str(desk), "--out", str(scan_p))

    # --target なしはエラー（multi-root + no target）
    sg_err = run("suggest", "--in", str(scan_p), "--out", str(plan_p))
    check("suggest/consolidate: --target 無しは rc=2", sg_err.returncode == 2, sg_err.stderr)

    sg = run("suggest", "--in", str(scan_p), "--out", str(plan_p), "--target", str(target))
    check("suggest/consolidate: rc=0", sg.returncode == 0, sg.stderr)
    plan = json.loads(plan_p.read_text(encoding="utf-8"))
    check("suggest/consolidate: mode は consolidate", plan["mode"] == "consolidate", plan.get("mode"))
    # 集約モードでは from が絶対パス
    abs_count = sum(1 for m in plan["moves"] if Path(m["from"]).is_absolute())
    check("suggest/consolidate: from は絶対パス", abs_count == len(plan["moves"]), str(plan["moves"]))
    # 重複1組 → 片方は _捨て、片方は ドキュメント/講義/ 等の通常分類
    trash_count = sum(1 for m in plan["moves"] if m["to"].startswith("_捨て/"))
    check("suggest/consolidate: 重複の片方を隔離", trash_count == 1, str(plan["moves"]))


def t_verify_clean_and_dirty(box: Path):
    """verify: 正常状態 OK / from が残ってる or to が消えると問題報告。"""
    r = box / "verify_box"
    r.mkdir()
    (r / "x.txt").write_text("x", encoding="utf-8")
    plan_p = box / "verify.plan.json"
    write_json(plan_p, {"root": str(r), "trash_dir": "_捨て",
                        "moves": [{"from": "x.txt", "to": "d/x.txt"}]})
    run("apply", str(r), "--in", str(plan_p), "--yes")
    v_ok = run("verify", str(r))
    check("verify: 正常状態は rc=0", v_ok.returncode == 0
          and "1 / 1" in v_ok.stdout, v_ok.stdout)

    # 移動先を消して dirty にする → verify が問題報告
    (r / "d" / "x.txt").unlink()
    v_bad = run("verify", str(r))
    check("verify: 移動先消失で rc=1", v_bad.returncode == 1
          and "消失" in v_bad.stdout, v_bad.stdout)


def t_verify_no_manifest(box: Path):
    """verify: manifest 無しなら rc=2。"""
    r = box / "verify_empty"
    r.mkdir()
    v = run("verify", str(r))
    check("verify: manifest 無しで rc=2", v.returncode == 2, v.stdout + v.stderr)


def t_redo_after_undo(box: Path):
    """redo: undo 後に redo して apply 後の状態に戻る。"""
    r = box / "redo_box"
    r.mkdir()
    (r / "a.txt").write_text("A", encoding="utf-8")
    (r / "b.txt").write_text("B", encoding="utf-8")
    plan_p = box / "redo.plan.json"
    write_json(plan_p, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "a.txt", "to": "g/a.txt"},
        {"from": "b.txt", "to": "g/b.txt"},
    ]})
    run("apply", str(r), "--in", str(plan_p), "--yes")
    applied = snapshot(r)
    run("undo", str(r))
    rd = run("redo", str(r))
    check("redo: rc=0", rd.returncode == 0, rd.stderr)
    check("redo: apply 後と同じ状態に戻る", snapshot(r) == applied,
          f"{applied} != {snapshot(r)}")
    # 同じ undo を二度 redo できない
    rd2 = run("redo", str(r))
    check("redo: 二回目は rc=2", rd2.returncode == 2, rd2.stdout + rd2.stderr)


def t_self_move_rejected(box: Path):
    """from == to の plan は preview と apply の両方で拒否される。"""
    r = box / "selfmove"
    r.mkdir()
    (r / "x.txt").write_text("x", encoding="utf-8")
    plan_p = box / "self.plan.json"
    write_json(plan_p, {"root": str(r), "trash_dir": "_捨て",
                        "moves": [{"from": "x.txt", "to": "x.txt"}]})
    pv = run("preview", str(r), "--in", str(plan_p))
    check("self-move: preview がエラー", pv.returncode == 1
          and "同じです" in pv.stdout, pv.stdout)
    ap = run("apply", str(r), "--in", str(plan_p), "--yes")
    check("self-move: apply もエラー", ap.returncode != 0
          and (r / "x.txt").is_file(), ap.stderr)


def t_apply_dry_run(box: Path):
    """apply --dry-run は preview と等価で、ファイルは動かない。"""
    r = box / "dryrun"
    r.mkdir()
    (r / "a.txt").write_text("a", encoding="utf-8")
    plan_p = box / "dryrun.plan.json"
    write_json(plan_p, {"root": str(r), "trash_dir": "_捨て",
                        "moves": [{"from": "a.txt", "to": "d/a.txt"}]})
    dr = run("apply", str(r), "--in", str(plan_p), "--dry-run")
    check("apply --dry-run: rc=0", dr.returncode == 0, dr.stderr)
    check("apply --dry-run: ファイルは動かない", (r / "a.txt").is_file(), "moved!")
    check("apply --dry-run: dry-run らしい表記が出る",
          "プレビュー" in dr.stdout or "移動しません" in dr.stdout, dr.stdout)


def t_preview_shows_size(box: Path):
    """preview の総サイズ表示が正しい。"""
    r = box / "size_box"
    r.mkdir()
    (r / "big.txt").write_text("x" * 2048, encoding="utf-8")
    (r / "small.txt").write_text("y", encoding="utf-8")
    plan_p = box / "size.plan.json"
    write_json(plan_p, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "big.txt", "to": "d/big.txt"},
        {"from": "small.txt", "to": "_捨て/small.txt"},
    ]})
    pv = run("preview", str(r), "--in", str(plan_p))
    check("preview: 総サイズが KB 表示される", "2.0 KB" in pv.stdout or "2049" in pv.stdout, pv.stdout)
    check("preview: 隔離分のサイズも表記される", "_捨て/ へ" in pv.stdout, pv.stdout)


def t_scan_extracts_python_imports(box: Path):
    """scan: Python の sibling import / dot relative / 外部 を区別する。"""
    r = box / "py_deps"
    r.mkdir()
    (r / "main.py").write_text(
        "from helper import greet\n"
        "from .utils import calc\n"
        "import os\n"
        "import requests\n"
        "print(greet(), calc())\n", encoding="utf-8")
    (r / "helper.py").write_text("def greet(): return 'hi'\n", encoding="utf-8")
    (r / "utils.py").write_text("def calc(): return 42\n", encoding="utf-8")
    sc = run("scan", str(r), "--out", str(box / "py_deps.json"))
    check("scan/py: rc=0", sc.returncode == 0, sc.stderr)
    d = json.loads((box / "py_deps.json").read_text(encoding="utf-8"))
    by_name = {Path(f["abspath"]).name: f for f in d["files"]}
    main_imports = sorted(Path(p).name for p in by_name["main.py"]["imports"])
    check("scan/py: sibling と dot relative の両方を解決",
          main_imports == ["helper.py", "utils.py"], str(main_imports))
    check("scan/py: 外部 (os, requests) は解決されない",
          "os" not in str(by_name["main.py"]["imports"]) and "requests" not in str(by_name["main.py"]["imports"]),
          str(by_name["main.py"]["imports"]))
    check("scan/py: helper.py は imported_by に main.py を持つ",
          any("main.py" in p for p in by_name["helper.py"]["imported_by"]),
          str(by_name["helper.py"]["imported_by"]))
    clusters = d["code_dependencies"]["clusters"]
    check("scan/py: 1クラスタ・3メンバー",
          len(clusters) == 1 and len(clusters[0]["members"]) == 3, str(clusters))


def t_scan_extracts_js_html_css(box: Path):
    """scan: JS の import/require、HTML の src/href、CSS の @import/url を解決する。"""
    r = box / "web_deps"
    r.mkdir()
    (r / "index.js").write_text(
        "import { Foo } from './bar'\n"
        "import lodash from 'lodash'\n"
        "const u = require('./utils.js')\n", encoding="utf-8")
    (r / "bar.js").write_text("export const Foo = 1\n", encoding="utf-8")
    (r / "utils.js").write_text("module.exports = 1\n", encoding="utf-8")
    (r / "page.html").write_text(
        '<link rel="stylesheet" href="./style.css">\n'
        '<script src="./app.js"></script>\n'
        '<img src="https://example.com/x.png">\n', encoding="utf-8")
    (r / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    (r / "style.css").write_text("body{color:red}\n", encoding="utf-8")
    run("scan", str(r), "--out", str(box / "web_deps.json"))
    d = json.loads((box / "web_deps.json").read_text(encoding="utf-8"))
    by_name = {Path(f["abspath"]).name: f for f in d["files"]}
    idx_imports = sorted(Path(p).name for p in by_name["index.js"]["imports"])
    check("scan/js: 拡張子なしと相対パス両方を解決",
          idx_imports == ["bar.js", "utils.js"], str(idx_imports))
    check("scan/js: lodash は解決されない", all("lodash" not in p for p in by_name["index.js"]["imports"]))
    page_imports = sorted(Path(p).name for p in by_name["page.html"]["imports"])
    check("scan/html: src と href を解決",
          page_imports == ["app.js", "style.css"], str(page_imports))
    check("scan/html: 外部 URL は除外",
          all("example.com" not in p for p in by_name["page.html"]["imports"]))
    cluster_names = sorted(c["name"] for c in d["code_dependencies"]["clusters"])
    check("scan: クラスタ 2 つ（page / index）", "page" in cluster_names and "index" in cluster_names,
          str(cluster_names))


def t_suggest_groups_cluster(box: Path):
    """suggest: クラスタは同じサブフォルダにまとめる + plan に clusters を埋め込む。"""
    r = box / "sg_cluster"
    r.mkdir()
    (r / "main.py").write_text("from helper import g\n", encoding="utf-8")
    (r / "helper.py").write_text("def g(): pass\n", encoding="utf-8")
    (r / "lonely.py").write_text("print('alone')\n", encoding="utf-8")
    (r / "data.csv").write_text("a,b\n", encoding="utf-8")
    sc = box / "sg_cl.scan.json"
    pl = box / "sg_cl.plan.json"
    run("scan", str(r), "--out", str(sc))
    run("suggest", "--in", str(sc), "--out", str(pl))
    plan = json.loads(pl.read_text(encoding="utf-8"))
    by_from = {m["from"]: m["to"] for m in plan["moves"]}
    # クラスタの2ファイルが同じディレクトリへ
    parents = {str(Path(by_from["main.py"]).parent), str(Path(by_from["helper.py"]).parent)}
    check("suggest/cluster: main.py と helper.py が同じ宛先 dir",
          len(parents) == 1 and "コード/main" in next(iter(parents)),
          f"{by_from.get('main.py')} vs {by_from.get('helper.py')}")
    # 単独ファイルは クラスタ無しの通常分類
    check("suggest/cluster: 孤立ファイルはクラスタ subfolder に入らない",
          by_from["lonely.py"] == "コード/lonely.py", by_from.get("lonely.py"))
    # plan に clusters が埋め込まれている
    check("suggest/cluster: plan に clusters セクション",
          isinstance(plan.get("clusters"), list) and len(plan["clusters"]) == 1,
          str(plan.get("clusters")))


def t_preview_warns_on_cluster_split(box: Path):
    """preview: plan を手で編集してクラスタを分断すると警告が出る。"""
    r = box / "pv_split"
    r.mkdir()
    (r / "a.py").write_text("from b import x\n", encoding="utf-8")
    (r / "b.py").write_text("x = 1\n", encoding="utf-8")
    sc = box / "split.scan.json"
    pl = box / "split.plan.json"
    run("scan", str(r), "--out", str(sc))
    run("suggest", "--in", str(sc), "--out", str(pl))
    # plan を改変: a.py だけ別フォルダへ
    plan = json.loads(pl.read_text(encoding="utf-8"))
    for m in plan["moves"]:
        if m["from"] == "a.py":
            m["to"] = "コード/別/a.py"
    pl.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    pv = run("preview", str(r), "--in", str(pl))
    check("preview/split: クラスタ分断警告が出る",
          "分断" in pv.stdout and "import が壊れる" in pv.stdout, pv.stdout)


def t_preview_warns_on_partial_cluster(box: Path):
    """preview: クラスタの一部だけが plan に入っていると警告が出る。"""
    r = box / "pv_partial"
    r.mkdir()
    (r / "a.py").write_text("from b import x\n", encoding="utf-8")
    (r / "b.py").write_text("x = 1\n", encoding="utf-8")
    sc = box / "partial.scan.json"
    pl = box / "partial.plan.json"
    run("scan", str(r), "--out", str(sc))
    run("suggest", "--in", str(sc), "--out", str(pl))
    plan = json.loads(pl.read_text(encoding="utf-8"))
    # b.py を plan から除外（a.py だけ動かして b.py は元の場所に残る形）
    plan["moves"] = [m for m in plan["moves"] if m["from"] != "b.py"]
    pl.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    pv = run("preview", str(r), "--in", str(pl))
    check("preview/partial: 未カバー警告が出る",
          "未カバー" in pv.stdout, pv.stdout)


def t_scan_no_deps_flag(box: Path):
    """scan --no-deps: 依存解析をスキップしても他は動く。"""
    r = box / "no_deps"
    r.mkdir()
    (r / "a.py").write_text("from b import x\n", encoding="utf-8")
    (r / "b.py").write_text("x = 1\n", encoding="utf-8")
    sc = box / "nd.scan.json"
    run("scan", str(r), "--out", str(sc), "--no-deps")
    d = json.loads(sc.read_text(encoding="utf-8"))
    check("scan/no-deps: 依存解析を行わない",
          d["code_dependencies"]["clusters"] == [] and d["code_dependencies"]["edges"] == [],
          str(d["code_dependencies"]))
    check("scan/no-deps: ファイルは全部出る",
          d["file_count"] == 2, str(d["file_count"]))


def t_review_list_and_restore(box: Path):
    """review: 一覧表示 → 特定パターンの復元 → undo 二重実行のように
    すでに復元済みの review --restore は対象なし扱い、を一通り。"""
    r = box / "rv_box"
    r.mkdir()
    (r / "note.txt").write_text("a", encoding="utf-8")
    (r / ".DS_Store").write_bytes(b"")
    (r / "junk.tmp").write_text("t", encoding="utf-8")
    plan = box / "rv.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": ".DS_Store", "to": "_捨て/.DS_Store", "reason": "システム生成"},
        {"from": "junk.tmp", "to": "_捨て/junk.tmp", "reason": "一時ファイル"},
        {"from": "note.txt", "to": "ドキュメント/メモ/note.txt", "reason": "メモ"},
    ]})
    run("apply", str(r), "--in", str(plan), "--yes")

    # list
    lst = run("review", str(r))
    check("review/list: rc=0", lst.returncode == 0, lst.stderr)
    check("review/list: 隔離2件を表示",
          ".DS_Store" in lst.stdout and "junk.tmp" in lst.stdout
          and "ファイル数: 2 件" in lst.stdout, lst.stdout)
    check("review/list: 元の場所と理由が出る",
          "システム生成" in lst.stdout and "元の場所" in lst.stdout, lst.stdout)

    # restore (dry-run なら --yes なしで rc=1 + ファイル動かず)
    drv = run("review", str(r), "--restore", "*.DS_Store")
    check("review/restore: --yes 無しは dry-run（rc=1）",
          drv.returncode == 1 and not (r / ".DS_Store").exists(), drv.stdout)

    # restore --yes
    rv = run("review", str(r), "--restore", "*.DS_Store", "--yes")
    check("review/restore --yes: rc=0", rv.returncode == 0, rv.stderr)
    check("review/restore --yes: .DS_Store が元の場所に戻る",
          (r / ".DS_Store").exists(), str(list(r.iterdir())))
    check("review/restore: junk.tmp は _捨て に残っている",
          (r / "_捨て" / "junk.tmp").exists())


def t_review_purge_requires_yes(box: Path):
    """review --purge: --yes なしでは消えない、--yes 付ければ消える。"""
    r = box / "rv_purge"
    r.mkdir()
    (r / "garbage.tmp").write_text("g", encoding="utf-8")
    plan = box / "rv_purge.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "garbage.tmp", "to": "_捨て/garbage.tmp", "reason": "一時"},
    ]})
    run("apply", str(r), "--in", str(plan), "--yes")
    trash_file = r / "_捨て" / "garbage.tmp"
    check("review/purge: 隔離されている", trash_file.exists())

    dry = run("review", str(r), "--purge")
    check("review/purge: --yes 無しでは消えない",
          dry.returncode == 1 and trash_file.exists(), dry.stdout)

    real = run("review", str(r), "--purge", "--yes")
    check("review/purge --yes: rc=0", real.returncode == 0, real.stderr)
    check("review/purge --yes: ファイルが物理削除される", not trash_file.exists())
    plog = list((r / "_整理ログ").glob("purge-*.jsonl"))
    check("review/purge: ログが残る", len(plog) == 1, str(plog))


def t_review_empty(box: Path):
    """review: _捨て/ が無い / 空 の挙動。"""
    r = box / "rv_empty"
    r.mkdir()
    no_trash = run("review", str(r))
    check("review: _捨て/ が無いと rc=2",
          no_trash.returncode == 2 and "ありません" in no_trash.stderr,
          no_trash.stderr)
    (r / "_捨て").mkdir()
    empty = run("review", str(r))
    check("review: _捨て/ が空のとき正常に rc=0 で「空です」",
          empty.returncode == 0 and "空です" in empty.stdout, empty.stdout)


def t_preview_summary_header(box: Path):
    """preview: トップフォルダごとの件数サマリが先頭に出る。"""
    r = box / "sum"
    r.mkdir()
    (r / "a.txt").write_text("a", encoding="utf-8")
    (r / "b.txt").write_text("b", encoding="utf-8")
    (r / "c.tmp").write_text("c", encoding="utf-8")
    plan = box / "sum.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "a.txt", "to": "ドキュメント/a.txt"},
        {"from": "b.txt", "to": "ドキュメント/b.txt"},
        {"from": "c.tmp", "to": "_捨て/c.tmp"},
    ]})
    pv = run("preview", str(r), "--in", str(plan))
    check("preview/summary: サマリ行が出る", "サマリ:" in pv.stdout, pv.stdout)
    check("preview/summary: 件数が並ぶ",
          "ドキュメント/ 2件" in pv.stdout and "_捨て/ 1件" in pv.stdout, pv.stdout)


def t_suggest_cross_target_single_source(box: Path):
    """suggest: 単一 root をスキャンし --target に別の dir を指定したとき、
    consolidate モードとして扱われ from が絶対パスになる（〜/Downloads を
    〜/Documents 下に振り分ける典型ケース）。"""
    src = box / "x_src"
    target = box / "x_target"
    src.mkdir(); target.mkdir()
    (src / "note.txt").write_text("memo\n", encoding="utf-8")
    (src / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    (src / "photo.png").write_bytes(b"\x89PNG")
    sc = box / "x.scan.json"
    pl = box / "x.plan.json"
    run("scan", str(src), "--out", str(sc))
    sg = run("suggest", "--in", str(sc), "--out", str(pl), "--target", str(target))
    check("cross-target: suggest rc=0", sg.returncode == 0, sg.stderr)

    plan = json.loads(pl.read_text(encoding="utf-8"))
    check("cross-target: mode は consolidate", plan["mode"] == "consolidate", plan.get("mode"))
    abs_count = sum(1 for m in plan["moves"] if Path(m["from"]).is_absolute())
    check("cross-target: from がすべて絶対パス",
          abs_count == len(plan["moves"]), str(plan["moves"]))

    # apply まで通って、ファイルが target 配下に出現
    ap = run("apply", str(target), "--in", str(pl), "--yes")
    check("cross-target: apply rc=0", ap.returncode == 0, ap.stderr)
    expected = [target / "コード/app.js", target / "ドキュメント/メモ/note.txt", target / "画像/photo.png"]
    check("cross-target: ファイルが target 配下に配置される",
          all(p.is_file() for p in expected), str(expected))
    # src 側は空になっている
    leftover = [p for p in src.iterdir() if p.is_file()]
    check("cross-target: src 側からはファイルが消える", not leftover, str(leftover))

    # undo で元の散らかった src に戻る
    run("undo", str(target))
    restored = sorted(p.name for p in src.iterdir() if p.is_file())
    check("cross-target: undo で src に完全復元",
          restored == ["app.js", "note.txt", "photo.png"], str(restored))


def t_cluster_keeps_assets_with_html(box: Path):
    """suggest: HTML が画像を参照しているとき、画像も同じクラスタ subfolder に入る。"""
    r = box / "html_assets"
    r.mkdir()
    (r / "page.html").write_text(
        '<img src="./logo.png">\n<link href="./style.css">\n', encoding="utf-8")
    (r / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (r / "style.css").write_text("body{}\n", encoding="utf-8")
    sc = box / "html.scan.json"
    pl = box / "html.plan.json"
    run("scan", str(r), "--out", str(sc))
    run("suggest", "--in", str(sc), "--out", str(pl))
    plan = json.loads(pl.read_text(encoding="utf-8"))
    by_from = {m["from"]: m["to"] for m in plan["moves"]}
    parents = {str(Path(p).parent) for p in by_from.values()
               if Path(p).name in {"page.html", "logo.png", "style.css"}}
    check("suggest/assets: HTML 参照アセットが同じ dir にまとまる",
          len(parents) == 1, str(by_from))


def t_history_records_and_timeline(box: Path):
    """history: apply / undo が ~/.tidy に記録され、タイムラインで新しい順に出る。"""
    home = box / "hist_home_1"
    r = box / "hist_box"
    r.mkdir()
    (r / "a.txt").write_text("a", encoding="utf-8")
    (r / "b.txt").write_text("b", encoding="utf-8")
    plan = box / "hist.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て", "moves": [
        {"from": "a.txt", "to": "ドキュメント/a.txt"},
        {"from": "b.txt", "to": "_捨て/b.txt"},
    ]})
    # 履歴が空のうちは「まだありません」
    empty = run("history", tidy_home=str(home))
    check("history: 初期は空で rc=0", empty.returncode == 0
          and "まだありません" in empty.stdout, empty.stdout)

    run("apply", str(r), "--in", str(plan), "--yes", tidy_home=str(home))
    run("undo", str(r), tidy_home=str(home))

    # jsonl が実際に書かれている
    hpath = home / "history.jsonl"
    check("history: history.jsonl が作られる", hpath.is_file(), str(home))
    lines = [json.loads(l) for l in hpath.read_text(encoding="utf-8").splitlines() if l.strip()]
    actions = [r_["action"] for r_ in lines]
    check("history: apply と undo が記録される",
          actions == ["apply", "undo"], str(actions))
    check("history: apply レコードに件数が入る",
          lines[0]["action"] == "apply" and lines[0].get("moves") == 2
          and lines[0].get("trashed") == 1, str(lines[0]))

    # タイムライン表示（新しい順 = undo が先頭）
    tl = run("history", tidy_home=str(home))
    check("history: タイムライン rc=0 で2件", tl.returncode == 0
          and "記録数: 2 件" in tl.stdout, tl.stdout)
    check("history: 新しい順（取り消しが整理より上）",
          tl.stdout.index("取り消し") < tl.stdout.index("整理"), tl.stdout)


def t_history_target_filter(box: Path):
    """history --target: 複数の場所を整理しても、1つに絞り込める。"""
    home = box / "hist_home_2"
    r1 = box / "hist_t1"
    r2 = box / "hist_t2"
    for r in (r1, r2):
        r.mkdir()
        (r / "x.txt").write_text("x", encoding="utf-8")
    for r in (r1, r2):
        plan = box / f"{r.name}.plan.json"
        write_json(plan, {"root": str(r), "trash_dir": "_捨て",
                          "moves": [{"from": "x.txt", "to": "ドキュメント/x.txt"}]})
        run("apply", str(r), "--in", str(plan), "--yes", tidy_home=str(home))

    allh = run("history", tidy_home=str(home))
    check("history: 両方の場所が記録される", allh.returncode == 0
          and "記録数: 2 件" in allh.stdout, allh.stdout)

    filtered = run("history", "--target", str(r1), tidy_home=str(home))
    check("history --target: 1件に絞れる",
          "記録数: 1 件" in filtered.stdout
          and str(r2) not in filtered.stdout, filtered.stdout)


def t_history_isolated_from_real_home(box: Path):
    """history: TIDY_HOME 未指定でも、テストの既定 TIDY_HOME に隔離されている
    （実 ~/.tidy を汚さないことの間接確認 — 既定 home にレコードが溜まる）。"""
    default_home = Path(os.environ["TIDY_HOME"])
    r = box / "hist_iso"
    r.mkdir()
    (r / "z.txt").write_text("z", encoding="utf-8")
    plan = box / "iso.plan.json"
    write_json(plan, {"root": str(r), "trash_dir": "_捨て",
                      "moves": [{"from": "z.txt", "to": "ドキュメント/z.txt"}]})
    run("apply", str(r), "--in", str(plan), "--yes")  # tidy_home 指定なし → 既定
    check("history: 既定 TIDY_HOME 配下に記録される（実 home を汚さない）",
          (default_home / "history.jsonl").is_file(), str(default_home))


CASES = [
    t_inplace_roundtrip, t_junk_and_types, t_consolidate_dedupe, t_collision,
    t_preview_uncovered, t_safety_escape, t_requires_yes, t_interrupt_resilience,
    t_undo_twice, t_assess_project, t_assess_clutter, t_assess_small, t_assess_organized,
    t_suggest_inplace, t_suggest_consolidate, t_verify_clean_and_dirty,
    t_verify_no_manifest, t_redo_after_undo, t_self_move_rejected,
    t_apply_dry_run, t_preview_shows_size,
    # dependency-aware
    t_scan_extracts_python_imports, t_scan_extracts_js_html_css,
    t_suggest_groups_cluster, t_preview_warns_on_cluster_split,
    t_preview_warns_on_partial_cluster, t_scan_no_deps_flag,
    t_cluster_keeps_assets_with_html,
    t_suggest_cross_target_single_source,
    # v5: review + preview summary
    t_review_list_and_restore, t_review_purge_requires_yes, t_review_empty,
    t_preview_summary_header,
    # v6: cross-location history
    t_history_records_and_timeline, t_history_target_filter,
    t_history_isolated_from_real_home,
]


def main() -> int:
    box = Path(tempfile.mkdtemp(prefix="tidy-test-"))
    print(f"sandbox: {box}\n")
    # 全テストの既定 TIDY_HOME を sandbox 内に固定し、実 ~/.tidy を汚さない。
    # 履歴を検証する個別テストは run(..., tidy_home=...) で専用パスを渡す。
    os.environ["TIDY_HOME"] = str(box / ".tidy-default")
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
