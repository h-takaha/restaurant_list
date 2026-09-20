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
    """URL → Place、店名 → Place。None なら特定できなかったことにする。"""

    def __init__(self, by_url: dict[str, Place] | None = None,
                 by_query: dict[str, Place | None] | None = None,
                 site_text: str | None = None) -> None:
        self.by_url = by_url or {}
        self.by_query = by_query or {}
        self.site_text = site_text
        self.searched: list[str] = []
        self.fetched: list[str] = []

    def pick_maps_url(self, urls): return urls[0] if urls else None

    # run.py は maps_resolver.Session() を with で使う
    def Session(self, **kw): return self
    def __enter__(self): return self
    def __exit__(self, *exc): return None

    def place(self, url): return self.by_url.get(url) or Place()

    def search(self, name):
        self.searched.append(name)
        return self.by_query.get(name)

    def text(self, url, max_chars=4000):
        self.fetched.append(url)
        return self.site_text


class FakeTagger:
    def __init__(self, confident: bool = True, tags=("麺", "ラーメン")) -> None:
        self.confident, self.tags = confident, list(tags)

    def tag(self, *, name, existing_tags, source_text):
        return TagResult(tags=self.tags, new_tags=[], recommended_menu="醤油ラーメン",
                         area="北見", confident=self.confident)


class FakeGit:
    """push の成否と現在のブランチを指定できる git。呼ばれたコマンドを記録する。"""

    def __init__(self, push_ok: bool = True, branch: str = "main",
                 commit_ok: bool = True) -> None:
        self.push_ok, self.branch, self.commit_ok = push_ok, branch, commit_ok
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout=f"{self.branch}\n", stderr="")
        rc = 0
        if args[0] == "commit" and not self.commit_ok:
            rc = 1
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
    run.REPO = repo
    run.gmail_client = gmail
    run.maps_resolver = maps
    run.git = git
    run.tagger_for = lambda kind: tagger
    sys.argv = ["run.py"] + argv
    try:
        return run.main()
    finally:
        (run.REPO, run.gmail_client, run.maps_resolver, run.git,
         run.tagger_for, sys.argv) = original


def rows_of(repo: Path) -> list:
    return restaurants_md.read_rows(repo / "restaurants.md")


URL_A, URL_B = "https://maps.app.goo.gl/a", "https://maps.app.goo.gl/b"

KNOWN = Place(name="ラーメン山岡家 北見店", address="北見市光西町165")
NEW = Place(name="新しいラーメン店", address="北見市北1条西1-1",
            website="https://example.com", lat=43.8, lng=143.9,
            hours=["月曜日 11:00～21:00"])
NO_ADDRESS = Place(name="住所の取れない店", lat=43.8, lng=143.9)


def mail_with_map(tid: str = "t1", subject: str = "新しいラーメン店",
                  url: str = URL_A) -> FakeMail:
    return FakeMail(tid, subject, [url])


def mail_without_map(tid: str = "t1", subject: str = "新しいラーメン店") -> FakeMail:
    return FakeMail(tid, subject, [])


# ── 検証 ─────────────────────────────────────────────────────────────────────

def test_subprocess_encoding() -> None:
    """外部コマンドの日本語出力を取りこぼさないか。

    text=True だけだと Windows はロケール既定（日本語環境では cp932）で
    デコードする。build.mjs は店名を UTF-8 で出すので復号に失敗し、読み取り
    スレッドが落ちて stdout が None になる。実機でここまで到達して落ちた:
        UnicodeDecodeError: 'cp932' codec can't decode byte 0x83
        TypeError: 'NoneType' object is not subscriptable
    """
    print("\n[外部コマンドの日本語出力]")

    result = run.run_cmd("node", "-e",
                         'console.log("キャッシュ利用 鳥若 北見総本店 43.80°")')
    check("日本語がそのまま読める", "鳥若 北見総本店" in (result.stdout or ""), True)
    check("stdout は None にならない", result.stdout is not None, True)
    check("stderr も None にならない", result.stderr is not None, True)

    # 復号できないバイトが来ても落ちず、置換文字にして進む
    broken = run.run_cmd("node", "-e",
                         'process.stdout.write(Buffer.from([0x83, 0x41]))')
    check("壊れたバイト列でも例外を投げない", broken.stdout is not None, True)


def test_empty_inbox(tmp: Path) -> None:
    print("\n[受信箱が空]")
    repo = make_repo(tmp / "empty")
    before = (repo / "restaurants.md").read_text(encoding="utf-8")
    gmail = FakeGmail([])
    code = drive(repo, gmail, FakeMaps(), FakeTagger(), FakeGit(), ["--apply", "--push"])
    check("exit 0", code, 0)
    check("restaurants.md は無変更", (repo / "restaurants.md").read_text(encoding="utf-8"), before)
    check("何もアーカイブしない", gmail.archived, [])


def test_dry_run(tmp: Path) -> None:
    print("\n[dry-run]")
    repo = make_repo(tmp / "dry")
    before = (repo / "restaurants.md").read_text(encoding="utf-8")
    gmail = FakeGmail([mail_with_map()])
    drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(), FakeGit(), [])
    check("restaurants.md に書かない", (repo / "restaurants.md").read_text(encoding="utf-8"), before)
    check("アーカイブしない", gmail.archived, [])
    check("メールは受信箱に残る", gmail.remaining, ["t1"])


def test_unidentifiable_stays(tmp: Path) -> None:
    print("\n[店を特定できないメール]")
    repo = make_repo(tmp / "unknown")
    n_before = len(rows_of(repo))
    # 件名が空、マップ URL も無い → 特定できない
    gmail = FakeGmail([FakeMail("t1", "", [])])
    drive(repo, gmail, FakeMaps(), FakeTagger(), FakeGit(), ["--apply", "--push"])
    check("行は増えない", len(rows_of(repo)), n_before)
    check("アーカイブしない", gmail.archived, [])
    check("受信箱に残る（＝未処理）", gmail.remaining, ["t1"])


def test_not_confident_stays(tmp: Path) -> None:
    print("\n[材料が足りないと判断した場合]")
    repo = make_repo(tmp / "unsure")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([mail_with_map()])
    drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(confident=False), FakeGit(),
          ["--apply", "--push"])
    check("行は増えない", len(rows_of(repo)), n_before)
    check("受信箱に残る", gmail.remaining, ["t1"])


def test_already_listed(tmp: Path) -> None:
    print("\n[既に載っている店]")
    repo = make_repo(tmp / "dupe")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([mail_with_map(subject="ラーメン山岡家 北見店")])
    drive(repo, gmail, FakeMaps({URL_A: KNOWN}), FakeTagger(), FakeGit(), ["--apply", "--push"])
    check("行を足さない", len(rows_of(repo)), n_before)
    check("メールはアーカイブする", gmail.archived, ["t1"])


def test_only_known_stores_still_archives(tmp: Path) -> None:
    """既出の店だけが届いた場合。★毎朝同じメールを処理し続ける経路

    追記が無いと commit するものが無い。以前はそこで return していたので
    アーカイブに到達せず、既に載っている店のメールが受信箱に残り続けていた。
    運用ルール「既に載っている店は行を足さずメールだけアーカイブ」が壊れていた。
    """
    print("\n[既出の店だけが届いた] ★受信箱に残り続ける経路")
    repo = make_repo(tmp / "allknown")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([
        mail_with_map("t1", "ラーメン山岡家 北見店", URL_A),
        mail_with_map("t2", "焼肉 珍来", URL_B),
    ])
    known2 = Place(name="焼肉 珍来", address="網走郡美幌町字東1条北2丁目")
    git = FakeGit()
    code = drive(repo, gmail, FakeMaps({URL_A: KNOWN, URL_B: known2}),
                 FakeTagger(), git, ["--apply", "--push"])

    check("exit 0", code, 0)
    check("行は増えない", len(rows_of(repo)), n_before)
    check("commit は試みない", [c for c in git.calls if c[0] == "commit"], [])
    check("それでも両方アーカイブする", sorted(gmail.archived), ["t1", "t2"])
    check("受信箱は空になる", gmail.remaining, [])


def test_commit_failure_blocks_archive(tmp: Path) -> None:
    """commit が失敗したらアーカイブしない。"""
    print("\n[commit が失敗した場合]")
    repo = make_repo(tmp / "commitfail")
    gmail = FakeGmail([mail_with_map()])
    git = FakeGit(commit_ok=False)
    code = drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(), git,
                 ["--apply", "--push"])

    check("非ゼロで終了", code, 1)
    check("push しない", [c for c in git.calls if c[0] == "push"], [])
    check("アーカイブしない", gmail.archived, [])
    check("受信箱に残る", gmail.remaining, ["t1"])


def test_wrong_branch_refuses(tmp: Path) -> None:
    """main 以外で --push を使わせない。★静かにメールを失う経路

    git commit は現在のブランチに乗るが、git push origin main はローカルの main を
    押す。別ブランチにいると commit はそこに残り、push は素通りで返り値0になる。
    「成功した」と誤認してアーカイブすると、変更は main に届かないのに元メールだけ
    消える。
    """
    print("\n[main 以外のブランチで --push] ★静かに失う経路")
    repo = make_repo(tmp / "branch")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([mail_with_map()])
    git = FakeGit(branch="prototype/local-ingest")
    code = drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(), git,
                 ["--apply", "--push"])

    check("非ゼロで終了", code, 1)
    check("commit しない", [c for c in git.calls if c[0] == "commit"], [])
    check("push しない", [c for c in git.calls if c[0] == "push"], [])
    check("1件もアーカイブしない", gmail.archived, [])
    check("メールは受信箱に残る", gmail.remaining, ["t1"])
    check("restaurants.md にも書かない", len(rows_of(repo)), n_before)


def test_push_failure_blocks_archive(tmp: Path) -> None:
    print("\n[push に失敗した場合] ★最重要")
    repo = make_repo(tmp / "pushfail")
    gmail = FakeGmail([mail_with_map()])
    code = drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(), FakeGit(push_ok=False),
                 ["--apply", "--push"])
    check("非ゼロで終了", code, 1)
    check("1件もアーカイブしない", gmail.archived, [])
    check("メールは受信箱に残る（次回また拾える）", gmail.remaining, ["t1"])


def test_success_path(tmp: Path) -> None:
    print("\n[成功パス]")
    repo = make_repo(tmp / "ok")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([
        mail_with_map(),
        mail_with_map("t2", "ラーメン山岡家 北見店", URL_B),
    ])
    git = FakeGit()
    code = drive(repo, gmail, FakeMaps({URL_A: NEW, URL_B: KNOWN}), FakeTagger(), git,
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
    gmail = FakeGmail([mail_with_map()])
    drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(), FakeGit(), ["--apply", "--push"])

    data = json.loads((repo / "docs/data.json").read_text(encoding="utf-8"))
    entry = next((r for r in data if r["name"] == "新しいラーメン店"), None)
    check("data.json に載る", entry is not None, True)
    if entry:
        check("Playwright の座標が入る（ジオコーダ不要）",
              (entry["lat"], entry["lng"]), (43.8, 143.9))

    existing = next(r for r in data if r["name"] == "ラーメン山岡家 北見店")
    check("既存店の座標は保たれる", existing["lat"] is not None, True)


def test_search_fallback(tmp: Path) -> None:
    print("\n[マップ URL が無いメール → 件名で検索して補完]")
    repo = make_repo(tmp / "search")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([mail_without_map(subject="新しいラーメン店")])
    maps = FakeMaps(by_query={"新しいラーメン店": NEW})
    drive(repo, gmail, maps, FakeTagger(), FakeGit(), ["--apply", "--push"])

    check("件名で検索した", maps.searched, ["新しいラーメン店"])
    check("行が1つ増える", len(rows_of(repo)) - n_before, 1)
    check("住所が入る（未確認にならない）", rows_of(repo)[-1].address, "北見市北1条西1-1")
    check("アーカイブされる", gmail.archived, ["t1"])


def test_search_ambiguous_stays(tmp: Path) -> None:
    print("\n[検索で1軒に絞れない]")
    repo = make_repo(tmp / "ambiguous")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([mail_without_map(subject="すすきのの店")])
    maps = FakeMaps(by_query={})  # 該当なし = 絞れなかった
    drive(repo, gmail, maps, FakeTagger(), FakeGit(), ["--apply", "--push"])

    check("検索は試みる", maps.searched, ["すすきのの店"])
    check("行は増えない（推測で足さない）", len(rows_of(repo)), n_before)
    check("受信箱に残る", gmail.remaining, ["t1"])


def test_no_address_stays(tmp: Path) -> None:
    print("\n[住所が取れなかった] ★セレクタが腐ったときの挙動")
    repo = make_repo(tmp / "noaddr")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([mail_with_map()])
    drive(repo, gmail, FakeMaps({URL_A: NO_ADDRESS}), FakeTagger(), FakeGit(),
          ["--apply", "--push"])

    check("住所 未確認 の行を足さない", len(rows_of(repo)), n_before)
    check("アーカイブしない", gmail.archived, [])
    check("受信箱に溜まって気づける", gmail.remaining, ["t1"])


def test_official_site_feeds_tagger(tmp: Path) -> None:
    print("\n[公式サイトの本文をタガーに渡す]")
    repo = make_repo(tmp / "site")

    seen = {}

    class RecordingTagger(FakeTagger):
        def tag(self, *, name, existing_tags, source_text):
            seen["source"] = source_text
            return super().tag(name=name, existing_tags=existing_tags,
                               source_text=source_text)

    gmail = FakeGmail([mail_with_map()])
    maps = FakeMaps({URL_A: NEW}, site_text="名物は特製醤油ラーメンです")
    drive(repo, gmail, maps, RecordingTagger(), FakeGit(), ["--apply", "--push"])

    check("公式サイトを読んだ", maps.fetched, ["https://example.com"])
    check("本文がタガーの材料に入る", "名物は特製醤油ラーメン" in seen.get("source", ""), True)
    check("マップの取得内容も材料に入る", "北見市北1条西1-1" in seen.get("source", ""), True)


def test_tag_policy_violation_stays(tmp: Path) -> None:
    print("\n[タガーが新しいタグを作りすぎた]")
    repo = make_repo(tmp / "tagpolicy")
    n_before = len(rows_of(repo))
    gmail = FakeGmail([mail_with_map()])
    # 既存に無いタグを3つ返す = 規則違反
    bad = FakeTagger(tags=("つけ麺", "油そば", "汁なし"))
    drive(repo, gmail, FakeMaps({URL_A: NEW}), bad, FakeGit(), ["--apply", "--push"])

    check("行は増えない", len(rows_of(repo)), n_before)
    check("アーカイブしない", gmail.archived, [])
    check("受信箱に残る", gmail.remaining, ["t1"])


def test_tag_variant_normalised(tmp: Path) -> None:
    print("\n[タガーが表記ゆれのタグを返した]")
    repo = make_repo(tmp / "tagvariant")
    gmail = FakeGmail([mail_with_map()])
    # 「ラーメン屋」は既存の「ラーメン」に寄せられるので、新タグ扱いにならない
    drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(tags=("麺", "ラーメン屋")),
          FakeGit(), ["--apply", "--push"])

    added = rows_of(repo)[-1]
    check("既存タグに寄せて追記される", added.tags, ["麺", "ラーメン"])
    check("アーカイブされる", gmail.archived, ["t1"])


def test_existing_records_untouched(tmp: Path) -> None:
    print("\n[利用者の記録を壊さないか]")
    repo = make_repo(tmp / "records")
    before = {r.name: (r.visits, r.rating) for r in rows_of(repo)}
    gmail = FakeGmail([mail_with_map()])
    drive(repo, gmail, FakeMaps({URL_A: NEW}), FakeTagger(), FakeGit(), ["--apply", "--push"])

    after = {r.name: (r.visits, r.rating) for r in rows_of(repo)}
    changed = {k: (before[k], after[k]) for k in before if before.get(k) != after.get(k)}
    check("既存行の来店回数・評価は全て不変", changed, {})
    check("中山商店の記録", after["麺屋 中山商店"], ("11", "5"))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_subprocess_encoding()
        test_empty_inbox(tmp)
        test_dry_run(tmp)
        test_unidentifiable_stays(tmp)
        test_not_confident_stays(tmp)
        test_already_listed(tmp)
        test_search_fallback(tmp)
        test_search_ambiguous_stays(tmp)
        test_no_address_stays(tmp)
        test_official_site_feeds_tagger(tmp)
        test_tag_policy_violation_stays(tmp)
        test_tag_variant_normalised(tmp)
        test_only_known_stores_still_archives(tmp)
        test_commit_failure_blocks_archive(tmp)
        test_wrong_branch_refuses(tmp)
        test_push_failure_blocks_archive(tmp)
        test_success_path(tmp)
        test_coords_written(tmp)
        test_existing_records_untouched(tmp)

    print(f"\n{'=' * 50}\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    sys.exit(1 if FAIL else 0)
