import os
import requests
from telegram import Bot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

BTC_LABELS = os.getenv("BTC_LABELS")
ETH_LABELS = os.getenv("ETH_LABELS")

btc_wallets = [{"type": "BTC", "address": a, "name": n} for a, n in (w.split(":") for w in BTC_LABELS.split(","))]
eth_wallets = [{"type": "ETH", "address": a, "name": n} for a, n in (w.split(":") for w in ETH_LABELS.split(","))]
wallets = btc_wallets + eth_wallets

bot = Bot(token=TELEGRAM_TOKEN)

def get_prices():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    r = requests.get(url).json()
    return {
        "BTC": r['bitcoin']['usd'],
        "ETH": r['ethereum']['usd']
    }

def check_btc(wallet, btc_price):
    url = f'https://api.blockcypher.com/v1/btc/main/addrs/{wallet["address"]}'
    r = requests.get(url).json()
    txs = r.get('txrefs', [])
    for tx in txs:
        if tx.get('tx_output_n', -1) >= 0:
            amount = tx['value'] / 1e8
            usd = amount * btc_price
            if usd < 2: continue
            msg = f"""🔔 BTC Incoming Transaction

🏷️ Wallet: {wallet["name"]}
💰 Amount: {amount:.8f} BTC
💵 USD Value: ${usd:,.2f}

📥 To: {wallet["address"]}
"""
            bot.send_message(chat_id=CHAT_ID, text=msg)
            break

def check_eth(wallet, eth_price):
    url = f'https://api.etherscan.io/api?module=account&action=txlist&address={wallet["address"]}&sort=desc&apikey={ETHERSCAN_API_KEY}'
    r = requests.get(url).json()
    txs = r.get("result", [])
    for tx in txs:
        if tx['to'].lower() == wallet['address'].lower():
            amount = int(tx['value']) / 1e18
            usd = amount * eth_price
            if usd < 2: continue
            msg = f"""🔔 ETH Incoming Transaction

🏷️ Wallet: {wallet["name"]}
💰 Amount: {amount:.6f} ETH
💵 USD Value: ${usd:,.2f}

📤 From: {tx['from']}
📥 To: {tx['to']}
"""
            bot.send_message(chat_id=CHAT_ID, text=msg)
            break

def run():
    prices = get_prices()
    for w in wallets:
        if w['type'] == 'BTC':
            check_btc(w, prices["BTC"])
        elif w['type'] == 'ETH':
            check_eth(w, prices["ETH"])

if __name__ == "__main__":
    run()
