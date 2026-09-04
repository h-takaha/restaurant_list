# 行きたい食事処リスト

一覧は [restaurants.md](restaurants.md)。地図は <https://h-takaha.github.io/restaurant_list/>。上部のタブで地図とリストを切り替えられ、ジャンルのボタンで絞り込める。ジャンルは `restaurants.md` の記述そのままなので、新しいジャンルの店を足せばボタンも増える。

## 地図の更新

`restaurants.md` を編集して push すれば、GitHub Actions（[build-map-data.yml](.github/workflows/build-map-data.yml)）が `node build.mjs` を回して `docs/data.json` を作り直し、差分があれば commit してくれる。手元で build する必要はない。GitHub Pages は main ブランチの `/docs` を公開している。

座標は住所から国土地理院のジオコーダで引いている（APIキー不要）。位置がずれていたら `docs/data.json` の `lat` / `lng` を手で直せばよい。住所を書き換えない限り、その値が使われ続ける。

## 来店回数と評価を地図から書き換える

行った直後にスマホで記録できるよう、**来店回数と評価だけ**は地図の画面から直接更新できる。ピンかリストの「来店 +1」を押すと回数が増え、星をタップすると 0.5 刻みで評価が入る。他の列は編集できない。

書き込み先は `restaurants.md` そのもの（GitHub Contents API 経由の commit）で、そのあとは通常どおり GitHub Actions が `docs/data.json` を作り直す。地図に反映されるまで1分ほどかかる。

使うには GitHub のアクセストークンが要る。画面右上の ⚙ から登録する。

- [Fine-grained personal access token](https://github.com/settings/personal-access-tokens/new) を作る
- Repository access は **restaurant_list のみ**、権限は **Contents: Read and write** だけ
- トークンはブラウザの localStorage に入る。`h-takaha.github.io` には他のページも同居していて localStorage を共有するので、リポジトリを絞っていないトークンは使わない
- 未設定なら閲覧専用になる。⚙ の「削除」でいつでも消せる

画面はボタンを押した時点で先に書き換わり、commit に失敗した場合だけ元に戻してエラーを出す。

## 登録のしかた

見つけた瞬間は **restaurant.map23@gmail.com へメールを送るだけ**。住所・HP・ジャンルの穴埋めは、あとでまとめて Claude が行う。この専用アドレスに届くものは全部お店なので、件名にタグを付けるといった約束事は要らない。受信箱に残っている＝未取り込み。

### 送り方（入口別）

| 見つけた場所 | やること | 補足 |
|---|---|---|
| Google マップ | 共有 → Gmail → 送信 | 件名に店名が自動で入る。何も書き足さなくてよい |
| SNS（Instagram / X など） | 共有 → Gmail → 送信 | **件名に店名かエリアを一言書く**。URL だけだと店の特定に手間がかかる |
| 道新などの紙面 | **件名か本文に店名を打つ** | 写真の添付だけでは取り込めない（下記） |

### 取り込み

毎朝 7:23 に routine（クラウド上の Claude）が自動で走る。受信箱を読み、住所・HP・ジャンル・オススメメニューを調べて `restaurants.md` に追記し、`node build.mjs` を回して main へ push し、**取り込んだメールをアーカイブする**。受信箱に残っているものが未取り込み、という状態を保つ。

裏が取れなかった項目は `未確認` と入る。もっともらしい住所を埋めることはしない。店を特定できなかったメールはアーカイブされず受信箱に残る。

待てないときは Claude に「メール取り込んで」と言えば同じことを手動で行う。

**routine が main を進めるので、ローカルで作業する前に `git pull` する。** routine の設定は <https://claude.ai/code/routines>。

routine の実行環境からは国土地理院のジオコーダに届かないため、routine が追加した店は座標が `null` のまま push される。その直後に GitHub Actions が走って座標を埋めるので、放っておいてよい。

## 設計メモ

- 一覧は Markdown の表1つ。検索も編集もエディタと GitHub で完結する
- Google マップの共有 URL は、リダイレクト先を解決してもページが JavaScript 描画のため住所を取れない。実際に効いているのは**件名の店名**で、そこから Web 検索して公式サイトで裏を取っている
- Instagram はプロフィール文なら取得できたが、店名も所在地もプロフィール次第。件名に一言あるかどうかで精度が変わる
- **添付画像は Gmail コネクタから取り出せない。** ファイル名と MIME タイプは見えるが、画像本体を取得するツールが無い。紙面を撮って送っても中身は読めないので、店名は文字で送る。写真から読み取らせたい場合は、その画像をチャットに直接貼るか、ローカルに保存してパスを渡す
