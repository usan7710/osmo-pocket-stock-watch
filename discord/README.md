# Discordコマンドから在庫確認する

Discordの `/stock` コマンドでGitHub Actionsを手動実行し、現在の在庫状況をDiscordへ送るための設定です。

## できること

- Discordで `/stock` を実行
- Cloudflare WorkerがGitHub Actionsを起動
- `send_current_status=true` で `Stock Watch` が走る
- `check_skipped_urls=true` で一時スキップ中の販売ページも今回だけ確認
- 結果は既存の `DISCORD_WEBHOOK_URL` へ通知

## 必要なもの

- Discord Developer Portalで作るDiscord Application
- Cloudflare Workers
- GitHub Fine-grained personal access token

## 1. Discord Applicationを作る

1. Discord Developer Portalで `New Application`
2. `General Information` の `PUBLIC KEY` を控える
3. `Bot` でBotを作る
4. Bot tokenを控える

Bot tokenは `/stock` コマンド登録にだけ使います。Cloudflare Workerには保存しません。

## 2. GitHub tokenを作る

GitHubの fine-grained personal access token を作ります。

- Repository: `usan7710/osmo-pocket-stock-watch`
- Actions: Read and write
- Contents: Read-only

作成したtokenはCloudflare WorkerのSecretに `GITHUB_TOKEN` として登録します。

## 3. Cloudflare Workerを作る

`discord/cloudflare-worker.js` の内容をCloudflare Workerへ貼り付けます。

Workerの環境変数/Secret:

- `DISCORD_PUBLIC_KEY`: Discord ApplicationのPUBLIC KEY
- `GITHUB_TOKEN`: GitHub fine-grained personal access token
- `GITHUB_OWNER`: `usan7710`
- `GITHUB_REPO`: `osmo-pocket-stock-watch`
- `GITHUB_WORKFLOW`: `stock-watch.yml`
- `GITHUB_REF`: `main`

Workerをデプロイしたら、Worker URLを控えます。

## 4. DiscordにInteractions Endpoint URLを設定する

Discord Developer Portalで対象Applicationを開きます。

1. `General Information`
2. `Interactions Endpoint URL`
3. Cloudflare Worker URLを貼る
4. 保存

保存できれば、Discord側からの署名検証が通っています。

## 5. `/stock` コマンドを登録する

PowerShellなどで次を実行します。値は自分のものに置き換えてください。

```powershell
$applicationId = "ここにDiscord Application ID"
$botToken = "ここにDiscord Bot Token"

Invoke-RestMethod `
  -Method Put `
  -Uri "https://discord.com/api/v10/applications/$applicationId/commands" `
  -Headers @{ Authorization = "Bot $botToken"; "Content-Type" = "application/json" } `
  -Body '[{"name":"stock","description":"Osmo Pocket 4Pの現在在庫状況を確認します"}]'
```

少し待つと、Discordで `/stock` が使えるようになります。

## 注意

- Discord Webhookだけではコマンド受信はできません。
- Cloudflare WorkerのURLは公開URLですが、Discord署名を検証しているため、Discordからの正規リクエストだけ処理します。
- GitHub tokenは絶対にREADMEやチャットに貼らないでください。
- `/stock` では無効化中のURLも確認対象に含めます。`provider: manual` のサイトには通信せず、リンク付きの「🔗 手動確認」として表示します。それ以外は最大12秒・再試行なしで一度だけ確認します。
- Amazonは直接スクレイピングしません。GitHub Secretsに `KEEPA_API_KEY` がない場合は取得エラーとして表示します。

## エラー対処

### GitHub API 401: Bad credentials

`GITHUB_TOKEN` が無効です。Cloudflare WorkerのSecretに入れた値を確認してください。

- tokenだけを貼る
- `Bearer ` は付けない
- 余計な空白や引用符を入れない
- Discord Bot TokenではなくGitHub tokenを使う

### GitHub API 403: Resource not accessible by personal access token

tokenは認証されていますが、GitHub Actionsを起動する権限が足りません。

fine-grained personal access tokenの場合は、次を確認してください。

- Resource owner: `usan7710`
- Repository access: `Only select repositories`
- Selected repository: `usan7710/osmo-pocket-stock-watch`
- Repository permissions:
  - `Actions`: `Read and write`
  - `Contents`: `Read-only`

classic personal access tokenを使う場合は、`repo` scopeが必要です。権限を直したら、Cloudflare Workerの `GITHUB_TOKEN` Secretを新しいtokenで保存し直してください。
