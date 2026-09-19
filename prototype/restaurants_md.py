"""restaurants.md の読み取りと追記。

追記は「最後の表の行の直後に1行挿入する」だけ。既存行は一切読み書きしない。
来店回数と評価は利用者が地図画面から更新する列なので、routine 側が触ると
記録が消える。行単位の挿入にしておけば、そもそも触りようがない。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ["店名", "タグ", "エリア", "住所", "HP", "オススメメニュー", "来店回数", "評価", "メモ"]

_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


@dataclass
class Row:
    name: str
    tags: list[str]
    area: str
    address: str
    hp: str
    menu: str
    visits: str = "0"
    rating: str = "-"
    memo: str = ""

    def render(self) -> str:
        cells = [
            self.name,
            " / ".join(self.tags) if self.tags else "-",
            self.area,
            self.address,
            self.hp or "-",
            self.menu,
            self.visits,
            self.rating,
            self.memo,
        ]
        return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _cell(value: str) -> str:
    """表のセルに入れられる形に直す。改行と | は表を壊す。"""
    return str(value).replace("\n", " ").replace("|", "\\|").strip()


def normalize(name: str) -> str:
    """全角半角・空白のゆれを吸収した突合用のキー。"""
    folded = unicodedata.normalize("NFKC", name)
    return "".join(folded.split()).lower()


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _split_cells(line: str) -> list[str]:
    """エスケープされていない | だけで区切る。

    素朴に split("|") すると、店名に含まれる `\\|` でセルが1つ増え、
    以降の列が全てずれる。来店回数の位置にメモが入るような壊れ方をするので、
    render() のエスケープと対称に扱う必要がある。
    """
    body = line.strip()
    parts = _CELL_SPLIT_RE.split(body)
    if len(parts) >= 2:
        parts = parts[1:-1]  # 行頭と行末の | が生む空要素
    return [p.strip().replace("\\|", "|") for p in parts]


def read_rows(path: str | Path) -> list[Row]:
    rows: list[Row] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not _is_table_line(line):
            continue
        cells = _split_cells(line)
        if len(cells) < len(COLUMNS):
            continue
        if cells[0] == COLUMNS[0] or set(cells[0]) <= {"-", ":"}:
            continue  # ヘッダと区切り行
        rows.append(
            Row(
                name=cells[0],
                tags=[t.strip() for t in cells[1].split("/") if t.strip() not in ("", "-")],
                area=cells[2],
                address=cells[3],
                hp=cells[4],
                menu=cells[5],
                visits=cells[6],
                rating=cells[7],
                memo=cells[8] if len(cells) > 8 else "",
            )
        )
    return rows


def existing_tags(rows: list[Row]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        for tag in row.tags:
            seen.setdefault(tag, None)
    return sorted(seen)


def existing_areas(rows: list[Row]) -> list[str]:
    """エリア列に出てくる地名。タグへの混入を弾くのに使う。

    「北見・端野」のような複合も個別に割っておく。
    """
    seen: dict[str, None] = {}
    for row in rows:
        for part in row.area.replace("・", "/").split("/"):
            if part.strip():
                seen.setdefault(part.strip(), None)
    return sorted(seen)


def implied_tags(rows: list[Row], min_support: int = 3) -> dict[str, list[str]]:
    """既存データで「必ず一緒に付いている」タグの対応。

    58行を見ると、ラーメンは必ず麺と、寿司は必ず海鮮と、焼き鳥は必ず居酒屋と
    一緒に付いている。地図の絞り込みは AND なので、麺の無いラーメン店は
    「麺」で絞ると出てこない。慣習をデータから読み取って補う。

    逆向きには効かない（麺はラーメン以外にも付くので何も含意しない）。
    慣習が崩れれば推論も自動で外れる。min_support は、1〜2行だけの偶然から
    規則を作らないための下限。
    """
    counts: dict[str, int] = {}
    together: dict[str, dict[str, int]] = {}
    for row in rows:
        for tag in row.tags:
            counts[tag] = counts.get(tag, 0) + 1
            pairs = together.setdefault(tag, {})
            for other in row.tags:
                if other != tag:
                    pairs[other] = pairs.get(other, 0) + 1

    return {
        tag: sorted(o for o, n in together.get(tag, {}).items() if n == total)
        for tag, total in counts.items()
        if total >= min_support
    }


def find(rows: list[Row], name: str) -> Row | None:
    key = normalize(name)
    for row in rows:
        if normalize(row.name) == key:
            return row
    # 「鳥貴族 すすきの店」のような表記ゆれの部分一致も拾う
    for row in rows:
        other = normalize(row.name)
        if key and other and (key in other or other in key):
            return row
    return None


def append_row(path: str | Path, row: Row) -> None:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    last_table_index = max(
        (i for i, line in enumerate(lines) if _is_table_line(line)), default=None
    )
    if last_table_index is None:
        raise ValueError(f"{path} に表が見つからない")

    lines.insert(last_table_index + 1, row.render())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
