# ローカル取り込みのプロトタイプ

クラウドの routine と同じことを PC 上で完結して行う実験。まだ本番運用には入れていない。

## なぜこの形か

routine の工程を分解すると、判断が要るのは一部だけだった。

| 工程 | 手段 |
|---|---|
| 受信箱を読む / アーカイブ | `gmail_client.py` — LLM 不要 |
| Google マップ URL から店・住所・座標を取る | `maps_resolver.py`（Playwright） |
| タグとオススメメニューを決める | `tagger.py` — **ここだけ LLM** |
| restaurants.md に追記 | `restaurants_md.py` — LLM 不要 |
| build / commit / push | `run.py` — LLM 不要 |

要点が3つある。

**1. Gmail はローカルで認証できる。** claude.ai の Gmail コネクタが使えないのは、OAuth の
redirect URL が claude.ai 固定だから。自分で「デスクトップアプリ」型の OAuth クライアントを
発行すれば redirect は `http://localhost` になり、普通に通る。初回だけブラウザで認可して、
あとは refresh token で無人実行できる。

scope は `gmail.modify` だけ。読み取りとラベルの付け外しは許すが、完全削除と送信は
許さない。「削除・送信をしない」という制約をトークンの権限で担保している。

**2. Playwright が README の詰まりを解く。** 本体の README 設計メモにある「Google マップの
共有 URL は JavaScript 描画のため住所を取れない」は、ヘッドレスブラウザなら解決する。
しかも place ページの URL には実際の座標が `!3d<lat>!4d<lng>` の形で入っているので、
**国土地理院のジオコーダに頼らず座標が手に入る。**

**3. LLM に知識を思い出させない。** 住所を「調べさせる」と小型モデルは平然と捏造する。
`tagger.py` は取得済みテキストを渡して、既存タグ一覧からの選択だけをさせる。分類に閉じて
いるので、ローカルの 8B クラスでも成立する。`ClaudeTagger` と `OllamaTagger` は
入出力が同じで差し替えられる。

## セットアップ

### 1. Gmail の OAuth クライアント

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作る
2. 「API とサービス」→ Gmail API を有効にする
3. 「OAuth 同意画面」を作る。ユーザーの種類は**外部**、テストユーザーに
   `restaurant.map23@gmail.com` を追加する（公開申請は不要）
4. 「認証情報」→ OAuth クライアント ID → 種類は**デスクトップ アプリ**
5. JSON をダウンロードして `~/.restaurant_list/credentials.json` に置く

### 2. 依存

```bash
pip install -r requirements.txt
playwright install chromium
```

`ClaudeTagger` を使うなら `ANTHROPIC_API_KEY` を設定する。`OllamaTagger` なら
`ollama serve` が動いていればよい。

### 3. 動かす

```bash
python run.py                      # dry-run。何も書かない
python run.py --apply              # restaurants.md に書いて build まで
python run.py --apply --push       # push してアーカイブまで
python run.py --tagger ollama      # ローカル LLM を使う
```

初回の `python run.py` でブラウザが開いて認可を求める。以降は開かない。

部品を単体で試すこともできる。

```bash
python maps_resolver.py "https://maps.app.goo.gl/..."
```

### 4. 定期実行

Claude Code に OS レベルのスケジューラは無いので、cron / launchd / タスクスケジューラから
叩く。

```cron
23 7 * * * cd /path/to/restaurant_list/prototype && /usr/bin/python3 run.py --apply --push >> ~/restaurant_list.log 2>&1
```

PC が 7:23 に起きている必要がある。ノート PC だと取りこぼす。

## 安全側の設計

- **dry-run が既定。** `--apply` と `--push` は明示的に付ける
- **アーカイブは push が成功したときだけ。** 途中で失敗したらメールは受信箱に残り、次回また拾われる
- **追記は行の挿入だけ。** 既存行は読みも書きもしない。来店回数と評価は利用者の記録なので、
  そもそも触りようがない構造にしてある
- **裏が取れなければ `未確認`。** 推測で埋めない
- **特定できないメールは触らない。**「受信箱に残っている＝未処理」を保つ

## 未検証・弱いところ

- **Playwright の Google マップ抽出は未検証。** この開発環境から Google マップに到達できず、
  セレクタを実データで確認できていない。Google の DOM は予告なく変わるので、
  最初に `python maps_resolver.py <URL>` で必ず確認すること。取れなければ `None` が返り、
  `未確認` として扱われる（捏造はしない）
- **Google のボット検出**に当たる可能性がある。`headless=False` で回避できることが多い
- **SNS のリンクだけ、紙面の店名だけ**といった変化球は、いまの routine は「賢さ」で吸収して
  いるが、このスクリプトは件名を店名とみなすだけ。外したら受信箱に残る
- **`tagger.py` の `confident` 判定**は未チューニング。実データで閾値を見る必要がある
