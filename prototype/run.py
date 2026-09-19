"""受信箱 → restaurants.md → build → push。

既定は dry-run。実際に書き込むには --apply、push まで行くには --apply --push。

アーカイブは push が成功したときだけ。店を特定できなかったメールは触らない。
「受信箱に残っている＝未処理」という状態を壊さないための順序。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import gmail_client
import maps_resolver
import restaurants_md
import tagger as tagger_module
from restaurants_md import Row

REPO = Path(__file__).resolve().parent.parent
QUERY = "in:inbox -from:no-reply@accounts.google.com"


def log(message: str) -> None:
    print(message, flush=True)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def build_place(session, mail: gmail_client.Mail) -> maps_resolver.Place | None:
    """メールから店を特定する。特定できなければ None（＝受信箱に残す）。"""
    maps_url = maps_resolver.pick_maps_url(mail.urls)
    if maps_url:
        log(f"  Google マップ: {maps_url}")
        place = session.place(maps_url)
        if place.name:
            return place
        log("  マップから店名を取れなかった")
        return None

    # マップ URL が無いメール（SNS のリンクだけ、紙面の店名を打っただけ）は、
    # 件名を店名とみなしてマップを検索する。1軒に決まらなければ None が返る。
    query = mail.subject.strip()
    if not query:
        return None
    log(f"  マップ URL が無いので件名で検索: {query}")
    place = session.search(query)
    if not place:
        log("  検索で1軒に絞れなかった")
    return place


def to_row(place: maps_resolver.Place, result, hp: str) -> Row:
    memo_parts = []
    if place.hours:
        memo_parts.append("。".join(place.hours))
    if place.phone:
        memo_parts.append(f"TEL {place.phone}")

    return Row(
        name=place.name,
        tags=result.tags,
        area=result.area or "未確認",
        address=place.address or "未確認",
        hp=f"[公式]({hp})" if hp else "-",
        menu=result.recommended_menu or "未確認",
        visits="0",      # 利用者が地図画面から更新する列。routine は 0 固定
        rating="-",      # 同上。- 固定
        memo="。".join(memo_parts),
    )


def patch_coords(data_path: Path, coords: dict[tuple[str, str], tuple[float, float]]) -> int:
    """Playwright が取った座標を data.json に書く。

    build.mjs のキャッシュキーは "店名:::住所" で、lat/lng が入っていれば
    次回以降もその値が使われる。ジオコーダに届かない環境でも座標が埋まる。
    """
    rows = json.loads(data_path.read_text(encoding="utf-8"))
    patched = 0
    for row in rows:
        key = (row["name"], row["address"])
        if row.get("lat") is None and key in coords:
            row["lat"], row["lng"] = coords[key]
            patched += 1
    if patched:
        data_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return patched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="restaurants.md に実際に書く")
    parser.add_argument("--push", action="store_true", help="commit して main へ push")
    parser.add_argument("--tagger", choices=["claude", "ollama"], default="claude")
    parser.add_argument("--credentials", default=str(Path.home() / ".restaurant_list/credentials.json"))
    parser.add_argument("--token", default=str(Path.home() / ".restaurant_list/token.json"))
    args = parser.parse_args()

    # --push は main へ押す。別のブランチにいると commit はそのブランチに乗る一方で
    # push は素通り（返り値0）になり、「成功した」と誤認してメールをアーカイブして
    # しまう。変更は main に届かないのに元メールだけ消えるので、先に止める。
    if args.push:
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "main":
            log(f"main ではなく {branch} にいる。--push は main でだけ使える。")
            log("  git checkout main してから実行してください。")
            return 1

    md_path = REPO / "restaurants.md"
    rows = restaurants_md.read_rows(md_path)
    tags = restaurants_md.existing_tags(rows)
    log(f"既存 {len(rows)} 件 / タグ {len(tags)} 種: {', '.join(tags)}\n")

    service = gmail_client.build_service(args.credentials, args.token)
    thread_ids = gmail_client.search_threads(service, QUERY)
    if not thread_ids:
        log("受信箱は空、対応不要")
        return 0

    log(f"受信箱に {len(thread_ids)} 件\n")
    tagger = tagger_for(args.tagger)

    added, skipped, archivable = [], [], []
    coords: dict[tuple[str, str], tuple[float, float]] = {}

    with maps_resolver.Session() as session:
        for thread_id in thread_ids:
            mail = gmail_client.fetch_mail(service, thread_id)
            log(f"[{mail.subject}]")

            place = build_place(session, mail)
            if not place or not place.name:
                log("  → 店を特定できない。受信箱に残す\n")
                skipped.append(mail.subject)
                continue

            existing = restaurants_md.find(rows, place.name)
            if existing:
                log(f"  → 既出（{existing.name}）。行は足さず、アーカイブ対象\n")
                archivable.append(thread_id)
                continue

            # 住所の取れない行は地図に置けない。セレクタが腐ったときに
            # 「未確認だらけの行が増えてメールは消える」のが一番まずいので、
            # 住所が無ければ何も書かずに受信箱へ残す。壊れれば溜まって気づける。
            if not place.address:
                log("  → 住所が取れなかった。受信箱に残す\n")
                skipped.append(mail.subject)
                continue

            source = mail.as_context()
            source += f"\n\nGoogle マップから取得:\n{json.dumps(place.to_dict(), ensure_ascii=False, indent=2)}"
            if place.website:
                site = session.text(place.website)
                if site:
                    log(f"  公式サイトを読んだ: {place.website}")
                    source += f"\n\n公式サイト（{place.website}）:\n{site}"

            result = tagger.tag(name=place.name, existing_tags=tags, source_text=source)
            if not result.confident:
                log("  → 材料が足りないと判断。受信箱に残す\n")
                skipped.append(mail.subject)
                continue

            # タグ規則はプロンプトの約束では守られない。コード側で確かめる
            result.tags, result.new_tags, problem = tagger_module.enforce_tag_policy(
                result.tags, tags)
            if problem:
                log(f"  → タグ規則に反する（{problem}）。受信箱に残す\n")
                skipped.append(mail.subject)
                continue

            row = to_row(place, result, place.website or "")
            log(f"  → {row.render()}")
            if result.new_tags:
                log(f"  新しいタグ: {', '.join(result.new_tags)}")
            if place.lat:
                coords[(row.name, row.address)] = (place.lat, place.lng)
                log(f"  座標: {place.lat}, {place.lng}")
            log("")

            added.append((thread_id, row))

    if not args.apply:
        log(f"\n--- dry-run --- 追記予定 {len(added)} 件 / 受信箱に残す {len(skipped)} 件")
        log("実際に書くには --apply を付ける")
        return 0

    for _, row in added:
        restaurants_md.append_row(md_path, row)
        tags = sorted(set(tags) | set(row.tags))

    if added:
        build = subprocess.run(["node", "build.mjs"], cwd=REPO, capture_output=True, text=True)
        log(build.stdout[-2000:] or build.stderr[-2000:])
        if build.returncode != 0:
            log("build.mjs が失敗した。push しない")
            return 1

        patched = patch_coords(REPO / "docs/data.json", coords)
        if patched:
            log(f"座標を {patched} 件書き込んだ")

    if not args.push:
        log("\n書き込み完了。push は --push を付けたときだけ")
        return 0

    git("add", "restaurants.md", "docs/data.json")
    names = "、".join(row.name for _, row in added)
    commit = git("commit", "-m", f"{names} を追加")
    if commit.returncode != 0:
        log(f"commit するものが無い: {commit.stdout}")
        return 0

    push = git("push", "-u", "origin", "main")
    if push.returncode != 0:
        log("push が弾かれた。pull --rebase して再試行する")
        if git("pull", "--rebase", "origin", "main").returncode != 0:
            log("rebase に失敗。アーカイブしない")
            return 1
        push = git("push", "-u", "origin", "main")
        if push.returncode != 0:
            log(f"push できなかった。アーカイブしない:\n{push.stderr}")
            return 1

    # ここまで来て初めてアーカイブする
    for thread_id, _ in added:
        gmail_client.archive(service, thread_id)
    for thread_id in archivable:
        gmail_client.archive(service, thread_id)

    log(f"\n追記 {len(added)} 件 / アーカイブ {len(added) + len(archivable)} 件 / 受信箱に残した {len(skipped)} 件")
    return 0


def tagger_for(kind: str):
    if kind == "ollama":
        from tagger import OllamaTagger

        return OllamaTagger()
    from tagger import ClaudeTagger

    return ClaudeTagger()


if __name__ == "__main__":
    sys.exit(main())
