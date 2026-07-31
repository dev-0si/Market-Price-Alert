#!/usr/bin/env python3
"""
Market Price Alert
Polls Telegram for new alert commands, checks crypto perp / FX prices,
and fires Telegram alerts when a target price is crossed.

Alert command format (sent to the bot as a plain message):
    SYMBOL above|below PRICE
    SYMBOL above|below PRICE | your note here

Examples:
    BTC/USDT.P above 65000
    EURUSD below 1.0800 | broke below the lowest low

Crypto perp symbols use the form BASE/QUOTE.P (e.g. BTC/USDT.P) and are
priced from MEXC, falling back to Binance.
FX symbols are six-letter pairs with no separator (e.g. EURUSD) and are
priced from Twelve Data, falling back to Finnhub.

Each alert fires every time price crosses its target (not on every run
it happens to sit past it), up to 5 times, after which it is retired
into state/alert_state.json under "history".
"""

import json
import os
import random
import re
import string
import sys
import time
import requests

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

CONFIG_PATH = "config/alerts.json"
STATE_PATH = "state/alert_state.json"
OFFSET_PATH = "state/telegram_offset.json"

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_FIRES = 5
REQUEST_TIMEOUT = 10

# Matches: SYMBOL above|below PRICE [| note]
ALERT_PATTERN = re.compile(
    r"^\s*(?P<symbol>\S+)\s+(?P<direction>above|below)\s+(?P<price>[0-9]*\.?[0-9]+)"
    r"\s*(?:\|\s*(?P<note>.+))?\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        content = f.read().strip()
        if not content:
            return default
        return json.loads(content)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def telegram_get_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 0}
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("result", [])


def _track_message_id(state, message_id):
    if state is None or message_id is None:
        return
    state.setdefault("sent_message_ids", []).append(message_id)


def telegram_send(text, state=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, skipping send:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        message_id = resp.json().get("result", {}).get("message_id")
        _track_message_id(state, message_id)
    except requests.RequestException as e:
        print(f"Failed to send Telegram message: {e}")


def telegram_send_with_keyboard(text, keyboard, state=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, skipping send:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "reply_markup": keyboard}
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        message_id = resp.json().get("result", {}).get("message_id")
        _track_message_id(state, message_id)
    except requests.RequestException as e:
        print(f"Failed to send Telegram message with keyboard: {e}")


def telegram_delete_message(message_id):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id}
    try:
        requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"Failed to delete message {message_id}: {e}")


def telegram_answer_callback(callback_query_id, text=""):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"Failed to answer callback query: {e}")


def telegram_set_commands():
    """
    Registers /list, /history, /delete in Telegram's native menu button
    (the icon next to the message box). Safe to call every run.
    """
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "list", "description": "Show active alerts"},
        {"command": "history", "description": "Show fired/retired alerts"},
        {"command": "delete", "description": "Tap an alert from a list to delete it"},
        {"command": "clear", "description": "Clear the chat (deletes messages < 48h old)"},
    ]
    try:
        requests.post(url, json={"commands": commands}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"Failed to set Telegram commands: {e}")


# ---------------------------------------------------------------------------
# Symbol / market helpers
# ---------------------------------------------------------------------------

def is_crypto_perp(symbol):
    return symbol.upper().endswith(".P")


def market_for(symbol):
    return "crypto" if is_crypto_perp(symbol) else "fx"


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

def crypto_symbol_to_pair(symbol):
    """
    Convert 'BTC/USDT.P' -> 'BTCUSDT' for exchange ticker endpoints.
    """
    base_quote = symbol.upper().rstrip(".P").rstrip(".")
    return base_quote.replace("/", "")


def get_price_mexc(symbol):
    pair = crypto_symbol_to_pair(symbol)
    url = "https://contract.mexc.com/api/v1/contract/ticker"
    resp = requests.get(url, params={"symbol": pair}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return float(data["data"]["lastPrice"])


def get_price_binance(symbol):
    pair = crypto_symbol_to_pair(symbol)
    url = "https://fapi.binance.com/fapi/v1/ticker/price"
    resp = requests.get(url, params={"symbol": pair}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return float(data["price"])


def get_crypto_price(symbol):
    try:
        return get_price_mexc(symbol)
    except Exception as e:
        print(f"MEXC failed for {symbol} ({e}), trying Binance...")
        return get_price_binance(symbol)


def get_price_twelve_data(symbol):
    url = "https://api.twelvedata.com/price"
    params = {"symbol": f"{symbol[:3]}/{symbol[3:]}", "apikey": TWELVE_DATA_API_KEY}
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "price" not in data:
        raise ValueError(f"Twelve Data error: {data}")
    return float(data["price"])


def get_price_finnhub(symbol):
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": f"OANDA:{symbol[:3]}_{symbol[3:]}", "token": FINNHUB_API_KEY}
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("c"):
        raise ValueError(f"Finnhub error: {data}")
    return float(data["c"])


def get_fx_price(symbol):
    try:
        return get_price_finnhub(symbol)
    except Exception as e:
        print(f"Finnhub failed for {symbol} ({e}), trying Twelve Data...")
        return get_price_twelve_data(symbol)


def get_price(symbol):
    if is_crypto_perp(symbol):
        return get_crypto_price(symbol)
    return get_fx_price(symbol)


# ---------------------------------------------------------------------------
# Telegram command parsing -> new alerts
# ---------------------------------------------------------------------------

ID_CHARS = string.ascii_uppercase + string.digits


def generate_alert_id(existing_ids):
    while True:
        candidate = "".join(random.choices(ID_CHARS, k=4))
        if candidate not in existing_ids:
            return candidate


def parse_alert_message(text, existing_ids):
    match = ALERT_PATTERN.match(text)
    if not match:
        return None
    symbol = match.group("symbol").upper()
    direction = match.group("direction").lower()
    price = float(match.group("price"))
    note = match.group("note")
    note = note.strip() if note else ""
    return {
        "id": generate_alert_id(existing_ids),
        "symbol": symbol,
        "market": market_for(symbol),
        "direction": direction,
        "target": price,
        "note": note,
    }


def format_active_list(alerts):
    if not alerts:
        return "No active alerts."
    lines = []
    for a in alerts:
        line = f"{a['id']}: {a['symbol']} {a['direction']} {a['target']}"
        if a.get("note"):
            line += f" [{a['note']}]"
        lines.append(line)
    return "\n".join(lines)


def format_history_list(state):
    history = state.get("history", [])
    if not history:
        return "No alert history yet."
    lines = []
    for a in history:
        line = f"{a['symbol']} {a['direction']} {a['target']}"
        if a.get("note"):
            line += f" [{a['note']}]"
        line += f" \u2014 fired {a.get('fire_count', 0)}x"
        lines.append(line)
    return "\n".join(lines)


def build_delete_keyboard(alerts):
    buttons = []
    for a in alerts:
        label = f"{a['symbol']} {a['direction']} {a['target']}"
        buttons.append([{"text": label, "callback_data": f"delete:{a['id']}"}])
    return {"inline_keyboard": buttons}


def send_delete_menu(alerts, state):
    if not alerts:
        telegram_send("No active alerts to delete.", state)
        return
    telegram_send_with_keyboard("Tap an alert to delete it:", build_delete_keyboard(alerts), state)


def handle_delete_callback(callback_query, alerts, state):
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")

    if not data.startswith("delete:"):
        telegram_answer_callback(callback_id)
        return alerts, state

    target_id = data.split(":", 1)[1]
    before_count = len(alerts)
    alerts = [a for a in alerts if a["id"] != target_id]
    state.pop(target_id, None)

    if len(alerts) < before_count:
        telegram_answer_callback(callback_id, "Deleted")
        telegram_send(f"Deleted alert {target_id}", state)
    else:
        telegram_answer_callback(callback_id, "Already gone")

    return alerts, state


def clear_chat(state):
    """
    Deletes every message this script knows about in the chat -- both its
    own replies and the messages you sent it (Telegram allows a bot to
    delete incoming messages in a private chat). Telegram's own hard limit
    applies: only messages from the last 48 hours can be deleted via the
    API; anything older is left in place.
    """
    message_ids = state.get("sent_message_ids", [])
    for message_id in message_ids:
        telegram_delete_message(message_id)
    state["sent_message_ids"] = []


def process_telegram_commands(alerts, state, offset_data):
    offset = offset_data.get("offset", 0)
    try:
        updates = telegram_get_updates(offset)
    except Exception as e:
        print(f"Failed to poll Telegram: {e}")
        return alerts, state, offset_data

    for update in updates:
        offset_data["offset"] = update["update_id"] + 1

        if "callback_query" in update:
            alerts, state = handle_delete_callback(update["callback_query"], alerts, state)
            continue

        message = update.get("message", {})
        text = message.get("text", "")
        if not text:
            continue

        # Track every incoming message so /clear can remove it too.
        _track_message_id(state, message.get("message_id"))

        stripped = text.strip()
        lowered = stripped.lower()

        if lowered == "/clear":
            clear_chat(state)
            continue

        if lowered == "/list":
            telegram_send(format_active_list(alerts), state)
            continue

        if lowered == "/history":
            telegram_send(format_history_list(state), state)
            continue

        if lowered == "/delete":
            send_delete_menu(alerts, state)
            continue

        if lowered.startswith("/delete "):
            parts = stripped.split(maxsplit=1)
            target_id = parts[1].strip()
            before_count = len(alerts)
            alerts = [a for a in alerts if a["id"] != target_id]
            state.pop(target_id, None)
            if len(alerts) < before_count:
                telegram_send(f"Deleted alert {target_id}", state)
            else:
                telegram_send(f"No active alert found with id {target_id}", state)
            continue

        new_alert = parse_alert_message(text, {a["id"] for a in alerts})
        if new_alert is None:
            continue

        alerts.append(new_alert)
        confirm = f"Alert added: {new_alert['symbol']} {new_alert['direction']} {new_alert['target']}"
        if new_alert["note"]:
            confirm += f" [{new_alert['note']}]"
        telegram_send(confirm, state)

    return alerts, state, offset_data


# ---------------------------------------------------------------------------
# Alert evaluation
# ---------------------------------------------------------------------------

def format_alert_message(alert):
    direction_icon = "\U0001F4C8" if alert["direction"] == "above" else "\U0001F4C9"  # 📈 / 📉
    text = f"\U0001F3AF {direction_icon} {alert['symbol']} {alert['direction']} {alert['target']}"
    if alert.get("note"):
        text += f" [{alert['note']}]"
    return text


def evaluate_alerts(alerts, state):
    still_active = []

    for alert in alerts:
        alert_id = alert["id"]
        try:
            price = get_price(alert["symbol"])
        except Exception as e:
            print(f"Could not fetch price for {alert['symbol']}: {e}")
            still_active.append(alert)
            continue

        current_side = "above" if price >= alert["target"] else "below"
        record = state.get(alert_id, {"last_side": None, "fire_count": 0})

        # First time we've ever seen this alert: if the condition is already
        # true right now, fire immediately instead of silently waiting for a
        # future crossing (otherwise an alert created when the price already
        # sits past its target would never fire until it dips back and
        # re-crosses, which may never happen).
        if record["last_side"] is None:
            if current_side == alert["direction"]:
                telegram_send(format_alert_message(alert), state)
                record["fire_count"] += 1
            record["last_side"] = current_side
            state[alert_id] = record

            if record["fire_count"] >= MAX_FIRES:
                history = state.setdefault("history", [])
                history.append({**alert, "fire_count": record["fire_count"]})
                state.pop(alert_id, None)
            else:
                still_active.append(alert)
            continue

        crossed_into_trigger_side = (
            current_side == alert["direction"] and record["last_side"] != alert["direction"]
        )

        if crossed_into_trigger_side:
            telegram_send(format_alert_message(alert), state)
            record["fire_count"] += 1

        record["last_side"] = current_side
        state[alert_id] = record

        if record["fire_count"] >= MAX_FIRES:
            history = state.setdefault("history", [])
            history.append({**alert, "fire_count": record["fire_count"]})
            state.pop(alert_id, None)
        else:
            still_active.append(alert)

    return still_active, state


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    telegram_set_commands()

    alerts = load_json(CONFIG_PATH, [])
    state = load_json(STATE_PATH, {})
    offset_data = load_json(OFFSET_PATH, {"offset": 0})

    alerts, state, offset_data = process_telegram_commands(alerts, state, offset_data)
    alerts, state = evaluate_alerts(alerts, state)

    save_json(CONFIG_PATH, alerts)
    save_json(STATE_PATH, state)
    save_json(OFFSET_PATH, offset_data)


if __name__ == "__main__":
    main()
