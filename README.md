# XAUUSD Gold Signal Bot (GitHub Actions -> Telegram)

Bot membaca OHLC M30/H1 dari Yahoo Finance (GC=F, proxy XAUUSD, tanpa API key),
memverifikasi dengan spot real-time (gold-api.com + PAXG Coinbase, dengan offset),
menggabungkan 4 strategi dengan voting ensemble, lalu mengirim sinyal
BUY/SELL + SL/TP ke Telegram untuk eksekusi MANUAL di MT5.

## Cara pasang (5 menit)
1. Buat repo GitHub baru, upload `main.py` + folder `.github/workflows/`.
2. Di repo: **Settings > Secrets and variables > Actions > New repository secret**:
   - `TELEGRAM_TOKEN`  : token dari @BotFather
   - `TELEGRAM_CHAT_ID`: chat id Anda (dapat dari @userinfobot)
3. Commit. Workflow jalan otomatis tiap jam 09 & 39 menit UTC (killzone saja yang lolos filter).
4. Sesuaikan `SPOT_OFFSETS` di `main.py` agar harga spot = harga MT5 broker Anda.
   Rumus: offset = Harga MT5 - Harga publik.

## Strategi (voting ensemble, minimal skor 3 dari kemungkinan 4)
- SMC Liquidity Sweep + reversal
- Multi-TF MSB (bias H1 EMA50 vs trigger M30 EMA20)
- Mean Reversion Z-Score + RSI
- Bollinger Squeeze + ATR Expansion
Difilter: regime volatilitas ATR, killzone London/NY, deviasi spot, cooldown antar sinyal.

## Anti-spam / anti-loop / anti-blokir
- Cooldown 90 menit searah, tersimpan via GitHub Actions cache (persist antar run).
- Retry Telegram dengan backoff (anti 429).
- Workflow timeout 8 menit + concurrency group (tidak overlap).
- Cron di menit acak (09/39), bukan menit 0 -> lebih natural.

## Disclaimer
Sinyal untuk eksekusi manual. Backtest dulu & uji di demo. Tidak ada jaminan profit.
