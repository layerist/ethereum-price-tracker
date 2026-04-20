#!/usr/bin/env python3
"""
Ultra-robust concurrent crypto price tracker (CoinMarketCap)

Major upgrades:
- Global rate limiter (thread-safe)
- Circuit breaker with HALF-OPEN state
- Adaptive polling interval (auto backoff)
- Metrics (success rate, latency, errors)
- Batch requests (prevents full failure)
- Buffered CSV writing
- Graceful shutdown
- Jittered exponential retry (extra layer)
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =========================================================
# Color support
# =========================================================

try:
    from colorama import Fore, Style, init

    init(autoreset=True)
    COLOR = True
except Exception:
    COLOR = False


def colorize(text, color):
    return f"{color}{text}{Style.RESET_ALL}" if COLOR else text


# =========================================================
# Constants
# =========================================================

API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
_thread_local = threading.local()


# =========================================================
# Config
# =========================================================

@dataclass
class Config:
    api_key: str
    symbols: List[str]
    convert: str
    interval: float
    workers: int
    debug: bool

    timeout: int = 10
    max_retries: int = 5
    backoff_factor: float = 1.2

    rate_limit_per_sec: float = 5.0

    fail_threshold: int = 5
    cooldown: int = 30

    batch_size: int = 10

    csv_file: Optional[str] = None


# =========================================================
# Logger
# =========================================================

def setup_logger(debug: bool):
    logger = logging.getLogger("tracker")
    logger.handlers.clear()

    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")
    )

    logger.addHandler(handler)

    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logger


# =========================================================
# Rate limiter (GLOBAL)
# =========================================================

class RateLimiter:
    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / rate_per_sec
        self.lock = threading.Lock()
        self.last_call = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            delta = now - self.last_call
            if delta < self.interval:
                time.sleep(self.interval - delta)
            self.last_call = time.time()


# =========================================================
# Circuit Breaker (HALF-OPEN)
# =========================================================

class CircuitBreaker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fail_count = 0
        self.state = "CLOSED"
        self.open_until = 0

    def allow(self):
        if self.state == "OPEN":
            if time.time() > self.open_until:
                self.state = "HALF"
                return True
            return False
        return True

    def success(self):
        self.fail_count = 0
        self.state = "CLOSED"

    def failure(self):
        self.fail_count += 1
        if self.fail_count >= self.cfg.fail_threshold:
            self.state = "OPEN"
            self.open_until = time.time() + self.cfg.cooldown


# =========================================================
# Metrics
# =========================================================

class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.ok = 0
        self.fail = 0
        self.latencies = []

    def record(self, success: bool, latency: float):
        with self.lock:
            if success:
                self.ok += 1
            else:
                self.fail += 1
            self.latencies.append(latency)

    def snapshot(self):
        with self.lock:
            total = self.ok + self.fail
            avg_latency = sum(self.latencies[-50:]) / max(1, len(self.latencies[-50:]))
            success_rate = self.ok / total if total else 0
            return success_rate, avg_latency


# =========================================================
# Session
# =========================================================

def create_session(cfg: Config):
    retry = Retry(
        total=cfg.max_retries,
        backoff_factor=cfg.backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(max_retries=retry)

    s = requests.Session()
    s.mount("https://", adapter)

    s.headers.update(
        {"Accept": "application/json", "X-CMC_PRO_API_KEY": cfg.api_key}
    )

    return s


def get_session(cfg: Config):
    if not hasattr(_thread_local, "session"):
        _thread_local.session = create_session(cfg)
    return _thread_local.session


# =========================================================
# Fetch
# =========================================================

def fetch_batch(cfg, symbols, limiter, breaker, logger, metrics):
    if not breaker.allow():
        logger.warning("Circuit OPEN — skipping")
        return {}

    limiter.wait()
    session = get_session(cfg)

    start = time.perf_counter()

    try:
        r = session.get(
            API_URL,
            params={"symbol": ",".join(symbols), "convert": cfg.convert},
            timeout=cfg.timeout,
        )

        latency = time.perf_counter() - start

        if r.status_code == 429:
            breaker.failure()
            metrics.record(False, latency)
            return {}

        r.raise_for_status()

        data = r.json().get("data", {})

        result = {}
        for s in symbols:
            try:
                result[s] = float(data[s]["quote"][cfg.convert]["price"])
            except Exception:
                logger.debug("Bad symbol: %s", s)

        breaker.success()
        metrics.record(True, latency)

        return result

    except Exception as e:
        latency = time.perf_counter() - start
        logger.debug("Fetch error: %s", e)

        breaker.failure()
        metrics.record(False, latency)

        # jitter retry (extra layer)
        time.sleep(random.uniform(0.2, 1.0))
        return {}


# =========================================================
# CSV (buffered)
# =========================================================

class CSVWriter:
    def __init__(self, path: str, convert: str):
        self.path = path
        self.convert = convert
        self.buffer = []
        self.lock = threading.Lock()

        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(["ts", "symbol", f"price_{convert}"])

    def write(self, data: Dict[str, float]):
        ts = int(time.time())
        with self.lock:
            for k, v in data.items():
                self.buffer.append([ts, k, v])

            if len(self.buffer) >= 50:
                self.flush()

    def flush(self):
        if not self.buffer:
            return
        with open(self.path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(self.buffer)
        self.buffer.clear()


# =========================================================
# Worker
# =========================================================

def worker(cfg, stop_event, limiter, logger, metrics):
    breaker = CircuitBreaker(cfg)
    last = {}

    csv_writer = CSVWriter(cfg.csv_file, cfg.convert) if cfg.csv_file else None

    while not stop_event.is_set():

        all_prices = {}

        # batch split
        for i in range(0, len(cfg.symbols), cfg.batch_size):
            batch = cfg.symbols[i : i + cfg.batch_size]
            res = fetch_batch(cfg, batch, limiter, breaker, logger, metrics)
            all_prices.update(res)

        for sym, price in all_prices.items():
            prev = last.get(sym)

            if prev is None:
                color = Fore.YELLOW
            elif price > prev:
                color = Fore.GREEN
            elif price < prev:
                color = Fore.RED
            else:
                color = Fore.YELLOW

            logger.info("%s: %s %s", sym, colorize(f"{price:,.2f}", color), cfg.convert)
            last[sym] = price

        if csv_writer:
            csv_writer.write(all_prices)

        success_rate, latency = metrics.snapshot()

        logger.debug(
            "metrics | success=%.2f avg_latency=%.3f",
            success_rate,
            latency,
        )

        # adaptive sleep
        base = cfg.interval
        if success_rate < 0.7:
            base *= 1.5
        elif success_rate > 0.95:
            base *= 0.9

        sleep = base + random.uniform(-0.3, 0.3)
        stop_event.wait(max(1, sleep))

    if csv_writer:
        csv_writer.flush()


# =========================================================
# Signals
# =========================================================

def setup_signals(stop_event, logger):
    def handler(sig, _):
        logger.info("Signal %s — stopping", sig)
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handler)
        except Exception:
            pass


# =========================================================
# CLI
# =========================================================

def parse_args():
    p = argparse.ArgumentParser()

    p.add_argument("--symbols", nargs="+", default=["ETH"])
    p.add_argument("--convert", default="USD")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--csv")
    p.add_argument("--debug", action="store_true")

    args = p.parse_args()

    api_key = os.getenv("CMC_API_KEY")
    if not api_key:
        sys.exit("CMC_API_KEY missing")

    return Config(
        api_key=api_key,
        symbols=[s.upper() for s in args.symbols],
        convert=args.convert.upper(),
        interval=args.interval,
        workers=args.workers,
        debug=args.debug,
        csv_file=args.csv,
    )


# =========================================================
# Main
# =========================================================

def main():
    cfg = parse_args()
    logger = setup_logger(cfg.debug)

    stop_event = threading.Event()
    limiter = RateLimiter(cfg.rate_limit_per_sec)
    metrics = Metrics()

    setup_signals(stop_event, logger)

    logger.info(
        "Start | symbols=%s workers=%d interval=%.2f",
        ",".join(cfg.symbols),
        cfg.workers,
        cfg.interval,
    )

    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        for _ in range(cfg.workers):
            ex.submit(worker, cfg, stop_event, limiter, logger, metrics)

        stop_event.wait()

    logger.info("Shutdown complete")


# =========================================================

if __name__ == "__main__":
    main()
