# main.py
import os
import time
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter, Retry

# ========= ENV & CONFIG =========
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

if not all([TG_TOKEN, TG_CHAT_ID, ETHERSCAN_API_KEY]):
    raise ValueError("❌ Missing env: TELEGRAM_TOKEN, CHAT_ID, ETHERSCAN_API_KEY")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))              # seconds
PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL", "30"))         # seconds
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))         # seconds
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "12"))
BTC_PROVIDER = os.getenv("BTC_PROVIDER", "mempool").lower()       # mempool | blockchair
BLOCKCHAIR_KEY = os.getenv("BLOCKCHAIR_KEY", "")                  # optional
MEMPOOL_BASE = os.getenv("MEMPOOL_BASE", "https://mempool.space/api")  # or https://blockstream.info/api

# ========= UTILS =========
def parse_wallets(env_str):
    wallets = {}
    for item in env_str.split(","):
        if ":" in item:
            addr, label = item.strip().split(":", 1)
            wallets[addr.strip()] = label.strip()
    return wallets

ETH_WALLETS = parse_wallets(os.getenv("ETH_LABELS", ""))
BTC_WALLETS = parse_wallets(os.getenv("BTC_LABELS", ""))
TRON_WALLETS = parse_wallets(os.getenv("TRON_LABELS", ""))

def mk_session():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"])
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=100, pool_maxsize=100)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

SESSION = mk_session()
LOCK = threading.Lock()

def send_message(msg: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": msg}
    try:
        SESSION.post(url, data=data, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print("Telegram Error:", e)

# -------- Price cache --------
_price_cache = {}
def get_price(symbol: str) -> float:
    now = time.time()
    if symbol in _price_cache:
        val, ts = _price_cache[symbol]
        if now - ts < PRICE_CACHE_TTL:
            return val
    try:
        r = SESSION.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            timeout=REQUEST_TIMEOUT
        )
        r.raise_for_status()
        price = float(r.json()["price"])
        _price_cache[symbol] = (price, now)
        return price
    except Exception as e:
        print(f"[Price Error] {symbol}: {e}")
        return _price_cache.get(symbol, (0.0, 0))[0]

# ========= ETH =========
def get_latest_eth_tx(address: str):
    # only first page, sorted desc; check inbound quickly
    url = (
        "https://api.etherscan.io/api"
        f"?module=account&action=txlist&address={address}"
        "&page=1&offset=10&sort=desc"
        f"&apikey={ETHERSCAN_API_KEY}"
    )
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        data = r.json().get("result", [])
        for tx in data:
            # inbound native ETH only
            if (tx.get("to") or "").lower() == address.lower():
                return tx
    except Exception as e:
        print(f"[ETH Error] {address}: {e}")
    return None

# ========= TRON (TRC20) =========
def get_latest_tron_tx(address: str):
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20?limit=5"
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        data = r.json().get("data", [])
        for tx in data:
            if (tx.get("to") or "").lower() == address.lower():
                return tx
    except Exception as e:
        print(f"[TRON Error] {address}: {e}")
    return None

# ========= BTC (FAST via mempool.space, fallback Blockchair) =========
def _btc_parse_incoming_from_txobj(txobj, target_addr):
    """
    Return (amount_btc, from_addr) for outputs paying target_addr in this tx object.
    Combines multiple vout to target_addr. from_addr = first vin prevout address not equal to target.
    """
    if not txobj:
        return 0.0, "unknown"

    # Sum all outputs paying to target
    amount_sats = 0
    for vout in txobj.get("vout", []):
        if vout.get("scriptpubkey_address") == target_addr:
            amount_sats += int(vout.get("value", 0))

    # Guess sender from first input
    from_addr = "unknown"
    for vin in txobj.get("vin", []):
        prev = vin.get("prevout") or {}
        a = prev.get("scriptpubkey_address")
        if a and a != target_addr:
            from_addr = a
            break

    return amount_sats / 1e8, from_addr

def get_btc_from_mempool(address: str):
    txs = []
    try:
        # mempool (unconfirmed first)
        url_m = f"{MEMPOOL_BASE}/address/{address}/txs/mempool"
        rm = SESSION.get(url_m, timeout=REQUEST_TIMEOUT)
        mempool_list = rm.json() if rm.ok else []
        for tx in mempool_list:
            amt, from_addr = _btc_parse_incoming_from_txobj(tx, address)
            if amt > 0:
                txs.append({
                    "hash": tx.get("txid"),
                    "from": from_addr,
                    "to": address,
                    "amount": amt,
                    "confirmed": False,
                    "time": int(time.time())
                })

        # latest confirmed page
        url_c = f"{MEMPOOL_BASE}/address/{address}/txs/chain"
        rc = SESSION.get(url_c, timeout=REQUEST_TIMEOUT)
        chain_list = rc.json() if rc.ok else []
        for tx in chain_list[:5]:  # limit a bit
            amt, from_addr = _btc_parse_incoming_from_txobj(tx, address)
            if amt > 0:
                # block_time may not exist if edge; derive from status
                status = tx.get("status", {})
                block_time = status.get("block_time") or int(time.time())
                txs.append({
                    "hash": tx.get("txid"),
                    "from": from_addr,
                    "to": address,
                    "amount": amt,
                    "confirmed": bool(status.get("confirmed", True)),
                    "time": block_time
                })
    except Exception as e:
        print(f"[BTC mempool Error] {address}: {e}")
    # Deduplicate by txid (prefer first seen)
    dedup = {}
    for t in txs:
        dedup.setdefault(t["hash"], t)
    # Sort newest first by time
    return sorted(dedup.values(), key=lambda x: x["time"], reverse=True)

def get_btc_from_blockchair(address: str):
    # Similar to your previous logic but optimized and with API key support
    params = f"?limit=5"
    if BLOCKCHAIR_KEY:
        params += f"&key={BLOCKCHAIR_KEY}"
    url = f"https://api.blockchair.com/bitcoin/dashboards/address/{address}{params}"
    out = []
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        data = r.json().get("data", {}).get(address, {})
        txs = data.get("transactions", [])[:5]
        if not txs:
            return []

        tx_ids = ",".join(txs)
        tx_params = ""
        if BLOCKCHAIR_KEY:
            tx_params = f"?key={BLOCKCHAIR_KEY}"
        tx_url = f"https://api.blockchair.com/bitcoin/dashboards/transactions/{tx_ids}{tx_params}"
        tr = SESSION.get(tx_url, timeout=REQUEST_TIMEOUT)
        raw = tr.json().get("data", {})

        for txid, info in raw.items():
            tx = info.get("transaction", {})
            outputs = info.get("outputs", [])
            amt_sats = sum(int(o.get("value", 0)) for o in outputs if o.get("recipient") == address)
            if amt_sats > 0:
                out.append({
                    "hash": txid,
                    "from": tx.get("sender") or "unknown",
                    "to": address,
                    "amount": amt_sats / 1e8,
                    "confirmed": tx.get("block_id", 0) > 0,
                    "time": tx.get("time") or int(time.time())
                })
    except Exception as e:
        print(f"[BTC Blockchair Error] {address}: {e}")
    return sorted(out, key=lambda x: x["time"], reverse=True)

def get_latest_btc_txs(address: str):
    if BTC_PROVIDER == "mempool":
        txs = get_btc_from_mempool(address)
        if txs:
            return txs
        # fallback
        return get_btc_from_blockchair(address)
    else:
        txs = get_btc_from_blockchair(address)
        if txs:
            return txs
        return get_btc_from_mempool(address)

# ========= MAIN LOOP =========
def main():
    last_seen = {}  # keys: eth_addr->hash, tron_addr->txid, f"{btc}_{txid}"->True
    print("✅ Bot started. Poll interval:", POLL_INTERVAL, "sec")

    while True:
        try:
            # cache prices once per loop
            eth_price = get_price("ETHUSDT")
            btc_price = get_price("BTCUSDT")

            futures = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                # ETH
                for eth, label in ETH_WALLETS.items():
                    futures.append(ex.submit(get_latest_eth_tx, eth))
                # BTC
                for btc, label in BTC_WALLETS.items():
                    futures.append(ex.submit(get_latest_btc_txs, btc))
                # TRON
                for tron, label in TRON_WALLETS.items():
                    futures.append(ex.submit(get_latest_tron_tx, tron))

                # Iterate results in completion order for speed
                idx_eth = 0
                idx_btc = len(ETH_WALLETS)
                idx_tron = idx_btc + len(BTC_WALLETS)

                # Map position to addr/label
                eth_items = list(ETH_WALLETS.items())
                btc_items = list(BTC_WALLETS.items())
                tron_items = list(TRON_WALLETS.items())

                pos = 0
                for f in as_completed(futures):
                    result = f.result()

                    # figure out which category this future belongs to
                    if pos < len(ETH_WALLETS):
                        # ETH
                        eth_addr, label = eth_items[pos]
                        tx = result
                        if tx and tx.get("hash") != last_seen.get(eth_addr):
                            try:
                                val_eth = int(tx["value"]) / 1e18
                            except Exception:
                                val_eth = float(tx.get("value", 0)) / 1e18
                            usd = val_eth * eth_price
                            if usd >= 2:
                                msg = (
                                    "🔔 ETH Incoming Transaction\n\n"
                                    f"🏷️ Wallet: {label}\n"
                                    f"💰 Amount: {val_eth:.6f} ETH\n"
                                    f"💵 USD Value: ${usd:,.2f}\n\n"
                                    f"📤 From: {tx.get('from')}\n"
                                    f"📥 To: {tx.get('to')}\n"
                                )
                                send_message(msg)
                            last_seen[eth_addr] = tx.get("hash")
                    elif pos < idx_tron:
                        # BTC
                        btc_index = pos - len(ETH_WALLETS)
                        btc_addr, label = btc_items[btc_index]
                        tx_list = result or []
                        for tx in tx_list:
                            tx_id = tx["hash"]
                            key = f"{btc_addr}_{tx_id}"
                            if key in last_seen:
                                continue
                            usd_val = tx["amount"] * btc_price
                            if usd_val >= 2:
                                state = "✅ Confirmed" if tx.get("confirmed") else "⏳ Unconfirmed"
                                msg = (
                                    "🔔 BTC Incoming Transaction\n\n"
                                    f"🏷️ Wallet: {label}\n"
                                    f"💰 Amount: {tx['amount']:.8f} BTC\n"
                                    f"💵 USD Value: ${usd_val:,.2f}\n"
                                    f"📊 Status: {state}\n\n"
                                    f"📤 From: {tx.get('from')}\n"
                                    f"📥 To: {tx.get('to')}\n"
                                )
                                send_message(msg)
                            last_seen[key] = True
                    else:
                        # TRON
                        tron_index = pos - len(ETH_WALLETS) - len(BTC_WALLETS)
                        tron_addr, label = tron_items[tron_index]
                        tx = result
                        if tx and tx.get("transaction_id") != last_seen.get(tron_addr):
                            try:
                                val = int(tx["value"]) / (10 ** int(tx["token_info"]["decimals"]))
                            except Exception:
                                val = float(tx.get("value", 0)) / (10 ** int(tx["token_info"]["decimals"]))
                            symbol = tx["token_info"]["symbol"]
                            if val > 0:
                                msg = (
                                    "🔔 TRC20 Incoming Transaction\n\n"
                                    f"🏷️ Wallet: {label}\n"
                                    f"💰 Amount: {val:.6f} {symbol}\n\n"
                                    f"📤 From: {tx.get('from')}\n"
                                    f"📥 To: {tx.get('to')}\n"
                                )
                                send_message(msg)
                            last_seen[tron_addr] = tx.get("transaction_id")

                    pos += 1

        except Exception as loop_err:
            print("[Loop Error]", loop_err)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
