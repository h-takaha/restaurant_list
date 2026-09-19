"""run.py の判断ロジックの検証。ネットワークに出ない。

検証するのは「何を書くか」ではなく「何を書かないか」。routine の安全側のルール
（特定できなければ受信箱に残す、push できなければアーカイブしない）が壊れると、
メールが黙って消えたり、行かない店が増えたりする。

Gmail・マップ・タガー・git を差し替え、build.mjs だけは本物を temp 上で走らせる。

    python test_pipeline.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import restaurants_md
import run
from maps_resolver import Place
from tagger import TagResult

SRC = Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(label: str, got, want) -> None:
    if got == want:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}\n        got : {got!r}\n        want: {want!r}")


# ── 差し替える層 ──────────────────────────────────────────────────────────────

@dataclass
class FakeMail:
    thread_id: str
    subject: str
    urls: list[str]

    def as_context(self) -> str:
        return f"件名: {self.subject}"


class FakeGmail:
    """受信箱と、アーカイブされたスレッドの記録。"""

    def __init__(self, mails: list[FakeMail]) -> None:
        self.inbox = {m.thread_id: m for m in mails}
        self.archived: list[str] = []

    def build_service(self, *a, **kw): return self
    def search_threads(self, service, query): return list(self.inbox)
    def fetch_mail(self, service, tid): return self.inbox[tid]

    def archive(self, service, tid):
        self.archived.append(tid)
        self.inbox.pop(tid, None)

    @property
    def remaining(self) -> list[str]:
        return sorted(self.inbox)


class FakeMaps:
    """thread_id → Place。None なら店を特定できなかったことにする。"""

    def __init__(self, table: dict[str, Place | None]) -> None:
        self.table = table
        self.current: str | None = None

    def pick_maps_url(self, urls): return urls[0] if urls else None
    def resolve(self, url, **kw): return self.table.get(self.current) or Place()


class FakeTagger:
    def __init__(self, confident: bool = True, tags=("麺", "ラーメン")) -> None:
        self.confident, self.tags = confident, list(tags)

    def tag(self, *, name, existing_tags, source_text):
        return TagResult(tags=self.tags, new_tags=[], recommended_menu="醤油ラーメン",
                         area="北見", confident=self.confident)


class FakeGit:
    """push の成否を指定できる git。呼ばれたコマンドを記録する。"""

    def __init__(self, push_ok: bool = True) -> None:
        self.push_ok, self.calls = push_ok, []

    def __call__(self, *args):
        self.calls.append(args)
        rc = 0
        if args[0] == "push" and not self.push_ok:
            rc = 1
        if args[0] == "pull":
            rc = 0 if self.push_ok else 1
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="fake")


# ── 足場 ─────────────────────────────────────────────────────────────────────

def make_repo(tmp: Path) -> Path:
    """build.mjs が本当に走る最小のリポジトリを temp に作る。

    docs/data.json をそのまま持ってくるので既存店は全部キャッシュに当たり、
    新規に足した店だけがジオコーダに出て（この環境では失敗して）null になる。
    routine が実際に置かれている状況と同じ。
    """
    repo = tmp / "repo"
    (repo / "docs").mkdir(parents=True)
    for name in ("restaurants.md", "build.mjs"):
        shutil.copy(SRC / name, repo / name)
    shutil.copy(SRC / "docs/data.json", repo / "docs/data.json")
    return repo


def drive(repo: Path, gmail: FakeGmail, maps: FakeMaps, tagger: FakeTagger,
          git: FakeGit, argv: list[str]) -> int:
    """run.main() を差し替えた層の上で動かす。"""
    original = (run.REPO, run.gmail_client, run.maps_resolver, run.git, run.tagger_for, sys.argv)

    # build_place は maps_resolver 越しに呼ばれるので、どのメールを処理中か伝える
    real_build_place = run.build_place

    def build_place(mail):
        maps.current = mail.thread_id
        return real_build_place(mail)

    run.REPO = repo
    run.gmail_client = gmail
    run.maps_resolver = maps
    run.git = git
    run.tagger_for = lambda kind: tagger
    run.build_place = build_place
    sys.argv = ["run.py"] + argv
    try:
        return run.main()
    finally:
        (run.REPO, run.gmail_client, run.maps_resolver, run.git,
         run.tagger_for, sys.argv) = original
        run.build_place = real_build_place


def rows_of(repo: Path) -> list:
    return restaurants_md.read_rows(repo / "restaurants.md")


KNOWN = Place(name="ラーメン山岡家 北見店", address="北見市光西町165")
NEW = Place(name="新しいラーメン店", address="北見市北1条西1-1",
            website="https://example.com", lat=43.8, lng=143.9,
            hours=["月曜日 11:00～21:00"])


# ── 検証 ─────────────────────────────────────────────────────────────────────

def test_empty_inbox(tmp: Path) -> None:
    print("\n[受信箱が空]")
    repo = make_repo(tmp / "empty")
    before = (repo / "restaurants.md").read_text(encoding="utf-8")
    gmail = FakeGmail([])
    code = drive(repo, gmail, FakeMaps({}), FakeTagger(), FakeGit(), ["--apply", "--push"])
    check("exit 0", code, 0)
    check("restaurants.md は無変更", (repo / "restaurants.md").read_text(encoding="utf-8"), before)
    check("何もアーカイブしない", gmail.archived, [])


def test_dry_run(tmp: Path) -> None:
    print("\n[dry-run]")
    repo = make_repo(tmp / "dry")
    before = (repo / "restaurants.md").read_text(encoding="utf-8")
    gmail = FakeGmail([FakeMail("t1", "新しいラーメン店", ["https://maps.app.goo.gl/a"])])
    drive(repo, gmail, FakeMaps({"t1": NEW}), FakeTagger(), FakeGit(), [])
    check("restaurants.md に書かない", (repo / "restaurants.md").read_text(encoding="utf-8"), before)
    check("アーカイブしない", gmail.archived, [])
    check("メールは受信箱に残る", gmail.remaining, ["t1"])


def test_unidentifiable_stays(tmp: Path) -> None:
    print("\n[店を特定できないメール]")
    repo = make_repo(tmp / "unknown")
    n_before = len(rows_of(repo))
    # 件名が空、マップ URL も無い → 特定できない
    gmail = FakeGmail([FakeMail("t1", "", [])])
    drive(repo, gmail, FakeMaps({"t1": None}), FakeTagger(), FakeGit(), ["--apply", "--push"])
    check("行は増えない", len(rows_of(repo)), n_before)
    check("アーカイブしない", gmail.archived, [])
    check("受信箱に残る（＝未処理）", gmail.remaining, ["t1"])


def test_not_confident_stays(tmp: Path) -> None:
    print("\n[材料が足りないと判断した場合]")
    repo = make_repo(tmp / "unsure")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([FakeMail("t1", "新しいラーメン店", ["https://maps.app.goo.gl/a"])])
    drive(repo, gmail, FakeMaps({"t1": NEW}), FakeTagger(confident=False), FakeGit(),
          ["--apply", "--push"])
    check("行は増えない", len(rows_of(repo)), n_before)
    check("受信箱に残る", gmail.remaining, ["t1"])


def test_already_listed(tmp: Path) -> None:
    print("\n[既に載っている店]")
    repo = make_repo(tmp / "dupe")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([FakeMail("t1", "ラーメン山岡家 北見店", ["https://maps.app.goo.gl/a"])])
    drive(repo, gmail, FakeMaps({"t1": KNOWN}), FakeTagger(), FakeGit(), ["--apply", "--push"])
    check("行を足さない", len(rows_of(repo)), n_before)
    check("メールはアーカイブする", gmail.archived, ["t1"])


def test_push_failure_blocks_archive(tmp: Path) -> None:
    print("\n[push に失敗した場合] ★最重要")
    repo = make_repo(tmp / "pushfail")
    gmail = FakeGmail([FakeMail("t1", "新しいラーメン店", ["https://maps.app.goo.gl/a"])])
    code = drive(repo, gmail, FakeMaps({"t1": NEW}), FakeTagger(), FakeGit(push_ok=False),
                 ["--apply", "--push"])
    check("非ゼロで終了", code, 1)
    check("1件もアーカイブしない", gmail.archived, [])
    check("メールは受信箱に残る（次回また拾える）", gmail.remaining, ["t1"])


def test_success_path(tmp: Path) -> None:
    print("\n[成功パス]")
    repo = make_repo(tmp / "ok")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([
        FakeMail("t1", "新しいラーメン店", ["https://maps.app.goo.gl/a"]),
        FakeMail("t2", "ラーメン山岡家 北見店", ["https://maps.app.goo.gl/b"]),
    ])
    git = FakeGit()
    code = drive(repo, gmail, FakeMaps({"t1": NEW, "t2": KNOWN}), FakeTagger(), git,
                 ["--apply", "--push"])
    check("exit 0", code, 0)

    rows = rows_of(repo)
    check("行が1つ増える", len(rows) - n_before, 1)
    added = rows[-1]
    check("店名", added.name, "新しいラーメン店")
    check("タグ", added.tags, ["麺", "ラーメン"])
    check("来店回数は 0", added.visits, "0")
    check("評価は -", added.rating, "-")
    check("HP は [公式](...) 形式", added.hp, "[公式](https://example.com)")
    check("営業時間がメモに入る", "月曜日 11:00～21:00" in added.memo, True)

    check("新規も既出もアーカイブ", sorted(gmail.archived), ["t1", "t2"])
    check("受信箱は空になる", gmail.remaining, [])
    check("push した", any(c[0] == "push" for c in git.calls), True)


def test_coords_written(tmp: Path) -> None:
    print("\n[座標の書き込み（ジオコーダに届かない状況）]")
    import json

    repo = make_repo(tmp / "coords")
    gmail = FakeGmail([FakeMail("t1", "新しいラーメン店", ["https://maps.app.goo.gl/a"])])
    drive(repo, gmail, FakeMaps({"t1": NEW}), FakeTagger(), FakeGit(), ["--apply", "--push"])

    data = json.loads((repo / "docs/data.json").read_text(encoding="utf-8"))
    entry = next((r for r in data if r["name"] == "新しいラーメン店"), None)
    check("data.json に載る", entry is not None, True)
    if entry:
        check("Playwright の座標が入る（ジオコーダ不要）",
              (entry["lat"], entry["lng"]), (43.8, 143.9))

    existing = next(r for r in data if r["name"] == "ラーメン山岡家 北見店")
    check("既存店の座標は保たれる", existing["lat"] is not None, True)


def test_existing_records_untouched(tmp: Path) -> None:
    print("\n[利用者の記録を壊さないか]")
    repo = make_repo(tmp / "records")
    before = {r.name: (r.visits, r.rating) for r in rows_of(repo)}
    gmail = FakeGmail([FakeMail("t1", "新しいラーメン店", ["https://maps.app.goo.gl/a"])])
    drive(repo, gmail, FakeMaps({"t1": NEW}), FakeTagger(), FakeGit(), ["--apply", "--push"])

    after = {r.name: (r.visits, r.rating) for r in rows_of(repo)}
    changed = {k: (before[k], after[k]) for k in before if before.get(k) != after.get(k)}
    check("既存行の来店回数・評価は全て不変", changed, {})
    check("中山商店の記録", after["麺屋 中山商店"], ("11", "5"))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_empty_inbox(tmp)
        test_dry_run(tmp)
        test_unidentifiable_stays(tmp)
        test_not_confident_stays(tmp)
        test_already_listed(tmp)
        test_push_failure_blocks_archive(tmp)
        test_success_path(tmp)
        test_coords_written(tmp)
        test_existing_records_untouched(tmp)

    print(f"\n{'=' * 50}\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    sys.exit(1 if FAIL else 0)
