import os
import time
import requests

# === ENV ===
TG_TOKEN = os.getenv("BOT_TOKEN")
TG_CHAT_ID = os.getenv("CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

ETH_ADDR_ENV = os.getenv("ETH_ADDRESS", "")
TRON_ADDR_ENV = os.getenv("TRON_ADDRESS", "")
BTC_ADDR_ENV = os.getenv("BTC_ADDRESS", "")

# === Helpers ===
def parse_addresses(env_value):
    """
    Parse 'addr[:label],addr2[:label2]' -> [{'address': addr, 'label': label}, ...]
    """
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
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            timeout=10,
        ).json()
        return float(r["price"])
    except Exception:
        return 0.0

def fmt_ts_utc(epoch_sec):
    """Format epoch seconds to 'YYYY-MM-DD HH:MM:SS UTC'"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(epoch_sec)))
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

# === Chain-specific fetchers ===
def get_latest_eth_tx(address):
    """
    Return latest incoming normal ETH tx to 'address' (not internal),
    from Etherscan 'txlist' (sorted desc).
    """
    url = (
        "https://api.etherscan.io/api"
        f"?module=account&action=txlist&address={address}"
        f"&sort=desc&apikey={ETHERSCAN_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15).json()
        txs = r.get("result", [])
        for tx in txs:
            # Incoming to our address
            if str(tx.get("to", "")).lower() == address.lower():
                # Add normalized fields we need
                tx["_amount_eth"] = int(tx.get("value", "0")) / 1e18
                tx["_time_utc"] = fmt_ts_utc(tx.get("timeStamp", 0))
                tx["_from"] = tx.get("from", "")
                tx["_to"] = tx.get("to", "")
                tx["_hash"] = tx.get("hash", "")
                return tx
    except Exception as e:
        print("ETH fetch error:", e)
    return None

def get_latest_tron_tx(address):
    """
    Latest incoming TRC20 transfer to 'address' via Trongrid.
    """
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20?limit=10"
    try:
        r = requests.get(url, timeout=15).json()
        txs = r.get("data", []) or []
        for tx in txs:
            if tx.get("to") == address:
                # Normalize
                decimals = int(tx.get("token_info", {}).get("decimals", 6))
                val = int(tx.get("value", "0")) / (10 ** decimals)
                symbol = tx.get("token_info", {}).get("symbol", "TRC20")
                ts_ms = tx.get("block_timestamp", 0)
                ts = int(ts_ms // 1000) if ts_ms else 0
                return {
                    "_amount": val,
                    "_symbol": symbol,
                    "_from": tx.get("from", ""),
                    "_to": tx.get("to", ""),
                    "_txid": tx.get("transaction_id", ""),
                    "_time_utc": fmt_ts_utc(ts),
                }
    except Exception as e:
        print("TRON fetch error:", e)
    return None

def _sum_outputs_to_address_btc(tx, address):
    """Sum vout value to 'address' in BTC (from satoshis)."""
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

def get_latest_btc_tx_mempool(address):
    """
    Use mempool.space (Esplora) API.
    - Check mempool txs first (/txs/mempool), then confirmed chain (/txs/chain).
    - Return normalized dict with amount, txid, time_utc (block_time if confirmed; else now).
    """
    base = "https://mempool.space/api/address"
    # 1) mempool (unconfirmed)
    try:
        mem_txs = requests.get(f"{base}/{address}/txs/mempool", timeout=15).json()
        for tx in mem_txs or []:
            amount_btc = _sum_outputs_to_address_btc(tx, address)
            if amount_btc > 0:
                txid = tx.get("txid", "")
                from_addr = _first_input_from_address_btc(tx)
                # No block_time for unconfirmed; use bot-seen time
                time_utc = fmt_ts_utc(int(time.time()))
                return {
                    "_amount_btc": amount_btc,
                    "_from": from_addr,
                    "_to": address,
                    "_txid": txid,
                    "_time_utc": time_utc,
                    "_confirmed": False,
                }
    except Exception as e:
        print("BTC mempool fetch error:", e)

    # 2) confirmed chain
    try:
        chain_txs = requests.get(f"{base}/{address}/txs/chain", timeout=15).json()
        for tx in chain_txs or []:
            amount_btc = _sum_outputs_to_address_btc(tx, address)
            if amount_btc > 0:
                txid = tx.get("txid", "")
                from_addr = _first_input_from_address_btc(tx)
                status = tx.get("status", {}) or {}
                block_time = status.get("block_time", 0)
                time_utc = fmt_ts_utc(block_time if block_time else int(time.time()))
                return {
                    "_amount_btc": amount_btc,
                    "_from": from_addr,
                    "_to": address,
                    "_txid": txid,
                    "_time_utc": time_utc,
                    "_confirmed": True,
                }
    except Exception as e:
        print("BTC chain fetch error:", e)

    return None

# === Main loop ===
def main():
    if not all([TG_TOKEN, TG_CHAT_ID]):
        raise ValueError("❌ Missing BOT_TOKEN or CHAT_ID")
    if not ETHERSCAN_API_KEY and ETH_ADDRESSES:
        print("⚠️ ETHERSCAN_API_KEY not set; ETH monitoring may fail.")

    last_seen = {}  # {address: txid/hash}

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
            if tx and tx["_hash"] != last_seen.get(addr):
                usd = tx["_amount_eth"] * eth_price
                name_line = f"（{label}）" if label else ""
                msg = (
                    f"*[ETH] 入金*\n"
                    f"账户{name_line}\n"
                    f"我们地址: `{tx['_to']}`\n"
                    f"客户地址: `{tx['_from']}`\n"
                    f"时间: {tx['_time_utc']}\n"
                    f"💰 {tx['_amount_eth']:.6f} ETH ≈ ${usd:,.2f}\n"
                    f"TXID: `{tx['_hash']}`"
                )
                send_message(msg)
                last_seen[addr] = tx["_hash"]

        # --- TRON (TRC20) ---
        for item in TRON_ADDRESSES:
            addr = item["address"].strip()
            label = item["label"]
            if not addr:
                continue
            tx = get_latest_tron_tx(addr)
            if tx and tx["_txid"] != last_seen.get(addr):
                name_line = f"（{label}）" if label else ""
                msg = (
                    f"*[TRC20] 入金*\n"
                    f"账户{name_line}\n"
                    f"我们地址: `{tx['_to']}`\n"
                    f"客户地址: `{tx['_from']}`\n"
                    f"时间: {tx['_time_utc']}\n"
                    f"💰 {tx['_amount']} {tx['_symbol']}\n"
                    f"TXID: `{tx['_txid']}`"
                )
                send_message(msg)
                last_seen[addr] = tx["_txid"]

        # --- BTC (mempool.space) ---
        for item in BTC_ADDRESSES:
            addr = item["address"].strip()
            label = item["label"]
            if not addr:
                continue
            tx = get_latest_btc_tx_mempool(addr)
            if tx and tx["_txid"] != last_seen.get(addr):
                usd_val = tx["_amount_btc"] * btc_price
                name_line = f"（{label}）" if label else ""
                status_line = "已确认 ✅" if tx["_confirmed"] else "未确认 ⏳"
                msg = (
                    f"*[BTC] 入金*\n"
                    f"账户{name_line}\n"
                    f"状态: {status_line}\n"
                    f"我们地址: `{tx['_to']}`\n"
                    f"客户地址: `{tx['_from']}`\n"
                    f"时间: {tx['_time_utc']}\n"
                    f"💰 {tx['_amount_btc']:.8f} BTC ≈ ${usd_val:,.2f}\n"
                    f"TXID: `{tx['_txid']}`"
                )
                send_message(msg)
                last_seen[addr] = tx["_txid"]

        time.sleep(5)

if __name__ == "__main__":
    main()
