#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Main Helper Module untuk TSMOM GOLD.
Menangani Logging, Telegram, dan Konfigurasi Global.
"""
import os
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

# Muat variabel dari file .env
load_dotenv()

# Konfigurasi Logging
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TSMOM_Helper")

# Headers standar untuk menghindari blokir sederhana dari Yahoo Finance
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

def log(message: str):
    """Wrapper untuk logging."""
    logger.info(message)

def send_telegram(text: str):
    """Mengirim pesan ke Telegram dengan penanganan error yang aman."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or token == "dummy" or not chat_id or chat_id == "dummy":
        logger.warning("Telegram tidak dikonfigurasi di .env. Pesan hanya dicatat di log.")
        logger.info(f"TELEGRAM MSG DROPPED:\n{text}")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.debug("Pesan Telegram berhasil dikirim.")
        else:
            logger.error(f"Gagal kirim Telegram: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Exception saat mengirim Telegram: {e}")

if __name__ == "__main__":
    # Tes modul
    log("Modul main.py berhasil dimuat.")
    send_telegram("🟢 <b>Tes Sistem</b>\nModul helper berfungsi dengan baik.")
