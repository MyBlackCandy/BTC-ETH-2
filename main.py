
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
    url = f"https://api.blockchair.com/bitcoin/dashboards/address/{address}"
    try:
        r = requests.get(url).json()
        txs = r["data"][address]["transactions"]
        for tx_hash in txs:
            tx_url = f"https://api.blockchair.com/bitcoin/raw/transaction/{tx_hash}"
            tx_data = requests.get(tx_url).json()
            tx = tx_data["data"][tx_hash]["decoded_raw_transaction"]
            for out in tx["vout"]:
                if "scriptpubkey_address" in out and out["scriptpubkey_address"] == address:
                    return {
                        "hash": tx_hash,
                        "from": tx["vin"][0]["prevout"]["scriptpubkey_address"] if tx["vin"] else "unknown",
                        "to": address,
                        "amount": out["value"] / 1e8
                    }
    except:
        pass
    return None

def main():
    seen_hashes = set()
    while True:
        eth_price = get_price("ETHUSDT")
        btc_price = get_price("BTCUSDT")

        for eth, label in ETH_WALLETS.items():
            tx = get_latest_eth_tx(eth)
            if tx and tx["hash"] not in seen_hashes:
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
                seen_hashes.add(tx["hash"])

        for btc, label in BTC_WALLETS.items():
            tx = get_latest_btc_tx(btc)
            if tx and tx["hash"] not in seen_hashes:
                usd_val = tx["amount"] * btc_price
                if usd_val >= 2:
                    msg = f"""🔔 BTC Incoming Transaction

🏷️ Wallet: {label}
💰 Amount: {tx["amount"]:.8f} BTC
💵 USD Value: ${usd_val:,.2f}

📤 From: {tx["from"]}
📥 To: {tx["to"]}
"""
                    send_message(msg)
                seen_hashes.add(tx["hash"])

        for tron, label in TRON_WALLETS.items():
            tx = get_latest_tron_tx(tron)
            if tx and tx["transaction_id"] not in seen_hashes:
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
                seen_hashes.add(tx["transaction_id"])

        time.sleep(10)

if __name__ == "__main__":
    main()
