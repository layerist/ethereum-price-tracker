#!/usr/bin/env python3
"""
Production-grade concurrent crypto price tracker for CoinMarketCap.

Key properties:
- One polling cycle fetches each symbol batch exactly once.
- Batch-level concurrency via ThreadPoolExecutor.
- Shared global token-bucket rate limiter.
- Shared circuit breaker with CLOSED / OPEN / HALF_OPEN states.
- Per-thread requests.Session with tuned connection pool and retries.
- Dedicated CSV writer thread with graceful draining.
- Atomic JSON snapshot writes.
- Adaptive polling interval based on rolling health metrics.
- Optional proxy support.
- Optional one-shot mode for scripts/cron.
- SIGINT/SIGTERM safe shutdown.

Requirements:
    pip install requests colorama

Usage:
    export CMC_API_KEY="YOUR_KEY"

    python cmc_tracker_improved.py \
        --symbols BTC ETH SOL XRP ADA \
        --interval 3 \
        --workers 4 \
        --csv prices.csv \
        --json latest_prices.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import queue
import random
import signal
import statistics
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from colorama import Fore, Style, init

    init(autoreset=True)
    COLOR = True
except Exception:  # colorama is optional
    COLOR = False

    class _NoColor:
        BLACK = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = RESET_ALL = ""

    Fore = Style = _NoColor()  # type: ignore[assignment]


API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_USER_AGENT = "CMC-Production-Tracker/3.0"
_thread_local = threading.local()


@dataclass(frozen=True)
class Config:
    api_key: str
    symbols: List[str]
    convert: str = "USD"

    interval: float = 5.0
    min_interval: float = 1.0
    max_interval: float = 60.0
    workers: int = 4
    batch_size: int = 50
    one_shot: bool = False

    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    max_retries: int = 3
    backoff_factor: float = 0.8

    pool_connections: int = 32
    pool_maxsize: int = 64
    rate_limit_per_sec: float = 8.0
    burst_capacity: int = 10

    fail_threshold: int = 5
    cooldown: float = 30.0
    half_open_max_calls: int = 2

    csv_file: Optional[Path] = None
    json_file: Optional[Path] = None
    write_queue_size: int = 10_000
    csv_flush_rows: int = 100
    csv_flush_sec: float = 2.0

    proxy: Optional[str] = None
    debug: bool = False
    quiet: bool = False
    metrics_window: int = 1_000


class TrackerError(RuntimeError):
    pass


def colorize(text: str, color: str) -> str:
    return f"{color}{text}{Style.RESET_ALL}" if COLOR else text


def setup_logger(debug: bool, quiet: bool) -> logging.Logger:
    logger = logging.getLogger("cmc_tracker")
    logger.handlers.clear()
    logger.propagate = False

    if quiet:
        level = logging.WARNING
    else:
        level = logging.DEBUG if debug else logging.INFO

    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(threadName)-16s | %(message)s")
    )
    logger.addHandler(handler)

    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logger


def chunks(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * percent / 100
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ordered[int(k)]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


class TokenBucketRateLimiter:
    def __init__(self, rate: float, capacity: int):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def wait(self, stop_event: threading.Event) -> bool:
        while not stop_event.is_set():
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.updated
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True

            stop_event.wait(0.02)
        return False


class CircuitBreaker:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, fail_threshold: int, cooldown: float, half_open_max_calls: int):
        self.fail_threshold = fail_threshold
        self.cooldown = cooldown
        self.half_open_max_calls = half_open_max_calls
        self.state = self.CLOSED
        self.failures = 0
        self.open_until = 0.0
        self.half_open_calls = 0
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = time.monotonic()
            if self.state == self.OPEN:
                if now < self.open_until:
                    return False
                self.state = self.HALF_OPEN
                self.half_open_calls = 0

            if self.state == self.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    return False
                self.half_open_calls += 1

            return True

    def success(self) -> None:
        with self.lock:
            self.failures = 0
            self.half_open_calls = 0
            self.state = self.CLOSED

    def failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.fail_threshold:
                self.state = self.OPEN
                self.open_until = time.monotonic() + self.cooldown

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            remaining = max(0.0, self.open_until - time.monotonic()) if self.state == self.OPEN else 0.0
            return {
                "state": self.state,
                "failures": self.failures,
                "open_remaining_sec": remaining,
            }


class Metrics:
    def __init__(self, window: int):
        self.lock = threading.Lock()
        self.success = 0
        self.failure = 0
        self.started = time.time()
        self.latencies: deque[float] = deque(maxlen=window)
        self.response_sizes: deque[int] = deque(maxlen=window)

    def record(self, ok: bool, latency: float, size: int = 0) -> None:
        with self.lock:
            if ok:
                self.success += 1
            else:
                self.failure += 1
            self.latencies.append(latency)
            self.response_sizes.append(size)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            total = self.success + self.failure
            latencies = list(self.latencies)
            uptime = max(0.001, time.time() - self.started)
            return {
                "success": self.success,
                "failure": self.failure,
                "requests": total,
                "success_rate": self.success / total if total else 1.0,
                "avg_latency": statistics.mean(latencies) if latencies else 0.0,
                "p95_latency": percentile(latencies, 95),
                "p99_latency": percentile(latencies, 99),
                "rps": total / uptime,
            }


class AdaptiveInterval:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.current = cfg.interval
        self.lock = threading.Lock()

    def update(self, metrics: Dict[str, Any], breaker_state: str) -> float:
        with self.lock:
            success_rate = float(metrics["success_rate"])
            avg_latency = float(metrics["avg_latency"])

            if breaker_state == CircuitBreaker.OPEN:
                self.current *= 1.5
            elif success_rate < 0.70:
                self.current *= 1.35
            elif success_rate < 0.90:
                self.current *= 1.15
            elif success_rate > 0.98 and 0 < avg_latency < 1.0:
                self.current *= 0.92

            self.current = max(self.cfg.min_interval, min(self.current, self.cfg.max_interval))
            return self.current

    def get(self) -> float:
        with self.lock:
            return self.current


def create_session(cfg: Config) -> requests.Session:
    retry = Retry(
        total=cfg.max_retries,
        connect=cfg.max_retries,
        read=cfg.max_retries,
        status=cfg.max_retries,
        backoff_factor=cfg.backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=cfg.pool_connections,
        pool_maxsize=cfg.pool_maxsize,
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "User-Agent": DEFAULT_USER_AGENT,
            "X-CMC_PRO_API_KEY": cfg.api_key,
        }
    )
    if cfg.proxy:
        session.proxies.update({"http": cfg.proxy, "https": cfg.proxy})
    return session


def get_session(cfg: Config) -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = create_session(cfg)
        _thread_local.session = session
    return session


class AsyncCSVWriter(threading.Thread):
    daemon = True

    def __init__(self, cfg: Config, stop_event: threading.Event, logger: logging.Logger):
        super().__init__(name="csv-writer")
        self.cfg = cfg
        self.stop_event = stop_event
        self.logger = logger
        self.queue: queue.Queue[Tuple[int, Dict[str, float]]] = queue.Queue(maxsize=cfg.write_queue_size)
        if cfg.csv_file:
            self._init_csv(cfg.csv_file)

    def _init_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["timestamp", "symbol", f"price_{self.cfg.convert}"])

    def enqueue(self, data: Dict[str, float]) -> None:
        if not self.cfg.csv_file or not data:
            return
        item = (int(time.time()), data)
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            # Preserve fresh data when possible: block briefly before dropping.
            try:
                self.queue.put(item, timeout=0.25)
            except queue.Full:
                self.logger.warning("CSV queue is full; dropped one price snapshot")

    def run(self) -> None:
        buffer: List[List[Any]] = []
        last_flush = time.monotonic()

        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                ts, data = self.queue.get(timeout=0.5)
                for symbol, price in sorted(data.items()):
                    buffer.append([ts, symbol, price])
                self.queue.task_done()
            except queue.Empty:
                pass
            except Exception as exc:
                self.logger.exception("CSV writer error: %s", exc)

            flush_due = time.monotonic() - last_flush >= self.cfg.csv_flush_sec
            if buffer and (len(buffer) >= self.cfg.csv_flush_rows or flush_due):
                self.flush(buffer)
                last_flush = time.monotonic()

        if buffer:
            self.flush(buffer)

    def flush(self, buffer: List[List[Any]]) -> None:
        if not self.cfg.csv_file or not buffer:
            return
        with self.cfg.csv_file.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(buffer)
        buffer.clear()


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def extract_prices(payload: Dict[str, Any], requested_symbols: Sequence[str], convert: str) -> Dict[str, float]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TrackerError("Invalid CoinMarketCap response: 'data' is missing or not an object")

    prices: Dict[str, float] = {}
    for symbol in requested_symbols:
        raw_price = data.get(symbol, {}).get("quote", {}).get(convert, {}).get("price")
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price >= 0:
            prices[symbol] = price
    return prices


def fetch_batch(
    cfg: Config,
    symbols: List[str],
    limiter: TokenBucketRateLimiter,
    breaker: CircuitBreaker,
    metrics: Metrics,
    stop_event: threading.Event,
    logger: logging.Logger,
) -> Dict[str, float]:
    if not breaker.allow():
        state = breaker.snapshot()
        logger.debug("Circuit breaker OPEN; skip batch=%s remaining=%.1fs", symbols, state["open_remaining_sec"])
        return {}

    if not limiter.wait(stop_event):
        return {}

    session = get_session(cfg)
    params = {"symbol": ",".join(symbols), "convert": cfg.convert}
    started = time.perf_counter()

    try:
        response = session.get(
            API_URL,
            params=params,
            timeout=(cfg.connect_timeout, cfg.read_timeout),
        )
        latency = time.perf_counter() - started
        status = response.status_code

        if status == 429:
            breaker.failure()
            metrics.record(False, latency, len(response.content))
            logger.warning("CoinMarketCap rate limit: batch=%s", ",".join(symbols))
            return {}

        if status >= 500:
            breaker.failure()
            metrics.record(False, latency, len(response.content))
            logger.warning("CoinMarketCap server error %s: batch=%s", status, ",".join(symbols))
            return {}

        if status >= 400:
            metrics.record(False, latency, len(response.content))
            # 4xx errors usually mean config/request problems; do not hide the reason.
            raise TrackerError(f"CoinMarketCap returned HTTP {status}: {response.text[:300]}")

        payload = response.json()
        prices = extract_prices(payload, symbols, cfg.convert)

        if prices:
            breaker.success()
            metrics.record(True, latency, len(response.content))
        else:
            breaker.failure()
            metrics.record(False, latency, len(response.content))
            logger.warning("No prices parsed from response for batch=%s", ",".join(symbols))

        missing = sorted(set(symbols) - set(prices))
        if missing:
            logger.debug("Missing symbols in response: %s", ",".join(missing))

        return prices

    except requests.RequestException as exc:
        latency = time.perf_counter() - started
        breaker.failure()
        metrics.record(False, latency)
        logger.debug("Network error for batch=%s: %s", ",".join(symbols), exc)
        return {}
    except Exception:
        latency = time.perf_counter() - started
        breaker.failure()
        metrics.record(False, latency)
        raise


def log_prices(logger: logging.Logger, prices: Dict[str, float], previous: Dict[str, float], convert: str) -> None:
    for symbol, price in sorted(prices.items()):
        old = previous.get(symbol)
        if old is None:
            color = Fore.YELLOW
            marker = "="
        elif price > old:
            color = Fore.GREEN
            marker = "↑"
        elif price < old:
            color = Fore.RED
            marker = "↓"
        else:
            color = Fore.YELLOW
            marker = "="

        logger.info("%-8s %s %s %s", symbol, marker, colorize(f"{price:,.8g}", color), convert)
        previous[symbol] = price


def run_cycle(
    cfg: Config,
    executor: ThreadPoolExecutor,
    limiter: TokenBucketRateLimiter,
    breaker: CircuitBreaker,
    metrics: Metrics,
    stop_event: threading.Event,
    logger: logging.Logger,
) -> Dict[str, float]:
    batches = list(chunks(cfg.symbols, cfg.batch_size))
    futures = [
        executor.submit(fetch_batch, cfg, batch, limiter, breaker, metrics, stop_event, logger)
        for batch in batches
    ]

    all_prices: Dict[str, float] = {}
    for future in as_completed(futures):
        if stop_event.is_set():
            break
        try:
            all_prices.update(future.result())
        except Exception as exc:
            logger.exception("Batch failed: %s", exc)

    return all_prices


def setup_signals(stop_event: threading.Event, logger: logging.Logger) -> None:
    def handler(sig: int, _frame: Any) -> None:
        logger.warning("Received signal %s; shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def parse_symbols(raw_symbols: List[str]) -> List[str]:
    symbols: List[str] = []
    seen = set()
    for item in raw_symbols:
        for part in item.replace(",", " ").split():
            symbol = part.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
    return sorted(symbols)


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> Config:
    parser = argparse.ArgumentParser(description="Concurrent CoinMarketCap price tracker")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to track. Commas are also accepted.")
    parser.add_argument("--convert", default="USD", help="Quote currency, e.g. USD, EUR, BTC")
    parser.add_argument("--interval", type=positive_float, default=5.0, help="Base polling interval in seconds")
    parser.add_argument("--min-interval", type=positive_float, default=1.0)
    parser.add_argument("--max-interval", type=positive_float, default=60.0)
    parser.add_argument("--workers", type=positive_int, default=4, help="Concurrent batch workers")
    parser.add_argument("--batch-size", type=positive_int, default=50, help="Symbols per CoinMarketCap request")
    parser.add_argument("--rate-limit", type=positive_float, default=8.0, help="Global request rate limit per second")
    parser.add_argument("--burst", type=positive_int, default=10, help="Token bucket burst capacity")
    parser.add_argument("--connect-timeout", type=positive_float, default=5.0)
    parser.add_argument("--read-timeout", type=positive_float, default=10.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff-factor", type=float, default=0.8)
    parser.add_argument("--csv", type=Path, help="Append price history to CSV")
    parser.add_argument("--json", type=Path, help="Write latest full snapshot to JSON atomically")
    parser.add_argument("--proxy", default=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"), help="Optional HTTP(S) proxy URL")
    parser.add_argument("--one-shot", action="store_true", help="Fetch once and exit")
    parser.add_argument("--quiet", action="store_true", help="Only warnings/errors")
    parser.add_argument("--debug", action="store_true", help="Verbose debug logging")

    args = parser.parse_args(argv)

    api_key = os.getenv("CMC_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("ERROR: CMC_API_KEY environment variable is missing")

    symbols = parse_symbols(args.symbols)
    if not symbols:
        raise SystemExit("ERROR: no valid symbols provided")

    if args.min_interval > args.max_interval:
        raise SystemExit("ERROR: --min-interval cannot be greater than --max-interval")

    if args.max_retries < 0:
        raise SystemExit("ERROR: --max-retries cannot be negative")

    return Config(
        api_key=api_key,
        symbols=symbols,
        convert=args.convert.upper().strip(),
        interval=args.interval,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
        workers=args.workers,
        batch_size=args.batch_size,
        rate_limit_per_sec=args.rate_limit,
        burst_capacity=args.burst,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_retries=args.max_retries,
        backoff_factor=args.backoff_factor,
        csv_file=args.csv,
        json_file=args.json,
        proxy=args.proxy,
        one_shot=args.one_shot,
        quiet=args.quiet,
        debug=args.debug,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg = parse_args(argv)
    logger = setup_logger(cfg.debug, cfg.quiet)
    stop_event = threading.Event()
    setup_signals(stop_event, logger)

    limiter = TokenBucketRateLimiter(cfg.rate_limit_per_sec, cfg.burst_capacity)
    breaker = CircuitBreaker(cfg.fail_threshold, cfg.cooldown, cfg.half_open_max_calls)
    metrics = Metrics(cfg.metrics_window)
    adaptive_interval = AdaptiveInterval(cfg)
    csv_writer = AsyncCSVWriter(cfg, stop_event, logger)
    previous_prices: Dict[str, float] = {}

    logger.info(
        "Starting | symbols=%d workers=%d batch_size=%d interval=%.2fs rate_limit=%.2f/s",
        len(cfg.symbols),
        cfg.workers,
        cfg.batch_size,
        cfg.interval,
        cfg.rate_limit_per_sec,
    )

    csv_writer.start()

    try:
        with ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="fetch") as executor:
            while not stop_event.is_set():
                cycle_started = time.perf_counter()
                prices = run_cycle(cfg, executor, limiter, breaker, metrics, stop_event, logger)
                cycle_elapsed = time.perf_counter() - cycle_started

                if prices:
                    csv_writer.enqueue(prices)
                    log_prices(logger, prices, previous_prices, cfg.convert)
                    if cfg.json_file:
                        write_json_atomic(
                            cfg.json_file,
                            {
                                "timestamp": int(time.time()),
                                "convert": cfg.convert,
                                "prices": prices,
                                "symbols_requested": cfg.symbols,
                            },
                        )

                snapshot = metrics.snapshot()
                breaker_snapshot = breaker.snapshot()
                interval = adaptive_interval.update(snapshot, breaker_snapshot["state"])

                logger.debug(
                    "metrics | success=%.2f%% avg=%.3fs p95=%.3fs p99=%.3fs rps=%.2f requests=%d breaker=%s failures=%d cycle=%.3fs",
                    snapshot["success_rate"] * 100,
                    snapshot["avg_latency"],
                    snapshot["p95_latency"],
                    snapshot["p99_latency"],
                    snapshot["rps"],
                    snapshot["requests"],
                    breaker_snapshot["state"],
                    breaker_snapshot["failures"],
                    cycle_elapsed,
                )

                if cfg.one_shot:
                    break

                jitter = random.uniform(-0.15 * interval, 0.15 * interval)
                sleep_time = max(cfg.min_interval, interval + jitter - cycle_elapsed)
                stop_event.wait(sleep_time)

    finally:
        stop_event.set()
        csv_writer.join(timeout=10)
        snapshot = metrics.snapshot()
        logger.info(
            "Shutdown | success=%d failure=%d success_rate=%.2f%% avg_latency=%.3fs requests=%d",
            snapshot["success"],
            snapshot["failure"],
            snapshot["success_rate"] * 100,
            snapshot["avg_latency"],
            snapshot["requests"],
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
