from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    load_dotenv = None


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; stock-watch/1.0; "
    "+https://github.com/your-name/stock-watch)"
)
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(slots=True)
class CheckResult:
    key: str
    product_id: str
    product_name: str
    priority: int
    site: str
    url: str
    provider: str
    status: str
    in_stock: bool | None
    reason: str
    checked_at: datetime
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor product stock and notify Discord on restock transitions."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--state", default="state.json", help="Path to state.json")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} が見つかりません。config.example.yaml を config.yaml にコピーして編集してください。"
        )
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError("config.yaml の形式が不正です。トップレベルはマッピングにしてください。")
    return loaded


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "targets": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
    except json.JSONDecodeError as exc:
        logging.warning("state.json を読み込めませんでした。新規状態として扱います: %s", exc)
        return {"version": 1, "targets": {}}

    if not isinstance(loaded, dict):
        return {"version": 1, "targets": {}}
    loaded.setdefault("version", 1)
    loaded.setdefault("targets", {})
    return loaded


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def get_timezone(config: dict[str, Any]) -> ZoneInfo:
    tz_name = str(config.get("settings", {}).get("timezone", "Asia/Tokyo"))
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logging.warning("timezone=%s が不正なため Asia/Tokyo を使います。", tz_name)
        return ZoneInfo("Asia/Tokyo")


def display_time(dt: datetime, config: dict[str, Any]) -> str:
    local = dt.astimezone(get_timezone(config))
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def normalize_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def keyword_matches(text: str, keywords: list[str] | None) -> list[str]:
    if not keywords:
        return []
    folded = text.casefold()
    return [word for word in keywords if word and str(word).casefold() in folded]


def decide_stock(
    text: str,
    in_stock_keywords: list[str] | None,
    out_of_stock_keywords: list[str] | None,
    conflict_policy: str,
) -> tuple[str, bool | None, str]:
    in_matches = keyword_matches(text, in_stock_keywords)
    out_matches = keyword_matches(text, out_of_stock_keywords)

    if in_matches and out_matches:
        if conflict_policy == "in_stock_wins":
            return (
                "in_stock",
                True,
                "在庫あり/なしの両方に一致。在庫あり優先: " + ", ".join(in_matches),
            )
        if conflict_policy == "unknown":
            return (
                "unknown",
                None,
                "在庫あり/なしの両方に一致したため判定保留: "
                + "あり="
                + ", ".join(in_matches)
                + " / なし="
                + ", ".join(out_matches),
            )
        return (
            "out_of_stock",
            False,
            "在庫なしキーワードにも一致したため保守的に在庫なし: "
            + ", ".join(out_matches),
        )

    if in_matches:
        return "in_stock", True, "在庫ありキーワードに一致: " + ", ".join(in_matches)
    if out_matches:
        return "out_of_stock", False, "在庫なしキーワードに一致: " + ", ".join(out_matches)
    return "unknown", None, "設定された在庫キーワードに一致しませんでした。"


def target_key(product_id: str, target: dict[str, Any]) -> str:
    explicit = target.get("id") or target.get("key")
    if explicit:
        return f"{product_id}:{explicit}"
    raw = f"{product_id}|{target.get('site', '')}|{target.get('url', '')}|{target.get('asin', '')}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{product_id}:{digest}"


def product_priority(product: dict[str, Any]) -> int:
    try:
        return int(product.get("priority", 999))
    except (TypeError, ValueError):
        return 999


def request_settings(config: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config.get("settings", {}).get("request", {}) or {})
    settings.update(target.get("request", {}) or {})
    return settings


def build_headers(config: dict[str, Any], target: dict[str, Any]) -> dict[str, str]:
    settings = request_settings(config, target)
    user_agent = (
        target.get("user_agent")
        or os.getenv("STOCK_WATCH_USER_AGENT")
        or settings.get("user_agent")
        or DEFAULT_USER_AGENT
    )
    headers = {
        "User-Agent": str(user_agent),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    }
    headers.update({str(k): str(v) for k, v in (target.get("headers") or {}).items()})
    return headers


def fetch_url(
    session: requests.Session,
    url: str,
    config: dict[str, Any],
    target: dict[str, Any],
) -> str:
    settings = request_settings(config, target)
    timeout = float(settings.get("timeout_seconds", 20))
    retries = int(settings.get("retries", 2))
    backoff = float(settings.get("backoff_seconds", 3))
    headers = build_headers(config, target)
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
            if response.status_code in TRANSIENT_STATUS_CODES:
                raise requests.RequestException(
                    f"一時的なHTTPエラー: {response.status_code}"
                )
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                break
            sleep_seconds = backoff * (2**attempt) + random.uniform(0, 0.5)
            logging.info(
                "一時エラーのため %.1f 秒後に再試行します: %s",
                sleep_seconds,
                exc,
            )
            time.sleep(sleep_seconds)

    raise RuntimeError(str(last_error) if last_error else "HTTP取得に失敗しました。")


def check_with_requests(
    session: requests.Session,
    product: dict[str, Any],
    target: dict[str, Any],
    config: dict[str, Any],
    checked_at: datetime,
) -> CheckResult:
    product_id = str(product.get("id", "unknown_product"))
    site = str(target.get("site", "unknown_site"))
    url = str(target.get("url", "")).strip()
    key = target_key(product_id, target)

    if not url.startswith(("http://", "https://")):
        return build_result(
            product,
            target,
            checked_at,
            status="skipped",
            in_stock=None,
            reason="URLが未設定または http(s) ではないためスキップしました。",
            key=key,
        )

    html = fetch_url(session, url, config, target)
    text = normalize_text(html)
    conflict_policy = str(
        target.get(
            "conflict_policy",
            config.get("settings", {}).get("conflict_policy", "out_of_stock_wins"),
        )
    )
    status, in_stock, reason = decide_stock(
        text,
        target.get("in_stock_keywords"),
        target.get("out_of_stock_keywords"),
        conflict_policy,
    )
    return build_result(
        product,
        target,
        checked_at,
        status=status,
        in_stock=in_stock,
        reason=reason,
        key=key,
    )


def check_with_playwright(
    product: dict[str, Any],
    target: dict[str, Any],
    config: dict[str, Any],
    checked_at: datetime,
) -> CheckResult:
    product_id = str(product.get("id", "unknown_product"))
    key = target_key(product_id, target)
    url = str(target.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return build_result(
            product,
            target,
            checked_at,
            status="skipped",
            in_stock=None,
            reason="URLが未設定または http(s) ではないためスキップしました。",
            key=key,
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return build_result(
            product,
            target,
            checked_at,
            status="error",
            in_stock=None,
            reason="Playwrightがインストールされていません。",
            key=key,
            error="pip install playwright && playwright install chromium が必要です。",
        )

    settings = request_settings(config, target)
    timeout_ms = int(float(settings.get("timeout_seconds", 20)) * 1000)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=build_headers(config, target)["User-Agent"])
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        html = page.content()
        browser.close()

    text = normalize_text(html)
    conflict_policy = str(
        target.get(
            "conflict_policy",
            config.get("settings", {}).get("conflict_policy", "out_of_stock_wins"),
        )
    )
    status, in_stock, reason = decide_stock(
        text,
        target.get("in_stock_keywords"),
        target.get("out_of_stock_keywords"),
        conflict_policy,
    )
    return build_result(
        product,
        target,
        checked_at,
        status=status,
        in_stock=in_stock,
        reason=reason,
        key=key,
    )


def check_with_keepa(
    session: requests.Session,
    product: dict[str, Any],
    target: dict[str, Any],
    config: dict[str, Any],
    checked_at: datetime,
) -> CheckResult:
    product_id = str(product.get("id", "unknown_product"))
    key = target_key(product_id, target)
    asin = str(target.get("asin", "")).strip()
    api_key_env = str(target.get("api_key_env", "KEEPA_API_KEY"))
    api_key = os.getenv(api_key_env, "").strip()
    marketplace_url = str(target.get("url") or f"https://www.amazon.co.jp/dp/{asin}")

    if not asin or "ここに" in asin:
        return build_result(
            product,
            target,
            checked_at,
            status="skipped",
            in_stock=None,
            reason="Keepa用ASINが未設定のためスキップしました。",
            key=key,
            url=marketplace_url,
        )
    if not api_key:
        return build_result(
            product,
            target,
            checked_at,
            status="error",
            in_stock=None,
            reason=f"{api_key_env} が未設定のためKeepa確認をスキップしました。",
            key=key,
            url=marketplace_url,
        )

    domain = int(target.get("keepa_domain", 5))  # 5 = Amazon.co.jp
    params = {"key": api_key, "domain": domain, "asin": asin, "stats": 1}
    response = session.get(
        "https://api.keepa.com/product",
        params=params,
        timeout=float(request_settings(config, target).get("timeout_seconds", 20)),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))

    products = payload.get("products") or []
    if not products:
        return build_result(
            product,
            target,
            checked_at,
            status="unknown",
            in_stock=None,
            reason="Keepa APIで商品情報が見つかりませんでした。",
            key=key,
            url=marketplace_url,
        )

    current = (products[0].get("stats") or {}).get("current") or []
    indexes = target.get("keepa_price_indexes", [0, 1])
    positive_indexes = [
        int(index)
        for index in indexes
        if isinstance(index, int)
        and index < len(current)
        and isinstance(current[index], (int, float))
        and current[index] > 0
    ]
    if positive_indexes:
        return build_result(
            product,
            target,
            checked_at,
            status="in_stock",
            in_stock=True,
            reason="Keepa current価格が存在: index " + ", ".join(map(str, positive_indexes)),
            key=key,
            url=marketplace_url,
        )
    return build_result(
        product,
        target,
        checked_at,
        status="out_of_stock",
        in_stock=False,
        reason="Keepa current価格が見つかりませんでした。",
        key=key,
        url=marketplace_url,
    )


def build_result(
    product: dict[str, Any],
    target: dict[str, Any],
    checked_at: datetime,
    *,
    status: str,
    in_stock: bool | None,
    reason: str,
    key: str,
    error: str | None = None,
    url: str | None = None,
) -> CheckResult:
    return CheckResult(
        key=key,
        product_id=str(product.get("id", "unknown_product")),
        product_name=str(product.get("name", "Unknown product")),
        priority=product_priority(product),
        site=str(target.get("site", "unknown_site")),
        url=str(url if url is not None else target.get("url", "")),
        provider=str(target.get("provider") or target.get("method") or "requests"),
        status=status,
        in_stock=in_stock,
        reason=reason,
        checked_at=checked_at,
        error=error,
    )


def check_target(
    session: requests.Session,
    product: dict[str, Any],
    target: dict[str, Any],
    config: dict[str, Any],
    checked_at: datetime,
) -> CheckResult:
    if target.get("enabled", True) is False:
        return build_result(
            product,
            target,
            checked_at,
            status="skipped",
            in_stock=None,
            reason="enabled: false のためスキップしました。",
            key=target_key(str(product.get("id", "unknown_product")), target),
        )

    provider = str(target.get("provider") or target.get("method") or "requests").lower()
    try:
        if provider in {"requests", "html", "beautifulsoup"}:
            return check_with_requests(session, product, target, config, checked_at)
        if provider == "playwright":
            return check_with_playwright(product, target, config, checked_at)
        if provider == "keepa":
            return check_with_keepa(session, product, target, config, checked_at)
        return build_result(
            product,
            target,
            checked_at,
            status="skipped",
            in_stock=None,
            reason=f"未対応のproviderです: {provider}",
            key=target_key(str(product.get("id", "unknown_product")), target),
        )
    except Exception as exc:
        logging.exception(
            "%s / %s の確認中にエラーが発生しました。",
            product.get("name", "Unknown product"),
            target.get("site", "unknown_site"),
        )
        return build_result(
            product,
            target,
            checked_at,
            status="error",
            in_stock=None,
            reason="確認中にエラーが発生しました。",
            key=target_key(str(product.get("id", "unknown_product")), target),
            error=str(exc),
        )


def run_checks(config: dict[str, Any], checked_at: datetime) -> list[CheckResult]:
    products = config.get("products") or []
    if not isinstance(products, list):
        raise ValueError("products は配列で指定してください。")

    session = requests.Session()
    results: list[CheckResult] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        targets = product.get("urls") or []
        if not isinstance(targets, list):
            logging.warning("%s の urls が配列ではありません。", product.get("name"))
            continue
        for target in targets:
            if not isinstance(target, dict):
                continue
            result = check_target(session, product, target, config, checked_at)
            results.append(result)
            stock_text = (
                "在庫あり"
                if result.in_stock is True
                else "在庫なし"
                if result.in_stock is False
                else "判定保留"
            )
            logging.info(
                "[%s] %s / %s: %s (%s)",
                result.status,
                result.product_name,
                result.site,
                stock_text,
                result.reason,
            )
    return results


def find_notification_events(
    results: list[CheckResult], state: dict[str, Any]
) -> list[CheckResult]:
    targets = state.get("targets", {})
    events: list[CheckResult] = []
    for result in results:
        if result.in_stock is not True:
            continue
        previous = targets.get(result.key)
        if previous is None:
            continue
        was_out_of_stock = previous.get("in_stock") is False
        pending_retry = (
            previous.get("in_stock") is True
            and previous.get("notified_in_stock") is False
        )
        if was_out_of_stock or pending_retry:
            events.append(result)
    return sorted(events, key=lambda item: (item.priority, item.product_name, item.site))


def build_discord_message(
    event: CheckResult,
    results: list[CheckResult],
    config: dict[str, Any],
) -> str:
    vlog_available_other = [
        item
        for item in results
        if item.in_stock is True and item.priority == 1 and item.key != event.key
    ]
    lower_available = [
        item
        for item in results
        if item.in_stock is True and item.priority > event.priority
    ]

    if event.priority == 1:
        lines = [
            f"🚨【最優先】{event.product_name}が在庫復活した可能性があります！",
        ]
        if lower_available:
            lines.append(
                "代替候補も在庫ありの可能性がありますが、まずVlogコンボを確認してください。"
            )
    else:
        lines = [
            f"⚠️【代替候補】{event.product_name}が在庫復活した可能性があります。",
            "Vlogコンボではありませんが、購入候補として確認してください。",
        ]
        if vlog_available_other:
            top = vlog_available_other[0]
            lines.insert(
                0,
                "🚨 Vlogコンボも在庫ありの可能性があります。最優先でVlogコンボを確認してください。",
            )
            lines.insert(1, f"最優先候補：{top.site} {top.url}")

    lines.extend(
        [
            f"販売サイト：{event.site}",
            f"商品ページ：{event.url}",
            f"判定理由：{event.reason}",
            f"確認時刻：{display_time(event.checked_at, config)}",
        ]
    )
    return "\n".join(lines)


def post_discord(webhook_url: str, content: str) -> bool:
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    for attempt in range(3):
        try:
            response = requests.post(webhook_url, json=payload, timeout=15)
            if response.status_code in {200, 204}:
                return True
            if response.status_code == 429 or response.status_code >= 500:
                sleep_seconds = 2**attempt
                logging.warning(
                    "Discord通知が一時失敗しました。%s 秒後に再試行します: HTTP %s",
                    sleep_seconds,
                    response.status_code,
                )
                time.sleep(sleep_seconds)
                continue
            logging.error("Discord通知に失敗しました: HTTP %s %s", response.status_code, response.text)
            return False
        except requests.RequestException as exc:
            sleep_seconds = 2**attempt
            logging.warning("Discord通知エラー。%s 秒後に再試行します: %s", sleep_seconds, exc)
            time.sleep(sleep_seconds)
    return False


def send_notifications(
    events: list[CheckResult],
    results: list[CheckResult],
    config: dict[str, Any],
) -> dict[str, bool]:
    if not events:
        logging.info("通知対象の在庫復活はありません。")
        return {}

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logging.error("DISCORD_WEBHOOK_URL が未設定のため通知できません。")
        return {event.key: False for event in events}

    notification_results: dict[str, bool] = {}
    for event in events:
        message = build_discord_message(event, results, config)
        success = post_discord(webhook_url, message)
        notification_results[event.key] = success
        if success:
            logging.info("Discordへ通知しました: %s / %s", event.product_name, event.site)
        else:
            logging.error("Discord通知に失敗しました: %s / %s", event.product_name, event.site)
    return notification_results


def initial_entry(result: CheckResult, checked_at: datetime) -> dict[str, Any]:
    notified = True if result.in_stock is True else False
    return {
        "product_id": result.product_id,
        "product_name": result.product_name,
        "priority": result.priority,
        "site": result.site,
        "url": result.url,
        "provider": result.provider,
        "status": result.status,
        "reason": result.reason,
        "error": result.error,
        "in_stock": result.in_stock,
        "notified_in_stock": notified,
        "first_seen_at": iso(checked_at),
        "last_changed_at": iso(checked_at),
        "last_notified_at": None,
    }


def apply_results_to_state(
    state: dict[str, Any],
    results: list[CheckResult],
    events: list[CheckResult],
    notification_results: dict[str, bool],
    checked_at: datetime,
) -> bool:
    targets = state.setdefault("targets", {})
    changed = False
    event_keys = {event.key for event in events}

    for result in results:
        old = targets.get(result.key)
        if old is None:
            targets[result.key] = initial_entry(result, checked_at)
            changed = True
            continue

        entry = dict(old)
        for field, value in {
            "product_id": result.product_id,
            "product_name": result.product_name,
            "priority": result.priority,
            "site": result.site,
            "url": result.url,
            "provider": result.provider,
        }.items():
            entry[field] = value

        if result.in_stock is True:
            if old.get("in_stock") is not True:
                entry["in_stock"] = True
                entry["status"] = result.status
                entry["reason"] = result.reason
                entry["error"] = result.error
                entry["last_changed_at"] = iso(checked_at)
            if old.get("in_stock") is None:
                entry["notified_in_stock"] = True
            elif result.key in event_keys:
                notification_success = notification_results.get(result.key, False)
                entry["notified_in_stock"] = notification_success
                if notification_success:
                    entry["last_notified_at"] = iso(checked_at)
            else:
                entry["notified_in_stock"] = old.get("notified_in_stock", True)
        elif result.in_stock is False:
            if old.get("in_stock") is not False:
                entry["in_stock"] = False
                entry["status"] = result.status
                entry["reason"] = result.reason
                entry["error"] = result.error
                entry["last_changed_at"] = iso(checked_at)
            entry["notified_in_stock"] = False
        elif old.get("in_stock") is None:
            entry["status"] = result.status
            entry["reason"] = result.reason
            entry["error"] = result.error

        if entry != old:
            targets[result.key] = entry
            changed = True

    if changed:
        state["version"] = 1
        state["updated_at"] = iso(checked_at)
    return changed


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)
    if load_dotenv:
        load_dotenv()

    config_path = Path(args.config)
    state_path = Path(args.state)
    try:
        config = load_yaml(config_path)
        state = load_state(state_path)
        checked_at = now_utc()
        results = run_checks(config, checked_at)
        events = find_notification_events(results, state)
        notification_results = send_notifications(events, results, config)
        changed = apply_results_to_state(
            state, results, events, notification_results, checked_at
        )
        if changed:
            save_state(state_path, state)
            logging.info("状態を保存しました: %s", state_path)
        else:
            logging.info("状態に変更はありません。")
    except Exception as exc:
        logging.exception("実行に失敗しました: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
