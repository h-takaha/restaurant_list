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


def test_hours_collapsed(tmp: Path) -> None:
    """実物のマップは営業時間を畳んでいて、開くまで table が DOM に無い。

    2026-09 に実 URL を --debug で見たときの構造を写したもの:
      <SPAN aria-label='1 週間の営業時間を表示'>   ← トグル
      table は展開後に現れる
    """
    print("\n[営業時間が畳まれているページ]")
    page = tmp / "collapsed.html"
    page.write_text("""<!doctype html><meta charset="utf-8">
<h1>そば・うどん 味登利家</h1>
<span aria-label="1 週間の営業時間を表示" onclick="
  document.body.insertAdjacentHTML('beforeend',
    '<table><tr><td>月曜日</td><td>定休日</td></tr>' +
    '<tr><td>金曜日</td><td>11:00～14:00, 16:30～19:00</td></tr>' +
    '<tr><td>土曜日</td><td>11:00～14:00, 16:30～19:00</td></tr></table>')
">営業時間</span>""", encoding="utf-8")

    place = maps_resolver.resolve(page.as_uri())
    check("展開して曜日ごとに読める", place.hours,
          ["月曜日 定休日", "金曜日 11:00～14:00, 16:30～19:00",
           "土曜日 11:00～14:00, 16:30～19:00"])


def test_hours_from_aria_labels(tmp: Path) -> None:
    """表が無くても、各曜日のコピー用ボタンから拾えるか。"""
    print("\n[コピー用ボタンの aria-label から拾う]")
    page = tmp / "labels.html"
    buttons = "".join(
        f'<button aria-label="{day}、11時00分～14時00分、16時30分～19時00分、'
        f'営業時間をコピーします"></button>'
        for day in ("金曜日", "土曜日", "日曜日"))
    page.write_text(f'<!doctype html><meta charset="utf-8"><h1>店</h1>{buttons}',
                    encoding="utf-8")

    place = maps_resolver.resolve(page.as_uri())
    # 時間帯の区切り「、」はマップの表記のまま残す（既存のメモも「、」を使っている）
    check("読み上げ用の「11時00分」を 11:00 に直す", place.hours,
          ["金曜日 11:00～14:00、16:30～19:00",
           "土曜日 11:00～14:00、16:30～19:00",
           "日曜日 11:00～14:00、16:30～19:00"])


def test_hours_tidying_real_data() -> None:
    """2026-09-19 に実物のマップから取れた生の値をそのまま整形にかける。

    そのままでは3つ困る値が返ってくる:
      - 時間帯が区切り無しで繋がる（14時00分16時30分）
      - 祝日の注記が入る。「月曜日(敬老の日)」は今年しか成り立たない
      - 今日起点で並ぶので実行日によって順番が変わる
    """
    print("\n[実物から取れた営業時間の整形]")
    raw = [
        "土曜日 11時00分～14時00分16時30分～19時00分",
        "日曜日 11時00分～14時00分16時30分～19時00分",
        "月曜日(敬老の日) 定休日 時間変更の可能性",
        "火曜日 11時00分～14時00分",
        "水曜日(秋分の日) 11時00分～14時00分 時間変更の可能性",
        "木曜日 11時00分～14時00分",
        "金曜日 11時00分～14時00分16時30分～19時00分",
    ]
    check("月曜から並べ、時刻を整え、祝日名は落として印だけ残す",
          maps_resolver._tidy_hours(raw),
          ["月曜日 定休日（祝日）",
           "火曜日 11:00～14:00",
           "水曜日 11:00～14:00（祝日）",
           "木曜日 11:00～14:00",
           "金曜日 11:00～14:00、16:30～19:00",
           "土曜日 11:00～14:00、16:30～19:00",
           "日曜日 11:00～14:00、16:30～19:00"])

    # 別の店（そばのかね久）。こちらは「祝休日の営業時間」の形で付いてきた。
    # その時刻は通常の週間営業時間ではないので、消すと嘘の記録になる
    check("「祝休日の営業時間」も印として残す",
          maps_resolver._tidy_hours([
              "月曜日 9時00分～15時00分、17時00分～19時30分 祝休日の営業時間",
              "火曜日 10時30分～19時30分",
              "木曜日 定休日"]),
          ["月曜日 9:00～15:00、17:00～19:30（祝日）",
           "火曜日 10:30～19:30",
           "木曜日 定休日"])

    # かね久の表から取れた生の値（inner_text なので時間帯が繋がっている）
    check("表から取れた生の値も整う",
          maps_resolver._tidy_hours([
              "月曜日(敬老の日) 9時00分～15時00分17時00分～19時30分 祝休日の営業時間",
              "木曜日 定休日",
              "金曜日 10時30分～19時30分"]),
          ["月曜日 9:00～15:00、17:00～19:30（祝日）",
           "木曜日 定休日",
           "金曜日 10:30～19:30"])

    # 曜日の直後の括弧だけを落とす。時刻側の補足は残す
    check("L.O. の括弧は消さない",
          maps_resolver._tidy_hours(["金曜日 11:00～14:00 (L.O.13:30)"]),
          ["金曜日 11:00～14:00 (L.O.13:30)"])


def test_search_accepts_search_url() -> None:
    """検索が1軒に決まったかを URL で判定しない。

    実物で確かめたところ、「そばのかね久 総本店」は1軒に決まったのに URL は
    /search/ のまま留まった。URL を条件にすると、店名も住所も営業時間も全部
    取れているのに取りこぼす。
    """
    print("\n[検索の絞り込み判定]")

    class StubSession(maps_resolver.Session):
        def __init__(self, result):
            self.result = result

        def place(self, url):
            return self.result

    single = maps_resolver.Place(
        name="かね久総本店", address="網走郡美幌町新町2丁目9",
        resolved_url="https://www.google.com/maps/search/%E3%81%9D%E3%81%B0...")
    check("URL が /search/ のままでも、名前と住所が揃えば採る",
          StubSession(single).search("そばのかね久 総本店") is not None, True)

    # 候補が並んでいる一覧には詳細パネルが無いので住所が取れない
    listing = maps_resolver.Place(name="そば 検索結果", address=None,
                                  resolved_url="https://www.google.com/maps/search/x")
    check("住所が無ければ一覧とみなして採らない",
          StubSession(listing).search("そば"), None)

    other = maps_resolver.Place(name="鳥貴族 麻生店", address="札幌市北区麻生町2-3-7",
                                resolved_url="https://www.google.com/maps/place/x")
    check("別の店が出てきたら採らない",
          StubSession(other).search("鳥貴族 琴似店"), None)


def test_hours_hidden_buttons(tmp: Path) -> None:
    """コピー用ボタンは DOM に在るが非表示。待ちは state="attached" が要る。

    Playwright の wait_for_selector は既定で「可視」を待つので、既定のままだと
    必ずタイムアウトする。実物ではその空振りの8秒がスリープとして働き、
    結果的に表が描けていた。偶然に頼らないよう固定する。
    """
    print("\n[非表示のコピー用ボタンを待てるか]")
    page = tmp / "hidden.html"
    buttons = "".join(
        f'<button aria-label="{day}、10時30分～19時30分、営業時間をコピーします"'
        f' style="display:none"></button>'
        for day in ("月曜日", "火曜日", "水曜日"))
    rows = "".join(f"<tr><td>{day}</td><td>10:30～19:30</td></tr>"
                   for day in ("月曜日", "火曜日", "水曜日"))
    page.write_text(f'<!doctype html><meta charset="utf-8"><h1>店</h1>'
                    f'{buttons}<table>{rows}</table>', encoding="utf-8")

    place = maps_resolver.resolve(page.as_uri())
    check("非表示でも待てて、表から読める", place.hours,
          ["月曜日 10:30～19:30", "火曜日 10:30～19:30", "水曜日 10:30～19:30"])


def test_hours_table_arrives_late(tmp: Path) -> None:
    """ボタンは先に DOM に付き、表は遅れて描かれる。表が出るまで待てるか。

    待つ対象を間違えて2度外した箇所:
      1. <span aria-label="営業時間"> は節の見出しで最初から在り、素通りした
      2. コピー用ボタンを state="attached" で待ったら、ボタンは付いたが表は
         まだという瞬間に読んで営業時間が消えた（実機で再現）
    欲しいデータ（曜日で始まる表の行）そのものを待つ。
    """
    print("\n[表が遅れて描かれるページ]")
    page = tmp / "late.html"
    rows = "".join(f"<tr><td>{d}</td><td>11:00～14:30</td></tr>"
                   for d in ("月曜日", "火曜日", "水曜日"))
    page.write_text(
        '<!doctype html><meta charset="utf-8"><h1>麺屋 蘭奢待</h1>'
        '<button aria-label="月曜日、11時00分～14時30分、営業時間をコピーします"'
        ' style="display:none"></button>'
        f'<script>setTimeout(() => document.body.insertAdjacentHTML('
        f'"beforeend", "<table>{rows}</table>"), 2000);</script>',
        encoding="utf-8")

    place = maps_resolver.resolve(page.as_uri())
    check("遅れて出た表を読める", place.hours,
          ["月曜日 11:00～14:30", "火曜日 11:00～14:30", "水曜日 11:00～14:30"])


def test_hours_today_only(tmp: Path) -> None:
    """今日1日ぶんしか無いなら書かない。半端な営業時間は誤解を招く。"""
    print("\n[今日ぶんしか取れない場合]")
    page = tmp / "today.html"
    page.write_text('<!doctype html><meta charset="utf-8"><h1>店</h1>'
                    '<button aria-label="土曜日、11時00分～14時00分、'
                    '営業時間をコピーします"></button>', encoding="utf-8")

    place = maps_resolver.resolve(page.as_uri())
    check("1日だけなら None（＝メモに書かない）", place.hours, None)


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


def test_area_not_a_tag() -> None:
    """地名がタグに混入しないか。実際に Ollama が「美幌」を入れてきた。

    エリア列が既に持っているので、タグにすると地図に使い道のない絞り込み
    ボタンが増える。タグは AND 検索なので選んでも意味がない。
    """
    print("\n[地名をタグにしない]")
    import tagger

    src = Path(__file__).resolve().parent.parent / "restaurants.md"
    rows = restaurants_md.read_rows(src)
    tags = restaurants_md.existing_tags(rows)
    areas = restaurants_md.existing_areas(rows)

    check("エリア一覧を拾える", "美幌" in areas and "北見" in areas, True)

    # 2026-09-19 の dry-run で Ollama が実際に返した値
    accepted, new, problem = tagger.enforce_tag_policy(["ラーメン", "美幌"], tags, areas)
    check("「美幌」は落ちる", "美幌" not in accepted, True)
    check("新しいタグ扱いにもしない", new, [])
    check("ラーメンは残る", "ラーメン" in accepted, True)
    check("行は追加される（受信箱に残さない）", problem, None)

    for area in ["北見", "札幌すすきの", "端野", "旭川"]:
        accepted, _, _ = tagger.enforce_tag_policy(["麺", "ラーメン", area], tags, areas)
        check(f"{area} も落ちる", area not in accepted, True)


def test_implied_tags() -> None:
    """既存データから慣習を読み取れるか。

    58行ではラーメンに必ず麺が、寿司に必ず海鮮が付いている。地図の絞り込みは
    AND なので、麺の無いラーメン店は「麺」で絞ると出てこない。
    """
    print("\n[既存の慣習からタグを補う]")
    import tagger

    src = Path(__file__).resolve().parent.parent / "restaurants.md"
    rows = restaurants_md.read_rows(src)
    implied = restaurants_md.implied_tags(rows)

    check("ラーメン → 麺", implied.get("ラーメン"), ["麺"])
    check("寿司 → 海鮮", implied.get("寿司"), ["海鮮"])
    check("焼き鳥 → 居酒屋", implied.get("焼き鳥"), ["居酒屋"])
    # 逆向きには効かない。麺はラーメン以外にも付くので何も含意しない
    check("麺 → 何も含意しない", implied.get("麺"), [])
    check("海鮮 → 何も含意しない", implied.get("海鮮"), [])

    tags = restaurants_md.existing_tags(rows)
    areas = restaurants_md.existing_areas(rows)
    accepted, _, _ = tagger.enforce_tag_policy(["ラーメン", "美幌"], tags, areas, implied)
    check("地名を落としたうえで麺を補う", sorted(accepted), ["ラーメン", "麺"])


def test_broken_llm_output() -> None:
    """壊れた出力で落ちないか。落ちると以降のメールが処理されない。"""
    print("\n[LLM が壊れた出力を返した場合]")
    import tagger

    r = tagger.TagResult.from_json("これは JSON ではない")
    check("例外を投げない", isinstance(r, tagger.TagResult), True)
    check("confident は False（＝受信箱に残る）", r.confident, False)

    r = tagger.TagResult.from_json('{"tags": ["麺"]}')
    check("欠けたフィールドは安全側の既定", (r.confident, r.recommended_menu), (False, "未確認"))


def test_tidy_address() -> None:
    """マップの住所を既存58行の書式に寄せる。

    実物のマップは「〒092-0232 北海道網走郡津別町新町１５−２２」を返すが、
    既存は「網走郡美幌町新町2丁目9」。揃えないと一覧で浮く。
    """
    print("\n[住所の書式合わせ]")
    t = maps_resolver.tidy_address
    check("郵便番号と都道府県を落とし、数字を半角に",
          t("〒092-0232 北海道網走郡津別町新町１５−２２"), "網走郡津別町新町15-22")
    check("ビル名は残す",
          t("〒090-0065 北海道北見市北5条西2丁目 東宝ビル1F"), "北見市北5条西2丁目 東宝ビル1F")
    check("既に整っていれば変えない", t("北見市本町2-5-16"), "北見市本町2-5-16")
    check("札幌市も市から始める",
          t("〒060-0063 北海道札幌市中央区南3条西2丁目17-2"), "札幌市中央区南3条西2丁目17-2")
    check("北海道以外の県も落とす", t("〒150-0002 東京都渋谷区渋谷1-1-1"), "渋谷区渋谷1-1-1")

    # 長音符は数字に挟まれたときだけハイフン扱いにする
    check("数字の間の長音符はハイフンに", t("北見市東三輪4ー12ー20"), "北見市東三輪4-12-20")
    check("カタカナの長音符は壊さない",
          t("札幌市中央区 サンタワー3階"), "札幌市中央区 サンタワー3階")
    check("None はそのまま", t(None), None)


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
        test_area_not_a_tag()
        test_implied_tags()
        test_broken_llm_output()
        test_tidy_address()
        test_search_url()
        test_oauth_scope()
        test_token_refresh(tmp)
        test_coords()
        test_md_roundtrip(tmp)
        test_maps_dom(tmp)
        test_hours_collapsed(tmp)
        test_hours_from_aria_labels(tmp)
        test_hours_tidying_real_data()
        test_hours_hidden_buttons(tmp)
        test_hours_table_arrives_late(tmp)
        test_hours_today_only(tmp)
        test_search_accepts_search_url()
        test_missing_fields(tmp)

    print(f"\n{'=' * 50}\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    sys.exit(1 if FAIL else 0)
