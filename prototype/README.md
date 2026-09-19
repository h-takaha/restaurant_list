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

## 検証状況

```bash
python test_extraction.py     # ネットワークに出ない。25 項目
```

通っているもの:

- Gmail の MIME 解析（multipart、text/plain 優先、HTML のみのメール、script 除去、href 抽出）
- 座標パース（`!3d!4d` を `@` より優先、短縮 URL は座標なし）
- restaurants.md の追記（行数 +1、既存行は1行も不変、`|` のエスケープ往復、来店回数 0 / 評価 -）
- Google マップ DOM からの抽出（店名・住所・公式サイト・電話・曜日ごとの営業時間）
- 情報が無いページで `None` を返す（＝`未確認`。捏造しない）

DOM の検証は `test_extraction.py` 内の固定 HTML に対するもの。**セレクタの書き方と
ブラウザの配線が正しいことまでは言えるが、Google の実物の DOM が fixture どおりかは
別問題。**

## 未検証・弱いところ

- **実物の Google マップに対しては未検証。** 開発環境の egress ポリシーで google.com に
  到達できなかった（curl も含め全て遮断）。本番投入前に必ず
  `python maps_resolver.py <実際の共有URL>` で突き合わせること。Google の DOM は予告なく
  変わる。取れなければ `None` → `未確認` になるので、壊れ方は安全側
- **Google のボット検出**に当たる可能性がある。`headless=False` で回避できることが多い
- **SNS のリンクだけ、紙面の店名だけ**といった変化球は、いまの routine は「賢さ」で吸収して
  いるが、このスクリプトは件名を店名とみなすだけ。外したら受信箱に残る
- **`tagger.py` の `confident` 判定**は未チューニング。実データで閾値を見る必要がある
- **OAuth の初回認可フローは未検証。** 実際の credentials.json が無いため
