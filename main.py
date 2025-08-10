import os
import time
import requests
import calendar
from collections import defaultdict

# === ENV ===
TG_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

ETH_ADDR_ENV = os.getenv("ETH_ADDRESS", "")
TRON_ADDR_ENV = os.getenv("TRON_ADDRESS", "")
BTC_ADDR_ENV = os.getenv("BTC_ADDRESS", "")

BTC_CONFIRM_UPDATE = os.getenv("BTC_CONFIRM_UPDATE", "true").lower() in ("1","true","yes","y")
BTC_USE_FIRST_SEEN = os.getenv("BTC_USE_FIRST_SEEN", "false").lower() in ("1","true","yes","y")

# Timezone for DISPLAY (+ optional cutoff in local)
TIME_OFFSET_HOURS = int(os.getenv("TIME_OFFSET_HOURS", "0"))
TIME_LABEL = os.getenv("TIME_LABEL", "").strip()
if not TIME_LABEL:
    TIME_LABEL = "北京时间" if TIME_OFFSET_HOURS == 8 else (f"UTC{('+' if TIME_OFFSET_HOURS>=0 else '')}{TIME_OFFSET_HOURS}" if TIME_OFFSET_HOURS else "UTC")
OFFSET_SEC = TIME_OFFSET_HOURS * 3600

# Cutoff (UTC and/or Local-Beijing)
GLOBAL_CUTOFF_UTC = os.getenv("NOTIFY_AFTER_UTC", "").strip()
GLOBAL_CUTOFF_LOCAL = os.getenv("NOTIFY_AFTER_LOCAL", "").strip()
BTC_CUTOFF_UTC = os.getenv("BTC_NOTIFY_AFTER_UTC", "").strip()
ETH_CUTOFF_UTC = os.getenv("ETH_NOTIFY_AFTER_UTC", "").strip()
TRON_CUTOFF_UTC = os.getenv("TRON_NOTIFY_AFTER_UTC", "").strip()
BTC_CUTOFF_LOCAL = os.getenv("BTC_NOTIFY_AFTER_LOCAL", "").strip()
ETH_CUTOFF_LOCAL = os.getenv("ETH_NOTIFY_AFTER_LOCAL", "").strip()
TRON_CUTOFF_LOCAL = os.getenv("TRON_NOTIFY_AFTER_LOCAL", "").strip()

SEEN_LIMIT = int(os.getenv("SEEN_LIMIT", "50"))

# === Helpers ===
def parse_addresses(env_value):
    result = []
    for raw in env_value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" in raw:
            addr, label = raw.split(":", 1)
            result.append({"address": addr.strip(), "label": label.strip()})
        else:
            result.append({"address": raw, "label": ""})
    return result

ETH_ADDRESSES = parse_addresses(ETH_ADDR_ENV)
TRON_ADDRESSES = parse_addresses(TRON_ADDR_ENV)
BTC_ADDRESSES = parse_addresses(BTC_ADDR_ENV)

def send_message(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Telegram Error:", e)

def get_price(symbol):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=10).json()
        return float(r["price"])
    except Exception:
        return 0.0

def fmt_ts_local(epoch_sec):
    """Return local time string with label, e.g. '2025-08-10 23:37:11 北京时间'."""
    try:
        local_epoch = int(epoch_sec) + OFFSET_SEC
        return time.strftime("%Y-%m-%d %H:%M:%S ", time.gmtime(local_epoch)) + TIME_LABEL
    except Exception:
        local_epoch = int(time.time()) + OFFSET_SEC
        return time.strftime("%Y-%m-%d %H:%M:%S ", time.gmtime(local_epoch)) + TIME_LABEL

def parse_cutoff(value: str, assume_local: bool=False) -> int:
    """
    Parse 'YYYY-MM-DD[ HH:MM:SS]' or epoch seconds (string).
    Return epoch (UTC). If assume_local=True, interpret the datetime string as LOCAL (UTC+OFFSET) then convert to UTC.
    """
    v = (value or "").strip()
    if not v:
        return 0
    # numeric -> assume epoch seconds (UTC)
    if v.isdigit() and len(v) >= 10:
        try:
            return int(v)
        except Exception:
            return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            t = time.strptime(v, fmt)
            epoch_utc = calendar.timegm(t)
            return epoch_utc - OFFSET_SEC if assume_local else epoch_utc
        except Exception:
            continue
    return 0

GLOBAL_CUTOFF_TS = parse_cutoff(GLOBAL_CUTOFF_LOCAL, assume_local=True) or parse_cutoff(GLOBAL_CUTOFF_UTC)
BTC_CUTOFF_TS = parse_cutoff(BTC_CUTOFF_LOCAL, assume_local=True) or parse_cutoff(BTC_CUTOFF_UTC) or GLOBAL_CUTOFF_TS
ETH_CUTOFF_TS = parse_cutoff(ETH_CUTOFF_LOCAL, assume_local=True) or parse_cutoff(ETH_CUTOFF_UTC) or GLOBAL_CUTOFF_TS
TRON_CUTOFF_TS = parse_cutoff(TRON_CUTOFF_LOCAL, assume_local=True) or parse_cutoff(TRON_CUTOFF_UTC) or GLOBAL_CUTOFF_TS

# === ETH ===
def get_latest_eth_tx(address):
    url = ("https://api.etherscan.io/api"
           f"?module=account&action=txlist&address={address}"
           f"&sort=desc&apikey={ETHERSCAN_API_KEY}")
    try:
        r = requests.get(url, timeout=15).json()
        txs = r.get("result", [])
        for tx in txs:
            if str(tx.get("to", "")).lower() == address.lower():
                ts = int(tx.get("timeStamp", "0"))
                tx["_amount_eth"] = int(tx.get("value", "0")) / 1e18
                tx["_time_local"] = fmt_ts_local(ts)
                tx["_epoch"] = ts
                tx["_from"] = tx.get("from", "")
                tx["_to"] = tx.get("to", "")
                tx["_hash"] = tx.get("hash", "")
                return tx
    except Exception as e:
        print("ETH fetch error:", e)
    return None

# === TRON (TRC20) ===
def get_latest_tron_tx(address):
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20?limit=10"
    try:
        r = requests.get(url, timeout=15).json()
        txs = r.get("data", []) or []
        for tx in txs:
            if tx.get("to") == address:
                decimals = int(tx.get("token_info", {}).get("decimals", 6))
                val = int(tx.get("value", "0")) / (10 ** decimals)
                symbol = tx.get("token_info", {}).get("symbol", "TRC20")
                ts_ms = int(tx.get("block_timestamp", 0))
                ts = int(ts_ms // 1000) if ts_ms else 0
                return {
                    "_amount": val,
                    "_symbol": symbol,
                    "_from": tx.get("from", ""),
                    "_to": tx.get("to", ""),
                    "_txid": tx.get("transaction_id", ""),
                    "_time_local": fmt_ts_local(ts),
                    "_epoch": ts,
                }
    except Exception as e:
        print("TRON fetch error:", e)
    return None

# === BTC via mempool.space ===
def _sum_outputs_to_address_btc(tx, address):
    total_sats = 0
    for vout in tx.get("vout", []):
        if vout.get("scriptpubkey_address") == address:
            total_sats += int(vout.get("value", 0))
    return total_sats / 1e8

def _first_input_from_address_btc(tx):
    try:
        vin0 = tx.get("vin", [])[0]
        prev = vin0.get("prevout", {})
        return prev.get("scriptpubkey_address") or "不明"
    except Exception:
        return "不明"

def _btc_first_seen_epoch(txid):
    if not BTC_USE_FIRST_SEEN:
        return int(time.time())
    try:
        j = requests.get(f"https://mempool.space/api/tx/{txid}", timeout=10).json()
        v = j.get("firstSeen") or j.get("received") or j.get("timestamp") or 0
        v = int(v)
        if v > 10**12:  # ms -> s
            v //= 1000
        return v if v > 0 else int(time.time())
    except Exception:
        return int(time.time())

def get_btc_txs_mempool(address, max_items=25):
    base = "https://mempool.space/api/address"
    results = []

    # Unconfirmed
    try:
        mem_txs = requests.get(f"{base}/{address}/txs/mempool", timeout=15).json()
        for tx in mem_txs or []:
            amount_btc = _sum_outputs_to_address_btc(tx, address)
            if amount_btc > 0:
                txid = tx.get("txid", "")
                epoch = _btc_first_seen_epoch(txid)
                results.append({
                    "_amount_btc": amount_btc,
                    "_from": _first_input_from_address_btc(tx),
                    "_to": address,
                    "_txid": txid,
                    "_time_local": fmt_ts_local(epoch),
                    "_epoch": epoch,
                    "_confirmed": False,
                })
            if len(results) >= max_items:
                break
    except Exception as e:
        print("BTC mempool fetch error:", e)

    # Confirmed
    try:
        chain_txs = requests.get(f"{base}/{address}/txs/chain", timeout=15).json()
        for tx in chain_txs or []:
            amount_btc = _sum_outputs_to_address_btc(tx, address)
            if amount_btc > 0:
                status = tx.get("status", {}) or {}
                block_time = int(status.get("block_time", 0)) or int(time.time())
                results.append({
                    "_amount_btc": amount_btc,
                    "_from": _first_input_from_address_btc(tx),
                    "_to": address,
                    "_txid": tx.get("txid", ""),
                    "_time_local": fmt_ts_local(block_time),
                    "_epoch": block_time,
                    "_confirmed": True,
                })
            if len(results) >= max_items:
                break
    except Exception as e:
        print("BTC chain fetch error:", e)

    return results

# === Main loop ===
def main():
    if not all([TG_TOKEN, TG_CHAT_ID]):
        raise ValueError("❌ Missing BOT_TOKEN/TELEGRAM_TOKEN or CHAT_ID")
    if not ETHERSCAN_API_KEY and ETH_ADDRESSES:
        print("⚠️ ETHERSCAN_API_KEY not set; ETH monitoring may fail.")

    # seen[address] = { txid: {"confirmed": bool, "ts": int} }
    seen = defaultdict(dict)

    while True:
        eth_price = get_price("ETHUSDT")
        btc_price = get_price("BTCUSDT")

        # --- ETH ---
        for item in ETH_ADDRESSES:
            addr = item["address"].strip()
            label = item["label"]
            if not addr:
                continue
            tx = get_latest_eth_tx(addr)
            if tx:
                if ETH_CUTOFF_TS and tx["_epoch"] and tx["_epoch"] < ETH_CUTOFF_TS:
                    continue
                if tx["_hash"] not in seen[addr]:
                    usd = tx["_amount_eth"] * eth_price
                    name_line = f"（{label}）" if label else ""
                    msg = (
                        f"*[ETH] 入金*\n"
                        f"账户{name_line}\n"
                        f"我们地址: `{tx['_to']}`\n"
                        f"客户地址: `{tx['_from']}`\n"
                        f"时间: {tx['_time_local']}\n"
                        f"💰 {tx['_amount_eth']:.6f} ETH ≈ ${usd:,.2f}\n"
                        f"TXID: `{tx['_hash']}`"
                    )
                    send_message(msg)
                    seen[addr][tx["_hash"]] = {"confirmed": True, "ts": int(time.time())}

        # --- TRON (TRC20) ---
        for item in TRON_ADDRESSES:
            addr = item["address"].strip()
            label = item["label"]
            if not addr:
                continue
            tx = get_latest_tron_tx(addr)
            if tx:
                if TRON_CUTOFF_TS and tx["_epoch"] and tx["_epoch"] < TRON_CUTOFF_TS:
                    continue
                if tx["_txid"] not in seen[addr]:
                    name_line = f"（{label}）" if label else ""
                    msg = (
                        f"*[TRC20] 入金*\n"
                        f"账户{name_line}\n"
                        f"我们地址: `{tx['_to']}`\n"
                        f"客户地址: `{tx['_from']}`\n"
                        f"时间: {tx['_time_local']}\n"
                        f"💰 {tx['_amount']} {tx['_symbol']}\n"
                        f"TXID: `{tx['_txid']}`"
                    )
                    send_message(msg)
                    seen[addr][tx["_txid"]] = {"confirmed": True, "ts": int(time.time())}

        # --- BTC ---
        for item in BTC_ADDRESSES:
            addr = item["address"].strip()
            label = item["label"]
            if not addr:
                continue

            txs = get_btc_txs_mempool(addr)
            if not txs:
                continue

            for tx in txs:
                if BTC_CUTOFF_TS and tx["_epoch"] and tx["_epoch"] < BTC_CUTOFF_TS:
                    continue

                prev = seen[addr].get(tx["_txid"])
                name_line = f"（{label}）" if label else ""
                status_line = "已确认 ✅" if tx["_confirmed"] else "未确认 ⏳"
                usd_val = tx["_amount_btc"] * btc_price

                if not prev:
                    msg = (
                        f"*[BTC] 入金*\n"
                        f"账户{name_line}\n"
                        f"状态: {status_line}\n"
                        f"我们地址: `{tx['_to']}`\n"
                        f"客户地址: `{tx['_from']}`\n"
                        f"时间: {tx['_time_local']}\n"
                        f"💰 {tx['_amount_btc']:.8f} BTC ≈ ${usd_val:,.2f}\n"
                        f"TXID: `{tx['_txid']}`"
                    )
                    send_message(msg)
                    seen[addr][tx["_txid"]] = {"confirmed": tx["_confirmed"], "ts": int(time.time())}
                else:
                    if BTC_CONFIRM_UPDATE and (not prev["confirmed"]) and tx["_confirmed"]:
                        msg = (
                            f"*[BTC] 状态更新*\n"
                            f"账户{name_line}\n"
                            f"状态: 已确认 ✅\n"
                            f"我们地址: `{tx['_to']}`\n"
                            f"客户地址: `{tx['_from']}`\n"
                            f"时间: {tx['_time_local']}\n"
                            f"💰 {tx['_amount_btc']:.8f} BTC ≈ ${usd_val:,.2f}\n"
                            f"TXID: `{tx['_txid']}`"
                        )
                        send_message(msg)
                        prev["confirmed"] = True

            # cleanup
            if len(seen[addr]) > SEEN_LIMIT:
                for txid in sorted(seen[addr], key=lambda k: seen[addr][k]["ts"])[:-SEEN_LIMIT]:
                    seen[addr].pop(txid, None)

        time.sleep(5)

if __name__ == "__main__":
    main()
