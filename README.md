# DJI Osmo Pocket 4P 在庫監視

DJI Osmo Pocket 4P の在庫復活を定期確認し、前回「在庫なし」から今回「在庫あり」に変わった時だけDiscord Webhookへ通知するPythonアプリです。初回起動時は現在状態を記録するだけで通知しません。

## 1. セットアップ手順

Python 3.12 を用意し、このフォルダで依存関係をインストールします。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

`.env` に `DISCORD_WEBHOOK_URL` を設定し、`config.yaml` の商品URLを実際の販売ページに置き換えます。

```powershell
python main.py --config config.yaml --state state.json
```

## 2. Discord Webhookの作り方

Discordで通知したいチャンネルを開き、チャンネル設定から「連携サービス」または「Integrations」を開きます。Webhookを新規作成し、Webhook URLをコピーしてください。

## 3. GitHub Secretsへの登録方法

GitHubリポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret` で登録します。

- `DISCORD_WEBHOOK_URL`: Discord Webhook URL
- `KEEPA_API_KEY`: AmazonをKeepaで確認する場合のみ任意

Actionsが `state.json` を保存できるように、リポジトリの `Settings` → `Actions` → `General` → `Workflow permissions` は `Read and write permissions` にしてください。

## 4. config.yamlの編集方法

`config.example.yaml` を `config.yaml` にコピーして編集します。商品ごとに `priority` を設定し、Vlogコンボを `1`、スタンダード/単品を `2` にしておくと通知文の優先度が反映されます。

```yaml
products:
  - id: osmo_pocket_4p_vlog
    name: "Osmo Pocket 4P Vlogコンボ"
    priority: 1
    urls:
      - site: "DJI公式"
        url: "https://example.com/product"
        provider: "requests"
        in_stock_keywords:
          - "カートに入れる"
          - "購入する"
          - "在庫あり"
        out_of_stock_keywords:
          - "在庫切れ"
          - "入荷待ち"
          - "売り切れ"
          - "在庫なし"
```

`conflict_policy` は、在庫あり/なし両方の文言が見つかった時の扱いです。誤通知を避けるため、初期値は `out_of_stock_wins` です。

## 5. 商品URLの追加方法

`urls` の下に監視対象を追加します。ヨドバシ、エディオン、ヤマダデンキなども同じ形で追加できます。

```yaml
- site: "エディオン"
  url: "https://example.com/item"
  provider: "requests"
  in_stock_keywords:
    - "カートに入れる"
    - "在庫あり"
  out_of_stock_keywords:
    - "在庫なし"
    - "売り切れ"
```

JavaScript描画が必要なページだけ `provider: "playwright"` にできます。その場合は追加で `playwright` のインストールとブラウザセットアップが必要です。ログイン、CAPTCHA、強いBot対策が出るページは監視対象から外してください。

AmazonはHTML直接監視に依存しすぎないため、サンプルでは `provider: "keepa"` を分けています。使う場合は `enabled: true`、`asin`、`KEEPA_API_KEY` を設定してください。

## 6. 手動実行方法

ローカルでは次のコマンドで実行できます。

```powershell
python main.py --config config.yaml --state state.json
```

GitHub Actionsでは、リポジトリの `Actions` → `Stock Watch` → `Run workflow` から手動実行できます。

Discordで現在の在庫状況を確認したい場合は、`Run workflow` を押す時に `send_current_status` にチェックを入れて実行します。この場合は、在庫復活の有無に関係なく、現在の判定結果サマリーがDiscordへ送られます。

## 7. GitHub Actionsでの定期実行方法

`.github/workflows/stock-watch.yml` は以下に対応しています。

- `workflow_dispatch`: 手動実行
- `send_current_status`: 手動実行時だけ現在状況をDiscordへ送信
- `schedule`: 10分おきの定期実行
- `DISCORD_WEBHOOK_URL`: GitHub Secretsから読み込み
- 実行ログ: 各商品の判定結果を表示
- `state.json`: 通知済み状態を保存し、同じ在庫あり状態で連続通知しない

`state.json` が存在しない初回実行では、現在の状態を基準として保存するだけで通知しません。状態をリセットしたい場合は `state.json` を削除して再実行してください。

## 8. 注意事項

- 在庫判定は100%保証ではありません。
- 販売ページの表示文言が変わると、判定に失敗することがあります。
- AmazonはKeepaやAmazon側の公式通知サービスの利用も検討してください。
- CAPTCHA、ログイン、過度なBot対策が出るページは監視対象から外してください。
- 5〜10分おき程度の常識的な間隔で実行し、短時間に大量アクセスしないでください。
- このアプリは購入処理やログイン操作を行いません。在庫復活の可能性を通知するだけです。
