"""Google マップの共有 URL を Playwright で解決する。

README の設計メモにある「リダイレクト先を解決してもページが JavaScript 描画のため
住所を取れない」への回答。ヘッドレスブラウザなら描画後の DOM を読めるので、
店名・住所・電話・営業時間・座標をそのまま取れる。

Google の DOM は予告なく変わる。セレクタは候補を並べて、取れなければ None を返す。
None は「未確認」として扱い、推測で埋めない。
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, asdict
from urllib.parse import quote, urlparse

import restaurants_md

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

MAPS_HOST_HINTS = ("maps.app.goo.gl", "goo.gl", "maps.google.", "google.com/maps")

# place ページの座標。data= の !3d!4d が実際の地点、@lat,lng は視点の中心。
# 前者を優先する。
_COORD_DATA_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_COORD_AT_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")


@dataclass
class Place:
    name: str | None = None
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    hours: list[str] | None = None
    lat: float | None = None
    lng: float | None = None
    resolved_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def is_maps_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(hint in url.lower() or hint in host for hint in MAPS_HOST_HINTS)


def pick_maps_url(urls: list[str]) -> str | None:
    return next((u for u in urls if is_maps_url(u)), None)


def _first_attr(page, selectors: list[str], attr: str) -> str | None:
    for selector in selectors:
        try:
            handle = page.query_selector(selector)
        except Exception:
            continue
        if not handle:
            continue
        value = handle.get_attribute(attr) if attr != "text" else handle.inner_text()
        if value and value.strip():
            return value.strip()
    return None


def _strip_label(value: str | None) -> str | None:
    """aria-label は「住所: 北見市…」の形で来る。ラベル部分を落とす。"""
    if not value:
        return None
    return re.sub(r"^[^:：]{0,12}[:：]\s*", "", value).strip() or None


# \u6570\u5B57\u306B\u631F\u307E\u308C\u305F\u3082\u306E\u3060\u3051\u3002\u524D\u5F8C\u3092\u898B\u306A\u3044\u3068\u300C\u30B5\u30F3\u30BF\u30EF\u30FC3\u968E\u300D\u304C\u300C\u30B5\u30F3\u30BF\u30EF-3\u968E\u300D\u306B\u306A\u308B
_DASH_RE = re.compile(r"(?<=\d)[\u2010-\u2015\u2212\uFF0D\u30FC](?=\d)")
_POSTAL_RE = re.compile(r"^〒?\s*\d{3}[-−–]?\d{4}\s*")
_PREFECTURE_RE = re.compile(r"^(北海道|東京都|(?:京都|大阪)府|.{2,3}県)")


def tidy_address(value: str | None) -> str | None:
    """マップの住所を、既存の表の書式に合わせる。

    マップは「〒092-0232 北海道網走郡津別町新町１５−２２」の形で返すが、
    既存58行は「網走郡美幌町新町2丁目9」— 郵便番号も都道府県も無く、数字は半角。
    揃えないと一覧で浮く。エリア列が地域を持っているので都道府県は要らない。

    座標はマップから直接もらうので、ここを削ってジオコーダの精度が落ちる心配は無い。
    """
    if not value:
        return None
    # NFKC は全角数字を半角にするが、マイナス記号（U+2212）やダッシュ類は
    # 変換しない。既存の表は「2-5-16」の ASCII ハイフンなので寄せる。
    text = unicodedata.normalize("NFKC", value).strip()
    text = _DASH_RE.sub("-", text)
    text = _POSTAL_RE.sub("", text)
    text = _PREFECTURE_RE.sub("", text)
    return text.strip() or None


def _coords(url: str) -> tuple[float | None, float | None]:
    for pattern in (_COORD_DATA_RE, _COORD_AT_RE):
        m = pattern.search(url)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def _dismiss_consent(page) -> None:
    if "consent." not in page.url:
        return
    for selector in ('button[aria-label*="同意"]', 'button:has-text("すべて同意")',
                     'form[action*="consent"] button'):
        try:
            page.click(selector, timeout=3000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            return
        except Exception:
            continue


_DAY_RE = re.compile(r"^[月火水木金土日](曜日)?[\s　]*$|^[月火水木金土日]曜日")
_COPY_LABEL_RE = re.compile(r"^([月火水木金土日]曜日)、(.+)、営業時間をコピーします$")
_JP_TIME_RE = re.compile(r"(\d{1,2})時(\d{2})分")


def _expand_hours(page) -> None:
    """営業時間は折りたたまれていて、開くまで table が DOM に無い。"""
    for selector in ('[aria-label*="1 週間の営業時間"]', '[aria-label*="週間の営業時間"]',
                     '[aria-label="営業時間"]'):
        try:
            page.click(selector, timeout=2000)
        except Exception:
            continue
        # 展開後の表が描かれるまで待つ。出なくても次のセレクタを試す
        try:
            page.wait_for_selector("table", timeout=4000)
        except PWTimeout:
            page.wait_for_timeout(1000)
        return


def _hours_from_table(page) -> list[str] | None:
    """展開後の表を読む。class 名ではなく「1列目が曜日か」で表を見分ける。"""
    for table in page.query_selector_all("table"):
        lines = []
        for row in table.query_selector_all("tr"):
            cells = [c.inner_text().strip() for c in row.query_selector_all("td, th")]
            cells = [c for c in cells if c]
            if len(cells) >= 2 and _DAY_RE.match(cells[0]):
                lines.append(f"{cells[0]} {cells[1]}")
        if len(lines) >= 3:
            return lines
    return None


def _hours_from_labels(page) -> list[str] | None:
    """表が無い場合、各曜日のコピー用ボタンの aria-label から拾う。

    「土曜日、11時00分～14時00分、16時30分～19時00分、営業時間をコピーします」
    の形。読み上げ用の「11時00分」は、表の表記に合わせて 11:00 に直す。
    """
    lines = []
    for el in page.query_selector_all('[aria-label*="営業時間をコピー"]'):
        m = _COPY_LABEL_RE.match((el.get_attribute("aria-label") or "").strip())
        if m:
            times = _JP_TIME_RE.sub(lambda t: f"{int(t.group(1))}:{t.group(2)}", m.group(2))
            lines.append(f"{m.group(1)} {times}")
    # 今日1日ぶんしか無いなら書かない。半端な営業時間はかえって誤解を招く
    return lines if len(lines) >= 3 else None


_DAY_ANNOT_RE = re.compile(r"^([月火水木金土日]曜日)[（(][^）)]{0,12}[）)]")
# その日の時刻が「通常の週間営業時間ではない」ことを示す印。消さずに畳む
_HOLIDAY_RE = re.compile(r"\s*(祝休日の営業時間|祝日の営業時間|時間変更の可能性|"
                         r"営業時間が異なる可能性があります)\s*")
_RANGE_JOIN_RE = re.compile(r"(?<=:\d{2})(?=\d{1,2}:\d{2})")
_DAY_ORDER = "月火水木金土日"


def _tidy_hours(lines: list[str]) -> list[str] | None:
    """マップの営業時間を、恒久的な記録として書ける形に整える。

    実物から取れるのは例えば
        土曜日 11時00分～14時00分16時30分～19時00分
        月曜日(敬老の日) 定休日 時間変更の可能性
        月曜日 9時00分～15時00分、17時00分～19時30分 祝休日の営業時間
    で、そのままでは4つ困る。

    - 時間帯が区切り無しで繋がっていて読めない
    - 祝日名が入る。「月曜日(敬老の日)」は今年しか成り立たないので、
      恒久的な一覧に書くと翌年から嘘になる
    - 一方で「祝休日の営業時間」は、**その時刻が通常の週間営業時間ではない**
      という意味。消して普通の月曜の営業時間として記録すると誤りになるので、
      祝日名は落としつつ「（祝日）」として残し、人が見直せるようにする
    - 今日を起点に並ぶので、実行日によって順番が変わる

    曜日と時刻そのものは省略せずに残す。
    """
    cleaned = []
    for line in lines:
        line = _JP_TIME_RE.sub(lambda t: f"{int(t.group(1))}:{t.group(2)}", line)
        atypical = bool(_HOLIDAY_RE.search(line)) or bool(_DAY_ANNOT_RE.search(line))
        line = _DAY_ANNOT_RE.sub(r"\1", line)
        line = _HOLIDAY_RE.sub(" ", line)
        line = _RANGE_JOIN_RE.sub("、", line)
        line = re.sub(r"\s{2,}", " ", line).strip()
        if line:
            cleaned.append(f"{line}（祝日）" if atypical else line)

    cleaned.sort(key=lambda l: _DAY_ORDER.find(l[0]) if l[:1] in _DAY_ORDER else 99)
    return cleaned or None


def _read_hours(page) -> list[str] | None:
    """曜日ごとの行をそのまま写す。要約や省略はしない。

    営業時間の節は店名や住所より遅れて描画される。同じ URL でも取れたり
    取れなかったりしたので、節が現れるまで待ってから読む。
    """
    try:
        page.wait_for_selector('[aria-label*="営業時間"], [aria-label*="時間をコピー"]',
                               timeout=6000)
    except PWTimeout:
        pass

    hours = _hours_from_table(page)
    if not hours:
        _expand_hours(page)
        hours = _hours_from_table(page) or _hours_from_labels(page)
    return _tidy_hours(hours) if hours else None


def search_url(name: str) -> str:
    """店名から Google マップの検索 URL を組み立てる。

    マップの共有 URL が無いメール（SNS のリンクだけ、紙面の店名を打っただけ）でも、
    同じ place ページに辿り着ければ住所も座標も取れる。検索エンジンを別途
    スクレイピングするより、検証済みの経路をそのまま使うほうが壊れにくい。
    """
    return "https://www.google.com/maps/search/" + quote(name)


def name_matches(query: str, found: str) -> bool:
    """検索で出てきた店が、探していた店かどうか。

    正規化して片方が他方を含むことを求める。件名は正式名そのもの（マップの
    共有メール）か、その略記（「鳥若」→「鳥若 北見総本店」）のどちらかなので、
    包含で足りる。

    類似度で測ってはいけない。このリストには鳥貴族8店・サイゼリヤ8店・福よし5店が
    あり、支店名以外は全部同じ文字列になる。実名58件を総当たりすると、文字
    バイグラムの重なりでは「サイゼリヤ イオンモール旭川駅前店」と「同 旭川西店」が
    0.86 で一致してしまう（72組が誤一致）。閾値を上げても解けない。
    包含なら同じ総当たりで誤一致は0。

    表記ゆれ（「回転寿司」と「回転寿し」）では一致しなくなるが、外した側は
    受信箱に残るだけなので害がない。別支店の住所を書き込むほうがずっと悪い。
    """
    q, f = restaurants_md.normalize(query), restaurants_md.normalize(found)
    if not q or not f:
        return False
    return q in f or f in q


class Session:
    """ブラウザを使い回す。1通のメールで検索と公式サイトの2回開くことがある。"""

    def __init__(self, *, headless: bool = True, timeout_ms: int = 30000) -> None:
        self.headless, self.timeout_ms = headless, timeout_ms

    def __enter__(self) -> "Session":
        # 環境に Playwright 同梱でない Chromium しか無い場合の逃げ道
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless,
                                                 executable_path=executable)
        self._context = self._browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        return self

    def __exit__(self, *exc) -> None:
        self._context.close()
        self._browser.close()
        self._pw.stop()

    def _open(self, url: str):
        page = self._context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        _dismiss_consent(page)
        return page

    def place(self, url: str) -> Place:
        page = self._open(url)
        try:
            try:
                page.wait_for_selector("h1", timeout=self.timeout_ms)
            except PWTimeout:
                pass
            page.wait_for_timeout(1500)  # 情報パネルの遅延描画ぶん

            final_url = page.url
            lat, lng = _coords(final_url)
            return Place(
                name=_first_attr(page, ["h1"], "text"),
                address=tidy_address(_strip_label(
                    _first_attr(page, ['button[data-item-id="address"]',
                                       '[data-item-id="address"]'], "aria-label")
                )),
                website=_first_attr(page, ['a[data-item-id="authority"]',
                                           '[data-item-id="authority"]'], "href"),
                phone=_strip_label(
                    _first_attr(page, ['button[data-item-id^="phone:tel:"]',
                                       '[data-item-id^="phone:tel:"]'], "aria-label")
                ),
                hours=_read_hours(page),
                lat=lat,
                lng=lng,
                resolved_url=final_url,
            )
        finally:
            page.close()

    def search(self, name: str) -> Place | None:
        """店名で検索する。1軒に決まらなければ None。

        1軒に決まったかは URL ではなく、店の詳細パネルが開いたかで見る。
        実物で確かめたところ、1軒に絞れても URL は /search/ のまま留まることが
        あった（「そばのかね久 総本店」で確認）。URL を条件にすると、全部取れて
        いるのに取りこぼす。

        住所（data-item-id="address"）は詳細パネルの要素で、候補が並んでいる
        一覧の状態では存在しない。名前と住所が揃っていれば1軒に決まっている。
        """
        place = self.place(search_url(name))
        if not place.name or not place.address:
            return None
        if not name_matches(name, place.name):
            return None
        return place

    def text(self, url: str, max_chars: int = 4000) -> str | None:
        """公式サイトの描画後テキスト。タガーに渡す材料にする。"""
        try:
            page = self._open(url)
        except Exception:
            return None
        try:
            page.wait_for_timeout(1000)
            body = page.query_selector("body")
            return (body.inner_text()[:max_chars].strip() or None) if body else None
        except Exception:
            return None
        finally:
            page.close()


def resolve(url: str, *, headless: bool = True, timeout_ms: int = 30000) -> Place:
    with Session(headless=headless, timeout_ms=timeout_ms) as session:
        return session.place(url)


def _verdict(place: Place) -> int:
    """セレクタが実物の DOM に当たっているかを人が読める形で出す。"""
    rows = [
        ("店名", place.name, True),
        ("住所", place.address, True),
        # 座標は共有 URL なら付いてくるが、検索経由だと URL に入らない。
        # 無くても build.mjs が住所からジオコーダで引くので必須ではない
        ("座標", f"{place.lat}, {place.lng}" if place.lat else None, False),
        ("公式サイト", place.website, False),
        ("電話", place.phone, False),
        ("営業時間", "／".join(place.hours) if place.hours else None, False),
    ]
    print(f"\n解決先: {place.resolved_url}\n")
    for label, value, required in rows:
        mark = "OK  " if value else ("NG  " if required else "--  ")
        print(f"  {mark}{label}: {value if value else '取れなかった'}")

    missing = [label for label, value, required in rows if required and not value]
    print()
    if missing:
        print(f"セレクタが実物に当たっていない: {'、'.join(missing)}")
        print("--debug を付けて実行し、出力をそのまま渡してもらえれば直せます。")
        return 1
    print("必須項目（店名・住所）は取れています。セレクタは生きています。")
    if not place.lat:
        print("座標はこの経路では取れません（検索だと URL に入らない）。"
              "build.mjs が住所から引くので問題ありません。")
    if not place.hours:
        print("営業時間だけ取れていません。メモが薄くなるだけで、取り込みは動きます。")
    return 0


def _debug_dump(url: str) -> None:
    """実物の DOM に何があるかを並べる。セレクタを直すための材料。"""
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=executable)
        page = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo").new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _dismiss_consent(page)
            page.wait_for_timeout(3000)
            print(f"\n最終 URL: {page.url}\n")
            print("--- h1 ---")
            for h in page.query_selector_all("h1")[:5]:
                print(f"  {h.inner_text()[:80]!r}")
            print("\n--- data-item-id を持つ要素 ---")
            for el in page.query_selector_all("[data-item-id]")[:25]:
                print(f"  data-item-id={el.get_attribute('data-item-id')!r}  "
                      f"aria-label={(el.get_attribute('aria-label') or '')[:60]!r}")
            print("\n--- aria-label に「時間」を含む要素 ---")
            for el in page.query_selector_all('[aria-label*="時間"]')[:12]:
                print(f"  <{el.evaluate('e => e.tagName')}> "
                      f"aria-label={(el.get_attribute('aria-label') or '')[:70]!r}")

            tables = page.query_selector_all("table")
            print(f"\n--- table 要素: {len(tables)} 個 ---")
            for i, table in enumerate(tables[:3]):
                rows = table.query_selector_all("tr")
                print(f"  [{i}] aria-label={(table.get_attribute('aria-label') or '')[:40]!r} "
                      f"行数={len(rows)}")
                for row in rows[:3]:
                    cells = [c.inner_text().strip()[:30]
                             for c in row.query_selector_all("td, th")]
                    print(f"        {cells}")

            print("\n--- 「曜日」を含む要素（表以外の描き方を探す）---")
            for el in page.query_selector_all('*:has-text("曜日")')[-6:]:
                tag = el.evaluate("e => e.tagName")
                text = (el.inner_text() or "").replace("\n", " / ")[:90]
                print(f"  <{tag}> {text!r}")
        finally:
            browser.close()


if __name__ == "__main__":
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(
            "usage: python maps_resolver.py <google-maps-url> [--debug] [--headful]"
        )

    if "--debug" in sys.argv:
        _debug_dump(args[0])
        raise SystemExit(0)

    place = resolve(args[0], headless="--headful" not in sys.argv)
    raise SystemExit(_verdict(place))
