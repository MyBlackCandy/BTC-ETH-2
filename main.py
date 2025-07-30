import os
import time
import requests

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

if not all([TG_TOKEN, TG_CHAT_ID, ETHERSCAN_API_KEY]):
    raise ValueError("❌ Environment variables missing: TELEGRAM_TOKEN, CHAT_ID, ETHERSCAN_API_KEY")

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

def send_message(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram Error:", e)

def get_price(symbol):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}").json()
        return float(r["price"])
    except:
        return 0

def get_latest_eth_tx(address):
    url = f"https://api.etherscan.io/api?module=account&action=txlist&address={address}&sort=desc&apikey={ETHERSCAN_API_KEY}"
    try:
        r = requests.get(url).json()
        txs = r.get("result", [])
        for tx in txs:
            if tx["to"].lower() == address.lower():
                return tx
    except:
        pass
    return None

def get_latest_tron_tx(address):
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20?limit=5"
    try:
        r = requests.get(url).json()
        txs = r.get("data", [])
        for tx in txs:
            if tx["to"].lower() == address.lower():
                return tx
    except:
        pass
    return None

def get_latest_btc_tx(address):
    url = f"https://api.blockchair.com/bitcoin/dashboards/address/{address}?limit=5"
    try:
        r = requests.get(url).json()
        data = r.get("data", {}).get(address, {})
        txs = data.get("transactions", [])
        raw_tx_map = {}

        if txs:
            tx_ids = ",".join(txs[:3])
            tx_url = f"https://api.blockchair.com/bitcoin/dashboards/transactions/{tx_ids}"
            tx_resp = requests.get(tx_url).json()
            raw_tx_map = tx_resp.get("data", {})

        tx_list = []
        for txid, info in raw_tx_map.items():
            tx = info.get("transaction", {})
            outputs = info.get("outputs", [])
            for out in outputs:
                if out.get("recipient") == address:
                    amount_btc = out.get("value", 0) / 1e8
                    tx_list.append({
                        "hash": txid,
                        "from": tx.get("sender") or "unknown",
                        "to": address,
                        "amount": amount_btc,
                        "time": tx.get("time"),
                        "block_id": tx.get("block_id", 0)
                    })
        return sorted(tx_list, key=lambda x: x["time"], reverse=True)
    except Exception as e:
        print(f"[BTC Error] {e}")
        return []

def main():
    last_seen = {}
    while True:
        eth_price = get_price("ETHUSDT")
        btc_price = get_price("BTCUSDT")

        for eth, label in ETH_WALLETS.items():
            tx = get_latest_eth_tx(eth)
            if tx and tx["hash"] != last_seen.get(eth):
                value_eth = int(tx["value"]) / 1e18
                usd = value_eth * eth_price
                if usd >= 2:
                    msg = f"""🔔 ETH Incoming Transaction

🏷️ Wallet: {label}
💰 Amount: {value_eth:.6f} ETH
💵 USD Value: ${usd:,.2f}

📤 From: {tx['from']}
📥 To: {tx['to']}
"""
                    send_message(msg)
                last_seen[eth] = tx["hash"]

        for btc, label in BTC_WALLETS.items():
            tx_list = get_latest_btc_tx(btc)
            for tx in tx_list:
                tx_id = tx["hash"]
                key = f"{btc}_{tx_id}"
                if key in last_seen:
                    continue
                usd_val = tx["amount"] * btc_price
                if usd_val >= 2:
                    msg = f"""🔔 BTC Incoming Transaction

🏷️ Wallet: {label}
💰 Amount: {tx['amount']:.8f} BTC
💵 USD Value: ${usd_val:,.2f}

📤 From: {tx['from']}
📥 To: {tx['to']}
"""
                    send_message(msg)
                last_seen[key] = True

        for tron, label in TRON_WALLETS.items():
            tx = get_latest_tron_tx(tron)
            if tx and tx["transaction_id"] != last_seen.get(tron):
                val = int(tx["value"]) / (10**int(tx["token_info"]["decimals"]))
                symbol = tx["token_info"]["symbol"]
                if val > 0:
                    msg = f"""🔔 TRC20 Incoming Transaction

🏷️ Wallet: {label}
💰 Amount: {val:.6f} {symbol}

📤 From: {tx['from']}
📥 To: {tx['to']}
"""
                    send_message(msg)
                last_seen[tron] = tx["transaction_id"]

        time.sleep(10)

if __name__ == "__main__":
    main()
