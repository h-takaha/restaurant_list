"""タグとオススメメニューの判定。

設計上の肝: LLM に知識を思い出させない。渡したテキストに書いてあることだけを
分類させる。住所や営業時間を LLM に「調べさせる」と小型モデルは平然と捏造するが、
取得済みテキストからの分類に閉じ込めれば、ローカルの小型モデルでも成立する。

同じ入出力の実装を2つ置いてある。ClaudeTagger と OllamaTagger は差し替え可能。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import restaurants_md

SYSTEM = """あなたは飲食店リストのタグ付けだけを行う。

厳守:
- 与えられたテキストに書いてあることだけを使う。自分の知識で補わない。
- 店名・住所・メニューを推測で作らない。テキストに無ければ空文字を返す。
- タグは既存タグ一覧から選ぶ。どれにも当てはまらないときだけ、新しいタグを1つだけ増やしてよい。
- 新しいタグは「〜専門店」「〜料理」のような語尾を付けず、短い名詞にする。
- 上位のくくりと具体名の両方を付けてよい（例: ラーメン店なら「麺」と「ラーメン」）。
- 表記ゆれで既存タグと重複する新タグを作らない。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {"type": "array", "items": {"type": "string"}},
        "new_tags": {"type": "array", "items": {"type": "string"}},
        "recommended_menu": {"type": "string"},
        "area": {"type": "string"},
        "confident": {"type": "boolean"},
    },
    "required": ["tags", "new_tags", "recommended_menu", "area", "confident"],
    "additionalProperties": False,
}


_SUFFIXES = ("専門店", "料理", "屋", "店")


def enforce_tag_policy(tags: list[str], existing: list[str]) -> tuple[list[str], list[str], str | None]:
    """返ってきたタグを既存一覧と突き合わせる。

    プロンプトで「既存タグを使い回せ」「新タグは1つまで」と頼んでも、守る保証は
    無い。守られないと地図の絞り込みボタンが表記ゆれで二重に増え、しかも
    AND 検索なので「ラーメン」と「ラーメン屋」の両方を選ぶと0件になる。
    規則はコード側で確かめる。

    返り値は (採用するタグ, 新しいタグ, 問題があればその説明)。
    """
    lookup = {restaurants_md.normalize(t): t for t in existing}

    accepted: list[str] = []
    new: list[str] = []
    for tag in dict.fromkeys(t.strip() for t in tags if t.strip()):
        key = restaurants_md.normalize(tag)

        # 全角半角・空白・大小文字だけの違いは既存の表記に寄せる
        if key in lookup:
            accepted.append(lookup[key])
            continue

        # 「〜屋」「〜料理」「〜専門店」を落として既存に当たるなら、それは語尾ゆれ
        canonical = next(
            (lookup[stripped] for suffix in _SUFFIXES
             if (stripped := key[:-len(suffix)]) and key.endswith(suffix) and stripped in lookup),
            None,
        )
        if canonical:
            accepted.append(canonical)
            continue

        accepted.append(tag)
        new.append(tag)

    if not accepted:
        return accepted, new, "タグが1つも付かなかった"
    if len(new) > 1:
        return accepted, new, f"新しいタグを{len(new)}個作ろうとした: {', '.join(new)}"
    return list(dict.fromkeys(accepted)), new, None


@dataclass
class TagResult:
    tags: list[str]
    new_tags: list[str]
    recommended_menu: str
    area: str
    confident: bool

    @classmethod
    def from_json(cls, raw: str) -> "TagResult":
        # スキーマを渡していても、ローカル LLM は壊れた JSON を返すことがある。
        # 落とさず「確信なし」にして受信箱に残す。
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls(tags=[], new_tags=[], recommended_menu="未確認",
                       area="未確認", confident=False)
        return cls(
            tags=data.get("tags", []),
            new_tags=data.get("new_tags", []),
            recommended_menu=data.get("recommended_menu", "") or "未確認",
            area=data.get("area", "") or "未確認",
            confident=bool(data.get("confident", False)),
        )


def build_prompt(*, name: str, existing_tags: list[str], source_text: str) -> str:
    return f"""既存タグ一覧（この中から選ぶ）:
{", ".join(existing_tags)}

店名: {name}

材料（これ以外の情報を使わないこと）:
---
{source_text}
---

この店のタグ、オススメメニュー、エリア（北見・美幌・札幌駅 などの通称）を決めて。
材料から読み取れないものは空文字にする。判断に足る材料が無ければ confident を false に。"""


class Tagger(Protocol):
    def tag(self, *, name: str, existing_tags: list[str], source_text: str) -> TagResult: ...


class ClaudeTagger:
    def __init__(self, model: str = "claude-opus-5") -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model

    def tag(self, *, name: str, existing_tags: list[str], source_text: str) -> TagResult:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": build_prompt(
                        name=name, existing_tags=existing_tags, source_text=source_text
                    ),
                }
            ],
            output_config={
                "effort": "low",  # 提示済みテキストの分類。深く考える必要はない
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
        )
        text = next(b.text for b in response.content if b.type == "text")
        return TagResult.from_json(text)


class OllamaTagger:
    """ローカル LLM 版。分類に閉じているので小型モデルで足りる。"""

    def __init__(self, model: str = "qwen3:8b", host: str = "http://localhost:11434") -> None:
        self.model = model
        self.host = host.rstrip("/")

    def tag(self, *, name: str, existing_tags: list[str], source_text: str) -> TagResult:
        payload = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": build_prompt(
                        name=name, existing_tags=existing_tags, source_text=source_text
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read())
        return TagResult.from_json(body["message"]["content"])
