"""抽出ロジックの検証。ネットワークに出ない。

Google マップの DOM を模した固定 HTML に対して実際の Playwright を走らせる。
これが通れば「セレクタの書き方とブラウザの配線は正しい」ことまでは言える。
Google の実物の DOM が fixture どおりかは別問題なので、本番投入前に必ず
    python maps_resolver.py <実際の共有URL>
で突き合わせること。

    python test_extraction.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import gmail_client
import maps_resolver
import restaurants_md
from restaurants_md import Row

FIXTURE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>鳥若 北見総本店 - Google マップ</title></head><body>
<h1 class="DUwDvf lfPIob">鳥若 北見総本店</h1>
<button data-item-id="address" aria-label="住所: 北見市北5条西2丁目 東宝ビル1F">
  <div>北見市北5条西2丁目 東宝ビル1F</div></button>
<a data-item-id="authority" href="https://life-v.co.jp/shop/toriwaka/"
   aria-label="ウェブサイト: life-v.co.jp">life-v.co.jp</a>
<button data-item-id="phone:tel:0157235621" aria-label="電話番号: 0157-23-5621">
  <div>0157-23-5621</div></button>
<table aria-label="営業時間"><tbody>
  <tr><td>月曜日</td><td>17:00～23:30</td></tr>
  <tr><td>火曜日</td><td>17:00～23:30</td></tr>
  <tr><td>日曜日</td><td>16:00～22:30</td></tr>
</tbody></table></body></html>"""

PASS, FAIL = [], []


def check(label: str, got, want) -> None:
    if got == want:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}\n        got : {got!r}\n        want: {want!r}")


def test_maps_dom(tmp: Path) -> None:
    print("\n[Google マップ DOM からの抽出]")
    fixture = tmp / "place.html"
    fixture.write_text(FIXTURE, encoding="utf-8")

    place = maps_resolver.resolve(fixture.as_uri())
    check("店名", place.name, "鳥若 北見総本店")
    check("住所（aria-label のラベル剥がし）", place.address, "北見市北5条西2丁目 東宝ビル1F")
    check("公式サイト", place.website, "https://life-v.co.jp/shop/toriwaka/")
    check("電話番号", place.phone, "0157-23-5621")
    check("営業時間（曜日ごとに省略せず）", place.hours,
          ["月曜日 17:00～23:30", "火曜日 17:00～23:30", "日曜日 16:00～22:30"])


def test_missing_fields(tmp: Path) -> None:
    """取れないときに捏造せず None を返すか。ここが安全性の要。"""
    print("\n[情報が無いページ]")
    bare = tmp / "bare.html"
    bare.write_text("<!doctype html><html><body><h1>名前だけの店</h1></body></html>",
                    encoding="utf-8")
    place = maps_resolver.resolve(bare.as_uri())
    check("店名は取れる", place.name, "名前だけの店")
    check("住所は None（未確認になる）", place.address, None)
    check("電話は None", place.phone, None)
    check("営業時間は None", place.hours, None)


def test_gmail_parsing() -> None:
    print("\n[Gmail の MIME 解析]")

    def b64(text: str) -> str:
        return base64.urlsafe_b64encode(text.encode()).decode()

    payload = {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "Subject", "value": "鳥若 北見総本店"},
            {"name": "From", "value": "hiroyuki <someone@example.com>"},
        ],
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("見つけた\nhttps://maps.app.goo.gl/abc123\n")}},
            {"mimeType": "text/html", "body": {"data": b64(
                '<div>見つけた<a href="https://maps.app.goo.gl/abc123">地図</a></div>')}},
        ],
    }

    class FakeService:
        def users(self): return self
        def threads(self): return self
        def get(self, **kw): return self
        def execute(self): return {"messages": [{"id": "m1", "payload": payload}]}

    mail = gmail_client.fetch_mail(FakeService(), "t1")
    check("件名", mail.subject, "鳥若 北見総本店")
    check("本文は text/plain を優先", mail.body, "見つけた\nhttps://maps.app.goo.gl/abc123")
    check("URL 抽出", mail.urls, ["https://maps.app.goo.gl/abc123"])
    check("マップ URL 判定", maps_resolver.pick_maps_url(mail.urls),
          "https://maps.app.goo.gl/abc123")


def test_html_only_mail() -> None:
    print("\n[HTML しか無いメール]")
    html = '<html><body><script>var x=1</script><p>すすきのの店</p>' \
           '<a href="https://www.instagram.com/foo">insta</a></body></html>'
    payload = {
        "mimeType": "text/html",
        "headers": [{"name": "Subject", "value": "SNS で見つけた"}],
        "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
    }

    class FakeService:
        def users(self): return self
        def threads(self): return self
        def get(self, **kw): return self
        def execute(self): return {"messages": [{"id": "m2", "payload": payload}]}

    mail = gmail_client.fetch_mail(FakeService(), "t2")
    check("script を除いた本文", mail.body, "すすきのの店insta")
    check("href を拾う", mail.urls, ["https://www.instagram.com/foo"])
    check("マップ URL は無い", maps_resolver.pick_maps_url(mail.urls), None)


def test_md_roundtrip(tmp: Path) -> None:
    print("\n[restaurants.md の追記]")
    src = Path(__file__).resolve().parent.parent / "restaurants.md"
    copy = tmp / "restaurants.md"
    copy.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    before = copy.read_text(encoding="utf-8").splitlines()
    rows = restaurants_md.read_rows(copy)

    restaurants_md.append_row(copy, Row(
        name="パイプ|入り 店", tags=["麺"], area="北見",
        address="北見市1-1", hp="-", menu="改行\n入り"))
    after = copy.read_text(encoding="utf-8").splitlines()

    check("行数は +1", len(after) - len(before), 1)

    inserted = next(i for i, line in enumerate(after) if "パイプ" in line)
    check("既存行は1行も変わらない", after[:inserted] + after[inserted + 1:], before)
    check("| をエスケープし改行を潰す",
          "パイプ\\|入り 店" in after[inserted] and "改行 入り" in after[inserted], True)

    reparsed = restaurants_md.read_rows(copy)
    check("追記後も表として読める", len(reparsed), len(rows) + 1)
    check("来店回数は 0", reparsed[-1].visits, "0")
    check("評価は -", reparsed[-1].rating, "-")


def test_name_matching() -> None:
    """検索で出てきた店が探していた店か。緩いと別支店の住所を書いてしまう。"""
    print("\n[店名の同一性判定]")
    m = maps_resolver.name_matches
    check("完全一致", m("鳥若 北見総本店", "鳥若 北見総本店"), True)
    check("件名が短い（鳥若 → 鳥若 北見総本店）", m("鳥若", "鳥若 北見総本店"), True)
    check("途中まで（鳥若 北見）", m("鳥若 北見", "鳥若 北見総本店"), True)
    check("全角スペースを吸収", m("ラーメン山岡家　北見店", "ラーメン山岡家 北見店"), True)
    check("全角英字を吸収", m("ＭＩＬＫ ＣＲＯＷＮ", "MILK CROWN"), True)
    check("大小文字を吸収", m("milk crown", "MILK CROWN"), True)
    check("曖昧な件名は弾く", m("すすきのの店", "とんかつ太郎"), False)
    check("無関係な店は弾く", m("鳥若 北見総本店", "サイゼリヤ 函館グランディールイチイ店"), False)
    check("空文字は弾く", m("", "鳥若"), False)

    # 支店名以外が同じチェーン店。ここを取り違えると別の街の住所が入る
    check("鳥貴族の別支店を弾く", m("鳥貴族 琴似店", "鳥貴族 麻生店"), False)
    check("サイゼリヤの紛らわしい別支店を弾く",
          m("サイゼリヤ イオンモール旭川駅前店", "サイゼリヤ イオンモール旭川西店"), False)
    check("福よしの別支店を弾く",
          m("元祖美唄やきとり福よし 札幌中央店", "元祖美唄やきとり福よし 美唄本店"), False)
    check("キャプテンラーメンの別支店を弾く",
          m("キャプテンラーメン東相内店", "キャプテンラーメン北見店"), False)


def test_no_confusion_in_real_list() -> None:
    """実名の総当たり。どの2店も取り違えないことを恒久的に固定する。

    以前は文字バイグラムの重なり（閾値0.3）で見ていて、1653ペア中 72組が
    誤一致していた。最悪は「サイゼリヤ イオンモール旭川駅前店」と「同 旭川西店」の
    0.86。新しい店を足したときにこのテストが落ちたら、判定を見直す合図。
    """
    print("\n[実名の総当たりで取り違えが無いか]")
    import itertools

    src = Path(__file__).resolve().parent.parent / "restaurants.md"
    names = [r.name for r in restaurants_md.read_rows(src)]
    pairs = list(itertools.combinations(names, 2))
    confused = [(a, b) for a, b in pairs if maps_resolver.name_matches(a, b)]

    check(f"{len(names)}店 / {len(pairs)}ペアで誤一致ゼロ", confused, [])
    check("自分自身とは必ず一致",
          all(maps_resolver.name_matches(n, n) for n in names), True)


def test_new_branch_not_swallowed() -> None:
    """既出判定が、同一チェーンの新しい支店を飲み込まないか。

    飲み込むと行が追加されないままメールだけアーカイブされ、静かに失われる。
    """
    print("\n[新しい支店が既出扱いされないか]")
    src = Path(__file__).resolve().parent.parent / "restaurants.md"
    rows = restaurants_md.read_rows(src)

    for probe in ["鳥貴族 円山店", "サイゼリヤ イオン旭川店", "ラーメン山岡家 帯広店"]:
        hit = restaurants_md.find(rows, probe)
        check(f"{probe} は新規として扱われる", hit.name if hit else None, None)

    check("既にある店は既出と判定",
          (restaurants_md.find(rows, "鳥貴族 琴似店") or Row("", [], "", "", "", "")).name,
          "鳥貴族 琴似店")


def test_tag_policy() -> None:
    """タグ規則をコードで確かめる。プロンプトの約束は守られる保証が無い。

    表記ゆれでタグが二重に増えると、地図の絞り込みは AND 検索なので
    「ラーメン」と「ラーメン屋」の両方を選ぶと0件になる。
    """
    print("\n[タグ規則の強制]")
    import tagger

    existing = ["麺", "ラーメン", "寿司", "海鮮", "焼き鳥", "居酒屋"]
    policy = tagger.enforce_tag_policy

    tags, new, problem = policy(["麺", "ラーメン"], existing)
    check("既存タグはそのまま通る", (tags, new, problem), (["麺", "ラーメン"], [], None))

    tags, _, _ = policy(["ラーメン屋"], existing)
    check("語尾「屋」のゆれを既存に寄せる", tags, ["ラーメン"])

    tags, _, _ = policy(["焼き鳥専門店"], existing)
    check("語尾「専門店」のゆれを既存に寄せる", tags, ["焼き鳥"])

    tags, _, _ = policy(["ラーメン", "ラーメン"], existing)
    check("重複を潰す", tags, ["ラーメン"])

    tags, new, problem = policy(["麺", "つけ麺"], existing)
    check("新タグ1つは許す", (new, problem), (["つけ麺"], None))

    _, _, problem = policy(["麺", "つけ麺", "油そば"], existing)
    check("新タグ2つ以上は拒否", problem is not None, True)

    _, _, problem = policy([], existing)
    check("タグ無しは拒否", problem is not None, True)


def test_broken_llm_output() -> None:
    """壊れた出力で落ちないか。落ちると以降のメールが処理されない。"""
    print("\n[LLM が壊れた出力を返した場合]")
    import tagger

    r = tagger.TagResult.from_json("これは JSON ではない")
    check("例外を投げない", isinstance(r, tagger.TagResult), True)
    check("confident は False（＝受信箱に残る）", r.confident, False)

    r = tagger.TagResult.from_json('{"tags": ["麺"]}')
    check("欠けたフィールドは安全側の既定", (r.confident, r.recommended_menu), (False, "未確認"))


def test_search_url() -> None:
    print("\n[検索 URL の組み立て]")
    url = maps_resolver.search_url("そばのかね久 総本店")
    check("maps の検索 URL になる", url.startswith("https://www.google.com/maps/search/"), True)
    check("日本語がエスケープされる", " " not in url and "久" not in url, True)
    check("maps URL として判定される", maps_resolver.is_maps_url(url), True)


def test_oauth_scope() -> None:
    """scope が広がっていないか。ここが緩むと削除・送信が可能になる。"""
    print("\n[OAuth の権限]")
    check("scope は gmail.modify のみ", gmail_client.SCOPES,
          ["https://www.googleapis.com/auth/gmail.modify"])
    check("削除・送信を含む全権 scope は使わない",
          any("mail.google.com" in s for s in gmail_client.SCOPES), False)


def test_token_refresh(tmp: Path) -> None:
    """毎朝走るのはこの経路。ブラウザを開かずに更新できるか。"""
    print("\n[トークンの自動更新（無人実行の経路）]")

    class FakeCreds:
        def __init__(self) -> None:
            self.expired, self.refresh_token, self.valid = True, "rt", True
            self.refreshed = False

        def refresh(self, request): self.refreshed = True
        def to_json(self): return '{"token": "refreshed"}'

    creds = FakeCreds()
    token = tmp / "nested" / "token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('{"token": "old"}', encoding="utf-8")

    orig = (gmail_client.Credentials, gmail_client.build, gmail_client.InstalledAppFlow)
    opened_browser = []
    gmail_client.Credentials = type("C", (), {
        "from_authorized_user_file": staticmethod(lambda p, s: creds)})
    gmail_client.build = lambda *a, **kw: "service"
    gmail_client.InstalledAppFlow = type("F", (), {
        "from_client_secrets_file": staticmethod(
            lambda *a: opened_browser.append(1) or (_ for _ in ()).throw(AssertionError))})
    try:
        service = gmail_client.build_service("creds.json", token)
    finally:
        gmail_client.Credentials, gmail_client.build, gmail_client.InstalledAppFlow = orig

    check("期限切れなら refresh する", creds.refreshed, True)
    check("ブラウザを開かない", opened_browser, [])
    check("更新後のトークンを保存する", token.read_text(encoding="utf-8"), '{"token": "refreshed"}')
    check("パーミッションは 600", oct(token.stat().st_mode)[-3:], "600")
    check("service を返す", service, "service")


def test_coords() -> None:
    print("\n[座標パース]")
    check("!3d!4d（実際の地点）を @（視点中心）より優先",
          maps_resolver._coords("/maps/place/X/@43.8058,143.8919,17z/data=!3d43.805836!4d143.891953"),
          (43.805836, 143.891953))
    check("!3d!4d が無ければ @ を使う",
          maps_resolver._coords("/maps/place/X/@35.6812,139.7671,17z/"), (35.6812, 139.7671))
    check("短縮 URL は座標を持たない",
          maps_resolver._coords("https://maps.app.goo.gl/abc"), (None, None))


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_gmail_parsing()
        test_html_only_mail()
        test_name_matching()
        test_no_confusion_in_real_list()
        test_new_branch_not_swallowed()
        test_tag_policy()
        test_broken_llm_output()
        test_search_url()
        test_oauth_scope()
        test_token_refresh(tmp)
        test_coords()
        test_md_roundtrip(tmp)
        test_maps_dom(tmp)
        test_missing_fields(tmp)

    print(f"\n{'=' * 50}\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    sys.exit(1 if FAIL else 0)
