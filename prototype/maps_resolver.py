"""Google マップの共有 URL を Playwright で解決する。

README の設計メモにある「リダイレクト先を解決してもページが JavaScript 描画のため
住所を取れない」への回答。ヘッドレスブラウザなら描画後の DOM を読めるので、
店名・住所・電話・営業時間・座標をそのまま取れる。

Google の DOM は予告なく変わる。セレクタは候補を並べて、取れなければ None を返す。
None は「未確認」として扱い、推測で埋めない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

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


def resolve(url: str, *, headless: bool = True, timeout_ms: int = 30000) -> Place:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            _dismiss_consent(page)
            try:
                page.wait_for_selector("h1", timeout=timeout_ms)
            except PWTimeout:
                pass
            page.wait_for_timeout(1500)  # 情報パネルの遅延描画ぶん

            final_url = page.url
            lat, lng = _coords(final_url)

            place = Place(
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
            return place
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: python maps_resolver.py <google-maps-url>")
    print(json.dumps(resolve(sys.argv[1]).to_dict(), ensure_ascii=False, indent=2))
