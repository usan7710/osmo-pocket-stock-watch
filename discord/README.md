# Discordコマンドから在庫確認する

Discordの `/stock` コマンドでGitHub Actionsを手動実行し、現在の在庫状況をDiscordへ送るための設定です。

## できること

- Discordで `/stock` を実行
- Cloudflare WorkerがGitHub Actionsを起動
- `send_current_status=true` で `Stock Watch` が走る
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
