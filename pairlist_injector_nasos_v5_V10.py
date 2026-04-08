#!/usr/bin/env python3
"""
================================================================================
PAIRLIST INJECTOR v10.0 — NASOSv5_mod3_pairs_safe_v5 Edition — Port 9999
================================================================================

KERNÄNDERUNGEN gegenüber V9:

┌──────────────────────────────────────────────────────────────────────────────┐
│ V9 PROBLEM                  │ V10 FIX                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│ Sucht Dips → Falling Knives │ [R1] Reversal Confidence Score (0.0–1.0)       │
│                             │      Dip alleine = 0 Punkte                    │
│                             │      Reversal-Signale = Punkte                 │
├──────────────────────────────────────────────────────────────────────────────┤
│ Dip-Proximity als Hauptscore│ [R2] Score-System neu: Reversal-Stärke zählt  │
│                             │      Dip-EWO bringt nur Trigger-Punkte         │
├──────────────────────────────────────────────────────────────────────────────┤
│ Kein Late-Entry-Filter      │ [R3] Anti-Late-Entry: bounce_from_low > 3%     │
│                             │      oder RSI > 55 → blockiert                 │
├──────────────────────────────────────────────────────────────────────────────┤
│ BTC nur EMA200-Check        │ [B1] BTC Momentum-Filter: 30min + RSI_fast     │
│                             │      + EMA200-Trend                            │
├──────────────────────────────────────────────────────────────────────────────┤
│ Falling-Knife bei -1.5%     │ [F1] Strenger: -1% in 15m, -2.5% in 1h        │
├──────────────────────────────────────────────────────────────────────────────┤
│ max_pairs = 40              │ [P1] max_pairs = 20 (Qualität > Quantität)     │
├──────────────────────────────────────────────────────────────────────────────┤
│ ATR nicht berücksichtigt    │ [V1] Volatility-Gate: ATR-Regime wie v5        │
├──────────────────────────────────────────────────────────────────────────────┤
│ Coin-Stärke nicht geprüft  │ [C1] Coin vs BTC relative Stärke               │
└──────────────────────────────────────────────────────────────────────────────┘

REVERSAL CONFIDENCE SCORE (Kernstück V10):
  0 Signale = 0.00 → kein Entry möglich
  RSI_fast steigt (2× gewichtet) + grüne Kerze + Volume + Higher Low
  Score ≥ 0.55 für Pre-Entry, ≥ 0.75 für direkten Entry

PORT: 9999
================================================================================
"""

import asyncio
import json
import logging
import os
import re
import ssl
import time
import certifi
import talib.abstract as ta
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
from aiohttp import web
import aiohttp
import numpy as np
from dotenv import load_dotenv


ENV_FILE = Path(".env")
DEFAULT_ENV_CONTENT = """PAIRLIST_BASE_CURRENCY=USDT
PAIRLIST_BASE_CURRENCIES=USDT
PAIRLIST_MAX_PAIRS=20
PAIRLIST_MIN_VOLUME=3000000
PAIRLIST_MIN_PRICE=0.05
PAIRLIST_MIN_SCORE=20
PAIRLIST_REVERSAL_ENTRY=0.60
PAIRLIST_MAX_DROP_1H=-0.025
PAIRLIST_BTC_RSI_MIN=42
PAIRLIST_UPDATE_INTERVAL=30
PAIRLIST_TOP_N_VOLUME=35
PAIRLIST_MAX_VOLATILITY=0.12
"""


def _ensure_env_file() -> None:
    if ENV_FILE.exists():
        return
    ENV_FILE.write_text(DEFAULT_ENV_CONTENT, encoding="utf-8")


_ensure_env_file()
load_dotenv(dotenv_path=ENV_FILE)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_list(name: str, default: str) -> list:
    raw_value = os.getenv(name, default)
    if raw_value is None:
        raw_value = default
    items = [item.strip().upper() for item in str(raw_value).split(",")]
    values = [item for item in items if item]
    if values:
        return values
    return [item.strip().upper() for item in default.split(",") if item.strip()]


if os.getenv("PAIRLIST_BASE_CURRENCIES"):
    BASE_CURRENCIES = _env_list("PAIRLIST_BASE_CURRENCIES", "USDT")
else:
    BASE_CURRENCIES = [os.getenv("PAIRLIST_BASE_CURRENCY", "USDT").strip().upper()]

PRIMARY_BASE_CURRENCY = BASE_CURRENCIES[0] if BASE_CURRENCIES else "USDT"


def _quote_currency_for_symbol(symbol: str) -> Optional[str]:
    if "/" in symbol:
        quote = symbol.split("/", 1)[1].strip().upper()
        return quote if quote in BASE_CURRENCIES else None
    if "-" in symbol:
        quote = symbol.split("-", 1)[1].strip().upper()
        return quote if quote in BASE_CURRENCIES else None
    for currency in sorted(BASE_CURRENCIES, key=len, reverse=True):
        if symbol.endswith(currency):
            return currency
    return None


def _symbol_matches_base_currency(symbol: str) -> bool:
    return any(symbol.endswith(currency) for currency in BASE_CURRENCIES)


# ─── Configuration ────────────────────────────────────────────────────────────
CONFIG = {
    "http_port":       _env_int("PAIRLIST_HTTP_PORT", 9999),
    "update_interval": _env_int("PAIRLIST_UPDATE_INTERVAL", 30),
    "base_currency":   PRIMARY_BASE_CURRENCY,
    "base_currencies": BASE_CURRENCIES,
    "kucoin_api_base_url": _env_str("PAIRLIST_KUCOIN_API_BASE_URL", "https://api.kucoin.com"),

    # [P1] Reduziert auf 20 – nur hochqualitative Coins
    "max_pairs":       _env_int("PAIRLIST_MAX_PAIRS", 20),
    "min_score":       _env_int("PAIRLIST_MIN_SCORE", 20),          # höher als V9 (15)
    "min_score_kucoin":18,
    "elite_score":     65,
    "elite_score_kucoin": 58,
    "state_file": _env_str("PAIRLIST_STATE_FILE", "injector_nasos_state_v10.json"),

    "min_volume_quote":          1_800_000,
    "min_volume_quote_binance":  3_000_000,
    "min_price": 0.05,
    "min_volume_quote_kucoin":   3_000_000,
    "min_price": 0.05,
    "strict_min_volume_filtering": True,
    "analyze_tradeable_only":    True,

    "ticker_cache_stale_seconds":      180,
    "ticker_cache_hard_stale_seconds": 21_600,
    "ticker_cache_warn_interval_seconds": 300,
    "ticker_error_log_interval_seconds": 120,
    "no_ticker_log_interval_seconds":   120,

    "http_timeout_total_seconds":      12,
    "http_timeout_connect_seconds":    4,
    "http_timeout_sock_connect_seconds": 4,
    "http_timeout_sock_read_seconds":  8,

    "binance_api_base_urls": [
        "https://data-api.binance.vision",
        "https://api.binance.com",
        "https://api-gcp.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
        "https://api4.binance.com",
    ],
    "binance_429_cooldown_seconds": 60,
    "binance_418_cooldown_seconds": 900,

    # ── Volume ──
    "volume_quality_enabled":           True,
    "volume_stability_filter_enabled":  True,
    "pump_fade_block_enabled":          True,
    "volume_sample_seconds":            600,
    "volume_history_hours":             72,
    "volume_stability_min_samples_24h": 12,
    "volume_stability_min_samples_72h": 24,
    "volume_stability_ratio_24h":       0.85,
    "volume_stability_ratio_72h":       0.70,
    "volume_warmup_multiplier":         1.10,
    "volume_keep_ratio":                0.90,
    "volume_weak_cycles_to_drop":       3,
    "pump_fade_spike_ratio":            1.7,
    "pump_fade_price_change_24h":       8.0,
    "pump_fade_require_weakness":       True,
    "pump_fade_weakness_15m_drop":      -0.0025,
    "pump_fade_weakness_rsi_slope":     -0.25,
    "pump_fade_weakness_bb_pos_min":    0.50,
    "pump_fade_probation_minutes":      120,
    "volume_stability_rank_bonus_max":  8.0,
    "volume_spike_rank_penalty_max":    10.0,
    "volume_spike_rank_penalty_max_kucoin": 7.0,
    "volume_seed_enabled":              True,
    "volume_seed_interval":             "1h",
    "volume_seed_lookback_bars":        72,
    "volume_seed_symbols_per_cycle":    120,
    "volume_seed_max_concurrency":      8,
    "volume_seed_min_request_gap_seconds": 0.18,
    "volume_seed_tradeable_priority":   True,
    "volume_require_history_for_admission": True,

    # ── Crash protection ──
    "crash_5m_threshold":    -0.07,   # [F1] strenger: -7% statt -8%
    "crash_1h_threshold":    -0.12,   # [F1] strenger: -12% statt -15%
    "atr_spike_multiplier":  2.5,     # [F1] strenger: 2.5× statt 3.0×
    "shadow_blacklist_hours": 6,
    "min_ban_minutes":       10,
    "recovery_required_cycles": 3,
    "recovery_5m_max_drop":  -0.01,
    "recovery_bounce_from_low": 0.03,
    "recovery_atr_spike_max":   1.6,
    "recovery_ema_offset_min":  0.94,
    "recovery_rsi_fast_min":    22,

    # ── Fast re-entry ──
    "fast_reentry_enabled":          True,
    "fast_reentry_allowed_reasons":  ["VERTICAL_CRASH", "PANIC_ATR_SPIKE"],
    "fast_reentry_min_ban_minutes":  4,
    "fast_reentry_required_cycles":  2,
    "fast_reentry_max_5m_drop":      -0.005,
    "fast_reentry_min_bounce_from_low": 0.015,
    "fast_reentry_atr_spike_max":    1.35,
    "fast_reentry_min_proximity":    0.78,
    "fast_reentry_require_tradeable_volume": True,
    "probation_minutes":     90,
    "probation_min_score":   35,
    "probation_min_score_kucoin": 28,

    # ── Sticky pairlist ──
    "elite_sticky_minutes":         90,   # kürzer als V9 (120) – schneller rotieren
    "good_sticky_minutes":          45,   # kürzer als V9 (60)
    "near_entry_hold_minutes":      30,   # kürzer als V9 (45)
    "near_entry_proximity_threshold": 0.65,

    "use_alignment_admission_gate":  True,
    "pre_entry_enabled":             True,
    "pre_entry_proximity_min":       0.55,  # strenger als V9 (0.60)
    "pre_entry_proximity_min_kucoin": 0.50,
    "pre_entry_min_score_floor":     12,    # höher als V9 (6)
    "pre_entry_min_score_floor_kucoin": 10,
    "pre_entry_profit_relax":        0.980,
    "pre_entry_profit_relax_kucoin": 0.965,
    "pre_entry_require_profit_near": False,
    "pre_entry_strong_proximity_delta": 0.08,

    "predictive_target_pairs":       10,   # [P1] weniger als V9 (12)
    "predictive_adaptive_relax_enabled": True,
    "predictive_max_proximity_relax": 0.15,  # enger als V9 (0.22)
    "predictive_max_score_relax":    0,
    "predictive_max_profit_relax":   0.02,
    "predictive_confidence_min":     0.55,   # höher als V9 (0.50)
    "predictive_confidence_min_kucoin": 0.48,
    "predictive_confidence_floor":   0.45,
    "predictive_confidence_floor_kucoin": 0.40,
    "predictive_max_confidence_relax": 0.06,
    "predictive_min_pre_entry_score": 8,
    "predictive_allow_tradeable_volume_pre_entry": False,
    "predictive_allow_tradeable_volume_pre_entry_kucoin": True,
    "predictive_tradeable_volume_ratio": 0.90,
    "predictive_tradeable_volume_ratio_kucoin": 0.88,
    "predictive_min_volume_quality": 0.55,
    "predictive_min_volume_quality_kucoin": 0.42,
    "predictive_max_spike_penalty":  0.50,
    "predictive_max_spike_penalty_kucoin": 0.65,
    "predictive_weakness_block_enabled": True,
    "predictive_weakness_drop_15m":  -0.008,  # strenger als V9 (-0.009)
    "predictive_weakness_rsi_slope": -1.4,
    "predictive_weakness_bb_pos_max": 0.42,
    "predictive_weakness_rsi_fast_min": 24,   # höher als V9 (22)

    # [F1] Falling-Filter – strenger als V9
    "falling_filter_enabled":        True,
    "falling_probation_minutes":     240,     # länger als V9 (180)
    "falling_drop_15m":             -0.010,   # strenger: -1% statt -1.2%
    "falling_drop_30m":             -0.016,
    "falling_drop_1h":              -0.025,   # strenger: -2.5%
    "falling_close_ema100_max":      0.992,   # strenger als V9 (0.990)
    "falling_ema100_trend_max":      0.9998,
    "falling_rsi_slope_max":        -0.8,
    "falling_rsi_fast_slope_max":   -1.2,
    "falling_ema_offset_delta_max": -0.002,
    "falling_exact_drop_15m":       -0.007,
    "falling_exact_drop_30m":       -0.014,
    "falling_exact_close_ema100_max": 0.995,

    # ── [R1] Reversal Confidence Score Schwellenwerte ──
    "reversal_confidence_min_entry":    0.60,  # mind. für Exact-Entry
    "reversal_confidence_min_pre":      0.40,  # mind. für Pre-Entry
    "reversal_score_bonus_max":         35,    # max. Score-Bonus für perfektes Reversal
    "reversal_rsi_fast_rise_weight":    2.0,   # RSI_fast-Anstieg doppelt gewichtet (Pflicht-Effekt)
    "reversal_green_candle_min":        0.0015, # Body > 0.15% für grüne Kerze
    "reversal_volume_ratio_min":        1.4,   # Volume-Spike: 1.4× Durchschnitt
    "reversal_penalty_per_missing":    -8,     # Penalty wenn kein Reversal sichtbar

    # [R3] Anti-Late-Entry
    "late_entry_bounce_max":  0.035,   # block wenn Coin >3.5% vom lokalen Tief weg
    "late_entry_rsi_max":     55,      # block wenn RSI schon zu hoch

    # ── [A1] v5-Parameter-Alignment ──
    "ewo_high":             4.8,
    "ewo_high_2":           7.2,
    "ewo_low":             -14.0,
    "base_nb_candles_buy":  18,
    "base_nb_candles_sell": 16,
    "lookback_candles":     8,
    "low_offset":           0.987,
    "low_offset_2":         0.965,
    "rsi_fast_buy":         30,
    "rsi_buy":              62,
    "rsi_ewo1_max":         40,   # v5 verschärft: 40 statt 42
    "rsi_ewo2_max":         36,   # v5 verschärft: 36 statt 38
    "high_offset":          1.006,
    "profit_threshold":     1.028,
    "profit_threshold_kucoin": 1.022,

    # ── [A3] Trend-Filter (v5 [S1]) ──
    "trend_ema200_min":          1.003,
    "trend_ema200_trend_min":    1.0,
    "trend_ema100_ema200_min":   1.001,
    "trend_ema100_trend_min":    0.9998,
    "trend_drawdown_max":        0.10,
    "trend_price_change_30m_min": -0.015,

    # ── [A4] Anti-Falling-Knife ──
    "antifk_ema100_hard_max":    0.990,
    "antifk_drawdown_threshold": 0.05,
    "antifk_ema100_soft_max":    0.995,

    # ── [V1] Volatility-Regime (neu in V10, spiegelt v5 [M1]) ──
    "atr_regime_min_ratio":  0.55,   # ATR nicht unter 55% des 4h-Durchschnitts
    "atr_regime_max_ratio":  3.5,    # ATR nicht über 3.5× des 4h-Durchschnitts

    # ── [B1] BTC-Filter (verschärft) ──
    "btc_ema200_min":           0.998,
    "btc_ema50_ema200_min":     0.996,
    "btc_rsi_min":              42,    # höher als V9 (40)
    "btc_price_change_30m_min": -0.010, # strenger als V9 (-0.012)
    "btc_ema200_trend_min":     0.9995, # NEU: BTC EMA200 muss stabil sein

    # ── Scoring ──
    "ewo_positive_threshold":      2.0,
    "ewo_high_threshold":          4.0,
    "ewo_very_high_threshold":     8.0,
    "ewo_negative_threshold":     -4.0,
    "ewo_very_negative_threshold": -8.0,
    "rsi_oversold":    40,
    "rsi_very_oversold": 30,
    "rsi_fast_weak":   35,
    "rsi_fast_oversold": 30,
    "ema_offset_weak": 0.99,
    "ema_offset_good": 0.98,
    "ema_offset_deep": 0.95,
    "approach_bonus_max": 20,       # kleiner als V9 (25) – Dip alleine reicht nicht
    "no_profit_penalty": 0,

    # Recentispumping
    "recentispumping_penalty":          -30,
    "recentispumping_lookback_candles": 300,
    "recentispumping_pct_threshold":    0.15,
    "recentispumping_roll_ispumping":   20,
    "recentispumping_roll_islong":      30,

    # Midtrend guard
    "midtrend_close_ema100_min": 0.97,
    "midtrend_ema50_ema100_min": 0.985,
    "midtrend_ema100_trend_min": 0.992,
    "midtrend_guard_penalty":   -15,

    # Backdata
    "backdata_window_candles": 120,
    "backdata_hit_bonus_max":    5,
    "backdata_miss_penalty_max": -3,

    # General
    "min_atr_threshold":        0.008,
    "min_atr_threshold_kucoin": 0.0055,
    "max_24h_pump":  25.0,
    "pump_penalty": -20,
    "kline_limit_5m":  350,
    "kline_limit_15m": 100,

    # WebSocket
    "ws_enabled":              _env_bool("PAIRLIST_WS_ENABLED", True),
    "ws_kline_intervals":      ["5m", "15m"],
    "ws_reconnect_seconds":    5,
    "ws_max_streams_per_conn": 250,

    # Blacklists
    "blacklist": [
        "USDT", "USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD", "BFUSD",
        "RLUSD", "XUSD", "USD1", "USDE", "EUR",
    ],
    "pair_blacklist_patterns": [
        "BNB/.*",
        ".*UP/.*", ".*DOWN/.*", ".*BEAR/.*", ".*BULL/.*"
    ],
}

CONFIG.update({
    "base_currency": PRIMARY_BASE_CURRENCY,
    "base_currencies": BASE_CURRENCIES,
    "max_pairs": _env_int("PAIRLIST_MAX_PAIRS", 20),
    "min_volume_quote": _env_int("PAIRLIST_MIN_VOLUME", 3_000_000),
    "min_volume_quote_binance": _env_int("PAIRLIST_MIN_VOLUME", 3_000_000),
    "min_volume_quote_kucoin": _env_int("PAIRLIST_MIN_VOLUME", 3_000_000),
    "min_price": _env_float("PAIRLIST_MIN_PRICE", 0.05),
    "reversal_confidence_min_entry": _env_float("PAIRLIST_REVERSAL_ENTRY", 0.60),
    "falling_drop_1h": _env_float("PAIRLIST_MAX_DROP_1H", -0.025),
    "btc_rsi_min": _env_int("PAIRLIST_BTC_RSI_MIN", 42),
    "update_interval": _env_int("PAIRLIST_UPDATE_INTERVAL", 30),
    "top_n_by_volume": _env_int("PAIRLIST_TOP_N_VOLUME", 35),
    "max_volatility": _env_float("PAIRLIST_MAX_VOLATILITY", 0.12),
})

logging.basicConfig(
    level=getattr(logging, _env_str("PAIRLIST_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def log_loaded_configuration() -> None:
    logger.info(
        "Loaded Pairlist configuration:\n"
        "Base currencies: %s\n"
        "Max pairs: %s\n"
        "Min volume: %s\n"
        "Min price: %s\n"
        "Min score: %s\n"
        "Reversal entry: %s\n"
        "Max drop 1h: %s\n"
        "BTC RSI min: %s\n"
        "Update interval: %ss\n"
        "Top N by volume: %s\n"
        "Max volatility: %s",
        CONFIG["base_currencies"],
        CONFIG["max_pairs"],
        CONFIG["min_volume_quote_binance"],
        CONFIG["min_price"],
        CONFIG["min_score"],
        CONFIG["reversal_confidence_min_entry"],
        CONFIG["falling_drop_1h"],
        CONFIG["btc_rsi_min"],
        CONFIG["update_interval"],
        CONFIG["top_n_by_volume"],
        CONFIG["max_volatility"],
    )


def build_http_timeout() -> aiohttp.ClientTimeout:
    total = max(1.0, float(CONFIG.get("http_timeout_total_seconds", 12)))
    connect = max(1.0, min(total, float(CONFIG.get("http_timeout_connect_seconds", 4))))
    sock_connect = max(1.0, min(total, float(CONFIG.get("http_timeout_sock_connect_seconds", 4))))
    sock_read = max(1.0, min(total, float(CONFIG.get("http_timeout_sock_read_seconds", 8))))
    return aiohttp.ClientTimeout(total=total, connect=connect, sock_connect=sock_connect, sock_read=sock_read)


# ─── PairAnalyzer ─────────────────────────────────────────────────────────────

class PairAnalyzer:
    """
    V10: get_indicators liefert jetzt zusätzlich:
    - reversal_confidence (0.0–1.0) [R1]
    - bounce_from_low (für Late-Entry-Filter) [R3]
    - atr_regime_ok (für Volatility-Gate) [V1]
    - btc_momentum_ok (für BTC-Filter) [B1]
    """

    MAX_5M  = 400
    MAX_15M = 120

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.opens_5m:   List[float] = []
        self.highs_5m:   List[float] = []
        self.lows_5m:    List[float] = []
        self.prices_5m:  List[float] = []
        self.volumes_5m: List[float] = []
        self.prices_15m: List[float] = []
        self.highs_15m:  List[float] = []
        self.btc_prices_15m:  List[float] = []
        self.btc_volumes_15m: List[float] = []
        self.last_update: float = 0
        self.last_candle_time_5m:  int = 0
        self.last_candle_time_15m: int = 0
        self.ws_active: bool = False
        self.backdata_hits:   int = 0
        self.backdata_checks: int = 0

    def update_data(self, m5_klines: List, m15_klines: List, btc_15m_klines: Optional[List] = None):
        self.opens_5m   = [float(k[1]) for k in m5_klines]
        self.highs_5m   = [float(k[2]) for k in m5_klines]
        self.lows_5m    = [float(k[3]) for k in m5_klines]
        self.prices_5m  = [float(k[4]) for k in m5_klines]
        self.volumes_5m = [float(k[7]) for k in m5_klines]
        self.prices_15m = [float(k[4]) for k in m15_klines]
        self.highs_15m  = [float(k[2]) for k in m15_klines]
        self.last_update = time.time()
        if m5_klines:  self.last_candle_time_5m  = int(m5_klines[-1][0])
        if m15_klines: self.last_candle_time_15m = int(m15_klines[-1][0])
        if btc_15m_klines:
            self.btc_prices_15m  = [float(k[4]) for k in btc_15m_klines][-self.MAX_15M:]
            self.btc_volumes_15m = [float(k[7]) for k in btc_15m_klines][-self.MAX_15M:]

    def update_ws_kline(self, interval: str, k: dict):
        t = int(k["t"])
        o, h, l, c, v = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["q"])
        if interval == "5m":
            if not self.prices_5m: return
            if t == self.last_candle_time_5m:
                self.opens_5m[-1]=o; self.highs_5m[-1]=h; self.lows_5m[-1]=l
                self.prices_5m[-1]=c; self.volumes_5m[-1]=v
            elif t > self.last_candle_time_5m:
                self.opens_5m.append(o); self.highs_5m.append(h); self.lows_5m.append(l)
                self.prices_5m.append(c); self.volumes_5m.append(v)
                self.last_candle_time_5m = t
                if len(self.prices_5m) > self.MAX_5M:
                    trim = len(self.prices_5m) - self.MAX_5M
                    self.opens_5m=self.opens_5m[trim:]; self.highs_5m=self.highs_5m[trim:]
                    self.lows_5m=self.lows_5m[trim:]; self.prices_5m=self.prices_5m[trim:]
                    self.volumes_5m=self.volumes_5m[trim:]
            self.ws_active = True; self.last_update = time.time()
        elif interval == "15m":
            if not self.prices_15m: return
            if t == self.last_candle_time_15m:
                self.prices_15m[-1]=c; self.highs_15m[-1]=h
            elif t > self.last_candle_time_15m:
                self.prices_15m.append(c); self.highs_15m.append(h)
                self.last_candle_time_15m = t
                if len(self.prices_15m) > self.MAX_15M:
                    self.prices_15m=self.prices_15m[-self.MAX_15M:]
                    self.highs_15m=self.highs_15m[-self.MAX_15M:]
            self.ws_active = True

    def detect_crash(self) -> Optional[Tuple[str, str]]:
        if len(self.prices_5m) < 60: return None
        c5 = (self.prices_5m[-1] - self.prices_5m[-6]) / self.prices_5m[-6]
        if c5 <= CONFIG["crash_5m_threshold"]:
            return ("VERTICAL_CRASH", f"{c5*100:.1f}% in 25min")
        if len(self.prices_5m) >= 13:
            c1h = (self.prices_5m[-1] - self.prices_5m[-13]) / self.prices_5m[-13]
            if c1h <= CONFIG["crash_1h_threshold"]:
                return ("STRUCTURAL_BREAK", f"{c1h*100:.1f}% in 1hour")
        spike = self.atr_spike_ratio()
        if spike is not None and spike > CONFIG["atr_spike_multiplier"]:
            return ("PANIC_ATR_SPIKE", f"ATR {spike:.1f}x normal")
        return None

    def atr_spike_ratio(self) -> Optional[float]:
        if len(self.prices_5m) < 60: return None
        h = np.array(self.highs_5m); l = np.array(self.lows_5m); c = np.array(self.prices_5m)
        atrs = ta.ATR(h, l, c, 14)
        if len(atrs) < 25: return None
        baseline = np.mean(atrs[-20:-1])
        return float(atrs[-1] / baseline) if baseline > 0 else None

    def _calc_recentispumping(self, c: np.ndarray) -> bool:
        n = len(c)
        if n < 9: return False
        threshold    = float(CONFIG.get("recentispumping_pct_threshold", 0.15))
        roll_isp     = int(CONFIG.get("recentispumping_roll_ispumping", 20))
        roll_islong  = int(CONFIG.get("recentispumping_roll_islong", 30))
        lookback     = int(CONFIG.get("recentispumping_lookback_candles", 300))
        pct = np.zeros(n, dtype=np.float64)
        prev = c[:-8]; safe_prev = np.where(prev == 0, 1.0, prev)
        pct[8:] = np.abs((c[8:] - prev) / safe_prev)
        pct_int = (pct > threshold).astype(np.int8)
        def rolling_has_hit(arr, window):
            out = np.zeros_like(arr, dtype=np.int8)
            if window <= 1: return (arr > 0).astype(np.int8)
            if len(arr) < window: return out
            csum = np.cumsum(np.insert(arr, 0, 0))
            sums = csum[window:] - csum[:-window]
            out[window-1:] = (sums >= 1).astype(np.int8)
            return out
        isp  = rolling_has_hit(pct_int, roll_isp)
        islp = rolling_has_hit(pct_int, roll_islong)
        source = ((isp > 0) | (islp > 0)).astype(np.int8)
        recent = rolling_has_hit(source, lookback)
        return bool(recent[-1] > 0)

    def get_indicators(self) -> Dict:
        """
        V10 erweitert um:
        - reversal_confidence [R1]
        - bounce_from_low [R3]
        - atr_regime_ok [V1]
        - btc_momentum_ok [B1]
        - coin_vs_btc_ok [C1]
        """
        if len(self.prices_5m) < 200: return {}

        c = np.array(self.prices_5m, dtype=np.float64)
        o = np.array(self.opens_5m,  dtype=np.float64) if self.opens_5m else c.copy()
        h = np.array(self.highs_5m,  dtype=np.float64)
        l = np.array(self.lows_5m,   dtype=np.float64)
        v = np.array(self.volumes_5m, dtype=np.float64)

        # ── EWO ──
        ema_fast_s = ta.EMA(c, 50); ema_slow_s = ta.EMA(c, 200)
        low_safe = np.where(l == 0, np.nan, l)
        ewo_s = (ema_fast_s - ema_slow_s) / low_safe * 120
        ewo_s = np.nan_to_num(ewo_s, nan=0.0)
        ewo   = float(ewo_s[-1])

        # ── RSI ──
        rsi_s      = ta.RSI(c, 20); rsi      = float(rsi_s[-1])
        rsi_fast_s = ta.RSI(c, 5);  rsi_fast = float(rsi_fast_s[-1])
        rsi_slow_v = float(ta.RSI(c, 25)[-1])
        prev_rsi      = float(rsi_s[-2])
        prev_rsi_fast = float(rsi_fast_s[-2])
        rsi_fast_2ago = float(rsi_fast_s[-3]) if len(rsi_fast_s) >= 3 else prev_rsi_fast
        rsi_slope     = rsi - prev_rsi
        rsi_fast_slope = rsi_fast - prev_rsi_fast

        # ── EMA ──
        ema18_s  = ta.EMA(c, 18);  ema_18  = float(ema18_s[-1])
        ema50_s  = ta.EMA(c, 50);  ema50   = float(ema50_s[-1])
        ema100_s = ta.EMA(c, 100); ema100  = float(ema100_s[-1])
        ema200_s = ta.EMA(c, 200); ema200  = float(ema200_s[-1])
        ema_offset = float(c[-1] / ema_18) if ema_18 > 0 else 1.0

        ema200_trend_ratio  = float(ema200_s[-1] / ema200_s[-13]) if len(ema200_s) >= 13 and ema200_s[-13] > 0 else 1.0
        ema100_trend_ratio  = float(ema100_s[-1] / ema100_s[-25]) if len(ema100_s) >= 25 and ema100_s[-25] > 0 else 1.0
        ema100_ema200_ratio = float(ema100 / ema200)  if ema200 > 0 else 1.0
        close_ema100_ratio  = float(c[-1] / ema100)  if ema100 > 0 else 1.0
        close_ema200_ratio  = float(c[-1] / ema200)  if ema200 > 0 else 1.0

        # ── Trend-Filter-Check ──
        trend_filter_ok = bool(
            close_ema200_ratio  >= CONFIG["trend_ema200_min"] and
            ema200_trend_ratio  >= CONFIG["trend_ema200_trend_min"] and
            ema100_ema200_ratio >= CONFIG["trend_ema100_ema200_min"] and
            ema100_trend_ratio  >= CONFIG["trend_ema100_trend_min"]
        )

        # ── Preis-Änderungen ──
        price_change_15m = float((c[-1]-c[-4])/c[-4])  if len(c)>4  and c[-4]>0  else 0.0
        price_change_30m = float((c[-1]-c[-7])/c[-7])  if len(c)>7  and c[-7]>0  else 0.0
        price_change_1h  = float((c[-1]-c[-13])/c[-13]) if len(c)>13 and c[-13]>0 else 0.0
        price_change_2h  = float((c[-1]-c[-25])/c[-25]) if len(c)>25 and c[-25]>0 else 0.0

        # ── Drawdown ──
        drawdown_from_high = 0.0
        if len(c) >= 144:
            rolling_max = float(np.max(c[-144:]))
            drawdown_from_high = float(1-(c[-1]/rolling_max)) if rolling_max > 0 else 0.0

        # ── Falling Knife ──
        falling_knife_hard = close_ema100_ratio < CONFIG["antifk_ema100_hard_max"]
        falling_knife_soft = (
            drawdown_from_high >= CONFIG["antifk_drawdown_threshold"] and
            close_ema100_ratio <= CONFIG["antifk_ema100_soft_max"] and
            rsi_fast < rsi_slow_v
        )
        falling_knife_1h = (price_change_1h <= -0.025 and ema100_trend_ratio <= CONFIG["trend_ema100_trend_min"])
        # [F1] Neuer strenger Falling-Knife-Check: -1% in 15m
        falling_knife_15m = price_change_15m <= -0.010 and rsi_fast_slope < 0
        is_falling_knife = falling_knife_hard or falling_knife_soft or falling_knife_1h or falling_knife_15m

        # ── Volume ──
        vol_avg_24  = float(np.mean(v[-24:])) if len(v) >= 24 else float(np.mean(v))
        vol_ratio   = float(v[-1] / vol_avg_24) if vol_avg_24 > 0 else 1.0

        # ── ATR ──
        atr_s   = ta.ATR(h, l, c, 14)
        atr_pct = float(atr_s[-1] / c[-1]) if c[-1] > 0 else 0.0
        # [V1] ATR-Regime: 4h-Durchschnitt (48 × 5min Kerzen)
        atr_ma48 = float(np.mean(atr_s[-48:] / c[-48:])) if len(atr_s) >= 48 else atr_pct
        atr_regime_ok = bool(
            atr_ma48 > 0 and
            (atr_pct / atr_ma48) >= CONFIG["atr_regime_min_ratio"] and
            (atr_pct / atr_ma48) <= CONFIG["atr_regime_max_ratio"]
        )

        # ── Candle ──
        green_candle  = bool(c[-1] > o[-1]) if len(o) == len(c) else False
        higher_close  = bool(c[-1] > c[-2])
        cur_change    = float((c[-1]-c[-2])/c[-2]) if c[-2]>0 else 0.0
        prev_change   = float((c[-2]-c[-3])/c[-3]) if len(c)>=3 and c[-3]>0 else 0.0
        prev_close    = float(c[-2])
        higher_low    = bool(l[-1] > l[-2]) if len(l) >= 2 else False

        # ── Profit Potential ──
        profit_potential = 1.0
        lookback_15m = max(1, int(CONFIG.get("lookback_candles", 8)))
        if len(self.prices_15m) >= lookback_15m:
            mx = max(self.prices_15m[-lookback_15m:])
            profit_potential = float(mx / c[-1]) if c[-1] > 0 else 1.0

        # ── BB ──
        tp = (h+l+c)/3.0
        bb_mid_s    = ta.SMA(tp, 20)
        bb_std_s    = ta.STDDEV(tp, 20) * np.sqrt(20.0/19.0)
        bb_upper_s  = bb_mid_s + 2.0*bb_std_s
        bb_lower_s  = bb_mid_s - 2.0*bb_std_s
        bb_mid  = float(bb_mid_s[-1])
        bb_low  = float(bb_lower_s[-1])
        bb_up   = float(bb_upper_s[-1])
        bb_span = max(bb_up - bb_low, 1e-12)
        bb_pos  = float((c[-1]-bb_low)/bb_span)
        bb_width= float((bb_up-bb_low)/bb_mid) if bb_mid > 0 else 0.0
        bb_lower_prev = float(bb_lower_s[-2])

        mfi_s    = ta.MFI(h, l, c, v, 14)
        mfi      = float(mfi_s[-1])
        prev_mfi = float(mfi_s[-2])

        # ── Pump ──
        recentispumping = self._calc_recentispumping(c)
        pct_change_8 = float((c[-1]-c[-9])/c[-9]) if len(c)>9 and c[-9]>0 else 0.0
        is_pumping   = abs(pct_change_8) > 0.15
        change_24h   = float((c[-1]-c[-288])/c[-288]*100) if len(c)>=288 else 0.0

        # ── Deltas ──
        ewo_d3      = float(ewo_s[-1]-ewo_s[-4])   if len(ewo_s)>=4  else 0.0
        rsi_fast_d3 = float(rsi_fast_s[-1]-rsi_fast_s[-4]) if len(rsi_fast_s)>=4 else 0.0
        ema_off_d3  = 0.0
        if len(ema18_s)>=4 and ema18_s[-1]>0 and ema18_s[-4]>0:
            ema_off_d3 = float((c[-1]/ema18_s[-1])-(c[-4]/ema18_s[-4]))

        # ── Core Space ──
        volume_ok    = bool(v[-1] > 0)
        close_lt_sell= bool((c[-1]/ema_18) < CONFIG["high_offset"]) if ema_18 > 0 else True
        in_core_space= bool(
            (rsi_fast<CONFIG["rsi_fast_buy"] and ema_offset<CONFIG["low_offset"] and
             ewo>CONFIG["ewo_high"] and rsi<CONFIG["rsi_buy"] and
             rsi<CONFIG["rsi_ewo1_max"] and volume_ok and close_lt_sell) or
            (rsi_fast<CONFIG["rsi_fast_buy"] and ema_offset<CONFIG["low_offset_2"] and
             ewo>CONFIG["ewo_high_2"] and rsi<CONFIG["rsi_buy"] and
             rsi<CONFIG["rsi_ewo2_max"] and volume_ok and close_lt_sell) or
            (rsi_fast<CONFIG["rsi_fast_buy"] and ema_offset<CONFIG["low_offset"] and
             ewo<CONFIG["ewo_low"] and volume_ok and close_lt_sell)
        )

        # ── Midtrend ──
        ema100_24ago = float(ema100_s[-25]) if len(ema100_s)>=25 else ema100
        midtrend_guard = bool(
            c[-1] > ema100 * CONFIG["midtrend_close_ema100_min"] and
            ema50  > ema100 * CONFIG["midtrend_ema50_ema100_min"] and
            (ema100_24ago<=0 or ema100 > ema100_24ago * CONFIG["midtrend_ema100_trend_min"])
        )

        # ═══════════════════════════════════════════════════════════════════
        # [R1] REVERSAL CONFIDENCE SCORE (Kernstück V10)
        # Misst wie stark ein Coin gerade dreht (0.0 = kein Reversal, 1.0 = perfekt)
        # RSI_fast-Anstieg ist doppelt gewichtet (Pflicht-Signal wie in v5)
        # ═══════════════════════════════════════════════════════════════════
        rev_rsi_fast_rising = (rsi_fast > prev_rsi_fast and rsi_fast > rsi_fast_2ago)
        rev_green_candle    = (green_candle and cur_change > CONFIG["reversal_green_candle_min"])
        rev_volume_spike    = (vol_ratio > CONFIG["reversal_volume_ratio_min"])
        rev_higher_low      = higher_low and higher_close

        # Gewichteter Score: RSI_fast 2× + Rest je 1×
        raw_score = (
            (2.0 if rev_rsi_fast_rising else 0.0) +
            (1.0 if rev_green_candle    else 0.0) +
            (1.0 if rev_volume_spike    else 0.0) +
            (1.0 if rev_higher_low      else 0.0)
        )
        # Normalisiert auf 0.0–1.0 (max = 5.0)
        reversal_confidence = float(raw_score / 5.0)

        # Reversal bestätigt wenn Score ≥ 0.60 (RSI_fast + 1 weiteres)
        reversal_confirmed = reversal_confidence >= CONFIG["reversal_confidence_min_entry"]

        # ═══════════════════════════════════════════════════════════════════
        # [R3] ANTI LATE-ENTRY: bounce_from_low
        # Misst wie weit der Coin bereits vom lokalen Tief gestiegen ist.
        # Wenn > 3.5% → Move bereits vorbei, kein Entry.
        # ═══════════════════════════════════════════════════════════════════
        local_low_12 = float(np.min(l[-12:])) if len(l) >= 12 else float(l[-1])
        bounce_from_low = float((c[-1] - local_low_12) / local_low_12) if local_low_12 > 0 else 0.0
        late_entry_blocked = (
            bounce_from_low > CONFIG["late_entry_bounce_max"] or
            rsi > CONFIG["late_entry_rsi_max"]
        )

        # ═══════════════════════════════════════════════════════════════════
        # [B1] BTC MOMENTUM FILTER (verschärft)
        # Prüft BTC EMA200-Trend + 30min Momentum + RSI
        # ═══════════════════════════════════════════════════════════════════
        btc_filter_ok    = True
        btc_momentum_ok  = True
        btc_ema200_val   = None
        btc_price_change_30m = 0.0

        if len(self.btc_prices_15m) >= 200:
            btc_c = np.array(self.btc_prices_15m, dtype=np.float64)
            btc_ema200_arr = ta.EMA(btc_c, 200)
            btc_ema50_arr  = ta.EMA(btc_c, 50)
            btc_rsi_arr    = ta.RSI(btc_c, 14)
            btc_ema200_val = float(btc_ema200_arr[-1])
            btc_ema50_val  = float(btc_ema50_arr[-1])
            btc_rsi_val    = float(btc_rsi_arr[-1])

            # BTC EMA200-Trend
            btc_ema200_trend = float(btc_ema200_arr[-1]/btc_ema200_arr[-5]) if len(btc_ema200_arr)>=5 and btc_ema200_arr[-5]>0 else 1.0
            btc_price_change_30m = float((btc_c[-1]-btc_c[-3])/btc_c[-3]) if len(btc_c)>=3 and btc_c[-3]>0 else 0.0
            btc_price_change_1h  = float((btc_c[-1]-btc_c[-5])/btc_c[-5]) if len(btc_c)>=5 and btc_c[-5]>0 else 0.0

            btc_filter_ok = (
                btc_c[-1] >= btc_ema200_val * CONFIG["btc_ema200_min"] and
                btc_ema50_val >= btc_ema200_val * CONFIG["btc_ema50_ema200_min"] and
                btc_rsi_val >= CONFIG["btc_rsi_min"]
            )
            # [B1] BTC Momentum: 30min Trend + EMA200-Stabilität
            btc_momentum_ok = (
                btc_price_change_30m >= CONFIG["btc_price_change_30m_min"] and
                btc_ema200_trend >= CONFIG["btc_ema200_trend_min"]
            )

        # ═══════════════════════════════════════════════════════════════════
        # [C1] COIN vs BTC STÄRKE
        # ═══════════════════════════════════════════════════════════════════
        coin_vs_btc_ok = True
        if len(self.btc_prices_15m) >= 5 and self.btc_prices_15m[-5] > 0:
            btc_arr = np.array(self.btc_prices_15m, dtype=np.float64)
            btc_ret_1h = float((btc_arr[-1]-btc_arr[-5])/btc_arr[-5])
            coin_vs_btc_ok = price_change_1h >= btc_ret_1h - 0.025

        return {
            # Core
            "ewo": ewo, "ewo_delta_3": ewo_d3,
            "rsi": rsi, "rsi_fast": rsi_fast,
            "rsi_fast_delta_3": rsi_fast_d3, "rsi_slow": rsi_slow_v,
            "ema_offset": ema_offset, "ema_offset_delta_3": ema_off_d3,
            "ema_18": ema_18, "ema_50": ema50, "ema_100": ema100, "ema_200": ema200,
            "close_ema100_ratio": close_ema100_ratio,
            "close_ema200_ratio": close_ema200_ratio,
            "ema100_ema200_ratio": ema100_ema200_ratio,
            "ema100_trend_ratio": ema100_trend_ratio,
            "ema200_trend_ratio": ema200_trend_ratio,
            # Trend
            "trend_filter_ok": trend_filter_ok,
            # Falling Knife
            "is_falling_knife": is_falling_knife,
            "falling_knife_hard": falling_knife_hard,
            "falling_knife_soft": falling_knife_soft,
            "falling_knife_1h": falling_knife_1h,
            "falling_knife_15m": falling_knife_15m,  # [F1] neu
            "drawdown_from_12h_high": drawdown_from_high,
            # BTC
            "btc_filter_ok": btc_filter_ok,
            "btc_momentum_ok": btc_momentum_ok,        # [B1] neu
            "btc_ema200": btc_ema200_val,
            "btc_price_change_30m": btc_price_change_30m,
            # Volatility
            "atr_pct": atr_pct,
            "atr_regime_ok": atr_regime_ok,            # [V1] neu
            # [R1] Reversal
            "reversal_confidence": reversal_confidence,
            "reversal_confirmed": reversal_confirmed,
            "reversal_rsi_fast_rising": rev_rsi_fast_rising,
            "reversal_green": rev_green_candle,
            "reversal_volume": rev_volume_spike,
            "reversal_higher_low": rev_higher_low,
            # [R3] Anti-Late-Entry
            "bounce_from_low": bounce_from_low,
            "late_entry_blocked": late_entry_blocked,  # [R3] neu
            # [C1] Coin Stärke
            "coin_vs_btc_ok": coin_vs_btc_ok,
            # Preis
            "price_change_15m": price_change_15m,
            "price_change_30m": price_change_30m,
            "price_change_1h": price_change_1h,
            "price_change_2h": price_change_2h,
            # BB
            "bb_width": bb_width, "bb_pos": bb_pos,
            "bb_lower": bb_low, "bb_lower_prev": bb_lower_prev,
            "bb_mid": bb_mid, "bb_upper": bb_up,
            # MFI / RSI
            "mfi": mfi, "prev_mfi": prev_mfi,
            "prev_rsi": prev_rsi, "prev_rsi_fast": prev_rsi_fast,
            "rsi_slope": rsi_slope, "rsi_fast_slope": rsi_fast_slope,
            "rsi_fast_gt_slow": bool(rsi_fast > rsi_slow_v),
            # Candle
            "cur_change": cur_change, "prev_change": prev_change,
            "green_candle": green_candle, "higher_close": higher_close,
            "higher_low": higher_low, "prev_close": prev_close,
            # Midtrend
            "midtrend_guard": midtrend_guard,
            "recentispumping": recentispumping,
            "in_core_space": in_core_space,
            # Profit / Volume
            "profit_potential": profit_potential,
            "vol_ratio": vol_ratio,
            "volume_candle": float(v[-1]) if len(v)>0 else 0.0,
            "is_pumping": is_pumping, "change_24h": change_24h,
            "price": float(c[-1]),
        }


# ─── WebSocket Manager (unverändert von V9) ──────────────────────────────────

class KlineWSManager:
    WS_BASE = "wss://stream.binance.com:9443/stream"

    def __init__(self, analyzers: Dict[str, PairAnalyzer]):
        self.analyzers = analyzers; self.symbols: List[str] = []
        self.running = False; self._ws = None; self._task = None
        self.connected = False; self.last_msg_ts: float = 0; self.msg_count: int = 0

    async def start(self, symbols: List[str]):
        if not CONFIG.get("ws_enabled", False): return
        self.symbols = list(symbols); self.running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self._ws and not self._ws.closed: await self._ws.close()
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass

    def update_symbols(self, symbols: List[str]):
        if set(symbols) != set(self.symbols):
            self.symbols = list(symbols)
            if self._ws and not self._ws.closed:
                asyncio.create_task(self._ws.close())

    async def _run(self):
        intervals = CONFIG.get("ws_kline_intervals", ["5m","15m"])
        reconnect_delay = CONFIG.get("ws_reconnect_seconds", 5)
        max_streams = CONFIG.get("ws_max_streams_per_conn", 250)
        while self.running:
            try:
                streams = []
                for sym in self.symbols:
                    for ivl in intervals:
                        streams.append(f"{sym.lower()}@kline_{ivl}")
                        if len(streams) >= max_streams: break
                    if len(streams) >= max_streams: break
                if not streams: await asyncio.sleep(5); continue
                url = f"{self.WS_BASE}?streams={'/'.join(streams)}"
                connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
                session = aiohttp.ClientSession(connector=connector)
                try:
                    self._ws = await session.ws_connect(url, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=30))
                    self.connected = True
                    logger.info(f"📡 WS connected: {len(streams)} streams")
                    async for msg in self._ws:
                        if not self.running: break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                kd = data.get("data",{}).get("k",{})
                                sym = kd.get("s",""); ivl = kd.get("i","")
                                if sym in self.analyzers and ivl in intervals:
                                    self.analyzers[sym].update_ws_kline(ivl, kd)
                                    self.last_msg_ts = time.time(); self.msg_count += 1
                            except Exception: pass
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED): break
                finally:
                    self.connected = False
                    if not self._ws.closed: await self._ws.close()
                    await session.close()
            except asyncio.CancelledError: break
            except Exception as e:
                logger.warning(f"📡 WS error: {e}"); self.connected = False
            if self.running: await asyncio.sleep(reconnect_delay)


# ─── Pairlist Injector ────────────────────────────────────────────────────────

class PairlistInjector:

    def __init__(self, exchange: str = "binance"):
        self.exchange = exchange.lower().strip()
        if self.exchange not in {"binance","kucoin"}:
            raise ValueError(f"Unsupported exchange: {exchange}")
        self.exchange_display = "KuCoin" if self.exchange == "kucoin" else "Binance"
        self.supports_ws = self.exchange == "binance"
        self.state_file  = self._resolve_state_file(CONFIG["state_file"])
        self.analyzers:   Dict[str, PairAnalyzer] = {}
        self.sticky_pairs:    Dict[str, dict] = {}
        self.shadow_blacklist: Dict[str, dict] = {}
        self.probation_pairs:  Dict[str, dict] = {}
        self.current_pairs:    List[str] = []
        self.current_pairs_by_exchange: Dict[str, List[str]] = {"binance":[],"kucoin":[]}
        self.pair_scores:  Dict[str, dict] = {}
        self.last_update   = None
        self.valid_symbols: Set[str] = set()
        self.valid_pairs_by_exchange: Dict[str, Set[str]] = {"binance":set(),"kucoin":set()}
        self.ticker_cache: Dict[str, dict] = {}
        self.ticker_cache_last_update: float = 0.0
        self.last_ticker_source: str = "none"
        self.last_ticker_error_log_ts:  float = 0.0
        self.last_ticker_cache_warn_ts: float = 0.0
        self.last_no_ticker_log_ts:     float = 0.0
        self.binance_cooldown_until_ts: float = 0.0
        self.last_binance_status_code: Optional[int] = None
        self.last_binance_cooldown_log_ts: float = 0.0
        self.volume_history: Dict[str, List[List[float]]] = {}
        self.last_volume_sample_ts: float = 0.0
        self.volume_seed_rate_lock = asyncio.Lock()
        self.volume_seed_next_request_ts: float = 0.0
        self.last_volume_seed_attempted = self.last_volume_seeded = 0
        self.last_volume_seed_failed = self.last_volume_seed_pending = 0
        self.running = False; self.cycle_count = 0
        self.ws_manager: Optional[KlineWSManager] = None
        self.initial_backfill_done = False
        # Stats
        self.stats_falling_knife_blocked: int = 0
        self.stats_trend_filter_blocked:  int = 0
        self.stats_btc_filter_blocked:    int = 0
        self.stats_late_entry_blocked:    int = 0
        self.stats_atr_regime_blocked:    int = 0
        self.stats_reversal_blocked:      int = 0
        self.stats_profit_gate_blocked:   int = 0
        self.stats_recentispumping_blocked: int = 0
        self.pair_blacklist_regexes: List[re.Pattern] = []
        for p in CONFIG.get("pair_blacklist_patterns",[]):
            try: self.pair_blacklist_regexes.append(re.compile(p))
            except re.error as e: logger.error(f"Invalid pattern '{p}': {e}")
        self.load_state()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def symbol_to_pair(self, symbol: str) -> str:
        if "/" in symbol: return symbol
        if self.exchange == "kucoin" and "-" in symbol:
            b, q = symbol.split("-",1); return f"{b}/{q}"
        quote = _quote_currency_for_symbol(symbol) or CONFIG["base_currency"]
        return f"{symbol[:-len(quote)]}/{quote}" if symbol.endswith(quote) else symbol

    def pair_to_symbol(self, pair: str) -> str:
        return pair.replace("/","-") if self.exchange == "kucoin" else pair.replace("/","")

    def _quote_currency_for_symbol(self, symbol: str) -> str:
        return _quote_currency_for_symbol(symbol) or CONFIG["base_currency"]

    def _btc_symbol_for_symbol(self, symbol: str) -> str:
        return f"BTC{self._quote_currency_for_symbol(symbol)}"

    def _resolve_state_file(self, sf: str) -> str:
        p = Path(sf)
        p.parent.mkdir(parents=True, exist_ok=True)
        stem = p.stem
        suf = p.suffix or ".json"
        return str(p.with_name(f"{stem}.{self.exchange}{suf}"))

    def _cfg(self, key: str, default=None):
        sk = f"{key}_{self.exchange}"
        return CONFIG[sk] if sk in CONFIG else CONFIG.get(key, default)

    def _min_volume_quote_threshold(self) -> float: return float(self._cfg("min_volume_quote",0.0))
    def _min_score_threshold(self) -> int:          return int(self._cfg("min_score",0))
    def _elite_score_threshold(self) -> int:        return int(self._cfg("elite_score",0))
    def _probation_min_score_threshold(self) -> int:return int(self._cfg("probation_min_score",0))
    def _profit_threshold(self) -> float:           return float(self._cfg("profit_threshold",1.0))

    def is_pair_blacklisted_pair(self, pair: str) -> bool:
        return any(rx.fullmatch(pair) for rx in self.pair_blacklist_regexes)
    def is_pair_blacklisted(self, symbol: str) -> bool:
        return self.is_pair_blacklisted_pair(self.symbol_to_pair(symbol))

    @staticmethod
    def _clamp01(v: float) -> float: return max(0.0, min(1.0, v))
    @staticmethod
    def _safe_float(value, default: float) -> float:
        try: out=float(value); return out if np.isfinite(out) else default
        except: return default
    @staticmethod
    def _safe_quote_volume(value) -> float:
        try: v=float(value); return v if np.isfinite(v) else 0.0
        except: return 0.0
    @staticmethod
    def _exc_text(exc: Exception) -> str:
        return str(exc).strip() or repr(exc)

    def _throttled_log(self, level: int, msg: str, attr: str, interval: float):
        now = time.time()
        if (now - float(getattr(self, attr, 0.0))) >= max(0.0, float(interval)):
            logger.log(level, msg); setattr(self, attr, now)

    def _binance_base_urls(self) -> List[str]:
        raw = CONFIG.get("binance_api_base_urls",[])
        out = []
        seen = set()
        for value in raw:
            if not isinstance(value, str):
                continue
            url = value.strip().rstrip("/")
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out or ["https://data-api.binance.vision", "https://api.binance.com"]

    def _kucoin_base_url(self) -> str:
        raw = str(CONFIG.get("kucoin_api_base_url","https://api.kucoin.com")).strip().rstrip("/")
        return raw if raw.startswith("http") else "https://api.kucoin.com"

    def _binance_cooldown_remaining(self) -> float:
        return max(0.0, float(self.binance_cooldown_until_ts) - time.time())

    def _extract_binance_retry_after_seconds(self, headers, payload, fallback_seconds: float) -> float:
        candidates = []
        if headers:
            for key in ("Retry-After", "retry-after", "retryAfter"):
                value = headers.get(key)
                if value:
                    candidates.append(value)
        if isinstance(payload, dict):
            value = payload.get("retryAfter")
            if value is not None:
                candidates.append(value)
        for value in candidates:
            try:
                retry_value = float(value)
            except (TypeError, ValueError):
                continue
            now = time.time()
            if retry_value > 1_000_000_000_000:
                return max(1.0, (retry_value / 1000.0) - now)
            if retry_value > 1_000_000_000:
                return max(1.0, retry_value - now)
            return max(1.0, retry_value)
        return max(1.0, float(fallback_seconds))

    def _activate_binance_cooldown(self, status_code: int, retry_seconds: float, *, base: str, path: str):
        retry_seconds = max(1.0, float(retry_seconds))
        self.binance_cooldown_until_ts = max(self.binance_cooldown_until_ts, time.time() + retry_seconds)
        self.last_binance_status_code = int(status_code)
        self._throttled_log(
            logging.WARNING,
            f"Binance HTTP {status_code} on {base}{path}; pausing Binance requests for {int(retry_seconds)}s",
            "last_binance_cooldown_log_ts",
            30.0,
        )

    async def _request_json_from_binance(self, session, path, params=None, *, log_failures=False, log_attr=None, log_interval=120.0, log_prefix=None):
        cooldown_remaining = self._binance_cooldown_remaining()
        if cooldown_remaining > 0:
            self._throttled_log(
                logging.WARNING,
                f"Binance requests paused for {int(cooldown_remaining)}s after HTTP {self.last_binance_status_code or '?'}",
                "last_binance_cooldown_log_ts",
                30.0,
            )
            return None
        errors = []
        for base in self._binance_base_urls():
            try:
                async with session.get(f"{base}{path}", params=params) as resp:
                    if resp.status in {418, 429}:
                        body_text = await resp.text()
                        payload = None
                        try:
                            payload = json.loads(body_text) if body_text else None
                        except json.JSONDecodeError:
                            payload = None
                        fallback = CONFIG["binance_418_cooldown_seconds"] if resp.status == 418 else CONFIG["binance_429_cooldown_seconds"]
                        retry_seconds = self._extract_binance_retry_after_seconds(resp.headers, payload, fallback)
                        self._activate_binance_cooldown(resp.status, retry_seconds, base=base, path=path)
                        errors.append(f"{base} HTTP {resp.status} cooldown={int(retry_seconds)}s")
                        break
                    if resp.status != 200:
                        errors.append(f"{base} HTTP {resp.status}")
                        continue
                    return await resp.json()
            except Exception as e:
                errors.append(f"{base} {type(e).__name__}: {self._exc_text(e)}")
        if log_failures and errors:
            msg = f"{log_prefix}: {' | '.join(errors)}" if log_prefix else " | ".join(errors)
            if log_attr: self._throttled_log(logging.ERROR, msg, log_attr, log_interval)
            else: logger.error(msg)
        return None

    async def _request_json_from_kucoin(self, session, path, params=None, *, log_failures=False, log_attr=None, log_interval=120.0, log_prefix=None):
        url = f"{self._kucoin_base_url()}{path}"
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    if log_failures:
                        msg = f"{log_prefix or 'KuCoin'}: HTTP {resp.status}"
                        if log_attr: self._throttled_log(logging.ERROR, msg, log_attr, log_interval)
                        else: logger.error(msg)
                    return None
                return await resp.json()
        except Exception as e:
            if log_failures:
                msg = f"{log_prefix or 'KuCoin'}: {type(e).__name__}: {self._exc_text(e)}"
                if log_attr: self._throttled_log(logging.ERROR, msg, log_attr, log_interval)
                else: logger.error(msg)
            return None

    # ═══════════════════════════════════════════════════════════════════════════
    # [R1] REVERSAL CONFIDENCE PROXIMITY
    # Entry-Proximity basiert jetzt auf Reversal-Stärke, nicht auf Dip-Tiefe.
    # ═══════════════════════════════════════════════════════════════════════════

    def predict_entry_alignment(self, indicators: Dict) -> Dict:
        """
        V10: Proximity = Reversal-Confidence × Trigger-Nähe
        Dip-Tiefe alleine gibt keine Punkte mehr.
        """
        ewo         = float(indicators.get("ewo", 0.0))
        rsi         = float(indicators.get("rsi", 100.0))
        rsi_fast    = float(indicators.get("rsi_fast", 100.0))
        ema_offset  = float(indicators.get("ema_offset", 1.0))
        profit_pot  = float(indicators.get("profit_potential", 1.0))
        ewo_delta   = float(indicators.get("ewo_delta_3", 0.0))
        rsi_fast_d  = float(indicators.get("rsi_fast_delta_3", 0.0))
        ema_off_d   = float(indicators.get("ema_offset_delta_3", 0.0))
        volume_candle   = float(indicators.get("volume_candle", 0.0))
        profit_threshold= self._profit_threshold()
        trend_ok    = bool(indicators.get("trend_filter_ok", False))
        is_fk       = bool(indicators.get("is_falling_knife", False))
        btc_ok      = bool(indicators.get("btc_filter_ok", True))
        btc_mom_ok  = bool(indicators.get("btc_momentum_ok", True))
        late_blocked= bool(indicators.get("late_entry_blocked", False))
        atr_ok      = bool(indicators.get("atr_regime_ok", True))
        rev_conf    = float(indicators.get("reversal_confidence", 0.0))
        cl = self._clamp01

        # Hard-Blocks: 0.0 Proximity
        if is_fk or late_blocked or not atr_ok:
            return {"best_pattern":"none","best_proximity":0.0,
                    "by_pattern":{"ewo1":0.0,"ewo2":0.0,"ewolow":0.0},
                    "near_entry":False,"reversal_confidence":rev_conf}

        # Penalties
        trend_pen = 0.0 if trend_ok  else 0.25
        btc_pen   = 0.0 if btc_ok    else 0.15
        btcm_pen  = 0.0 if btc_mom_ok else 0.10

        def _trigger_proximity_ewo1() -> float:
            d = [
                cl(max(0.0, rsi_fast - CONFIG["rsi_fast_buy"]) / 15.0),
                cl(max(0.0, ema_offset - CONFIG["low_offset"]) / 0.03),
                cl(max(0.0, CONFIG["ewo_high"] - ewo) / 5.0),
                cl(max(0.0, rsi - CONFIG["rsi_ewo1_max"]) / 20.0),
                0.0 if volume_candle > 0 else 1.0,
                cl(max(0.0, profit_threshold - profit_pot) / 0.03),
            ]
            base = 1.0 - (sum(d)/len(d))
            mom = 0.0
            if ewo_delta   > 0: mom += 0.05
            if rsi_fast_d  < 0: mom += 0.05
            if ema_off_d   < 0: mom += 0.05
            return cl(base + mom - trend_pen - btc_pen - btcm_pen)

        def _trigger_proximity_ewo2() -> float:
            d = [
                cl(max(0.0, rsi_fast - CONFIG["rsi_fast_buy"]) / 15.0),
                cl(max(0.0, ema_offset - CONFIG["low_offset_2"]) / 0.05),
                cl(max(0.0, CONFIG["ewo_high_2"] - ewo) / 8.0),
                cl(max(0.0, rsi - CONFIG["rsi_ewo2_max"]) / 20.0),
                0.0 if volume_candle > 0 else 1.0,
                cl(max(0.0, profit_threshold - profit_pot) / 0.03),
            ]
            base = 1.0 - (sum(d)/len(d))
            mom = 0.0
            if ewo_delta  > 0: mom += 0.06
            if rsi_fast_d < 0: mom += 0.05
            if ema_off_d  < 0: mom += 0.04
            return cl(base + mom - trend_pen - btc_pen - btcm_pen)

        def _trigger_proximity_ewolow() -> float:
            rsi_window_ok = 20 < rsi < 34
            rsi_pen = 0.0 if rsi_window_ok else 0.20
            d = [
                cl(max(0.0, rsi_fast - CONFIG["rsi_fast_buy"]) / 15.0),
                cl(max(0.0, ema_offset - CONFIG["low_offset"]) / 0.03),
                cl(max(0.0, ewo - CONFIG["ewo_low"]) / 8.0),
                0.0 if volume_candle > 0 else 1.0,
                cl(max(0.0, profit_threshold - profit_pot) / 0.03),
            ]
            base = 1.0 - (sum(d)/len(d))
            mom = 0.0
            if ewo_delta  < 0: mom += 0.06
            if rsi_fast_d < 0: mom += 0.04
            if ema_off_d  < 0: mom += 0.04
            return cl(base + mom - trend_pen - btc_pen - btcm_pen - rsi_pen)

        trigger_prox = {
            "ewo1":   _trigger_proximity_ewo1(),
            "ewo2":   _trigger_proximity_ewo2(),
            "ewolow": _trigger_proximity_ewolow(),
        }
        best_pattern = max(trigger_prox, key=trigger_prox.get)
        trigger_best = trigger_prox[best_pattern]

        # [R1] Finale Proximity = Trigger × Reversal-Confidence
        # Ohne Reversal kein hoher Score möglich
        combined_proximity = cl(trigger_best * 0.5 + rev_conf * 0.5)

        return {
            "best_pattern":       best_pattern,
            "best_proximity":     combined_proximity,
            "trigger_proximity":  trigger_best,
            "reversal_confidence":rev_conf,
            "by_pattern":         trigger_prox,
            "near_entry":         combined_proximity >= CONFIG["near_entry_proximity_threshold"],
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # SCORE PAIR – V10 Reversal-First Scoring
    # ═══════════════════════════════════════════════════════════════════════════

    def score_pair(self, symbol: str, ticker: dict) -> dict:
        """
        V10 Score-System:
        - Falling Knife / Late Entry / ATR-Regime / BTC-Momentum → sofort 0
        - Trend-Filter: +20 / -20
        - [R1] Reversal Confidence: bis +35 Bonus (Hauptscore)
        - Dip-EWO: nur noch Trigger-Punkte (kleiner als v9)
        - [R3] Late Entry: Penalty wenn Bounce bereits > 2%
        """
        analyzer   = self.analyzers[symbol]
        indicators = analyzer.get_indicators()

        empty_result = {
            "symbol": symbol, "score": 0, "reasons": [],
            "entries": [], "indicators": {}, "prediction": {},
            "tier": "none", "exact_entry_ready": False,
            "exact_entry_tags": [], "profit_window_near": False,
        }
        if not indicators:
            empty_result["reasons"] = ["Insufficient data"]
            return empty_result

        score = 0; reasons = []; entries = []; exact_entry_tags = []

        ewo          = indicators["ewo"]
        rsi          = indicators["rsi"]
        rsi_fast     = indicators["rsi_fast"]
        ema_offset   = indicators["ema_offset"]
        profit_pot   = indicators["profit_potential"]
        is_pumping   = indicators["is_pumping"]
        atr_pct      = indicators["atr_pct"]
        vol_ratio    = indicators["vol_ratio"]
        recentispumping = indicators["recentispumping"]
        trend_ok     = indicators["trend_filter_ok"]
        is_fk        = indicators["is_falling_knife"]
        btc_ok       = indicators["btc_filter_ok"]
        btc_mom_ok   = indicators["btc_momentum_ok"]
        atr_ok       = indicators["atr_regime_ok"]
        rev_conf     = indicators["reversal_confidence"]
        late_blocked = indicators["late_entry_blocked"]
        bounce       = indicators["bounce_from_low"]
        profit_thr   = self._profit_threshold()

        # ── Hard Blocks ──────────────────────────────────────────────────────
        if is_fk:
            self.stats_falling_knife_blocked += 1
            fk_type = ("hard" if indicators.get("falling_knife_hard") else
                       "soft" if indicators.get("falling_knife_soft") else
                       "15m"  if indicators.get("falling_knife_15m")  else "1h")
            return {**empty_result, "reasons": [f"🔪 FALLING KNIFE ({fk_type})"],
                    "entries": ["falling_knife"],
                    "prediction": {"best_pattern":"none","best_proximity":0.0,
                                   "reversal_confidence":rev_conf,"near_entry":False}}

        if not atr_ok:
            self.stats_atr_regime_blocked += 1
            return {**empty_result, "reasons": [f"⚡ ATR-REGIME BLOCKED (atr={atr_pct:.3f})"],
                    "entries": ["atr_blocked"]}

        if late_blocked:
            self.stats_late_entry_blocked += 1
            return {**empty_result, "reasons": [f"⏰ LATE ENTRY BLOCKED (bounce={bounce*100:.1f}%, rsi={rsi:.0f})"],
                    "entries": ["late_entry"]}

        # ── [A3] Trend-Filter ────────────────────────────────────────────────
        if trend_ok:
            score += 20; reasons.append("📈 Trend OK (+20)"); entries.append("trend_ok")
        else:
            score -= 20; reasons.append("⚠️ Trend FAIL (-20)"); entries.append("trend_fail")
            self.stats_trend_filter_blocked += 1

        # ── [B1] BTC-Filter ──────────────────────────────────────────────────
        if not btc_ok:
            score -= 15; reasons.append("₿ BTC unter EMA200 (-15)")
            self.stats_btc_filter_blocked += 1
        if not btc_mom_ok:
            score -= 10; reasons.append("₿ BTC Momentum negativ (-10)")

        # ── [C1] Coin Stärke ─────────────────────────────────────────────────
        if not indicators.get("coin_vs_btc_ok", True):
            score -= 8; reasons.append("📉 Coin schwächer als BTC (-8)")

        # ── Profit Gate ──────────────────────────────────────────────────────
        has_profit = profit_pot >= profit_thr
        if not has_profit: self.stats_profit_gate_blocked += 1

        # ── EWO (Trigger-Punkte) ─────────────────────────────────────────────
        # V10: EWO bringt nur Trigger-Punkte, kein Full-Score mehr
        if ewo >= CONFIG["ewo_very_high_threshold"]:
            score += 25; reasons.append(f"EWO trigger strong ({ewo:.1f})"); entries.append("ewo_strong")
        elif ewo >= CONFIG["ewo_high_threshold"]:
            score += 18; reasons.append(f"EWO trigger ({ewo:.1f})"); entries.append("ewo_strong")
        elif ewo >= CONFIG["ewo_positive_threshold"]:
            score += 10; reasons.append(f"EWO positive ({ewo:.1f})"); entries.append("ewo_positive")
        if ewo <= CONFIG["ewo_very_negative_threshold"]:
            score += 25; reasons.append(f"EWO neg trigger ({ewo:.1f})"); entries.append("ewo_negative")
        elif ewo <= CONFIG["ewo_negative_threshold"]:
            score += 15; reasons.append(f"EWO neg ({ewo:.1f})"); entries.append("ewo_negative")

        # ── RSI ──────────────────────────────────────────────────────────────
        if rsi < CONFIG["rsi_very_oversold"]:
            score += 15; reasons.append(f"RSI very oversold ({rsi:.0f})"); entries.append("rsi_weak")
        elif rsi < CONFIG["rsi_oversold"]:
            score += 10; reasons.append(f"RSI oversold ({rsi:.0f})"); entries.append("rsi_weak")
        if rsi_fast < CONFIG["rsi_fast_oversold"]:
            score += 15; reasons.append(f"RSI_fast entry ({rsi_fast:.0f})"); entries.append("rsi_weak")
        elif rsi_fast < CONFIG["rsi_fast_weak"]:
            score += 8; reasons.append(f"RSI_fast weak ({rsi_fast:.0f})")

        # ── EMA Offset ───────────────────────────────────────────────────────
        if ema_offset < CONFIG["ema_offset_deep"]:
            score += 15; reasons.append(f"Deep below EMA ({(1-ema_offset)*100:.1f}%)"); entries.append("below_ema")
        elif ema_offset < CONFIG["ema_offset_good"]:
            score += 12; reasons.append(f"Below EMA ({(1-ema_offset)*100:.1f}%)"); entries.append("below_ema")
        elif ema_offset < CONFIG["ema_offset_weak"]:
            score += 6; reasons.append("Slightly below EMA")

        # ── [R1] REVERSAL CONFIDENCE SCORE BONUS ────────────────────────────
        # Hauptbonus in V10: bis +35 Punkte für perfektes Reversal
        if rev_conf >= 0.80:
            rev_bonus = int(CONFIG["reversal_score_bonus_max"])
            score += rev_bonus; reasons.append(f"🔄 Reversal sehr stark +{rev_bonus} (conf={rev_conf:.2f})")
            entries.append("reversal_strong")
        elif rev_conf >= 0.60:
            rev_bonus = int(CONFIG["reversal_score_bonus_max"] * 0.70)
            score += rev_bonus; reasons.append(f"🔄 Reversal bestätigt +{rev_bonus} (conf={rev_conf:.2f})")
            entries.append("reversal_confirmed")
        elif rev_conf >= 0.40:
            rev_bonus = int(CONFIG["reversal_score_bonus_max"] * 0.35)
            score += rev_bonus; reasons.append(f"🔄 Reversal schwach +{rev_bonus} (conf={rev_conf:.2f})")
            entries.append("reversal_weak")
        else:
            # Kein Reversal = Penalty
            score += CONFIG["reversal_penalty_per_missing"]
            reasons.append(f"⛔ Kein Reversal ({CONFIG['reversal_penalty_per_missing']} pts)")
            self.stats_reversal_blocked += 1

        # [R3] Late-Entry Partial Penalty (nicht hard-block, aber Score-Abzug)
        if bounce > 0.02:
            bounce_pen = int(-10 * min(1.0, (bounce - 0.02) / 0.02))
            if bounce_pen < 0:
                score += bounce_pen; reasons.append(f"⏰ Bounce {bounce*100:.1f}% ({bounce_pen})")

        # ── Exakte Entry-Erkennung (mod3 v5 exakt) ──────────────────────────
        volume_candle = float(indicators.get("volume_candle", 0.0))
        volume_ok     = volume_candle > 0
        close_lt_sell = ema_offset < CONFIG["high_offset"]

        core_ewo1 = (rsi_fast < CONFIG["rsi_fast_buy"] and ema_offset < CONFIG["low_offset"] and
                     ewo > CONFIG["ewo_high"] and rsi < CONFIG["rsi_buy"] and
                     rsi < CONFIG["rsi_ewo1_max"] and volume_ok and close_lt_sell)
        if core_ewo1 and has_profit and trend_ok and rev_conf >= CONFIG["reversal_confidence_min_entry"]:
            score += 40; reasons.append("🎯 EWO1 ENTRY MET"); entries.append("ewo1_ready")
            exact_entry_tags.append("ewo1")

        core_ewo2 = (rsi_fast < CONFIG["rsi_fast_buy"] and ema_offset < CONFIG["low_offset_2"] and
                     ewo > CONFIG["ewo_high_2"] and rsi < CONFIG["rsi_buy"] and
                     rsi < CONFIG["rsi_ewo2_max"] and volume_ok and close_lt_sell)
        if core_ewo2 and has_profit and trend_ok and rev_conf >= CONFIG["reversal_confidence_min_entry"]:
            score += 50; reasons.append("🎯 EWO2 DEEP DIP MET"); entries.append("ewo2_ready")
            exact_entry_tags.append("ewo2")

        core_ewolow = (rsi_fast < CONFIG["rsi_fast_buy"] and ema_offset < CONFIG["low_offset"] and
                       ewo < CONFIG["ewo_low"] and volume_ok and close_lt_sell)
        rsi_ewolow_ok = 20 < rsi < 34
        ema200_rising = indicators.get("ema200_trend_ratio", 1.0) >= 1.001
        if core_ewolow and has_profit and trend_ok and rsi_ewolow_ok and ema200_rising and rev_conf >= CONFIG["reversal_confidence_min_entry"]:
            score += 45; reasons.append("🎯 EWOLOW CAPITULATION MET"); entries.append("ewolow_ready")
            exact_entry_tags.append("ewolow")

        # ── Proximity Bonus ──────────────────────────────────────────────────
        prediction = self.predict_entry_alignment(indicators)
        proximity  = float(prediction.get("best_proximity", 0.0))
        if proximity >= 0.55:
            bonus = int(round(CONFIG["approach_bonus_max"] * (proximity-0.55)/0.45))
            bonus = max(3, min(CONFIG["approach_bonus_max"], bonus))
            score += bonus
            pat = prediction.get("best_pattern","")
            reasons.append(f"Approaching {pat} ({proximity:.2f})")
            if pat == "ewo1":   entries.append("approaching_ewo1")
            elif pat == "ewo2": entries.append("approaching_ewo2")
            elif pat == "ewolow": entries.append("approaching_ewolow")

        # ── Volume ───────────────────────────────────────────────────────────
        if vol_ratio >= 2.0:
            score += 12; reasons.append(f"Volume spike ({vol_ratio:.1f}x)"); entries.append("volume_interest")
        elif vol_ratio >= 1.5:
            score += 6; reasons.append(f"Volume elevated ({vol_ratio:.1f}x)")
        if has_profit:
            if profit_pot >= 1.05: score += 8;  reasons.append(f"Profit pot ({(profit_pot-1)*100:.1f}%)")
            elif profit_pot >= profit_thr: score += 4; reasons.append("Meets profit")

        # ── Penalties ────────────────────────────────────────────────────────
        if atr_pct < float(self._cfg("min_atr_threshold", 0.0)):
            score -= 15; reasons.append("Low volatility")
        if is_pumping:
            score -= 30; reasons.append("Pump detected")
        change_24h = float(ticker.get("priceChangePercent",0))
        if change_24h > CONFIG["max_24h_pump"]:
            score += CONFIG["pump_penalty"]; reasons.append(f"24h pump (+{change_24h:.1f}%)")
        if recentispumping:
            self.stats_recentispumping_blocked += 1

        # ── Backdata ─────────────────────────────────────────────────────────
        if analyzer.backdata_checks >= 10:
            hr = analyzer.backdata_hits / analyzer.backdata_checks
            if hr >= 0.5:
                bd = min(CONFIG["backdata_hit_bonus_max"], int(round(hr*CONFIG["backdata_hit_bonus_max"])))
                score += bd; reasons.append(f"Backdata +{bd}")
            elif hr <= 0.1:
                score += CONFIG["backdata_miss_penalty_max"]

        entries = list(set(entries))
        exact_entry_tags = list(set(exact_entry_tags))
        exact_entry_ready = len(exact_entry_tags) > 0
        profit_window_near = profit_pot >= (profit_thr * float(self._cfg("pre_entry_profit_relax", 0.975)))

        return {
            "symbol": symbol, "score": score, "reasons": reasons, "entries": entries,
            "exact_entry_ready": bool(exact_entry_ready), "exact_entry_tags": exact_entry_tags,
            "profit_window_near": bool(profit_window_near), "indicators": indicators,
            "prediction": prediction,
            "reversal_confidence": rev_conf,
            "tier": "elite" if score >= self._elite_score_threshold() else
                    "good"  if score >= self._min_score_threshold()  else "none",
        }

    # ── Falling Profile ──────────────────────────────────────────────────────

    def _falling_profile(self, pair_score: dict) -> Dict:
        ind = pair_score.get("indicators", {}) or {}
        if bool(ind.get("is_falling_knife", False)):
            return {"blocked": True, "reason": "FALLING_KNIFE_DIRECT", "details": ""}
        drop_15m    = float(ind.get("price_change_15m", 0.0))
        drop_30m    = float(ind.get("price_change_30m", 0.0))
        drop_1h     = float(ind.get("price_change_1h",  0.0))
        c_ema100    = float(ind.get("close_ema100_ratio", 1.0))
        ema100_tr   = float(ind.get("ema100_trend_ratio", 1.0))
        rsi_slope   = float(ind.get("rsi_slope", 0.0))
        rsi_fast_sl = float(ind.get("rsi_fast_slope", 0.0))
        ema_off_d   = float(ind.get("ema_offset_delta_3", 0.0))
        green       = bool(ind.get("green_candle", False))
        higher      = bool(ind.get("higher_close", False))
        structural  = (drop_15m <= float(CONFIG.get("falling_drop_15m", -0.010)) and
                       drop_1h  <= float(CONFIG.get("falling_drop_1h",  -0.025)) and
                       c_ema100 <= float(CONFIG.get("falling_close_ema100_max", 0.992)) and
                       ema100_tr<= float(CONFIG.get("falling_ema100_trend_max", 0.9998)) and
                       rsi_slope<= float(CONFIG.get("falling_rsi_slope_max", -0.8)))
        continuation = (drop_30m <= float(CONFIG.get("falling_drop_30m", -0.016)) and
                        ema_off_d <= float(CONFIG.get("falling_ema_offset_delta_max", -0.002)) and
                        rsi_fast_sl <= float(CONFIG.get("falling_rsi_fast_slope_max", -1.2)) and
                        not (green and higher))
        reason = ("STRUCTURAL_DOWNTREND" if structural else
                  "CONTINUATION_SELL_OFF" if continuation else "")
        details = (f"d15={drop_15m*100:.2f}%, d1h={drop_1h*100:.2f}%, "
                   f"c/ema100={c_ema100:.3f}, ema100_trend={ema100_tr:.4f}")
        return {"blocked": bool(reason), "reason": reason, "details": details}

    # ── State ────────────────────────────────────────────────────────────────

    def save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"sticky": self.sticky_pairs, "blacklist": self.shadow_blacklist,
                           "probation": self.probation_pairs, "volume_history": self.volume_history,
                           "last_volume_sample_ts": self.last_volume_sample_ts,
                           "ticker_cache": self.ticker_cache,
                           "ticker_cache_last_update": self.ticker_cache_last_update,
                           "ts": time.time()}, f, indent=2)
        except Exception as e: logger.error(f"Failed to save state: {e}")

    def load_state(self):
        p = Path(self.state_file)
        if not p.exists(): return
        try:
            with open(p,"r") as f: s = json.load(f)
            now = time.time(); age = now - self._safe_float(s.get("ts",0.0),0.0)
            if age < 7200:
                self.sticky_pairs     = s.get("sticky",{})
                self.shadow_blacklist = s.get("blacklist",{})
                self.probation_pairs  = s.get("probation",{})
                raw = s.get("volume_history",{})
                if isinstance(raw, dict):
                    parsed = {}
                    for sym, rows in raw.items():
                        if not isinstance(rows, list): continue
                        clean = [[self._safe_float(r[0],0.0), self._safe_quote_volume(r[1])]
                                 for r in rows if isinstance(r,(list,tuple)) and len(r)>=2]
                        clean = [r for r in clean if r[0]>0]
                        if clean: parsed[sym] = clean[-800:]
                    self.volume_history = parsed
                self.last_volume_sample_ts = self._safe_float(s.get("last_volume_sample_ts",0.0),0.0)
                logger.info(f"💾 STATE RESTORED: {len(self.sticky_pairs)} sticky, "
                            f"{len(self.shadow_blacklist)} banned, {len(self.probation_pairs)} probation")
        except Exception as e: logger.error(f"State load failed: {e}")

    def format_time_remaining(self, expires: float) -> str:
        rem = expires - time.time()
        if rem <= 0: return "expired"
        m = int(rem/60)
        return f"{m//60}h {m%60}m" if m >= 60 else f"{m}m"

    # ── Crash Recovery ────────────────────────────────────────────────────────

    def check_recovery(self, symbol: str, ban: dict) -> Tuple[bool, str]:
        analyzer = self.analyzers.get(symbol)
        if not analyzer or len(analyzer.prices_5m) < 60: return False, "insufficient data"
        if time.time() - ban.get("banned_at",0) < CONFIG["min_ban_minutes"]*60: return False, "min ban time"
        if analyzer.detect_crash(): return False, "still crashing"
        ind = analyzer.get_indicators()
        if ind.get("is_falling_knife", False): return False, "falling knife"
        c5 = (analyzer.prices_5m[-1]-analyzer.prices_5m[-2])/analyzer.prices_5m[-2]
        if c5 < CONFIG["recovery_5m_max_drop"]: return False, f"weak 5m ({c5*100:.1f}%)"
        atr_r = analyzer.atr_spike_ratio()
        if atr_r and atr_r > CONFIG["recovery_atr_spike_max"]: return False, f"atr high ({atr_r:.2f}x)"
        ban_low = ban.get("ban_low", analyzer.prices_5m[-1])
        bounce = (analyzer.prices_5m[-1]-ban_low)/ban_low if ban_low else 0.0
        if bounce < CONFIG["recovery_bounce_from_low"]: return False, f"no bounce ({bounce*100:.1f}%)"
        if ind.get("ema_offset",0) < CONFIG["recovery_ema_offset_min"]: return False, "below ema"
        if ind.get("rsi_fast",0) < CONFIG["recovery_rsi_fast_min"]:    return False, "rsi_fast low"
        return True, "recovered"

    def check_fast_reentry(self, symbol: str, ban: dict, pair_score: dict) -> Tuple[bool, str]:
        if not CONFIG.get("fast_reentry_enabled", False): return False, "disabled"
        reason = ban.get("reason","")
        if reason not in set(CONFIG.get("fast_reentry_allowed_reasons",[])): return False, f"reason {reason}"
        analyzer = self.analyzers.get(symbol)
        if not analyzer or len(analyzer.prices_5m)<60: return False, "insufficient data"
        if time.time()-ban.get("banned_at",0)<CONFIG["fast_reentry_min_ban_minutes"]*60: return False, "min time"
        if analyzer.detect_crash(): return False, "still crashing"
        ind = analyzer.get_indicators()
        if ind.get("is_falling_knife",False): return False, "falling knife"
        c5 = (analyzer.prices_5m[-1]-analyzer.prices_5m[-2])/analyzer.prices_5m[-2]
        if c5 < CONFIG["fast_reentry_max_5m_drop"]: return False, f"weak 5m"
        atr_r = analyzer.atr_spike_ratio()
        if atr_r and atr_r > CONFIG["fast_reentry_atr_spike_max"]: return False, f"atr high"
        ban_low = ban.get("ban_low", analyzer.prices_5m[-1])
        bounce = (analyzer.prices_5m[-1]-ban_low)/ban_low if ban_low else 0.0
        if bounce < CONFIG["fast_reentry_min_bounce_from_low"]: return False, "no bounce"
        rev_conf = float(ind.get("reversal_confidence",0.0))
        if rev_conf < CONFIG["reversal_confidence_min_pre"]: return False, f"no reversal ({rev_conf:.2f})"
        prediction = pair_score.get("prediction",{})
        proximity  = float(prediction.get("best_proximity",0.0))
        entry_set  = set(pair_score.get("entries",[]))
        ready = bool(entry_set.intersection({"ewo1_ready","ewo2_ready","ewolow_ready"}))
        if not (proximity >= CONFIG["fast_reentry_min_proximity"] or ready): return False, f"not close ({proximity:.2f})"
        if pair_score.get("score",0) < self._probation_min_score_threshold(): return False, "low score"
        return True, f"fast reversal ({rev_conf:.2f}, {prediction.get('best_pattern','-')})"

    def _update_backdata(self, symbol: str, pair_score: dict):
        analyzer = self.analyzers.get(symbol)
        if not analyzer: return
        prediction = pair_score.get("prediction",{})
        proximity  = prediction.get("best_proximity",0.0)
        entry_set  = set(pair_score.get("entries",[]))
        if proximity >= 0.80:
            analyzer.backdata_checks += 1
            if entry_set.intersection({"ewo1_ready","ewo2_ready","ewolow_ready"}):
                analyzer.backdata_hits += 1
        if analyzer.backdata_checks > CONFIG.get("backdata_window_candles",120):
            analyzer.backdata_checks = int(analyzer.backdata_checks*0.9)
            analyzer.backdata_hits   = int(analyzer.backdata_hits*0.9)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def log_dashboard(self, new_elite, new_good, expired, new_bans,
                      universe_size, tradeable_count, analyzed_count, *args):
        now = time.time()
        elite_count = sum(1 for d in self.sticky_pairs.values() if d.get("tier")=="elite")
        good_count  = sum(1 for d in self.sticky_pairs.values() if d.get("tier")=="good")
        ws_status   = "up" if (self.ws_manager and self.ws_manager.connected) else "off"
        exl = self.exchange_display.upper()
        pc  = 22

        print("\n" + "="*115)
        print(f"{'🎯 NASOS INJECTOR V10 — v5 Reversal Edition — ' + exl + ' — CYCLE ' + str(self.cycle_count):^115}")
        print(f"{'Trigger→Reversal System | max_pairs=20 | WS: ' + ws_status:^115}")
        print("="*115)
        print(f"\n📊 ACTIVE: {len(self.sticky_pairs)} ({elite_count}🔥 {good_count}📈) "
              f"| 🚫 BANNED: {len(self.shadow_blacklist)} | 🧪 PROBATION: {len(self.probation_pairs)}")
        print(f"🧭 Universe: {universe_size} | Tradeable: {tradeable_count} | Analyzed: {analyzed_count}")
        print(f"🔪 FK: {self.stats_falling_knife_blocked} | "
              f"📈 Trend-fail: {self.stats_trend_filter_blocked} | "
              f"₿ BTC: {self.stats_btc_filter_blocked} | "
              f"⏰ Late: {self.stats_late_entry_blocked} | "
              f"⚡ ATR: {self.stats_atr_regime_blocked} | "
              f"⛔ NoRev: {self.stats_reversal_blocked}")
        print(f"🌐 Serving: {len(self.current_pairs)} pairs")

        elite_pairs = [(s,d) for s,d in self.sticky_pairs.items() if d.get("tier")=="elite"]
        if elite_pairs:
            print(f"\n{'🔥 ELITE (' + str(len(elite_pairs)) + ')':=^115}")
            print(f"  {'PAIR':<{pc}} {'SCORE':<6} {'EWO':<7} {'RSI':<5} {'RSI_F':<6} {'EMA%':<6} {'REV':<5} {'TREND':<6} {'TIME':<8} ENTRY")
            for sym, data in sorted(elite_pairs, key=lambda x: x[1]["peak_score"], reverse=True)[:15]:
                ind = self.analyzers[sym].get_indicators() if sym in self.analyzers else {}
                sd  = self.pair_scores.get(sym, {})
                ent = ", ".join(sd.get("entries",[])[:2]) if sd.get("entries") else "-"
                tl  = self.format_time_remaining(data["expires"])
                new = "🆕" if any(p["symbol"]==sym for p in new_elite) else "  "
                ema_pct = (1-ind.get("ema_offset",1))*100
                rev = f"{ind.get('reversal_confidence',0.0):.2f}"
                trend = "✅" if ind.get("trend_filter_ok",False) else "❌"
                print(f"{new}{exl}:{sym:<{pc}} {data['peak_score']:<6} {ind.get('ewo',0):<7.1f} "
                      f"{ind.get('rsi',0):<5.0f} {ind.get('rsi_fast',0):<6.0f} {ema_pct:<6.1f} "
                      f"{rev:<5} {trend:<6} {tl:<8} {ent}")
        else:
            print(f"\n{'🔥 ELITE (0)':=^115}\n  Keine Elite-Pairs")

        good_pairs = [(s,d) for s,d in self.sticky_pairs.items() if d.get("tier")=="good"]
        if good_pairs:
            print(f"\n{'📈 GOOD (' + str(len(good_pairs)) + ')':=^115}")
            for sym, data in sorted(good_pairs, key=lambda x: x[1]["peak_score"], reverse=True)[:20]:
                ind = self.analyzers[sym].get_indicators() if sym in self.analyzers else {}
                sd  = self.pair_scores.get(sym, {})
                ent = ", ".join(sd.get("entries",[])[:2]) if sd.get("entries") else "-"
                tl  = self.format_time_remaining(data["expires"])
                new = "🆕" if any(p["symbol"]==sym for p in new_good) else "  "
                ema_pct = (1-ind.get("ema_offset",1))*100
                rev = f"{ind.get('reversal_confidence',0.0):.2f}"
                print(f"{new}{exl}:{sym:<{pc}} {data['peak_score']:<6} {ind.get('ewo',0):<7.1f} "
                      f"{ind.get('rsi',0):<5.0f} {ind.get('rsi_fast',0):<6.0f} {ema_pct:<6.1f} "
                      f"{rev:<5} {tl:<8} {ent}")

        if self.shadow_blacklist:
            print(f"\n{'🚫 BANNED (' + str(len(self.shadow_blacklist)) + ')':=^115}")
            for sym, bd in sorted(self.shadow_blacklist.items(),
                                   key=lambda x: x[1].get("expires",0) if isinstance(x[1],dict) else x[1]):
                if isinstance(bd, dict):
                    print(f"  {exl}:{sym:<{pc}} {bd.get('reason','?'):<22} "
                          f"{bd.get('details','')[:30]:<32} {self.format_time_remaining(bd['expires'])}")

        print("\n" + "="*115 + "\n")

    # ── Exchange ──────────────────────────────────────────────────────────────

    def _normalize_ticker_payload_item(self, item: dict) -> Optional[dict]:
        if not isinstance(item, dict): return None
        if self.exchange == "kucoin":
            raw = str(item.get("symbol") or item.get("symbolName") or "")
            if not raw: return None
            cr = self._safe_float(item.get("changeRate",0.0),0.0)
            if abs(cr) <= 1.0: cr *= 100.0
            return {"symbol": self.symbol_to_pair(raw),
                    "quoteVolume": str(item.get("volValue", item.get("amount",0.0))),
                    "priceChangePercent": str(cr)}
        sym = str(item.get("symbol") or item.get("s") or "")
        if not sym: return None
        return {"symbol": sym,
                "quoteVolume": str(item.get("quoteVolume", item.get("q",0.0))),
                "priceChangePercent": str(item.get("priceChangePercent", item.get("P",0.0)))}

    def _filter_tradeable_tickers(self, tickers: Dict[str, dict]) -> Dict[str, dict]:
        return {s: t for s,t in tickers.items()
                if any(
                    s.endswith(currency)
                    for currency in CONFIG["base_currencies"]
                ) and
                (not self.valid_symbols or s in self.valid_symbols) and
                not self.is_pair_blacklisted(s)}

    async def fetch_exchange_info(self, session: aiohttp.ClientSession):
        self.valid_symbols = set()
        self.valid_pairs_by_exchange[self.exchange] = set()
        base_currencies = set(CONFIG["base_currencies"]); base = ", ".join(CONFIG["base_currencies"]); bl = set(CONFIG["blacklist"])
        if self.exchange == "kucoin":
            data = await self._request_json_from_kucoin(session, "/api/v2/symbols", log_failures=True)
            rows = data.get("data") if isinstance(data,dict) else None
            if not isinstance(rows, list): return
            for row in rows:
                if not isinstance(row,dict) or not row.get("enableTrading",False): continue
                ba=str(row.get("baseCurrency") or ""); qa=str(row.get("quoteCurrency") or "")
                if qa not in base_currencies or ba in bl: continue
                pair = f"{ba}/{qa}"
                if not self.is_pair_blacklisted_pair(pair):
                    self.valid_symbols.add(pair); self.valid_pairs_by_exchange["kucoin"].add(pair)
            logger.info(f"✅ Loaded {len(self.valid_symbols)} valid {base} pairs for KuCoin")
            return
        data = await self._request_json_from_binance(session, "/api/v3/exchangeInfo", log_failures=True)
        if not isinstance(data,dict): return
        symbols = data.get("symbols")
        if not isinstance(symbols,list): return
        for row in symbols:
            if not isinstance(row,dict) or row.get("status")!="TRADING": continue
            if row.get("quoteAsset") not in base_currencies or row.get("baseAsset") in bl: continue
            sym = str(row.get("symbol") or "")
            if sym and not self.is_pair_blacklisted(sym):
                self.valid_symbols.add(sym); self.valid_pairs_by_exchange["binance"].add(self.symbol_to_pair(sym))
        logger.info(f"✅ Loaded {len(self.valid_symbols)} valid {base} pairs")

    async def fetch_kucoin_symbols(self, session: aiohttp.ClientSession):
        if self.exchange == "kucoin": return
        self.valid_pairs_by_exchange["kucoin"] = set()

    async def fetch_klines(self, session, symbol: str, interval: str, limit: int) -> List:
        try:
            params = {"symbol": symbol, "interval": interval, "limit": limit}
            data = await self._request_json_from_binance(session, "/api/v3/klines", params=params)
            if not isinstance(data, list): return []
            return [k for k in data if isinstance(k,(list,tuple)) and len(k)>=8]
        except Exception: return []

    async def fetch_tickers_http(self, session: aiohttp.ClientSession) -> Dict[str, dict]:
        if self.exchange == "kucoin":
            data = await self._request_json_from_kucoin(session, "/api/v1/market/allTickers",
                                                         log_failures=True, log_attr="last_ticker_error_log_ts")
            rows = data.get("data",{}).get("ticker") if isinstance(data,dict) else None
            if not isinstance(rows,list): return {}
            out = {}
            for item in rows:
                n = self._normalize_ticker_payload_item(item)
                if n: out[n["symbol"]] = n
            return self._filter_tradeable_tickers(out)
        raw = await self._request_json_from_binance(session, "/api/v3/ticker/24hr",
                                                     log_failures=True, log_attr="last_ticker_error_log_ts")
        if not isinstance(raw, list): return {}
        out = {}
        for item in raw:
            n = self._normalize_ticker_payload_item(item)
            if n: out[n["symbol"]] = n
        return self._filter_tradeable_tickers(out)

    async def get_tickers(self, session) -> Tuple[Dict[str, dict], str]:
        http_tickers = await self.fetch_tickers_http(session)
        if http_tickers:
            self.ticker_cache.update(http_tickers); self.ticker_cache_last_update = time.time()
            return http_tickers, "http_pull"
        stale      = max(30.0, float(CONFIG.get("ticker_cache_stale_seconds",180)))
        hard_stale = max(stale, float(CONFIG.get("ticker_cache_hard_stale_seconds",21600)))
        cache_age  = time.time()-self.ticker_cache_last_update if self.ticker_cache_last_update>0 else 1e9
        if self.ticker_cache and cache_age <= stale:
            cached = self._filter_tradeable_tickers(self.ticker_cache)
            if cached: return cached, "http_stale_cache"
        if self.ticker_cache and cache_age <= hard_stale:
            cached = self._filter_tradeable_tickers(self.ticker_cache)
            if cached: return cached, "http_expired_cache"
        return {}, "none"

    # ── Volume ────────────────────────────────────────────────────────────────

    def _has_required_volume_history(self, symbol: str, now: float) -> bool:
        history = self.volume_history.get(symbol,[])
        if not history: return False
        h24 = now-86400; h72 = now-259200
        c24 = sum(1 for ts,_ in history if ts>=h24)
        c72 = sum(1 for ts,_ in history if ts>=h72)
        return (c24 >= int(CONFIG.get("volume_stability_min_samples_24h",12)) and
                c72 >= int(CONFIG.get("volume_stability_min_samples_72h",24)))

    def _sample_volume_history(self, tickers: Dict[str, dict], now: float):
        if not CONFIG.get("volume_quality_enabled",True): return
        sample_sec = max(30, int(CONFIG.get("volume_sample_seconds",600)))
        if self.last_volume_sample_ts>0 and (now-self.last_volume_sample_ts)<sample_sec: return
        lookback = max(6, int(CONFIG.get("volume_history_hours",72)))*3600; cutoff = now-lookback
        for sym, t in tickers.items():
            vol_now = self._safe_quote_volume(t.get("quoteVolume",0.0))
            history = self.volume_history.setdefault(sym,[])
            history.append([now, vol_now])
            trimmed = [r for r in history if r[0]>=cutoff]
            self.volume_history[sym] = trimmed[-800:]
        self.last_volume_sample_ts = now

    def _volume_profile(self, symbol: str, ticker: dict, now: float) -> Dict:
        vol_now = self._safe_quote_volume(ticker.get("quoteVolume",0.0))
        history = self.volume_history.get(symbol,[])
        h24 = now-86400; h72 = now-259200
        v24 = [float(v) for ts,v in history if ts>=h24]
        v72 = [float(v) for ts,v in history if ts>=h72]
        if vol_now > 0: v24.append(vol_now); v72.append(vol_now)
        med24 = float(np.median(v24)) if v24 else vol_now
        med72 = float(np.median(v72)) if v72 else vol_now
        thr = self._min_volume_quote_threshold()
        keep_thr = thr * float(self._cfg("volume_keep_ratio",0.90))
        sr24 = float(self._cfg("volume_stability_ratio_24h",0.85))
        sr72 = float(self._cfg("volume_stability_ratio_72h",0.70))
        has24 = len(v24) >= int(CONFIG.get("volume_stability_min_samples_24h",12))
        has72 = len(v72) >= int(CONFIG.get("volume_stability_min_samples_72h",24))
        stable24 = (med24>=thr*sr24) if has24 else (vol_now>=thr*1.1)
        stable72 = (med72>=thr*sr72) if has72 else (vol_now>=thr*1.1)
        spike_r24 = vol_now/max(med24,1.0)
        chg24 = self._safe_float(ticker.get("priceChangePercent",0.0),0.0)
        pump_fade = (has24 and vol_now>=thr and
                     spike_r24>=float(CONFIG.get("pump_fade_spike_ratio",1.7)) and
                     chg24>=float(CONFIG.get("pump_fade_price_change_24h",8.0)) and
                     med24<thr*sr24)
        admission_ok = vol_now>=thr and stable24 and stable72
        keep_ok = vol_now>=keep_thr and (stable24 or med24>=keep_thr)
        q24 = min(1.0, med24/max(thr,1.0)); q72 = min(1.0, med72/max(thr,1.0))
        quality = max(0.0, min(1.0, q24*0.6+q72*0.4))
        spike_pen = max(0.0, min(1.0, (spike_r24-1.0)/3.0))
        return {"vol_now":vol_now,"vol_med_24h":med24,"vol_med_72h":med72,
                "tradeable_now":vol_now>=thr, "admission_ok":bool(admission_ok),
                "keep_ok":bool(keep_ok), "pump_fade_risk":bool(pump_fade),
                "quality_score":quality, "spike_penalty":spike_pen, "spike_ratio_24":spike_r24}

    async def _pace_volume_seed_requests(self):
        gap = max(0.0, float(CONFIG.get("volume_seed_min_request_gap_seconds",0.18)))
        if gap <= 0: return
        async with self.volume_seed_rate_lock:
            now = time.time()
            if now < self.volume_seed_next_request_ts:
                await asyncio.sleep(self.volume_seed_next_request_ts-now); now = time.time()
            self.volume_seed_next_request_ts = max(self.volume_seed_next_request_ts, now)+gap

    async def _seed_volume_history_from_exchange(self, session, tickers, now):
        self.last_volume_seed_attempted = self.last_volume_seeded = 0
        self.last_volume_seed_failed    = self.last_volume_seed_pending = 0
        if not CONFIG.get("volume_seed_enabled",True): return
        unseeded = [s for s in tickers.keys() if not self._has_required_volume_history(s,now)]
        if not unseeded: return
        per_cycle = max(1, int(CONFIG.get("volume_seed_symbols_per_cycle",120)))
        interval  = str(CONFIG.get("volume_seed_interval","1h"))
        bars      = max(24, min(1000, int(CONFIG.get("volume_seed_lookback_bars",72))))
        concurrency = max(1, int(CONFIG.get("volume_seed_max_concurrency",8)))
        cutoff    = now - (max(6, int(CONFIG.get("volume_history_hours",72)))*3600)
        targets   = unseeded[:per_cycle]
        self.last_volume_seed_pending = max(0, len(unseeded)-len(targets))
        sem = asyncio.Semaphore(concurrency); seeded = 0; failed = 0

        async def seed_one(symbol: str):
            nonlocal seeded, failed
            klines = []
            try:
                async with sem:
                    await self._pace_volume_seed_requests()
                    klines = await self.fetch_klines(session, symbol, interval, bars)
            except Exception: pass
            if not klines: failed += 1; return
            hourly = []
            for k in klines:
                if not isinstance(k,(list,tuple)) or len(k)<8: continue
                ts = self._safe_float(k[6],0.0)/1000.0; vol = self._safe_quote_volume(k[7])
                if ts > 0: hourly.append((ts,vol))
            if not hourly: failed+=1; return
            hourly.sort(key=lambda x:x[0])
            hv=[v for _,v in hourly]; tsv=[ts for ts,_ in hourly]
            rows=[]
            for i,ts in enumerate(tsv):
                w=hv[max(0,i-23):i+1]
                if w: rows.append([ts, float(np.sum(w))*(24.0/float(len(w)))])
            rows=[r for r in rows if r[0]>=cutoff]
            if rows: self.volume_history[symbol]=rows[-800:]; seeded+=1
            else: failed+=1

        await asyncio.gather(*[seed_one(s) for s in targets])
        self.last_volume_seed_attempted=len(targets); self.last_volume_seeded=seeded; self.last_volume_seed_failed=failed

    # ── Main Cycle ────────────────────────────────────────────────────────────

    async def run_cycle(self, session: aiohttp.ClientSession):
        self.cycle_count += 1; now = time.time()
        self.stats_falling_knife_blocked = self.stats_trend_filter_blocked = 0
        self.stats_btc_filter_blocked = self.stats_late_entry_blocked = 0
        self.stats_atr_regime_blocked = self.stats_reversal_blocked = 0
        self.stats_profit_gate_blocked = self.stats_recentispumping_blocked = 0

        # Cleanup
        for s in [s for s in self.sticky_pairs if self.is_pair_blacklisted(s)]: del self.sticky_pairs[s]
        for s in [s for s in self.shadow_blacklist if self.is_pair_blacklisted(s)]: del self.shadow_blacklist[s]
        for s in list(self.probation_pairs.keys()):
            if now >= self.probation_pairs[s].get("expires",0): del self.probation_pairs[s]
        expired = [(s,d) for s,d in self.sticky_pairs.items() if now>=d["expires"]]
        for sym,_ in expired: del self.sticky_pairs[sym]
        for s in list(self.shadow_blacklist.keys()):
            bd=self.shadow_blacklist[s]; exp=bd["expires"] if isinstance(bd,dict) else bd
            if now>=exp: logger.info(f"✅ UNBAN: {s}"); del self.shadow_blacklist[s]

        tickers, ticker_source = await self.get_tickers(session)
        self.last_ticker_source = ticker_source
        if not tickers:
            self._throttled_log(logging.ERROR,"❌ No ticker data","last_no_ticker_log_ts",120.0); return

        universe_size = len(self.valid_symbols)
        await self._seed_volume_history_from_exchange(session, tickers, now)
        self._sample_volume_history(tickers, now)
        volume_profiles  = {s: self._volume_profile(s,t,now) for s,t in tickers.items()}
        tradeable_symbols = {s for s,vp in volume_profiles.items() if vp.get("tradeable_now",False)}
        admission_symbols = {s for s,vp in volume_profiles.items() if vp.get("admission_ok",False)}

        if CONFIG.get("strict_min_volume_filtering",True):
            wc = int(CONFIG.get("volume_weak_cycles_to_drop",3))
            for s in list(self.sticky_pairs.keys()):
                if s not in tickers: del self.sticky_pairs[s]; continue
                vp = volume_profiles.get(s,{})
                if vp.get("keep_ok",False): self.sticky_pairs[s]["vol_weak_cycles"]=0
                else:
                    w = int(self.sticky_pairs[s].get("vol_weak_cycles",0))+1
                    self.sticky_pairs[s]["vol_weak_cycles"]=w
                    if w >= wc: del self.sticky_pairs[s]

        symbols = sorted(tradeable_symbols) if CONFIG.get("analyze_tradeable_only",True) else sorted(tickers.keys())
        sticky_symbols = [s for s in self.sticky_pairs if s in tickers and s not in symbols]
        banned_symbols = [s for s in self.shadow_blacklist if s in tickers]
        all_symbols    = list(dict.fromkeys(symbols+sticky_symbols+banned_symbols))

        ws_fresh = (self.ws_manager and self.ws_manager.connected and
                    self.initial_backfill_done and (now-self.ws_manager.last_msg_ts)<60)

        btc_15m_by_symbol: Dict[str, Optional[List]] = {}
        if not ws_fresh:
            btc_symbols = {self._btc_symbol_for_symbol(symbol) for symbol in all_symbols} or {self._btc_symbol_for_symbol(f"BTC{CONFIG['base_currency']}")}
            for btc_symbol in btc_symbols:
                btc_15m_by_symbol[btc_symbol] = await self.fetch_klines(session, btc_symbol, "15m", 250)

        if not ws_fresh:
            limit_5m=CONFIG.get("kline_limit_5m",350); limit_15m=CONFIG.get("kline_limit_15m",100)
            tasks = [asyncio.gather(
                self.fetch_klines(session, s, "5m",  limit_5m),
                self.fetch_klines(session, s, "15m", limit_15m),
            ) for s in all_symbols]
            results = await asyncio.gather(*tasks)
            for sym, (m5,m15) in zip(all_symbols, results):
                if not m5 or not m15: continue
                if sym not in self.analyzers: self.analyzers[sym] = PairAnalyzer(sym)
                self.analyzers[sym].update_data(m5, m15, btc_15m_by_symbol.get(self._btc_symbol_for_symbol(sym)))
            if not self.initial_backfill_done:
                self.initial_backfill_done = True
                if self.ws_manager and not self.ws_manager.connected:
                    await self.ws_manager.start(all_symbols)
                logger.info(f"📥 Backfill done: {len(all_symbols)} symbols")
        else:
            new_syms = [s for s in all_symbols if s not in self.analyzers or len(self.analyzers[s].prices_5m)<200]
            if new_syms:
                btc_symbols = {self._btc_symbol_for_symbol(symbol) for symbol in new_syms} or {self._btc_symbol_for_symbol(f"BTC{CONFIG['base_currency']}")}
                for btc_symbol in btc_symbols:
                    btc_15m_by_symbol[btc_symbol] = await self.fetch_klines(session, btc_symbol, "15m", 250)
                limit_5m=CONFIG.get("kline_limit_5m",350); limit_15m=CONFIG.get("kline_limit_15m",100)
                tasks = [asyncio.gather(self.fetch_klines(session,s,"5m",limit_5m),
                                        self.fetch_klines(session,s,"15m",limit_15m)) for s in new_syms]
                results = await asyncio.gather(*tasks)
                for sym,(m5,m15) in zip(new_syms,results):
                    if m5 and m15:
                        if sym not in self.analyzers: self.analyzers[sym]=PairAnalyzer(sym)
                        self.analyzers[sym].update_data(m5,m15,btc_15m_by_symbol.get(self._btc_symbol_for_symbol(sym)))
            if self.ws_manager: self.ws_manager.update_symbols(all_symbols)

        # ── Scoring Loop ──────────────────────────────────────────────────────
        new_elite=[]; new_good=[]; new_bans=[]; candidate_pool=[]

        target_pairs = max(1, min(int(CONFIG["max_pairs"]), int(CONFIG.get("predictive_target_pairs",10))))
        needed       = max(0, target_pairs - len(self.sticky_pairs))
        relax_ratio  = min(1.0, needed/float(target_pairs))
        adaptive     = bool(CONFIG.get("predictive_adaptive_relax_enabled",True))
        base_prox    = float(self._cfg("pre_entry_proximity_min",0.55))
        base_floor   = int(self._cfg("pre_entry_min_score_floor",12))
        base_relax   = float(self._cfg("pre_entry_profit_relax",0.980))
        if adaptive:
            a_prox  = max(0.40, base_prox  - relax_ratio*float(CONFIG.get("predictive_max_proximity_relax",0.15)))
            a_floor = max(int(self._cfg("predictive_min_pre_entry_score",8)), base_floor)
            a_relax = max(0.92, base_relax - relax_ratio*float(CONFIG.get("predictive_max_profit_relax",0.02)))
        else:
            a_prox=base_prox; a_floor=base_floor; a_relax=base_relax
        base_conf = float(self._cfg("predictive_confidence_min",0.55))
        conf_floor= float(self._cfg("predictive_confidence_floor",0.45))
        a_conf = max(conf_floor, base_conf - relax_ratio*float(CONFIG.get("predictive_max_confidence_relax",0.06))) if adaptive else base_conf

        for symbol in all_symbols:
            analyzer = self.analyzers.get(symbol)
            if not analyzer or len(analyzer.prices_5m)<200: continue

            if symbol in self.shadow_blacklist:
                ban = self.shadow_blacklist[symbol]
                if not isinstance(ban,dict): continue
                fast_ps = self.score_pair(symbol,tickers[symbol]) if symbol in tickers and symbol in tradeable_symbols else None
                ok, note = self.check_recovery(symbol, ban)
                if ok:
                    ban["ok_cycles"]=ban.get("ok_cycles",0)+1
                    if ban["ok_cycles"]>=CONFIG["recovery_required_cycles"]:
                        logger.info(f"✅ UNBAN: {symbol} ({note})"); del self.shadow_blacklist[symbol]
                    else: continue
                else:
                    ban["ok_cycles"]=0
                    if fast_ps:
                        fok, fnote = self.check_fast_reentry(symbol,ban,fast_ps)
                        if fok:
                            ban["fast_ok_cycles"]=ban.get("fast_ok_cycles",0)+1
                            if ban["fast_ok_cycles"]>=CONFIG["fast_reentry_required_cycles"]:
                                logger.info(f"⚡ FAST UNBAN: {symbol} ({fnote})")
                                del self.shadow_blacklist[symbol]
                                self.probation_pairs[symbol]={"expires":now+CONFIG["probation_minutes"]*60,
                                                               "source":ban.get("reason",""),"notes":fnote,"added_at":now}
                            else: continue
                        else: ban["fast_ok_cycles"]=0; continue
                    else: continue

            crash = analyzer.detect_crash()
            if crash:
                ct,cd = crash
                if symbol in self.sticky_pairs: del self.sticky_pairs[symbol]
                self.shadow_blacklist[symbol]={"expires":now+CONFIG["shadow_blacklist_hours"]*3600,
                    "reason":ct,"details":cd,"banned_at":now,
                    "ban_price":analyzer.prices_5m[-1],
                    "ban_low":min(analyzer.lows_5m[-12:]) if analyzer.lows_5m else analyzer.prices_5m[-1],
                    "ok_cycles":0,"fast_ok_cycles":0}
                if symbol in self.probation_pairs: del self.probation_pairs[symbol]
                new_bans.append((symbol,self.shadow_blacklist[symbol])); continue

            if symbol not in tradeable_symbols: continue

            pair_score = self.score_pair(symbol, tickers[symbol])

            # Hard blocks → probation
            if pair_score.get("entries",[])[0:1] in [["falling_knife"],["atr_blocked"],["late_entry"]]:
                entry_type = (pair_score.get("entries") or ["?"])[0]
                if symbol in self.sticky_pairs: del self.sticky_pairs[symbol]
                self.probation_pairs[symbol]={"expires":now+float(CONFIG.get("falling_probation_minutes",240))*60,
                    "source":entry_type.upper(), "notes":pair_score["reasons"][0] if pair_score["reasons"] else "",
                    "added_at":now}
                continue

            vp = volume_profiles.get(symbol, self._volume_profile(symbol,tickers[symbol],now))
            pair_score["volume_profile"]    = vp
            pair_score["tradeable_volume"]  = bool(vp.get("tradeable_now",False))
            pair_score["admission_volume"]  = bool(vp.get("admission_ok",False))

            vol_adj = int(round(
                float(vp.get("quality_score",0.0))*float(self._cfg("volume_stability_rank_bonus_max",8.0)) -
                float(vp.get("spike_penalty",0.0))*float(self._cfg("volume_spike_rank_penalty_max",10.0))
            ))
            if vol_adj != 0:
                pair_score["score"] += vol_adj
                pair_score["reasons"].append(f"Volume quality ({vol_adj:+d})")

            probation = self.probation_pairs.get(symbol)
            in_probation = bool(probation and now<probation.get("expires",0))
            if in_probation and pair_score["score"]<self._probation_min_score_threshold():
                if symbol in self.sticky_pairs: del self.sticky_pairs[symbol]
                continue

            falling_profile = self._falling_profile(pair_score)
            if CONFIG.get("falling_filter_enabled",True) and falling_profile.get("blocked",False):
                if symbol in self.sticky_pairs: del self.sticky_pairs[symbol]
                self.probation_pairs[symbol]={"expires":now+float(CONFIG.get("falling_probation_minutes",240))*60,
                    "source":falling_profile.get("reason","FALLING_FILTER"),
                    "notes":falling_profile.get("details",""), "added_at":now}
                continue

            ind        = pair_score.get("indicators",{}) or {}
            rsi_slope  = float(ind.get("rsi_slope",0.0))
            bb_pos     = float(ind.get("bb_pos",0.5))
            drop_15m   = float(ind.get("price_change_15m",0.0))
            weakness   = (drop_15m<=float(CONFIG.get("pump_fade_weakness_15m_drop",-0.0025)) or
                          (rsi_slope<=float(CONFIG.get("pump_fade_weakness_rsi_slope",-0.25)) and
                           bb_pos>=float(CONFIG.get("pump_fade_weakness_bb_pos_min",0.50))))
            if (CONFIG.get("pump_fade_block_enabled",True) and vp.get("tradeable_now") and
                    vp.get("pump_fade_risk",False) and weakness and symbol not in self.sticky_pairs):
                self.probation_pairs[symbol]={"expires":now+float(CONFIG.get("pump_fade_probation_minutes",120))*60,
                    "source":"PUMP_FADE_RISK","notes":f"spike={vp.get('spike_ratio_24',0):.2f}, d15={drop_15m*100:.2f}%",
                    "added_at":now}
                continue

            self._update_backdata(symbol, pair_score)
            candidate_pool.append(pair_score)

            prediction   = pair_score.get("prediction",{})
            proximity    = float(prediction.get("best_proximity",0.0))
            rev_conf     = float(pair_score.get("reversal_confidence",0.0))
            exact_ready  = bool(pair_score.get("exact_entry_ready",False))
            profit_now   = float(ind.get("profit_potential",1.0))
            profit_near  = profit_now >= (self._profit_threshold()*a_relax)
            strong_prox  = proximity >= (a_prox + float(CONFIG.get("pre_entry_strong_proximity_delta",0.08)))
            profit_ok    = (not bool(CONFIG.get("pre_entry_require_profit_near",False))) or profit_near or strong_prox
            vol_quality  = float(vp.get("quality_score",0.0))
            spike_pen    = float(vp.get("spike_penalty",0.0))
            rsi_slope_n  = max(-1.0, min(1.0, rsi_slope/4.0))
            pred_conf    = max(0.0, min(1.0, proximity + 0.08*rsi_slope_n + 0.06*vol_quality - 0.05*spike_pen))
            rsi_fast_now = float(ind.get("rsi_fast",50.0))
            weakness_block = (bool(self._cfg("predictive_weakness_block_enabled",True)) and
                              drop_15m<=float(self._cfg("predictive_weakness_drop_15m",-0.008)) and
                              rsi_slope<=float(self._cfg("predictive_weakness_rsi_slope",-1.4)) and
                              bb_pos<=float(self._cfg("predictive_weakness_bb_pos_max",0.42)) and
                              rsi_fast_now>=float(self._cfg("predictive_weakness_rsi_fast_min",24)) and
                              not exact_ready)

            has_adm_vol = symbol in admission_symbols
            pred_tv_ok  = (bool(self._cfg("predictive_allow_tradeable_volume_pre_entry",False)) and
                           symbol in tradeable_symbols and
                           float(vp.get("vol_now",0.0)) >= self._min_volume_quote_threshold()*float(self._cfg("predictive_tradeable_volume_ratio",0.90)) and
                           vol_quality>=float(self._cfg("predictive_min_volume_quality",0.55)) and
                           spike_pen<=float(self._cfg("predictive_max_spike_penalty",0.50)) and
                           not bool(vp.get("pump_fade_risk",False)))
            has_pre_vol = has_adm_vol or pred_tv_ok

            qualifies_exact    = has_adm_vol and exact_ready
            # [R1] Pre-Entry braucht Reversal-Confidence
            qualifies_pre_entry = (CONFIG.get("pre_entry_enabled",True) and has_pre_vol and
                                   pair_score["score"]>=a_floor and proximity>=a_prox and
                                   profit_ok and pred_conf>=a_conf and not weakness_block and
                                   rev_conf>=CONFIG["reversal_confidence_min_pre"])

            if CONFIG.get("use_alignment_admission_gate",True):
                admit = qualifies_exact or qualifies_pre_entry
            else:
                admit = pair_score["score"]>=self._min_score_threshold() and has_adm_vol

            if admit:
                tier     = "elite" if pair_score["score"]>=self._elite_score_threshold() else "good"
                duration = (CONFIG["elite_sticky_minutes"] if tier=="elite" else CONFIG["good_sticky_minutes"])*60
                near_ent = (bool(prediction.get("near_entry",False)) or proximity>=a_prox or
                            bool(set(pair_score.get("entries",[])).intersection({"ewo1_ready","ewo2_ready","ewolow_ready"})))
                hold_until = now+CONFIG["near_entry_hold_minutes"]*60 if near_ent else None
                if in_probation: hold_until=max(hold_until or 0, probation.get("expires",0))

                if symbol not in self.sticky_pairs:
                    if len(self.sticky_pairs) >= CONFIG["max_pairs"]:
                        replaceable = [s for s,d in self.sticky_pairs.items() if now>=d.get("hold_until",0)]
                        if not replaceable and exact_ready:
                            good_cands = [s for s,d in self.sticky_pairs.items() if d.get("tier")=="good"]
                            if good_cands:
                                replaceable=[min(good_cands, key=lambda k: self.sticky_pairs[k]["peak_score"])]
                        if not replaceable: continue
                        lowest = min(replaceable, key=lambda k: self.sticky_pairs[k]["peak_score"])
                        if pair_score["score"] > self.sticky_pairs[lowest]["peak_score"]:
                            del self.sticky_pairs[lowest]
                        else: continue
                    self.sticky_pairs[symbol]={"expires":now+duration,"peak_score":pair_score["score"],
                        "tier":tier,"added":now,"vol_weak_cycles":0,
                        "volume_quality_score":float(vp.get("quality_score",0.0)),
                        "volume_spike_penalty":float(vp.get("spike_penalty",0.0)),
                        "hold_until":hold_until or 0,
                        "reversal_confidence":rev_conf}  # [R1] im State speichern
                    if hold_until: self.sticky_pairs[symbol]["expires"]=max(self.sticky_pairs[symbol]["expires"],hold_until)
                    if tier=="elite": new_elite.append(pair_score)
                    else: new_good.append(pair_score)
                else:
                    ex = self.sticky_pairs[symbol]
                    ex["expires"]=max(ex["expires"],now+duration)
                    ex["peak_score"]=max(ex["peak_score"],pair_score["score"])
                    ex["volume_quality_score"]=float(vp.get("quality_score",0.0))
                    ex["volume_spike_penalty"]=float(vp.get("spike_penalty",0.0))
                    ex["vol_weak_cycles"]=0; ex["reversal_confidence"]=rev_conf
                    if hold_until:
                        ex["hold_until"]=max(ex.get("hold_until",0),hold_until)
                        ex["expires"]=max(ex["expires"],ex["hold_until"])
                    if tier=="elite" and ex.get("tier")!="elite":
                        ex["tier"]="elite"; logger.info(f"⬆️ UPGRADED: {symbol} → ELITE")

        self.pair_scores = {p["symbol"]:p for p in candidate_pool}
        top_pairs = sorted(
            self.sticky_pairs.items(),
            key=lambda x: (x[1]["tier"]=="elite",
                           float(x[1].get("peak_score",0.0))+
                           float(x[1].get("volume_quality_score",0.0))*8.0-
                           float(x[1].get("volume_spike_penalty",0.0))*10.0+
                           float(x[1].get("reversal_confidence",0.0))*15.0),  # [R1] Reversal im Ranking
            reverse=True,
        )[:CONFIG["max_pairs"]]

        self.current_pairs = [self.symbol_to_pair(s) for s,_ in top_pairs]
        self.current_pairs_by_exchange[self.exchange] = list(self.current_pairs)
        self.last_update = datetime.now().isoformat()
        self.log_dashboard(new_elite,new_good,expired,new_bans,
                           universe_size,len(tradeable_symbols),len(all_symbols))
        self.save_state()


# ─── HTTP Endpoints ────────────────────────────────────────────────────────────

def _filter_pairs_by_quote(pairs: List[str], quote_currency: str) -> List[str]:
    quote = quote_currency.strip().upper()
    return [pair for pair in pairs if pair.endswith(f"/{quote}")]


def _build_pairs_response(inj, exchange: str, quote_currency: Optional[str] = None):
    payload = {
        "pairs": list(inj.current_pairs),
        "exchange": exchange,
        "refresh_period": CONFIG["update_interval"],
        "updated": inj.last_update,
    }
    if quote_currency:
        quote = quote_currency.strip().upper()
        payload["pairs"] = _filter_pairs_by_quote(inj.current_pairs, quote)
        payload["quote_currency"] = quote
        payload["configured"] = quote in CONFIG["base_currencies"]
    return web.json_response(payload)


async def handle_pairs(request):
    inj = request.app["injector_binance"]
    return _build_pairs_response(inj, "binance")

async def handle_pairs_kucoin(request):
    inj = request.app["injector_kucoin"]
    return _build_pairs_response(inj, "kucoin")


async def handle_pairs_binance_quote(request):
    inj = request.app["injector_binance"]
    return _build_pairs_response(inj, "binance", request.match_info["quote_currency"])


async def handle_pairs_kucoin_quote(request):
    inj = request.app["injector_kucoin"]
    return _build_pairs_response(inj, "kucoin", request.match_info["quote_currency"])


async def handle_pairs_binance_usdt(request):
    inj = request.app["injector_binance"]
    return _build_pairs_response(inj, "binance", "USDT")


async def handle_pairs_binance_usdc(request):
    inj = request.app["injector_binance"]
    return _build_pairs_response(inj, "binance", "USDC")


async def handle_pairs_kucoin_usdt(request):
    inj = request.app["injector_kucoin"]
    return _build_pairs_response(inj, "kucoin", "USDT")


async def handle_pairs_kucoin_usdc(request):
    inj = request.app["injector_kucoin"]
    return _build_pairs_response(inj, "kucoin", "USDC")

async def handle_details(request):
    inj = request.app["injector_binance"]
    return web.json_response({
        "pairs": list(inj.pair_scores.values())[:20],
        "sticky_count": len(inj.sticky_pairs),
        "banned_count": len(inj.shadow_blacklist),
        "probation_count": len(inj.probation_pairs),
        "updated": inj.last_update,
        "stats": {"falling_knife_blocked": inj.stats_falling_knife_blocked,
                  "trend_filter_blocked":  inj.stats_trend_filter_blocked,
                  "btc_filter_blocked":    inj.stats_btc_filter_blocked,
                  "late_entry_blocked":    inj.stats_late_entry_blocked,
                  "atr_regime_blocked":    inj.stats_atr_regime_blocked,
                  "reversal_blocked":      inj.stats_reversal_blocked}
    })

async def handle_banned(request):
    inj = request.app["injector_binance"]
    return web.json_response({"banned": [
        {"symbol":s,"reason":d.get("reason",""),"details":d.get("details",""),
         "expires_in":inj.format_time_remaining(d["expires"])}
        for s,d in inj.shadow_blacklist.items() if isinstance(d,dict)
    ]})

async def handle_health(request):
    inj = request.app["injector_binance"]
    ws_ok = inj.ws_manager and inj.ws_manager.connected if inj.ws_manager else False
    return web.json_response({
        "status":"ok","strategy":"NASOSv5_mod3_pairs_safe_v5",
        "pairs_count":len(inj.current_pairs), "banned_count":len(inj.shadow_blacklist),
        "ws_connected":ws_ok, "ticker_source":inj.last_ticker_source, "cycle":inj.cycle_count,
        "stats":{"falling_knife_blocked":inj.stats_falling_knife_blocked,
                 "late_entry_blocked":inj.stats_late_entry_blocked,
                 "reversal_blocked":inj.stats_reversal_blocked,
                 "atr_regime_blocked":inj.stats_atr_regime_blocked}
    })


# ─── Lifecycle ─────────────────────────────────────────────────────────────────

async def start_background_tasks(app):
    for key in ("injector_binance","injector_kucoin"):
        inj = app[key]; inj.running = True
        if inj.supports_ws and CONFIG.get("ws_enabled",False):
            inj.ws_manager = KlineWSManager(inj.analyzers)
    app["scanner_task_binance"] = asyncio.create_task(scanner_loop(app["injector_binance"]))
    app["scanner_task_kucoin"]  = asyncio.create_task(scanner_loop(app["injector_kucoin"]))

async def cleanup_background_tasks(app):
    for key, task_key in (("injector_binance","scanner_task_binance"),("injector_kucoin","scanner_task_kucoin")):
        inj = app[key]; inj.running = False
        if inj.ws_manager: await inj.ws_manager.stop()
        app[task_key].cancel()
        try: await app[task_key]
        except asyncio.CancelledError: pass

async def scanner_loop(injector: PairlistInjector):
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    timeout   = build_http_timeout()
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        await injector.fetch_exchange_info(session)
        await injector.fetch_kucoin_symbols(session)
        while injector.running:
            start = time.time()
            try: await injector.run_cycle(session)
            except Exception as e:
                logger.error(f"❌ Cycle error: {e}")
                import traceback; traceback.print_exc()
            await asyncio.sleep(max(0, CONFIG["update_interval"]-(time.time()-start)))


async def main():
    log_loaded_configuration()
    print("\n" + "="*115)
    print(f"{'🎯 NASOS PAIRLIST INJECTOR V10.0 — v5 Reversal Edition':^115}")
    print(f"{'Trigger → Reversal Confirmation | max_pairs=20 | Strict Filters':^115}")
    print("="*115)
    print(f"  Port: {CONFIG['http_port']}  |  Interval: {CONFIG['update_interval']}s  |  Max Pairs: {CONFIG['max_pairs']}")
    print(f"\n  Reversal Confidence Score:")
    print(f"    RSI_fast steigt (2×) + grüne Kerze + Volume×{CONFIG['reversal_volume_ratio_min']} + Higher Low")
    print(f"    Score ≥ {CONFIG['reversal_confidence_min_entry']} → Entry bestätigt  |  Score ≥ {CONFIG['reversal_confidence_min_pre']} → Pre-Entry")
    print(f"    Reversal Bonus: +{CONFIG['reversal_score_bonus_max']} pts max  |  Kein Reversal: {CONFIG['reversal_penalty_per_missing']} pts")
    print(f"\n  Anti-Late-Entry:")
    print(f"    Bounce > {CONFIG['late_entry_bounce_max']*100:.0f}% vom lokalen Tief → blockiert")
    print(f"    RSI > {CONFIG['late_entry_rsi_max']} → blockiert")
    print(f"\n  BTC-Filter:")
    print(f"    EMA200-Trend ≥ {CONFIG['btc_ema200_trend_min']}  |  30min Change ≥ {CONFIG['btc_price_change_30m_min']*100:.1f}%")
    print(f"\n  Endpoints: /pairs /pairs-kucoin /details /banned /health")
    print("="*115 + "\n")

    injector_binance = PairlistInjector("binance")
    injector_kucoin  = PairlistInjector("kucoin")

    app = web.Application()
    app["injector_binance"] = injector_binance
    app["injector_kucoin"]  = injector_kucoin
    app.router.add_get("/pairs",         handle_pairs)
    app.router.add_get("/pairs-binance", handle_pairs)
    app.router.add_get("/pairs-binance-usdt", handle_pairs_binance_usdt)
    app.router.add_get("/pairs-binance-usdc", handle_pairs_binance_usdc)
    app.router.add_get("/pairs-binance/{quote_currency}", handle_pairs_binance_quote)
    app.router.add_get("/pairs-kucoin",  handle_pairs_kucoin)
    app.router.add_get("/pairs-kucoin-usdt", handle_pairs_kucoin_usdt)
    app.router.add_get("/pairs-kucoin-usdc", handle_pairs_kucoin_usdc)
    app.router.add_get("/pairs-kucoin/{quote_currency}", handle_pairs_kucoin_quote)
    app.router.add_get("/paris-kucoin",  handle_pairs_kucoin)
    app.router.add_get("/details",       handle_details)
    app.router.add_get("/banned",        handle_banned)
    app.router.add_get("/health",        handle_health)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", CONFIG["http_port"])
    await site.start()
    logger.info(f"✅ NASOS Injector v10 running on http://0.0.0.0:{CONFIG['http_port']}")
    while True: await asyncio.sleep(3600)


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("👋 Shutting down...")
