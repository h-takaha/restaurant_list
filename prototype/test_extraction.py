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
        test_coords()
        test_md_roundtrip(tmp)
        test_maps_dom(tmp)
        test_missing_fields(tmp)

    print(f"\n{'=' * 50}\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    sys.exit(1 if FAIL else 0)
