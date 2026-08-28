"""
AUD/TWD Exchange Rate Tracker
- Fetches current AUD/TWD mid-market rate
- Appends to historical data (JSON)
- Sends Telegram alert if price crosses user-defined thresholds
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────
DATA_FILE = "data/rates.json"
ALERTS_FILE = "data/alerts.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Exchange rate API (free, no key required)
# Primary: frankfurter.dev (ECB data, reliable)
# Fallback: exchangerate-api (free tier)
PRIMARY_API = "https://api.frankfurter.dev/latest?from=AUD&to=TWD"
FALLBACK_API = "https://open.er-api.com/v6/latest/AUD"

# ── Fetch rate ──────────────────────────────────────────────────

def fetch_rate_primary():
    """Fetch from frankfurter.dev (ECB-based, no API key)."""
    req = urllib.request.Request(PRIMARY_API, headers={"User-Agent": "AUD-Tracker/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return float(data["rates"]["TWD"])


def fetch_rate_fallback():
    """Fetch from exchangerate-api (free, no API key)."""
    req = urllib.request.Request(FALLBACK_API, headers={"User-Agent": "AUD-Tracker/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return float(data["rates"]["TWD"])


def fetch_rate():
    """Try primary API, fall back if it fails."""
    try:
        return fetch_rate_primary()
    except Exception as e:
        print(f"[WARN] Primary API failed: {e}, trying fallback...")
        return fetch_rate_fallback()


# ── Data storage ────────────────────────────────────────────────

def load_json(path, default):
    """Load a JSON file, return default if not found."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    """Save data to JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_rate(rate):
    """Append a new rate entry to the history file."""
    history = load_json(DATA_FILE, [])
    now = datetime.now(timezone.utc)
    entry = {
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time_utc": now.strftime("%H:%M"),
        "rate": round(rate, 4)
    }
    history.append(entry)

    # Keep last 365 days of data (max ~2200 entries at 6x/day)
    cutoff = (now - timedelta(days=365)).isoformat()
    history = [h for h in history if h["timestamp"] >= cutoff]

    save_json(DATA_FILE, history)
    return history


# ── Statistics ──────────────────────────────────────────────────

def calc_stats(history, rate):
    """Calculate useful statistics for the alert message."""
    if len(history) < 2:
        return {}

    rates = [h["rate"] for h in history]

    # Moving averages
    last_30d = [h["rate"] for h in history if h["timestamp"] >= (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()]
    last_90d = [h["rate"] for h in history if h["timestamp"] >= (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()]

    ma30 = round(sum(last_30d) / len(last_30d), 4) if last_30d else None
    ma90 = round(sum(last_90d) / len(last_90d), 4) if last_90d else None

    # Percentile (what % of historical rates are below current)
    below = sum(1 for r in rates if r < rate)
    percentile = round(below / len(rates) * 100, 1)

    # 24h change
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent = [h for h in history if h["timestamp"] >= day_ago]
    change_24h = None
    if len(recent) >= 2:
        change_24h = round(rate - recent[0]["rate"], 4)

    return {
        "ma30": ma30,
        "ma90": ma90,
        "percentile": percentile,
        "change_24h": change_24h,
        "high_90d": round(max(last_90d), 4) if last_90d else None,
        "low_90d": round(min(last_90d), 4) if last_90d else None,
    }


# ── Telegram ────────────────────────────────────────────────────

def send_telegram(message):
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram credentials not set, skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "AUD-Tracker/1.0"
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print("[OK] Telegram message sent.")
            else:
                print(f"[ERROR] Telegram API error: {result}")
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram message: {e}")


# ── Alert logic ─────────────────────────────────────────────────

def check_alerts(rate, stats):
    """Check if rate crosses any alert thresholds."""
    alerts = load_json(ALERTS_FILE, {
        "below": [],       # Alert when rate drops below these values
        "above": [],       # Alert when rate rises above these values
        "ma_cross": True,  # Alert when rate crosses below 30-day MA
        "last_alerted": {} # Track last alert time to avoid spam
    })

    now_iso = datetime.now(timezone.utc).isoformat()
    messages = []

    # Check "below" alerts (good time to buy AUD — rate is cheap)
    for threshold in alerts.get("below", []):
        alert_key = f"below_{threshold}"
        last = alerts.get("last_alerted", {}).get(alert_key, "")
        # Only alert once per 24 hours per threshold
        if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 86400:
            continue
        if rate <= threshold:
            messages.append(f"📉 AUD/TWD *跌破 {threshold}*")
            alerts.setdefault("last_alerted", {})[alert_key] = now_iso

    # Check "above" alerts (AUD getting expensive)
    for threshold in alerts.get("above", []):
        alert_key = f"above_{threshold}"
        last = alerts.get("last_alerted", {}).get(alert_key, "")
        if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 86400:
            continue
        if rate >= threshold:
            messages.append(f"📈 AUD/TWD *突破 {threshold}*")
            alerts.setdefault("last_alerted", {})[alert_key] = now_iso

    # Check MA cross (rate drops below 30-day average = potential buy signal)
    if alerts.get("ma_cross") and stats.get("ma30"):
        alert_key = "ma30_cross_below"
        last = alerts.get("last_alerted", {}).get(alert_key, "")
        if not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= 86400:
            if rate < stats["ma30"]:
                diff_pct = round((stats["ma30"] - rate) / stats["ma30"] * 100, 2)
                messages.append(f"📊 匯率跌破 30 日均線（低於均價 {diff_pct}%）")
                alerts.setdefault("last_alerted", {})[alert_key] = now_iso

    if messages:
        # Build full alert message
        mel_time = datetime.now(timezone(timedelta(hours=10))).strftime("%m/%d %H:%M")
        header = f"🔔 *AUD/TWD 匯率警報*\n📅 {mel_time} (Melbourne)\n\n"

        body = "\n".join(messages) + "\n\n"

        details = f"💰 目前匯率：*{rate}*\n"
        if stats.get("ma30"):
            details += f"📏 30日均價：{stats['ma30']}\n"
        if stats.get("ma90"):
            details += f"📏 90日均價：{stats['ma90']}\n"
        if stats.get("percentile") is not None:
            details += f"📊 歷史百分位：{stats['percentile']}%\n"
        if stats.get("change_24h") is not None:
            arrow = "⬆️" if stats["change_24h"] > 0 else "⬇️"
            details += f"{arrow} 24h變化：{stats['change_24h']:+.4f}\n"

        send_telegram(header + body + details)
        save_json(ALERTS_FILE, alerts)
        return True

    save_json(ALERTS_FILE, alerts)
    return False


# ── Main ────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print(f"AUD/TWD Tracker — {datetime.now(timezone.utc).isoformat()}")
    print("=" * 50)

    # 1. Fetch current rate
    try:
        rate = fetch_rate()
        print(f"[OK] Current AUD/TWD rate: {rate}")
    except Exception as e:
        print(f"[FATAL] Could not fetch exchange rate: {e}")
        send_telegram(f"⚠️ 匯率抓取失敗：{e}")
        sys.exit(1)

    # 2. Append to history
    history = append_rate(rate)
    print(f"[OK] History updated — {len(history)} data points")

    # 3. Calculate stats
    stats = calc_stats(history, rate)
    if stats:
        print(f"[OK] Stats — MA30: {stats.get('ma30')}, MA90: {stats.get('ma90')}, Percentile: {stats.get('percentile')}%")

    # 4. Check alerts
    alerted = check_alerts(rate, stats)
    if not alerted:
        print("[OK] No alert conditions triggered.")

    print("[DONE]")


if __name__ == "__main__":
    main()
