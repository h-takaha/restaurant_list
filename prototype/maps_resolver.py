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


def _read_hours(page) -> list[str] | None:
    """曜日ごとの行をそのまま写す。要約や省略はしない。"""
    for selector in ('table[aria-label*="営業時間"] tr', 'table[aria-label*="時間"] tr'):
        try:
            rows = page.query_selector_all(selector)
        except Exception:
            continue
        lines = []
        for row in rows:
            cells = [c.inner_text().strip() for c in row.query_selector_all("td, th")]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                lines.append(f"{cells[0]} {cells[1]}")
        if lines:
            return lines
    return None


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
                address=_strip_label(
                    _first_attr(page, ['button[data-item-id="address"]',
                                       '[data-item-id="address"]'], "aria-label")
                ),
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

        候補が複数あると URL は /search/ のまま留まる。どれか選ぶのは推測に
        なるので選ばない。呼び出し側は受信箱に残す。
        """
        place = self.place(search_url(name))
        if not place.resolved_url or "/place/" not in place.resolved_url:
            return None
        if not place.name or not name_matches(name, place.name):
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
        ("座標", f"{place.lat}, {place.lng}" if place.lat else None, True),
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
    print("必須項目（店名・住所・座標）は取れています。セレクタは生きています。")
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
            for el in page.query_selector_all('[aria-label*="時間"]')[:10]:
                print(f"  <{el.evaluate('e => e.tagName')}> "
                      f"aria-label={(el.get_attribute('aria-label') or '')[:60]!r}")
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
