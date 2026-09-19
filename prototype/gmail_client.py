"""Gmail の読み取りとアーカイブ。

scope は gmail.modify のみ。これは読み取りとラベルの付け外しは許すが、完全削除
（https://mail.google.com/ が必要）と送信は許さない。routine の「削除・ゴミ箱移動・
送信をしない」という制約を、プロンプトの約束ではなくトークンの権限で担保する。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")


@dataclass
class Mail:
    thread_id: str
    message_id: str
    subject: str
    sender: str
    body: str
    urls: list[str] = field(default_factory=list)

    def as_context(self) -> str:
        """LLM に渡す素材。ここに無いことは書かせない。"""
        lines = [f"件名: {self.subject}", f"差出人: {self.sender}", "", self.body]
        if self.urls:
            lines += ["", "URL:"] + [f"- {u}" for u in self.urls]
        return "\n".join(lines)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.hrefs.append(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def build_service(credentials_path: str | Path, token_path: str | Path):
    """OAuth を通して Gmail API のクライアントを返す。

    credentials_path は Google Cloud で発行した「デスクトップアプリ」型の
    OAuth クライアント。redirect が http://localhost になるので、claude.ai の
    コネクタと違ってローカルで認可を完結できる。

    初回だけブラウザが開く。以降は token_path の refresh token で無人実行できる。
    """
    token_path = Path(token_path)
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def search_threads(service, query: str) -> list[str]:
    result = service.users().threads().list(userId="me", q=query).execute()
    return [t["id"] for t in result.get("threads", [])]


def _walk(payload: dict):
    yield payload
    for part in payload.get("parts") or []:
        yield from _walk(part)


def _decode(part: dict) -> str | None:
    data = (part.get("body") or {}).get("data")
    if not data:
        return None
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def fetch_mail(service, thread_id: str) -> Mail:
    """スレッドの最初のメッセージを読む。この用途では1通スレッドしか来ない。"""
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    message = thread["messages"][0]
    payload = message["payload"]

    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    plain, html = None, None
    for part in _walk(payload):
        mime = part.get("mimeType", "")
        if mime == "text/plain" and plain is None:
            plain = _decode(part)
        elif mime == "text/html" and html is None:
            html = _decode(part)

    hrefs: list[str] = []
    if plain:
        body = plain.strip()
    elif html:
        parser = _TextExtractor()
        parser.feed(html)
        body = parser.text()
        hrefs = parser.hrefs
    else:
        body = ""

    urls = URL_RE.findall(f"{headers.get('subject', '')}\n{body}") + hrefs
    seen, deduped = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)

    return Mail(
        thread_id=thread_id,
        message_id=message["id"],
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        body=body,
        urls=deduped,
    )


def archive(service, thread_id: str) -> None:
    """INBOX と UNREAD を外す。メッセージ自体には触れない。"""
    service.users().threads().modify(
        userId="me",
        id=thread_id,
        body={"removeLabelIds": ["INBOX", "UNREAD"]},
    ).execute()
