#!/usr/bin/env python3
"""
Reliable concurrent CoinMarketCap price tracker (v4).

Main improvements over the previous version:
- Rolling request-health metrics instead of lifetime-only adaptive decisions.
- Circuit breaker with safe HALF_OPEN probe accounting under concurrency.
- Explicit application-level retries with interruptible backoff and Retry-After support.
- Last-known-price cache: partial API failures no longer erase valid JSON prices.
- Per-symbol freshness metadata and stale-symbol reporting.
- Atomic and durable JSON replacement with unique temporary files.
- CSV writer failure propagation and deterministic graceful draining.
- Monotonic cycle scheduling with bounded jitter (no gradual timing drift).
- Strict configuration validation and CLI controls for breaker/metrics/writer settings.
- Optional environment-file loading when python-dotenv is installed.

Requirements:
    pip install requests colorama

Optional:
    pip install python-dotenv

Example:
    export CMC_API_KEY="YOUR_KEY"
    python cmc_tracker_pro_v4.py \
        --symbols BTC ETH SOL XRP ADA \
        --interval 5 \
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
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    from colorama import Fore, Style, init

    init(autoreset=True)
    COLOR = True
except ImportError:
    COLOR = False

    class _NoColor:
        BLACK = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = RESET_ALL = ""

    Fore = Style = _NoColor()  # type: ignore[assignment]


API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_USER_AGENT = "CMC-Reliable-Tracker/4.0"
CSV_STOP = object()
_thread_local = threading.local()


class TrackerError(RuntimeError):
    """Base tracker error."""


class FatalAPIError(TrackerError):
    """Non-retryable API/configuration error."""


@dataclass(frozen=True)
class Config:
    api_key: str
    symbols: Tuple[str, ...]
    convert: str = "USD"

    interval: float = 5.0
    min_interval: float = 1.0
    max_interval: float = 60.0
    jitter_ratio: float = 0.10
    workers: int = 4
    batch_size: int = 50
    one_shot: bool = False

    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    max_retries: int = 3
    backoff_factor: float = 0.8
    max_backoff: float = 30.0

    pool_connections: int = 8
    pool_maxsize: int = 8
    rate_limit_per_sec: float = 8.0
    burst_capacity: int = 10

    fail_threshold: int = 5
    cooldown: float = 30.0
    half_open_max_calls: int = 1

    csv_file: Optional[Path] = None
    json_file: Optional[Path] = None
    write_queue_size: int = 10_000
    csv_flush_rows: int = 100
    csv_flush_sec: float = 2.0

    proxy: Optional[str] = None
    debug: bool = False
    quiet: bool = False
    metrics_window: int = 200
    stale_after: float = 120.0


@dataclass(frozen=True)
class BatchResult:
    requested: Tuple[str, ...]
    prices: Dict[str, float]
    fetched_at: float
    attempts: int


@dataclass(frozen=True)
class PricePoint:
    price: float
    updated_at: float


def utc_iso(timestamp: Optional[float] = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def colorize(text: str, color: str) -> str:
    return f"{color}{text}{Style.RESET_ALL}" if COLOR else text


def setup_logger(debug: bool, quiet: bool) -> logging.Logger:
    logger = logging.getLogger("cmc_tracker")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING if quiet else logging.DEBUG if debug else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(threadName)-14s | %(message)s")
    )
    logger.addHandler(handler)

    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logger


def chunks(items: Sequence[str], size: int) -> Iterable[Tuple[str, ...]]:
    for index in range(0, len(items), size):
        yield tuple(items[index : index + size])


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


class TokenBucketRateLimiter:
    def __init__(self, rate: float, capacity: int):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def wait(self, stop_event: threading.Event) -> bool:
        while not stop_event.is_set():
            delay = 0.05
            with self.lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self.updated)
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                delay = min(0.25, max(0.01, (1.0 - self.tokens) / self.rate))

            stop_event.wait(delay)
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
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.half_open_inflight = 0
        self.lock = threading.Lock()

    def acquire(self) -> bool:
        with self.lock:
            now = time.monotonic()
            if self.state == self.OPEN:
                if now < self.open_until:
                    return False
                self.state = self.HALF_OPEN
                self.half_open_inflight = 0

            if self.state == self.HALF_OPEN:
                if self.half_open_inflight >= self.half_open_max_calls:
                    return False
                self.half_open_inflight += 1

            return True

    def record_success(self) -> None:
        with self.lock:
            if self.state == self.HALF_OPEN:
                self.half_open_inflight = max(0, self.half_open_inflight - 1)
                self.state = self.CLOSED
            self.consecutive_failures = 0

    def record_failure(self) -> None:
        with self.lock:
            if self.state == self.HALF_OPEN:
                self.half_open_inflight = max(0, self.half_open_inflight - 1)
                self._open_locked()
                return

            self.consecutive_failures += 1
            if self.consecutive_failures >= self.fail_threshold:
                self._open_locked()

    def release_cancelled_probe(self) -> None:
        with self.lock:
            if self.state == self.HALF_OPEN:
                self.half_open_inflight = max(0, self.half_open_inflight - 1)

    def _open_locked(self) -> None:
        self.state = self.OPEN
        self.open_until = time.monotonic() + self.cooldown
        self.half_open_inflight = 0

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            remaining = (
                max(0.0, self.open_until - time.monotonic()) if self.state == self.OPEN else 0.0
            )
            return {
                "state": self.state,
                "consecutive_failures": self.consecutive_failures,
                "open_remaining_sec": remaining,
                "half_open_inflight": self.half_open_inflight,
            }


class Metrics:
    def __init__(self, window: int):
        self.lock = threading.Lock()
        self.started_monotonic = time.monotonic()
        self.total_success = 0
        self.total_failure = 0
        self.recent: deque[Tuple[bool, float, int]] = deque(maxlen=window)

    def record(self, ok: bool, latency: float, size: int = 0) -> None:
        with self.lock:
            if ok:
                self.total_success += 1
            else:
                self.total_failure += 1
            self.recent.append((ok, latency, size))

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            recent = list(self.recent)
            total_requests = self.total_success + self.total_failure
            uptime = max(0.001, time.monotonic() - self.started_monotonic)

        latencies = [latency for _, latency, _ in recent]
        recent_success = sum(1 for ok, _, _ in recent if ok)
        recent_count = len(recent)
        return {
            "success": self.total_success,
            "failure": self.total_failure,
            "requests": total_requests,
            "success_rate": self.total_success / total_requests if total_requests else 1.0,
            "recent_requests": recent_count,
            "recent_success_rate": recent_success / recent_count if recent_count else 1.0,
            "avg_latency": statistics.fmean(latencies) if latencies else 0.0,
            "p95_latency": percentile(latencies, 95),
            "p99_latency": percentile(latencies, 99),
            "rps": total_requests / uptime,
        }


class AdaptiveInterval:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.current = min(max(cfg.interval, cfg.min_interval), cfg.max_interval)

    def update(self, metrics: Mapping[str, Any], breaker_state: str) -> float:
        success_rate = float(metrics["recent_success_rate"])
        avg_latency = float(metrics["avg_latency"])
        sample_size = int(metrics["recent_requests"])

        if breaker_state == CircuitBreaker.OPEN:
            multiplier = 1.50
        elif sample_size >= 5 and success_rate < 0.70:
            multiplier = 1.35
        elif sample_size >= 5 and success_rate < 0.90:
            multiplier = 1.15
        elif sample_size >= 10 and success_rate >= 0.99 and 0 < avg_latency < 1.0:
            multiplier = 0.95
        else:
            # Slowly return to the configured base interval instead of drifting forever.
            target = min(max(self.cfg.interval, self.cfg.min_interval), self.cfg.max_interval)
            self.current += (target - self.current) * 0.08
            multiplier = 1.0

        self.current *= multiplier
        self.current = min(max(self.current, self.cfg.min_interval), self.cfg.max_interval)
        return self.current


class PriceCache:
    def __init__(self):
        self._data: Dict[str, PricePoint] = {}

    def update(self, prices: Mapping[str, float], fetched_at: float) -> None:
        for symbol, price in prices.items():
            self._data[symbol] = PricePoint(price=price, updated_at=fetched_at)

    def prices(self) -> Dict[str, float]:
        return {symbol: point.price for symbol, point in sorted(self._data.items())}

    def metadata(self, now: float, stale_after: float) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for symbol, point in sorted(self._data.items()):
            age = max(0.0, now - point.updated_at)
            result[symbol] = {
                "updated_at": utc_iso(point.updated_at),
                "age_sec": round(age, 3),
                "stale": age > stale_after,
            }
        return result


class AsyncCSVWriter(threading.Thread):
    daemon = False

    def __init__(self, cfg: Config, logger: logging.Logger):
        super().__init__(name="csv-writer")
        self.cfg = cfg
        self.logger = logger
        self.queue: queue.Queue[object] = queue.Queue(maxsize=cfg.write_queue_size)
        self.error: Optional[BaseException] = None
        if cfg.csv_file:
            self._init_csv(cfg.csv_file)

    def _init_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(
                ["timestamp_unix", "timestamp_utc", "symbol", f"price_{self.cfg.convert}"]
            )

    def enqueue(self, fetched_at: float, data: Mapping[str, float]) -> bool:
        if not self.cfg.csv_file or not data or self.error is not None:
            return False
        item = (fetched_at, dict(data))
        try:
            self.queue.put(item, timeout=0.5)
            return True
        except queue.Full:
            self.logger.warning("CSV queue is full; dropped one price snapshot")
            return False

    def close(self) -> None:
        if not self.is_alive():
            return
        while self.is_alive():
            try:
                self.queue.put(CSV_STOP, timeout=0.5)
                return
            except queue.Full:
                if self.error is not None:
                    return

    def run(self) -> None:
        buffer: List[List[Any]] = []
        last_flush = time.monotonic()
        stopping = False

        try:
            while not stopping:
                timeout = max(0.05, self.cfg.csv_flush_sec - (time.monotonic() - last_flush))
                try:
                    item = self.queue.get(timeout=timeout)
                    if item is CSV_STOP:
                        stopping = True
                    else:
                        fetched_at, data = item  # type: ignore[misc]
                        iso_time = utc_iso(fetched_at)
                        for symbol, price in sorted(data.items()):
                            buffer.append([int(fetched_at), iso_time, symbol, price])
                    self.queue.task_done()
                except queue.Empty:
                    pass

                flush_due = time.monotonic() - last_flush >= self.cfg.csv_flush_sec
                if buffer and (len(buffer) >= self.cfg.csv_flush_rows or flush_due or stopping):
                    self._flush(buffer)
                    last_flush = time.monotonic()
        except BaseException as exc:
            self.error = exc
            self.logger.exception("CSV writer terminated: %s", exc)

    def _flush(self, buffer: List[List[Any]]) -> None:
        if not self.cfg.csv_file or not buffer:
            return
        with self.cfg.csv_file.open("a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerows(buffer)
            file.flush()
        buffer.clear()


class SessionRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions: List[requests.Session] = []

    def register(self, session: requests.Session) -> None:
        with self.lock:
            self.sessions.append(session)

    def close_all(self) -> None:
        with self.lock:
            sessions, self.sessions = self.sessions, []
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass


SESSION_REGISTRY = SessionRegistry()


def create_session(cfg: Config) -> requests.Session:
    # Retries are implemented explicitly in fetch_batch so attempts, metrics,
    # breaker state and shutdown remain observable and controllable.
    adapter = HTTPAdapter(
        max_retries=0,
        pool_connections=cfg.pool_connections,
        pool_maxsize=cfg.pool_maxsize,
        pool_block=True,
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": DEFAULT_USER_AGENT,
            "X-CMC_PRO_API_KEY": cfg.api_key,
        }
    )
    if cfg.proxy:
        session.proxies.update({"http": cfg.proxy, "https": cfg.proxy})
    SESSION_REGISTRY.register(session)
    return session


def get_session(cfg: Config) -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = create_session(cfg)
        _thread_local.session = session
    return session


def retry_after_seconds(response: requests.Response, maximum: float) -> Optional[float]:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(maximum, max(0.0, float(value)))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return min(maximum, max(0.0, parsed.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return None


def backoff_delay(cfg: Config, retry_index: int, response: Optional[requests.Response] = None) -> float:
    if response is not None:
        header_delay = retry_after_seconds(response, cfg.max_backoff)
        if header_delay is not None:
            return header_delay
    exponential = cfg.backoff_factor * (2 ** retry_index)
    return min(cfg.max_backoff, random.uniform(exponential * 0.75, exponential * 1.25))


def parse_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        status = payload.get("status", {}) if isinstance(payload, dict) else {}
        message = status.get("error_message") if isinstance(status, dict) else None
        if message:
            return str(message)
    except ValueError:
        pass
    text = " ".join(response.text.split())
    return text[:500] or response.reason or "unknown error"


def extract_prices(
    payload: Mapping[str, Any], requested_symbols: Sequence[str], convert: str
) -> Dict[str, float]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TrackerError("Invalid CoinMarketCap response: 'data' is missing or not an object")

    prices: Dict[str, float] = {}
    for symbol in requested_symbols:
        entry = data.get(symbol)
        if not isinstance(entry, dict):
            continue
        quote = entry.get("quote")
        converted = quote.get(convert) if isinstance(quote, dict) else None
        raw_price = converted.get("price") if isinstance(converted, dict) else None
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price >= 0:
            prices[symbol] = price
    return prices


def fetch_batch(
    cfg: Config,
    symbols: Tuple[str, ...],
    limiter: TokenBucketRateLimiter,
    breaker: CircuitBreaker,
    metrics: Metrics,
    stop_event: threading.Event,
    logger: logging.Logger,
) -> BatchResult:
    if not breaker.acquire():
        state = breaker.snapshot()
        logger.debug(
            "Breaker blocks batch=%s state=%s remaining=%.1fs",
            ",".join(symbols),
            state["state"],
            state["open_remaining_sec"],
        )
        return BatchResult(symbols, {}, time.time(), 0)

    probe_released = False
    attempts = 0
    last_error = "request failed"

    try:
        for retry_index in range(cfg.max_retries + 1):
            if stop_event.is_set():
                breaker.release_cancelled_probe()
                probe_released = True
                break
            if not limiter.wait(stop_event):
                breaker.release_cancelled_probe()
                probe_released = True
                break

            attempts += 1
            started = time.perf_counter()
            response: Optional[requests.Response] = None
            try:
                response = get_session(cfg).get(
                    API_URL,
                    params={"symbol": ",".join(symbols), "convert": cfg.convert},
                    timeout=(cfg.connect_timeout, cfg.read_timeout),
                )
                latency = time.perf_counter() - started
                size = len(response.content)

                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        metrics.record(False, latency, size)
                        last_error = f"invalid JSON: {exc}"
                    else:
                        prices = extract_prices(payload, symbols, cfg.convert)
                        if prices:
                            metrics.record(True, latency, size)
                            breaker.record_success()
                            missing = sorted(set(symbols) - set(prices))
                            if missing:
                                logger.warning("Missing prices: %s", ",".join(missing))
                            return BatchResult(symbols, prices, time.time(), attempts)
                        metrics.record(False, latency, size)
                        last_error = "response contained no usable prices"

                elif response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    metrics.record(False, latency, size)
                    last_error = f"HTTP {response.status_code}: {parse_error_message(response)}"
                else:
                    metrics.record(False, latency, size)
                    breaker.record_failure()
                    raise FatalAPIError(
                        f"CoinMarketCap HTTP {response.status_code}: {parse_error_message(response)}"
                    )

            except requests.RequestException as exc:
                latency = time.perf_counter() - started
                metrics.record(False, latency)
                last_error = f"{type(exc).__name__}: {exc}"

            if retry_index < cfg.max_retries and not stop_event.is_set():
                delay = backoff_delay(cfg, retry_index, response)
                logger.debug(
                    "Retry batch=%s attempt=%d/%d in %.2fs: %s",
                    ",".join(symbols),
                    attempts,
                    cfg.max_retries + 1,
                    delay,
                    last_error,
                )
                if stop_event.wait(delay):
                    break

        if not probe_released:
            breaker.record_failure()
        logger.warning(
            "Batch failed after %d attempt(s): symbols=%s error=%s",
            attempts,
            ",".join(symbols),
            last_error,
        )
        return BatchResult(symbols, {}, time.time(), attempts)
    except Exception:
        if not probe_released:
            # FatalAPIError already records failure before raising.
            if not isinstance(sys.exc_info()[1], FatalAPIError):
                breaker.record_failure()
        raise


def write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def log_prices(
    logger: logging.Logger,
    prices: Mapping[str, float],
    previous: Dict[str, float],
    convert: str,
) -> None:
    for symbol, price in sorted(prices.items()):
        old = previous.get(symbol)
        if old is None or price == old:
            color, marker = Fore.YELLOW, "="
        elif price > old:
            color, marker = Fore.GREEN, "↑"
        else:
            color, marker = Fore.RED, "↓"

        change = ""
        if old not in (None, 0.0):
            change = f" ({(price / old - 1.0) * 100:+.3f}%)"
        logger.info(
            "%-8s %s %s %s%s",
            symbol,
            marker,
            colorize(f"{price:,.10g}", color),
            convert,
            change,
        )
        previous[symbol] = price


def run_cycle(
    cfg: Config,
    executor: ThreadPoolExecutor,
    limiter: TokenBucketRateLimiter,
    breaker: CircuitBreaker,
    metrics: Metrics,
    stop_event: threading.Event,
    logger: logging.Logger,
) -> List[BatchResult]:
    future_map: Dict[Future[BatchResult], Tuple[str, ...]] = {
        executor.submit(fetch_batch, cfg, batch, limiter, breaker, metrics, stop_event, logger): batch
        for batch in chunks(cfg.symbols, cfg.batch_size)
    }

    results: List[BatchResult] = []
    fatal_error: Optional[BaseException] = None
    for future in as_completed(future_map):
        try:
            results.append(future.result())
        except FatalAPIError as exc:
            fatal_error = exc
            stop_event.set()
            logger.error("Fatal API error for batch=%s: %s", ",".join(future_map[future]), exc)
            break
        except Exception as exc:
            logger.exception("Unexpected batch error for %s: %s", ",".join(future_map[future]), exc)

    if fatal_error is not None:
        for future in future_map:
            future.cancel()
        raise fatal_error
    return results


def setup_signals(stop_event: threading.Event, logger: logging.Logger) -> None:
    signal_count = 0

    def handler(sig: int, _frame: Any) -> None:
        nonlocal signal_count
        signal_count += 1
        if signal_count == 1:
            logger.warning("Received signal %s; shutting down gracefully", sig)
            stop_event.set()
        else:
            logger.error("Received signal %s again; forcing exit", sig)
            raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def parse_symbols(raw_symbols: Sequence[str]) -> Tuple[str, ...]:
    symbols: List[str] = []
    seen = set()
    for item in raw_symbols:
        for part in item.replace(",", " ").split():
            symbol = part.strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return tuple(symbols)


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a finite number > 0")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("value must be a finite number >= 0")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> Config:
    parser = argparse.ArgumentParser(description="Reliable concurrent CoinMarketCap price tracker")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols; commas are accepted")
    parser.add_argument("--convert", default="USD", help="Quote currency, e.g. USD or EUR")
    parser.add_argument("--interval", type=positive_float, default=5.0)
    parser.add_argument("--min-interval", type=positive_float, default=1.0)
    parser.add_argument("--max-interval", type=positive_float, default=60.0)
    parser.add_argument("--jitter-ratio", type=non_negative_float, default=0.10)
    parser.add_argument("--workers", type=positive_int, default=4)
    parser.add_argument("--batch-size", type=positive_int, default=50)
    parser.add_argument("--rate-limit", type=positive_float, default=8.0)
    parser.add_argument("--burst", type=positive_int, default=10)
    parser.add_argument("--connect-timeout", type=positive_float, default=5.0)
    parser.add_argument("--read-timeout", type=positive_float, default=10.0)
    parser.add_argument("--max-retries", type=non_negative_int, default=3)
    parser.add_argument("--backoff-factor", type=positive_float, default=0.8)
    parser.add_argument("--max-backoff", type=positive_float, default=30.0)
    parser.add_argument("--fail-threshold", type=positive_int, default=5)
    parser.add_argument("--cooldown", type=positive_float, default=30.0)
    parser.add_argument("--half-open-max-calls", type=positive_int, default=1)
    parser.add_argument("--metrics-window", type=positive_int, default=200)
    parser.add_argument("--stale-after", type=positive_float, default=120.0)
    parser.add_argument("--csv", type=Path, help="Append price history to CSV")
    parser.add_argument("--json", type=Path, help="Atomically write latest snapshot")
    parser.add_argument("--csv-queue-size", type=positive_int, default=10_000)
    parser.add_argument("--csv-flush-rows", type=positive_int, default=100)
    parser.add_argument("--csv-flush-sec", type=positive_float, default=2.0)
    parser.add_argument(
        "--proxy",
        default=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
        help="Optional HTTP(S) proxy URL",
    )
    parser.add_argument("--one-shot", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.getenv("CMC_API_KEY", "").strip()
    if not api_key:
        parser.error("CMC_API_KEY environment variable is missing")

    symbols = parse_symbols(args.symbols)
    if not symbols:
        parser.error("no valid symbols provided")
    if args.min_interval > args.max_interval:
        parser.error("--min-interval cannot be greater than --max-interval")
    if not args.min_interval <= args.interval <= args.max_interval:
        parser.error("--interval must be between --min-interval and --max-interval")
    if args.jitter_ratio > 1.0:
        parser.error("--jitter-ratio cannot be greater than 1.0")
    if args.batch_size > 100:
        parser.error("--batch-size cannot be greater than 100")

    return Config(
        api_key=api_key,
        symbols=symbols,
        convert=args.convert.upper().strip(),
        interval=args.interval,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
        jitter_ratio=args.jitter_ratio,
        workers=args.workers,
        batch_size=args.batch_size,
        rate_limit_per_sec=args.rate_limit,
        burst_capacity=args.burst,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_retries=args.max_retries,
        backoff_factor=args.backoff_factor,
        max_backoff=args.max_backoff,
        fail_threshold=args.fail_threshold,
        cooldown=args.cooldown,
        half_open_max_calls=args.half_open_max_calls,
        metrics_window=args.metrics_window,
        stale_after=args.stale_after,
        csv_file=args.csv,
        json_file=args.json,
        write_queue_size=args.csv_queue_size,
        csv_flush_rows=args.csv_flush_rows,
        csv_flush_sec=args.csv_flush_sec,
        proxy=args.proxy,
        one_shot=args.one_shot,
        quiet=args.quiet,
        debug=args.debug,
    )


def build_json_snapshot(
    cfg: Config,
    cache: PriceCache,
    fresh_symbols: Sequence[str],
    failed_symbols: Sequence[str],
    metrics: Mapping[str, Any],
    breaker: Mapping[str, Any],
) -> Dict[str, Any]:
    now = time.time()
    metadata = cache.metadata(now, cfg.stale_after)
    stale_symbols = [symbol for symbol, item in metadata.items() if item["stale"]]
    return {
        "timestamp": int(now),
        "timestamp_utc": utc_iso(now),
        "convert": cfg.convert,
        "prices": cache.prices(),
        "price_metadata": metadata,
        "symbols_requested": list(cfg.symbols),
        "fresh_symbols": sorted(fresh_symbols),
        "failed_symbols": sorted(failed_symbols),
        "stale_symbols": stale_symbols,
        "metrics": dict(metrics),
        "circuit_breaker": dict(breaker),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg = parse_args(argv)
    logger = setup_logger(cfg.debug, cfg.quiet)
    stop_event = threading.Event()
    setup_signals(stop_event, logger)

    limiter = TokenBucketRateLimiter(cfg.rate_limit_per_sec, cfg.burst_capacity)
    breaker = CircuitBreaker(cfg.fail_threshold, cfg.cooldown, cfg.half_open_max_calls)
    metrics = Metrics(cfg.metrics_window)
    adaptive_interval = AdaptiveInterval(cfg)
    csv_writer = AsyncCSVWriter(cfg, logger)
    cache = PriceCache()
    previous_prices: Dict[str, float] = {}
    exit_code = 0

    logger.info(
        "Starting | symbols=%d workers=%d batches=%d interval=%.2fs rate_limit=%.2f/s proxy=%s",
        len(cfg.symbols),
        cfg.workers,
        math.ceil(len(cfg.symbols) / cfg.batch_size),
        cfg.interval,
        cfg.rate_limit_per_sec,
        "yes" if cfg.proxy else "no",
    )

    if cfg.csv_file:
        csv_writer.start()

    executor = ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="fetch")
    next_cycle_at = time.monotonic()

    try:
        while not stop_event.is_set():
            cycle_started = time.monotonic()
            results = run_cycle(cfg, executor, limiter, breaker, metrics, stop_event, logger)

            fresh_prices: Dict[str, float] = {}
            successful_symbols = set()
            for result in results:
                if result.prices:
                    cache.update(result.prices, result.fetched_at)
                    fresh_prices.update(result.prices)
                    successful_symbols.update(result.prices)
                    csv_writer.enqueue(result.fetched_at, result.prices)

            failed_symbols = sorted(set(cfg.symbols) - successful_symbols)
            if fresh_prices:
                log_prices(logger, fresh_prices, previous_prices, cfg.convert)
            elif not stop_event.is_set():
                logger.warning("Cycle returned no fresh prices")

            snapshot = metrics.snapshot()
            breaker_snapshot = breaker.snapshot()
            interval = adaptive_interval.update(snapshot, breaker_snapshot["state"])

            if cfg.json_file:
                write_json_atomic(
                    cfg.json_file,
                    build_json_snapshot(
                        cfg,
                        cache,
                        list(successful_symbols),
                        failed_symbols,
                        snapshot,
                        breaker_snapshot,
                    ),
                )

            cycle_elapsed = time.monotonic() - cycle_started
            logger.debug(
                "metrics | recent_success=%.2f%% total_success=%.2f%% avg=%.3fs "
                "p95=%.3fs p99=%.3fs rps=%.2f requests=%d breaker=%s "
                "consecutive_failures=%d cycle=%.3fs next_interval=%.3fs",
                snapshot["recent_success_rate"] * 100,
                snapshot["success_rate"] * 100,
                snapshot["avg_latency"],
                snapshot["p95_latency"],
                snapshot["p99_latency"],
                snapshot["rps"],
                snapshot["requests"],
                breaker_snapshot["state"],
                breaker_snapshot["consecutive_failures"],
                cycle_elapsed,
                interval,
            )

            if cfg.one_shot:
                if not fresh_prices:
                    exit_code = 2
                break

            jitter = random.uniform(-cfg.jitter_ratio, cfg.jitter_ratio) * interval
            next_cycle_at = max(next_cycle_at + interval + jitter, time.monotonic())
            stop_event.wait(max(0.0, next_cycle_at - time.monotonic()))

    except FatalAPIError:
        exit_code = 3
    except KeyboardInterrupt:
        stop_event.set()
        exit_code = 130
    except Exception as exc:
        logger.exception("Fatal tracker error: %s", exc)
        exit_code = 1
    finally:
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=True)
        SESSION_REGISTRY.close_all()

        if cfg.csv_file:
            csv_writer.close()
            csv_writer.join(timeout=max(5.0, cfg.csv_flush_sec * 3))
            if csv_writer.is_alive():
                logger.error("CSV writer did not stop cleanly")
                exit_code = exit_code or 1
            elif csv_writer.error is not None:
                logger.error("CSV writer failed: %s", csv_writer.error)
                exit_code = exit_code or 1

        snapshot = metrics.snapshot()
        logger.info(
            "Shutdown | success=%d failure=%d success_rate=%.2f%% avg_latency=%.3fs requests=%d",
            snapshot["success"],
            snapshot["failure"],
            snapshot["success_rate"] * 100,
            snapshot["avg_latency"],
            snapshot["requests"],
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
