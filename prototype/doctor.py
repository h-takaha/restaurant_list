"""動かす前の点検。足りないものと、その直し方を出す。

    python doctor.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home() / ".restaurant_list"
REPO = Path(__file__).resolve().parent.parent

problems: list[str] = []


def report(ok: bool, label: str, detail: str = "", fix: str = "") -> None:
    print(f"  {'OK  ' if ok else 'NG  '}{label}" + (f": {detail}" if detail else ""))
    if not ok and fix:
        problems.append(f"{label}\n      → {fix}")


def check_python() -> None:
    v = sys.version_info
    report(v >= (3, 9), "Python 3.9 以上", f"{v.major}.{v.minor}.{v.micro}",
           "新しい Python を入れる")


def check_packages() -> None:
    for module, package, why in [
        ("playwright", "playwright", "マップの解決に必須"),
        ("googleapiclient", "google-api-python-client", "Gmail に必須"),
        ("google_auth_oauthlib", "google-auth-oauthlib", "Gmail の認証に必須"),
        ("anthropic", "anthropic", "ClaudeTagger を使う場合のみ"),
    ]:
        found = importlib.util.find_spec(module) is not None
        report(found, package, why if found else "未インストール",
               f"pip install {package}")


def check_chromium() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report(False, "Chromium", "playwright が無いので確認できない",
               "pip install playwright && playwright install chromium")
        return
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None)
            version = browser.version
            browser.close()
        report(True, "Chromium", version)
    except Exception as exc:
        report(False, "Chromium", str(exc).split("\n")[0][:70],
               "playwright install chromium")


def check_node() -> None:
    node = shutil.which("node")
    if not node:
        report(False, "node", "見つからない", "Node.js v18 以上を入れる")
        return
    version = subprocess.run([node, "-v"], capture_output=True, text=True).stdout.strip()
    major = int(version.lstrip("v").split(".")[0]) if version else 0
    report(major >= 18, "node", version, "Node.js v18 以上に上げる")


def check_credentials() -> None:
    creds = HOME / "credentials.json"
    report(creds.exists(), "Gmail の credentials.json", str(creds),
           "Google Cloud Console でデスクトップアプリ型の OAuth クライアントを作り、"
           f"JSON を {creds} に置く（README の手順1）")

    token = HOME / "token.json"
    if not token.exists():
        print("  --  token.json: まだ無い（初回の run.py で作られる）")
    elif os.name == "nt":
        # Windows の権限は ACL 側。chmod のビットを見ても意味が無い
        print(f"  --  token.json: あり（{token}）")
    else:
        mode = oct(token.stat().st_mode)[-3:]
        report(mode == "600", "token.json のパーミッション", mode, f"chmod 600 {token}")


def check_tagger() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        report(True, "ClaudeTagger", "ANTHROPIC_API_KEY あり")
        return
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3):
            report(True, "OllamaTagger", "localhost:11434 が応答")
            return
    except Exception:
        pass
    report(False, "タガー", "API キーも Ollama も無い",
           "export ANTHROPIC_API_KEY=... するか、ollama serve を起動する")


def check_repo() -> None:
    md = REPO / "restaurants.md"
    report(md.exists(), "restaurants.md", str(md), "リポジトリの直下で実行する")

    status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                            capture_output=True, text=True)
    dirty = [l for l in status.stdout.splitlines() if "prototype/" not in l]
    if dirty:
        print(f"  --  未コミットの変更が {len(dirty)} 件ある（push 時に巻き込まれる）")

    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    print(f"  --  いまのブランチ: {branch}")
    if branch != "main":
        print("      run.py --push は main に push する。ブランチを確認すること")


if __name__ == "__main__":
    print("\n[実行環境]")
    check_python()
    check_packages()
    check_chromium()
    check_node()

    print("\n[認証]")
    check_credentials()
    check_tagger()

    print("\n[リポジトリ]")
    check_repo()

    print()
    if problems:
        print(f"{'=' * 60}\n足りないもの {len(problems)} 件:\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        sys.exit(1)
    print("=" * 60)
    print("準備できています。まず `python run.py`（dry-run）から。")
