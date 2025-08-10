# main.py
import os
import time
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter, Retry

# ================== REQUIRED ENVs ==================
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
if not all([TG_TOKEN, TG_CHAT_ID, ETHERSCAN_API_KEY]):
    raise ValueError("❌ Missing env: TELEGRAM_TOKEN, CHAT_ID, ETHERSCAN_API_KEY")

# ================== OPTIONAL ENVs ==================
POLL_INTERVAL       = int(os.getenv("POLL_INTERVAL", "5"))          # sec
PRICE_CACHE_TTL     = int(os.getenv("PRICE_CACHE_TTL", "30"))       # sec
REQUEST_TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "10"))       # sec
MAX_WORKERS         = int(os.getenv("MAX_WORKERS", "12"))
BTC_PROVIDER        = os.getenv("BTC_PROVIDER", "mempool").lower()  # mempool | blockchair
BLOCKCHAIR_KEY      = os.getenv("BLOCKCHAIR_KEY", "")
MEMPOOL_BASE        = os.getenv("MEMPOOL_BASE", "https://mempool.space/api")
TZ_NAME             = os.getenv("TZ_NAME", "Asia/Bangkok")
MIN_USD_THRESHOLD   = float(os.getenv("MIN_USD_THRESHOLD", "2"))
STATE_FILE          = os.getenv("STATE_FILE", "/data/last_seen.json")
SEEN_TTL_HOURS      = int(os.getenv("SEEN_TTL_HOURS", "168"))       # prune after 7d
BOOTSTRAP_ON_START  = os.getenv("BOOTSTRAP_ON_START", "1") == "1"
BTC_ONLY_LATEST     = os.getenv("BTC_ONLY_LATEST", "1") == "1"      # แจ้งเฉพาะรายการล่าสุด (initial)
DROP_BTC_BACKLOG    = os.getenv("DROP_BTC_BACKLOG", "1") == "1"     # มาร์ค backlog เป็น seen
BTC_CONFIRM_UPDATE  = os.getenv("BTC_CONFIRM_UPDATE", "1") == "1"   # ส่งอีกครั้งเมื่อคอนเฟิร์ม

# ================== TIMEZONE ==================
try:
    from zoneinfo import ZoneInfo  # Py3.9+
except Exception:
    ZoneInfo = None

def fmt_ts(ts_sec: int) -> str:
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

def short_tx(txid: str, left=10, right=8) -> str:
    if not txid or len(txid) <= left+right:
        return txid or ""
    return f"{txid[:left]}…{txid[-right:]}"

# -------- Price cache --------
_price_cache = {}
def get_price(symbol: str) -> float:
    now = time.time()
    if symbol in _price_cache:
        val, ts = _price_cache[symbol]
        if now - ts < PRICE_CACHE_TTL:
            return val
    try:
        r = SESSION.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        price = float(r.json()["price"])
        _price_cache[symbol] = (price, now)
        return price
    except Exception as e:
        print(f"[Price Error] {symbol}: {e}")
        return _price_cache.get(symbol, (0.0, 0))[0]

# ================== STATE (anti-duplicate + confirm-tracking) ==================
def _state_default():
    # btc_seen: key=f"{addr}:{txid}" -> {"first_ts":int,"last_ts":int,"notified":bool,"confirmed":bool}
    return {"eth_last":{}, "tron_last":{}, "btc_seen":{}, "updated_at": int(time.time())}

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            if not isinstance(s, dict): return _state_default()
            s.setdefault("eth_last", {}); s.setdefault("tron_last", {}); s.setdefault("btc_seen", {})
            # migrate old format (int timestamp) -> dict (treat as already confirmed+notified to avoid spam)
            for k, v in list(s["btc_seen"].items()):
                if isinstance(v, int):
                    s["btc_seen"][k] = {"first_ts": v, "last_ts": v, "notified": True, "confirmed": True}
            return s
    except Exception:
        return _state_default()

def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("[State Save Error]", e)

STATE = load_state()

def prune_state(state):
    cutoff = int(time.time() - SEEN_TTL_HOURS * 3600)
    newmap = {}
    for k, v in state["btc_seen"].items():
        try:
            keep_ts = v.get("last_ts") if isinstance(v, dict) else int(v)
        except Exception:
            keep_ts = 0
        if keep_ts >= cutoff:
            newmap[k] = v
    state["btc_seen"] = newmap
    state["updated_at"] = int(time.time())

def mark_eth(addr, txhash):
    if txhash:
        STATE["eth_last"][addr] = txhash
        prune_state(STATE); save_state(STATE)

def seen_eth(addr, txhash) -> bool:
    return STATE["eth_last"].get(addr) == txhash

def mark_tron(addr, txid):
    if txid:
        STATE["tron_last"][addr] = txid
        prune_state(STATE); save_state(STATE)

def seen_tron(addr, txid) -> bool:
    return STATE["tron_last"].get(addr) == txid

def _btc_key(addr, txid): return f"{addr}:{txid}"

def get_btc_rec(addr, txid):
    return STATE["btc_seen"].get(_btc_key(addr, txid))

def mark_btc(addr, txid, ts=None, notified=None, confirmed=None):
    key = _btc_key(addr, txid)
    rec = STATE["btc_seen"].get(key) or {"first_ts": int(ts or time.time()), "last_ts": int(ts or time.time()), "notified": False, "confirmed": False}
    if ts is not None:
        rec["last_ts"] = int(ts)
        rec.setdefault("first_ts", int(ts))
    if notified is not None:
        rec["notified"] = bool(notified)
    if confirmed is not None:
        rec["confirmed"] = bool(confirmed)
    STATE["btc_seen"][key] = rec
    prune_state(STATE); save_state(STATE)

def seen_btc(addr, txid) -> bool:
    return _btc_key(addr, txid) in STATE["btc_seen"]

# ================== ETH (Etherscan) ==================
def get_latest_eth_tx(address: str):
    url = ("https://api.etherscan.io/api"
           f"?module=account&action=txlist&address={address}"
           "&page=1&offset=10&sort=desc"
           f"&apikey={ETHERSCAN_API_KEY}")
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        data = r.json().get("result", [])
        for tx in data:
            if (tx.get("to") or "").lower() == address.lower():
                tx["_ts"] = int(tx.get("timeStamp", time.time()))
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
                ts_ms = tx.get("block_timestamp")
                tx["_ts"] = int(ts_ms/1000) if ts_ms else int(time.time())
                return tx
    except Exception as e:
        print(f"[TRON Error] {address}: {e}")
    return None

# ================== BTC (mempool + fallback blockchair) ==================
def _btc_sum_to_addr_and_sender(txobj, target_addr):
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
            sender = a; break
    return amount_sats / 1e8, sender

def get_btc_from_mempool(address: str):
    txs = []
    try:
        rm = SESSION.get(f"{MEMPOOL_BASE}/address/{address}/txs/mempool", timeout=REQUEST_TIMEOUT)
        mempool_list = rm.json() if rm.ok else []
        now = int(time.time())
        for tx in mempool_list:
            amt, sender = _btc_sum_to_addr_and_sender(tx, address)
            if amt > 0:
                txs.append({"hash": tx.get("txid"), "from": sender, "to": address,
                            "amount": amt, "confirmed": False, "time": now})
        rc = SESSION.get(f"{MEMPOOL_BASE}/address/{address}/txs/chain", timeout=REQUEST_TIMEOUT)
        chain_list = rc.json() if rc.ok else []
        for tx in chain_list[:5]:
            amt, sender = _btc_sum_to_addr_and_sender(tx, address)
            if amt > 0:
                status = tx.get("status", {})
                block_time = status.get("block_time") or int(time.time())
                txs.append({"hash": tx.get("txid"), "from": sender, "to": address,
                            "amount": amt, "confirmed": bool(status.get("confirmed", True)),
                            "time": int(block_time)})
    except Exception as e:
        print(f"[BTC mempool Error] {address}: {e}")
    dedup = {}
    for t in txs:
        dedup.setdefault(t["hash"], t)
    return sorted(dedup.values(), key=lambda x: x["time"], reverse=True)

def get_btc_from_blockchair(address: str):
    params = "?limit=5"
    if BLOCKCHAIR_KEY: params += f"&key={BLOCKCHAIR_KEY}"
    url = f"https://api.blockchair.com/bitcoin/dashboards/address/{address}{params}"
    out = []
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        data = r.json().get("data", {}).get(address, {})
        txs = (data.get("transactions") or [])[:5]
        if not txs: return []
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
                t_raw = tx.get("time")
                try:
                    if isinstance(t_raw, str):
                        t_sec = int(datetime.strptime(t_raw, "%Y-%m-%d %H:%M:%S")
                                    .replace(tzinfo=timezone.utc).timestamp())
                    else:
                        t_sec = int(t_raw or time.time())
                except Exception:
                    t_sec = int(time.time())
                out.append({"hash": txid, "from": tx.get("sender") or "unknown", "to": address,
                            "amount": amt_sats / 1e8, "confirmed": tx.get("block_id", 0) > 0,
                            "time": t_sec})
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

# ================== BOOTSTRAP ==================
def bootstrap_mark_latest():
    if not BOOTSTRAP_ON_START:
        return
    print("⏳ Bootstrapping state...")
    for addr in ETH_WALLETS.keys():
        tx = get_latest_eth_tx(addr)
        if tx: mark_eth(addr, tx.get("hash"))
    for addr in BTC_WALLETS.keys():
        txs = get_latest_btc_txs(addr) or []
        if txs:
            latest = txs[0]
            mark_btc(addr, latest["hash"], ts=latest.get("time"), notified=False, confirmed=bool(latest.get("confirmed")))
    for addr in TRON_WALLETS.keys():
        tx = get_latest_tron_tx(addr)
        if tx: mark_tron(addr, tx.get("transaction_id"))
    print("✅ Bootstrap done.")

# ================== MAIN LOOP ==================
def main():
    print(f"✅ Bot started. Poll: {POLL_INTERVAL}s | TZ: {TZ_NAME} | BTC: {BTC_PROVIDER} | OnlyLatest(BTC)={BTC_ONLY_LATEST} | ConfirmUpdate={BTC_CONFIRM_UPDATE}")
    bootstrap_mark_latest()

    while True:
        try:
            eth_price = get_price("ETHUSDT")
            btc_price = get_price("BTCUSDT")

            futures = {}
            ex = ThreadPoolExecutor(max_workers=MAX_WORKERS)
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
                    if not tx: continue
                    txhash = tx.get("hash")
                    if seen_eth(addr, txhash):
                        continue
                    try:
                        val_eth = int(tx["value"]) / 1e18
                    except Exception:
                        val_eth = float(tx.get("value", 0)) / 1e18
                    usd = val_eth * eth_price
                    if usd >= MIN_USD_THRESHOLD:
                        when = fmt_ts(tx.get("_ts", int(time.time())))
                        msg = ("🔔 ETH Incoming Transaction\n\n"
                               f"🏷️ Wallet: {label}\n"
                               f"💰 Amount: {val_eth:.6f} ETH\n"
                               f"💵 USD Value: ${usd:,.2f}\n"
                               f"🕒 Time: {when}\n\n"
                               f"📤 From: {tx.get('from')}\n"
                               f"📥 To: {tx.get('to')}\n")
                        send_message(msg)
                    mark_eth(addr, txhash)

                elif chain == "BTC":
                    txs = result or []
                    if not txs: continue

                    # 1) Initial alert (only latest per address if enabled)
                    if BTC_ONLY_LATEST:
                        newest = txs[0]
                        txid = newest["hash"]
                        rec = get_btc_rec(addr, txid)
                        if not rec or not rec.get("notified", False):
                            usd_val = newest["amount"] * btc_price
                            if usd_val >= MIN_USD_THRESHOLD:
                                state = "✅ Confirmed" if newest.get("confirmed") else "⏳ Unconfirmed"
                                when = fmt_ts(int(newest["time"]))
                                msg = ("🔔 BTC Incoming Transaction\n\n"
                                       f"🏷️ Wallet: {label}\n"
                                       f"🔗 TX: {short_tx(txid)}\n"
                                       f"💰 Amount: {newest['amount']:.8f} BTC\n"
                                       f"💵 USD Value: ${usd_val:,.2f}\n"
                                       f"📊 Status: {state}\n"
                                       f"🕒 Time: {when}\n\n"
                                       f"📤 From: {newest.get('from')}\n"
                                       f"📥 To: {newest.get('to')}\n")
                                send_message(msg)
                            mark_btc(addr, txid, ts=newest.get("time"), notified=True,
                                     confirmed=bool(newest.get("confirmed")))
                        # mark backlog as seen (ไม่แจ้งตามเก็บ)
                        if DROP_BTC_BACKLOG and len(txs) > 1:
                            for old in txs[1:]:
                                mark_btc(addr, old["hash"], ts=old.get("time"),
                                         notified=False, confirmed=bool(old.get("confirmed")))
                    else:
                        # แจ้งทุก TX ที่ยังไม่เคยแจ้ง
                        for tx in txs:
                            txid = tx["hash"]
                            rec = get_btc_rec(addr, txid)
                            if rec and rec.get("notified", False):
                                continue
                            usd_val = tx["amount"] * btc_price
                            if usd_val >= MIN_USD_THRESHOLD:
                                state = "✅ Confirmed" if tx.get("confirmed") else "⏳ Unconfirmed"
                                when = fmt_ts(int(tx["time"]))
                                msg = ("🔔 BTC Incoming Transaction\n\n"
                                       f"🏷️ Wallet: {label}\n"
                                       f"🔗 TX: {short_tx(txid)}\n"
                                       f"💰 Amount: {tx['amount']:.8f} BTC\n"
                                       f"💵 USD Value: ${usd_val:,.2f}\n"
                                       f"📊 Status: {state}\n"
                                       f"🕒 Time: {when}\n\n"
                                       f"📤 From: {tx.get('from')}\n"
                                       f"📥 To: {tx.get('to')}\n")
                                send_message(msg)
                            mark_btc(addr, txid, ts=tx.get("time"), notified=True,
                                     confirmed=bool(tx.get("confirmed")))

                    # 2) Confirmation updates (scan a few recent TXs to catch status changes)
                    if BTC_CONFIRM_UPDATE:
                        for tx in txs[:5]:
                            txid = tx["hash"]
                            rec = get_btc_rec(addr, txid)
                            if not rec:  # ไม่เคยเห็น ไม่ต้องอัปเดต
                                continue
                            if rec.get("notified", False) and not rec.get("confirmed", False) and tx.get("confirmed"):
                                # เปลี่ยนสถานะ unconfirmed -> confirmed
                                when = fmt_ts(int(tx["time"]))
                                usd_val = tx["amount"] * btc_price
                                msg = ("🔔 BTC Transaction Confirmed\n\n"
                                       f"🏷️ Wallet: {label}\n"
                                       f"🔗 TX: {short_tx(txid)}\n"
                                       f"💰 Amount: {tx['amount']:.8f} BTC\n"
                                       f"💵 USD Value: ${usd_val:,.2f}\n"
                                       f"📊 Status: ✅ Confirmed\n"
                                       f"🕒 Time: {when}\n")
                                send_message(msg)
                                mark_btc(addr, txid, ts=tx.get("time"), confirmed=True)

                else:  # TRON
                    tx = result
                    if not tx: continue
                    txid = tx.get("transaction_id")
                    if seen_tron(addr, txid):
                        continue
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
                        msg = ("🔔 TRC20 Incoming Transaction\n\n"
                               f"🏷️ Wallet: {label}\n"
                               f"💰 Amount: {val:.6f} {symbol}\n"
                               f"🕒 Time: {when}\n\n"
                               f"📤 From: {tx.get('from')}\n"
                               f"📥 To: {tx.get('to')}\n")
                        send_message(msg)
                    mark_tron(addr, txid)

            ex.shutdown(wait=False)

        except Exception as loop_err:
            print("[Loop Error]", loop_err)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
