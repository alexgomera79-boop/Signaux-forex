"""Signaux d'entrée forex.

Stratégie de départ (H1) :
  - ACHAT : l'EMA20 croise l'EMA50 vers le haut ET le RSI(14) est entre 50 et 70
  - VENTE : l'EMA20 croise l'EMA50 vers le bas  ET le RSI(14) est entre 30 et 50
  - Stop-loss = 1,5 x ATR(14) ; objectif = 3 x ATR(14)  (ratio 1:2)

Usage :
  python signals.py          # analyse et envoie les signaux sur Telegram
  python signals.py --test   # envoie seulement un message de test
  python signals.py --dry    # analyse et affiche, sans envoyer
"""

import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

# ----------------------------- RÉGLAGES ------------------------------------
# nom affiché -> symbole Yahoo Finance
PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "XAU/USD (Or)": "GC=F",
}
INTERVAL = "1h"
PERIOD = "30d"
EMA_FAST, EMA_SLOW = 20, 50
RSI_PERIOD = 14
ATR_PERIOD = 14
SL_ATR, TP_ATR = 1.5, 3.0
MAX_SIGNAL_AGE_MIN = 59   # un signal n'est envoyé que si sa bougie s'est fermée il y a moins de 59 min
# ---------------------------------------------------------------------------


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["Close"].shift()
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ema(df["Close"], EMA_FAST)
    df["ema_slow"] = ema(df["Close"], EMA_SLOW)
    df["rsi"] = rsi(df["Close"], RSI_PERIOD)
    df["atr"] = atr(df, ATR_PERIOD)
    return df


def detect_signal(df: pd.DataFrame):
    """Analyse la dernière bougie clôturée. Retourne un dict ou None."""
    if len(df) < EMA_SLOW + 5:
        return None
    df = add_indicators(df)
    last, prev = df.iloc[-1], df.iloc[-2]
    if last[["ema_fast", "ema_slow", "rsi", "atr"]].isna().any():
        return None

    crossed_up = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    crossed_down = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

    price, a = float(last["Close"]), float(last["atr"])
    if crossed_up and 50 <= last["rsi"] <= 70:
        side = "ACHAT"
        sl, tp = price - SL_ATR * a, price + TP_ATR * a
    elif crossed_down and 30 <= last["rsi"] <= 50:
        side = "VENTE"
        sl, tp = price + SL_ATR * a, price - TP_ATR * a
    else:
        return None

    return {
        "side": side,
        "price": price,
        "sl": sl,
        "tp": tp,
        "rsi": float(last["rsi"]),
        "time": df.index[-1].tz_convert("UTC") if df.index[-1].tzinfo else df.index[-1],
    }


def fmt_price(name: str, x: float) -> str:
    if "JPY" in name:
        return f"{x:.3f}"
    if "XAU" in name:
        return f"{x:.2f}"
    return f"{x:.5f}"


def fetch(symbol: str) -> pd.DataFrame:
    df = yf.download(symbol, period=PERIOD, interval=INTERVAL, progress=False, auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    # retire la bougie en cours (pas encore clôturée)
    now = pd.Timestamp.now(tz="UTC")
    idx = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
    if len(df) and idx[-1] + pd.Timedelta(hours=1) > now:
        df = df.iloc[:-1]
    return df


def is_fresh(sig: dict, now=None) -> bool:
    """Vrai si la bougie du signal vient de se fermer (évite d'envoyer deux fois le même signal)."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    t = pd.Timestamp(sig["time"])
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    age = now - (t + pd.Timedelta(hours=1))
    return age <= pd.Timedelta(minutes=MAX_SIGNAL_AGE_MIN)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant.")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    r.raise_for_status()


def build_message(name: str, sig: dict) -> str:
    emoji = "🟢" if sig["side"] == "ACHAT" else "🔴"
    return (
        f"{emoji} SIGNAL {sig['side']} - {name} (H1)\n"
        f"Entrée : {fmt_price(name, sig['price'])}\n"
        f"Stop-loss : {fmt_price(name, sig['sl'])}\n"
        f"Objectif : {fmt_price(name, sig['tp'])}\n"
        f"RSI : {sig['rsi']:.0f}\n"
        f"Bougie : {sig['time']:%Y-%m-%d %H:%M} UTC\n\n"
        "Rappel : aucun signal n'est garanti. Risquez peu par trade."
    )


def main() -> None:
    args = sys.argv[1:]
    if "--test" in args:
        send_telegram("✅ Test réussi : votre bot de signaux est bien connecté.")
        print("Message de test envoyé.")
        return

    dry = "--dry" in args
    found = 0
    for name, symbol in PAIRS.items():
        try:
            df = fetch(symbol)
            sig = detect_signal(df)
        except Exception as e:  # une paire en erreur ne bloque pas les autres
            print(f"[{name}] erreur : {e}")
            continue
        if sig is None:
            print(f"[{name}] pas de signal.")
            continue
        if not is_fresh(sig):
            print(f"[{name}] signal déjà traité lors d'un passage précédent, ignoré.")
            continue
        found += 1
        msg = build_message(name, sig)
        print(msg, "\n")
        if not dry:
            send_telegram(msg)

    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC - {found} signal(s).")


if __name__ == "__main__":
    main()
