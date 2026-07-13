const DISCORD_INTERACTION_PONG = 1;
const DISCORD_CHANNEL_MESSAGE_WITH_SOURCE = 4;
const DISCORD_EPHEMERAL = 64;

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

async function verifyDiscordRequest(request, body, env) {
  const signature = request.headers.get("x-signature-ed25519");
  const timestamp = request.headers.get("x-signature-timestamp");
  if (!signature || !timestamp || !env.DISCORD_PUBLIC_KEY) {
    return false;
  }

  const key = await crypto.subtle.importKey(
    "raw",
    hexToBytes(env.DISCORD_PUBLIC_KEY),
    { name: "Ed25519" },
    false,
    ["verify"],
  );
  const data = new TextEncoder().encode(timestamp + body);
  return crypto.subtle.verify(
    { name: "Ed25519" },
    key,
    hexToBytes(signature),
    data,
  );
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function dispatchStockWatch(env) {
  const githubToken = (env.GITHUB_TOKEN || "").trim().replace(/^Bearer\s+/i, "");
  if (!githubToken) {
    throw new Error("Cloudflare WorkerのGITHUB_TOKENが未設定です。");
  }

  const owner = env.GITHUB_OWNER || "usan7710";
  const repo = env.GITHUB_REPO || "osmo-pocket-stock-watch";
  const workflow = env.GITHUB_WORKFLOW || "stock-watch.yml";
  const ref = env.GITHUB_REF || "main";
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      authorization: `Bearer ${githubToken}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "osmo-pocket-stock-watch-discord-command",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      ref,
      inputs: {
        send_current_status: "true",
        check_skipped_urls: "true",
      },
    }),
  });

  if (!response.ok) {
    const text = await response.text();
    if (response.status === 401) {
      throw new Error(
        "GitHub tokenが無効です。Cloudflare WorkerのSecret GITHUB_TOKENを作り直して保存してください。",
      );
    }
    if (response.status === 403) {
      throw new Error(
        "GitHub tokenの権限不足です。Actions: Read and write が必要です。",
      );
    }
    throw new Error(`GitHub API ${response.status}: ${text}`);
  }
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK");
    }

    const body = await request.text();
    const verified = await verifyDiscordRequest(request, body, env);
    if (!verified) {
      return new Response("Bad request signature", { status: 401 });
    }

    const interaction = JSON.parse(body);
    if (interaction.type === 1) {
      return jsonResponse({ type: DISCORD_INTERACTION_PONG });
    }

    const commandName = interaction.data?.name;
    if (commandName !== "stock") {
      return jsonResponse({
        type: DISCORD_CHANNEL_MESSAGE_WITH_SOURCE,
        data: {
          content: "未対応のコマンドです。",
          flags: DISCORD_EPHEMERAL,
        },
      });
    }

    try {
      await dispatchStockWatch(env);
      return jsonResponse({
        type: DISCORD_CHANNEL_MESSAGE_WITH_SOURCE,
        data: {
          content:
            "全販売ページの在庫状況チェックを起動しました。取得できないページも理由付きで、数分以内にこのチャンネルへ結果が届きます。",
          flags: DISCORD_EPHEMERAL,
        },
      });
    } catch (error) {
      return jsonResponse({
        type: DISCORD_CHANNEL_MESSAGE_WITH_SOURCE,
        data: {
          content: `GitHub Actionsの起動に失敗しました: ${error.message}`,
          flags: DISCORD_EPHEMERAL,
        },
      });
    }
  },
};
