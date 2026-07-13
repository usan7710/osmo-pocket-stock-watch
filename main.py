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
        "--notify-current",
        action="store_true",
        help="Send the current stock status summary to Discord.",
    )
    parser.add_argument(
        "--check-disabled",
        action="store_true",
        help="Temporarily check targets with enabled: false for this run.",
    )
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


def decide_structured_stock(html: str) -> tuple[str, bool | None, str] | None:
    availability_values = re.findall(
        r'"availability"\s*:\s*"([^"]+)"',
        html,
        flags=re.IGNORECASE,
    )
    if not availability_values:
        return None

    normalized = [value.rsplit("/", 1)[-1].casefold() for value in availability_values]
    out_tokens = {"outofstock", "soldout", "discontinued"}
    in_tokens = {"instock", "limitedavailability", "preorder"}

    out_matches = [value for value in normalized if value in out_tokens]
    in_matches = [value for value in normalized if value in in_tokens]
    if out_matches:
        return (
            "out_of_stock",
            False,
            "構造化データavailabilityで在庫なしを検出: "
            + ", ".join(sorted(set(out_matches))),
        )
    if in_matches:
        return (
            "in_stock",
            True,
            "構造化データavailabilityで在庫ありを検出: "
            + ", ".join(sorted(set(in_matches))),
        )
    return None


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
    structured = decide_structured_stock(html)
    if structured:
        status, in_stock, reason = structured
    else:
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
    structured = decide_structured_stock(html)
    if structured:
        status, in_stock, reason = structured
    else:
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
    *,
    check_disabled: bool = False,
) -> CheckResult:
    target_disabled = target.get("enabled", True) is False
    if target_disabled and not check_disabled:
        return build_result(
            product,
            target,
            checked_at,
            status="skipped",
            in_stock=None,
            reason="enabled: false のためスキップしました。",
            key=target_key(str(product.get("id", "unknown_product")), target),
        )

    effective_target = target
    if target_disabled:
        effective_target = dict(target)
        manual_request = dict(target.get("request") or {})
        manual_request.setdefault("timeout_seconds", 12)
        manual_request.setdefault("retries", 0)
        effective_target["request"] = manual_request

    provider = str(
        effective_target.get("provider")
        or effective_target.get("method")
        or "requests"
    ).lower()
    try:
        if provider in {"requests", "html", "beautifulsoup"}:
            return check_with_requests(
                session, product, effective_target, config, checked_at
            )
        if provider == "playwright":
            return check_with_playwright(product, effective_target, config, checked_at)
        if provider == "keepa":
            return check_with_keepa(
                session, product, effective_target, config, checked_at
            )
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


def run_checks(
    config: dict[str, Any],
    checked_at: datetime,
    *,
    check_disabled: bool = False,
) -> list[CheckResult]:
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
            result = check_target(
                session,
                product,
                target,
                config,
                checked_at,
                check_disabled=check_disabled,
            )
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


def status_label(result: CheckResult) -> str:
    if result.in_stock is True:
        return "✅ 在庫あり"
    if result.in_stock is False:
        return "❌ 在庫なし"
    if result.status == "skipped":
        return "⏸ スキップ"
    if result.status == "error":
        return "⚠️ エラー"
    return "❓ 判定保留"


def clean_reason(result: CheckResult) -> str:
    reason = result.reason
    if result.in_stock is True:
        if ":" in reason:
            return "在庫あり表示を検出（" + reason.split(":", 1)[1].strip() + "）"
        return "在庫あり表示を検出"
    if result.in_stock is False:
        if ":" in reason:
            return "在庫なし表示を検出（" + reason.split(":", 1)[1].strip() + "）"
        return "在庫なし表示を検出"
    if result.status == "skipped":
        if "enabled: false" in result.reason:
            return "設定で一時停止中（enabled: false）"
        return result.reason
    if result.status == "error":
        return "取得エラー: " + (result.error or reason)
    if "キーワードに一致しません" in reason:
        return "在庫判定キーワードに一致せず"
    return reason


def status_color(results: list[CheckResult]) -> int:
    active = [result for result in results if result.status != "skipped"]
    if any(result.in_stock is True for result in active):
        return 0x2ECC71
    if any(result.status == "error" for result in active):
        return 0xF1C40F
    if any(result.in_stock is None for result in active):
        return 0xE67E22
    return 0xE74C3C


def count_statuses(results: list[CheckResult]) -> dict[str, int]:
    return {
        "in_stock": sum(result.in_stock is True for result in results),
        "out_of_stock": sum(result.in_stock is False for result in results),
        "unknown": sum(
            result.in_stock is None
            and result.status not in {"skipped", "error"}
            for result in results
        ),
        "error": sum(result.status == "error" for result in results),
        "skipped": sum(result.status == "skipped" for result in results),
    }


def truncate_value(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 10].rstrip() + "\n...省略"


def append_chunked_field(
    embed: dict[str, Any],
    name: str,
    lines: list[str],
    *,
    inline: bool = False,
    limit: int = 1024,
) -> None:
    if not lines:
        return

    chunk: list[str] = []
    chunk_len = 0
    field_count = 1
    for line in lines:
        addition = len(line) + (1 if chunk else 0)
        if chunk and chunk_len + addition > limit:
            field_name = name if field_count == 1 else f"{name} ({field_count})"
            embed["fields"].append(
                {"name": field_name, "value": "\n".join(chunk), "inline": inline}
            )
            field_count += 1
            chunk = [line]
            chunk_len = len(line)
        else:
            chunk.append(line)
            chunk_len += addition

    if chunk:
        field_name = name if field_count == 1 else f"{name} ({field_count})"
        embed["fields"].append(
            {"name": field_name, "value": "\n".join(chunk), "inline": inline}
        )


def format_result_line(result: CheckResult, *, include_reason: bool = True) -> str:
    line = f"{status_label(result)} [{result.site}]({result.url})"
    if include_reason:
        line += f"\n{clean_reason(result)}"
    return line


def build_restock_embed(
    event: CheckResult,
    results: list[CheckResult],
    config: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
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
        content = f"🚨【最優先】{event.product_name} が在庫復活した可能性があります！"
        title = "最優先候補の在庫復活"
        description = "まずVlogコンボを確認してください。"
        color = 0xE74C3C
    else:
        content = f"⚠️【代替候補】{event.product_name} が在庫復活した可能性があります。"
        title = "代替候補の在庫復活"
        description = "Vlogコンボではありませんが、購入候補として確認してください。"
        color = 0xF1C40F

    if event.priority != 1 and vlog_available_other:
        top = vlog_available_other[0]
        description = (
            "Vlogコンボも在庫ありの可能性があります。"
            f"\n最優先候補: [{top.site}]({top.url})"
        )
    elif event.priority == 1 and lower_available:
        description += "\n代替候補も在庫ありの可能性があります。"

    embed = {
        "title": title,
        "description": description,
        "url": event.url,
        "color": color,
        "fields": [
            {"name": "販売サイト", "value": event.site, "inline": True},
            {"name": "商品", "value": event.product_name, "inline": True},
            {"name": "判定理由", "value": clean_reason(event), "inline": False},
        ],
        "footer": {"text": f"確認時刻: {display_time(event.checked_at, config)}"},
    }
    return content, [embed]


def build_current_status_embed(
    results: list[CheckResult],
    config: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    active = [result for result in results if result.status != "skipped"]
    skipped = [result for result in results if result.status == "skipped"]
    counts = count_statuses(results)
    active_counts = count_statuses(active)
    summary = (
        f"✅ 在庫あり {active_counts['in_stock']}件 / "
        f"❌ 在庫なし {active_counts['out_of_stock']}件 / "
        f"❓ 判定保留 {active_counts['unknown']}件 / "
        f"⚠️ エラー {active_counts['error']}件 / "
        f"⏸ スキップ {counts['skipped']}件"
    )

    embed: dict[str, Any] = {
        "title": "Osmo Pocket 4P 現在の在庫状況",
        "description": summary,
        "color": status_color(results),
        "fields": [],
        "footer": {"text": f"確認時刻: {display_time(now_utc(), config)}"},
    }

    vlog_lines = [
        format_result_line(result)
        for result in sorted(active, key=lambda item: (item.priority, item.site))
        if result.priority == 1
    ]
    alt_lines = [
        format_result_line(result)
        for result in sorted(active, key=lambda item: (item.priority, item.site))
        if result.priority != 1
    ]
    skipped_lines = [
        (
            f"⏸ [{result.site}]({result.url})\n"
            f"商品: {result.product_name}\n"
            f"URL: <{result.url}>\n"
            f"理由: {clean_reason(result)}"
        )
        for result in sorted(skipped, key=lambda item: (item.priority, item.site))
    ]

    append_chunked_field(embed, "最優先: Vlogコンボ", vlog_lines or ["有効な監視対象なし"])
    append_chunked_field(embed, "代替候補", alt_lines or ["有効な監視対象なし"])
    append_chunked_field(
        embed,
        f"一時スキップ中（{len(skipped_lines)}件）",
        skipped_lines or ["なし"],
    )

    return "📋 現在の在庫状況を確認しました。", [embed]


def post_discord(
    webhook_url: str,
    content: str = "",
    embeds: list[dict[str, Any]] | None = None,
) -> bool:
    payload: dict[str, Any] = {"allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
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


def send_current_status(results: list[CheckResult], config: dict[str, Any]) -> bool:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logging.error("DISCORD_WEBHOOK_URL が未設定のため現在状況を通知できません。")
        return False

    content, embeds = build_current_status_embed(results, config)
    success = post_discord(webhook_url, content, embeds)
    if success:
        logging.info("Discordへ現在状況を通知しました。")
    else:
        logging.error("Discordへの現在状況通知に失敗しました。")
    return success


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
        content, embeds = build_restock_embed(event, results, config)
        success = post_discord(webhook_url, content, embeds)
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
        results = run_checks(
            config,
            checked_at,
            check_disabled=args.check_disabled,
        )
        events = find_notification_events(results, state)
        notification_results = send_notifications(events, results, config)
        if args.notify_current:
            send_current_status(results, config)
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
