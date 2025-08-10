# main.py
import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter, Retry

# ================== REQUIRED ENVs ==================
# TELEGRAM_TOKEN, CHAT_ID, ETHERSCAN_API_KEY
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
if not all([TG_TOKEN, TG_CHAT_ID, ETHERSCAN_API_KEY]):
    raise ValueError("❌ Missing env: TELEGRAM_TOKEN, CHAT_ID, ETHERSCAN_API_KEY")

# ================== OPTIONAL ENVs ==================
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "5"))       # seconds
PRICE_CACHE_TTL   = int(os.getenv("PRICE_CACHE_TTL", "30"))    # seconds
REQUEST_TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT", "10"))    # seconds
MAX_WORKERS       = int(os.getenv("MAX_WORKERS", "12"))
BTC_PROVIDER      = os.getenv("BTC_PROVIDER", "mempool").lower()  # mempool | blockchair
BLOCKCHAIR_KEY    = os.getenv("BLOCKCHAIR_KEY", "")            # optional (fallback)
MEMPOOL_BASE      = os.getenv("MEMPOOL_BASE", "https://mempool.space/api")
TZ_NAME           = os.getenv("TZ_NAME", "Asia/Bangkok")
MIN_USD_THRESHOLD = float(os.getenv("MIN_USD_THRESHOLD", "2")) # ลิมิตขั้นต่ำ USD

# ================== TIMEZONE ==================
try:
    from zoneinfo import ZoneInfo  # Py3.9+
except Exception:
    ZoneInfo = None

def fmt_ts(ts_sec: int) -> str:
    """epoch seconds -> formatted local time"""
    try:
        tz = ZoneInfo(TZ_NAME) if ZoneInfo else timezone.utc
    except Exception:
        tz = timezone.utc
    dt = datetime.fromtimestamp(int(ts_sec), tz=timezone.utc).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

# ================== SESSION (Keep-Alive + Retries) ==================
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

# ================== UTILS ==================
def parse_wallets(env_str: str):
    wallets = {}
    for item in env_str.split(","):
        if ":" in item:
            addr, label = item.strip().split(":", 1)
            wallets[addr.strip()] = label.strip()
    return wallets

ETH_WALLETS  = parse_wallets(os.getenv("ETH_LABELS", ""))
BTC_WALLETS  = parse_wallets(os.getenv("BTC_LABELS", ""))
TRON_WALLETS = parse_wallets(os.getenv("TRON_LABELS", ""))

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

# ================== ETH (Etherscan) ==================
def get_latest_eth_tx(address: str):
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
            if (tx.get("to") or "").lower() == address.lower():
                # Normalize timestamp to int seconds
                ts = int(tx.get("timeStamp", time.time()))
                tx["_ts"] = ts
                return tx
    except Exception as e:
        print(f"[ETH Error] {address}: {e}")
    return None

# ================== TRON (TRC20, Trongrid) ==================
def get_latest_tron_tx(address: str):
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20?limit=5"
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        txs = r.json().get("data", [])
        for tx in txs:
            if (tx.get("to") or "").lower() == address.lower():
                # Normalize timestamp to int seconds
                ts_ms = tx.get("block_timestamp")
                tx["_ts"] = int(ts_ms/1000) if ts_ms else int(time.time())
                return tx
    except Exception as e:
        print(f"[TRON Error] {address}: {e}")
    return None

# ================== BTC FAST (mempool.space) ==================
def _btc_sum_to_addr_and_sender(txobj, target_addr):
    """Sum outputs paying target_addr and guess sender from first vin."""
    if not txobj:
        return 0.0, "unknown"
    amount_sats = 0
    for vout in txobj.get("vout", []):
        if vout.get("scriptpubkey_address") == target_addr:
            amount_sats += int(vout.get("value", 0))
    sender = "unknown"
    for vin in txobj.get("vin", []):
        prev = vin.get("prevout") or {}
        a = prev.get("scriptpubkey_address")
        if a and a != target_addr:
            sender = a
            break
    return amount_sats / 1e8, sender

def get_btc_from_mempool(address: str):
    txs = []
    try:
        # Unconfirmed
        rm = SESSION.get(f"{MEMPOOL_BASE}/address/{address}/txs/mempool", timeout=REQUEST_TIMEOUT)
        mempool_list = rm.json() if rm.ok else []
        now = int(time.time())
        for tx in mempool_list:
            amt, sender = _btc_sum_to_addr_and_sender(tx, address)
            if amt > 0:
                txs.append({
                    "hash": tx.get("txid"),
                    "from": sender,
                    "to": address,
                    "amount": amt,
                    "confirmed": False,
                    "time": now,  # no block_time for unconfirmed
                })

        # Recent confirmed
        rc = SESSION.get(f"{MEMPOOL_BASE}/address/{address}/txs/chain", timeout=REQUEST_TIMEOUT)
        chain_list = rc.json() if rc.ok else []
        for tx in chain_list[:5]:
            amt, sender = _btc_sum_to_addr_and_sender(tx, address)
            if amt > 0:
                status = tx.get("status", {})
                block_time = status.get("block_time") or int(time.time())
                txs.append({
                    "hash": tx.get("txid"),
                    "from": sender,
                    "to": address,
                    "amount": amt,
                    "confirmed": bool(status.get("confirmed", True)),
                    "time": int(block_time),
                })
    except Exception as e:
        print(f"[BTC mempool Error] {address}: {e}")

    # Dedup by txid (prefer first)
    dedup = {}
    for t in txs:
        dedup.setdefault(t["hash"], t)
    return sorted(dedup.values(), key=lambda x: x["time"], reverse=True)

# ================== BTC Fallback (Blockchair) ==================
def get_btc_from_blockchair(address: str):
    params = "?limit=5"
    if BLOCKCHAIR_KEY:
        params += f"&key={BLOCKCHAIR_KEY}"
    url = f"https://api.blockchair.com/bitcoin/dashboards/address/{address}{params}"
    out = []
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        data = r.json().get("data", {}).get(address, {})
        txs = (data.get("transactions") or [])[:5]
        if not txs:
            return []

        tx_ids = ",".join(txs)
        tx_params = f"?key={BLOCKCHAIR_KEY}" if BLOCKCHAIR_KEY else ""
        tx_url = f"https://api.blockchair.com/bitcoin/dashboards/transactions/{tx_ids}{tx_params}"
        tr = SESSION.get(tx_url, timeout=REQUEST_TIMEOUT)
        raw = tr.json().get("data", {})

        for txid, info in raw.items():
            tx = info.get("transaction", {})
            outputs = info.get("outputs", [])
            amt_sats = sum(int(o.get("value", 0)) for o in outputs if o.get("recipient") == address)
            if amt_sats > 0:
                # time string like "2024-06-01 12:34:56" (UTC)
                t_raw = tx.get("time")
                try:
                    if isinstance(t_raw, str):
                        t_sec = int(datetime.strptime(t_raw, "%Y-%m-%d %H:%M:%S")
                                    .replace(tzinfo=timezone.utc).timestamp())
                    else:
                        t_sec = int(t_raw or time.time())
                except Exception:
                    t_sec = int(time.time())
                out.append({
                    "hash": txid,
                    "from": tx.get("sender") or "unknown",
                    "to": address,
                    "amount": amt_sats / 1e8,
                    "confirmed": tx.get("block_id", 0) > 0,
                    "time": t_sec,
                })
    except Exception as e:
        print(f"[BTC Blockchair Error] {address}: {e}")
    return sorted(out, key=lambda x: x["time"], reverse=True)

def get_latest_btc_txs(address: str):
    if BTC_PROVIDER == "mempool":
        txs = get_btc_from_mempool(address)
        return txs if txs else get_btc_from_blockchair(address)
    else:
        txs = get_btc_from_blockchair(address)
        return txs if txs else get_btc_from_mempool(address)

# ================== MAIN LOOP ==================
def main():
    last_seen = {}  # eth_addr->hash, tron_addr->txid, f"{btc}_{txid}"->True
    print(f"✅ Bot started. Poll interval: {POLL_INTERVAL}s | TZ: {TZ_NAME} | BTC: {BTC_PROVIDER}")

    while True:
        try:
            eth_price = get_price("ETHUSDT")
            btc_price = get_price("BTCUSDT")

            futures = {}
            ex = ThreadPoolExecutor(max_workers=MAX_WORKERS)

            # Queue jobs and remember (chain, address, label)
            for addr, label in ETH_WALLETS.items():
                futures[ex.submit(get_latest_eth_tx, addr)] = ("ETH", addr, label)
            for addr, label in BTC_WALLETS.items():
                futures[ex.submit(get_latest_btc_txs, addr)] = ("BTC", addr, label)
            for addr, label in TRON_WALLETS.items():
                futures[ex.submit(get_latest_tron_tx, addr)] = ("TRON", addr, label)

            for f in as_completed(futures):
                chain, addr, label = futures[f]
                try:
                    result = f.result()
                except Exception as e:
                    print(f"[Future Error] {chain} {addr}: {e}")
                    continue

                if chain == "ETH":
                    tx = result
                    if tx and tx.get("hash") != last_seen.get(addr):
                        # value can be str
                        try:
                            val_eth = int(tx["value"]) / 1e18
                        except Exception:
                            val_eth = float(tx.get("value", 0)) / 1e18
                        usd = val_eth * eth_price
                        if usd >= MIN_USD_THRESHOLD:
                            when = fmt_ts(tx.get("_ts", int(time.time())))
                            msg = (
                                "🔔 ETH Incoming Transaction\n\n"
                                f"🏷️ Wallet: {label}\n"
                                f"💰 Amount: {val_eth:.6f} ETH\n"
                                f"💵 USD Value: ${usd:,.2f}\n"
                                f"🕒 Time: {when}\n\n"
                                f"📤 From: {tx.get('from')}\n"
                                f"📥 To: {tx.get('to')}\n"
                            )
                            send_message(msg)
                        last_seen[addr] = tx.get("hash")

                elif chain == "BTC":
                    tx_list = result or []
                    for tx in tx_list:
                        key = f"{addr}_{tx['hash']}"
                        if key in last_seen:
                            continue
                        usd_val = tx["amount"] * btc_price
                        if usd_val >= MIN_USD_THRESHOLD:
                            state = "✅ Confirmed" if tx.get("confirmed") else "⏳ Unconfirmed"
                            when = fmt_ts(int(tx["time"]))
                            msg = (
                                "🔔 BTC Incoming Transaction\n\n"
                                f"🏷️ Wallet: {label}\n"
                                f"💰 Amount: {tx['amount']:.8f} BTC\n"
                                f"💵 USD Value: ${usd_val:,.2f}\n"
                                f"📊 Status: {state}\n"
                                f"🕒 Time: {when}\n\n"
                                f"📤 From: {tx.get('from')}\n"
                                f"📥 To: {tx.get('to')}\n"
                            )
                            send_message(msg)
                        last_seen[key] = True

                else:  # TRON
                    tx = result
                    if tx and tx.get("transaction_id") != last_seen.get(addr):
                        try:
                            decimals = int(tx["token_info"]["decimals"])
                        except Exception:
                            decimals = int(tx.get("tokenInfo", {}).get("decimals", 6) or 6)
                        try:
                            raw_val = int(tx["value"])
                        except Exception:
                            raw_val = int(float(tx.get("value", 0)))
                        val = raw_val / (10 ** decimals)
                        symbol = tx.get("token_info", {}).get("symbol") or tx.get("tokenInfo", {}).get("symbol", "TRC20")
                        if val > 0:
                            when = fmt_ts(tx.get("_ts", int(time.time())))
                            msg = (
                                "🔔 TRC20 Incoming Transaction\n\n"
                                f"🏷️ Wallet: {label}\n"
                                f"💰 Amount: {val:.6f} {symbol}\n"
                                f"🕒 Time: {when}\n\n"
                                f"📤 From: {tx.get('from')}\n"
                                f"📥 To: {tx.get('to')}\n"
                            )
                            send_message(msg)
                        last_seen[addr] = tx.get("transaction_id")

            ex.shutdown(wait=False)

        except Exception as loop_err:
            print("[Loop Error]", loop_err)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
