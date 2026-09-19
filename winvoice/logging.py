"""
Structured logging with structlog + orjson.

- JSON Lines output with trace_id/span_id propagation
- Daily rotation + gzip compression (7 days retention)
- Prometheus metrics pushgateway integration
"""

from __future__ import annotations

import atexit
import gzip
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import orjson
import structlog
from structlog.types import EventDict, WrappedLogger

from .config import get_config
from .contracts import ProcessName


# ──────────────────────────────────────────────────────────────
# Custom Processors
# ──────────────────────────────────────────────────────────────

def add_process_name(logger: WrappedLogger, name: str, event_dict: EventDict) -> EventDict:
    event_dict["process"] = name
    return event_dict


def add_timestamp(logger: WrappedLogger, name: str, event_dict: EventDict) -> EventDict:
    from datetime import datetime, timezone
    event_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
    return event_dict


def ensure_trace_id(logger: WrappedLogger, name: str, event_dict: EventDict) -> EventDict:
    if "trace_id" not in event_dict:
        import uuid
        event_dict["trace_id"] = uuid.uuid4().hex[:16]
    return event_dict


def orjson_renderer(logger: WrappedLogger, name: str, event_dict: EventDict) -> bytes:
    return orjson.dumps(event_dict, option=orjson.OPT_APPEND_NEWLINE)


# ──────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────

_configured = False
_config_lock = threading.Lock()


def _normalise_process_name(process_name: "ProcessName | str") -> str:
    """Accept either a ProcessName member or a plain string."""
    if isinstance(process_name, ProcessName):
        return process_name.value
    return str(process_name)


def configure_logging(
    process_name: "ProcessName | str" = ProcessName.MAIN,
    log_dir: str | Path = "logs",
    level: str = "INFO",
    json_lines: bool = True,
) -> structlog.BoundLogger:
    """
    Configure structlog once per process.

    `process_name` accepts either a ProcessName member or a plain string
    (e.g. "main", "enroll"). Returns a logger bound with that name.
    """
    global _configured

    proc = _normalise_process_name(process_name)

    with _config_lock:
        if _configured:
            return structlog.get_logger().bind(process=proc)

        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        # File handler with daily rotation + gzip
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_path / f"{proc}.jsonl",
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
            delay=True,
        )

        # Compress rotated files
        def namer(name: str) -> str:
            return name + ".gz"

        def rotator(source: str, dest: str) -> None:
            with open(source, "rb") as f_in:
                with gzip.open(dest, "wb") as f_out:
                    f_out.write(f_in.read())
            os.remove(source)

        file_handler.namer = namer
        file_handler.rotator = rotator

        # Console handler for development
        console_handler = logging.StreamHandler(sys.stderr)

        # Root logger
        root = logging.getLogger()
        root.setLevel(getattr(logging, level.upper()))
        root.handlers.clear()
        root.addHandler(file_handler)
        if os.getenv("WINVOICE_CONSOLE_LOG", "0") == "1":
            root.addHandler(console_handler)

        # Structlog configuration
        processors = [
            structlog.contextvars.merge_contextvars,
            add_process_name,
            add_timestamp,
            ensure_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            orjson_renderer if json_lines else structlog.dev.ConsoleRenderer(),
        ]

        structlog.configure(
            processors=processors,
            wrapper_class=structlog.BoundLogger,
            logger_factory=structlog.BytesLoggerFactory() if json_lines else structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )

        _configured = True

    logger = structlog.get_logger().bind(process=proc)
    return logger


def get_logger(name: Optional[str] = None) -> structlog.BoundLogger:
    """Get a logger bound with optional name."""
    logger = structlog.get_logger()
    if name:
        logger = logger.bind(module=name)
    return logger


# ──────────────────────────────────────────────────────────────
# Trace Context Helpers
# ──────────────────────────────────────────────────────────────

_trace_context: threading.local = threading.local()


def set_trace_context(trace_id: str, span_id: Optional[str] = None) -> None:
    """Bind trace_id/span_id to current thread for automatic inclusion."""
    _trace_context.trace_id = trace_id
    if span_id:
        _trace_context.span_id = span_id


def clear_trace_context() -> None:
    if hasattr(_trace_context, "trace_id"):
        del _trace_context.trace_id
    if hasattr(_trace_context, "span_id"):
        del _trace_context.span_id


def bind_trace_context(logger: structlog.BoundLogger) -> structlog.BoundLogger:
    """Bind current thread's trace context to logger."""
    if hasattr(_trace_context, "trace_id"):
        logger = logger.bind(trace_id=_trace_context.trace_id)
    if hasattr(_trace_context, "span_id"):
        logger = logger.bind(span_id=_trace_context.span_id)
    return logger


# ──────────────────────────────────────────────────────────────
# Prometheus Metrics (optional)
# ──────────────────────────────────────────────────────────────

_metrics_initialized = False
_metrics_lock = threading.Lock()


def init_metrics(pushgateway_url: Optional[str] = None, job: str = "winvoice") -> None:
    """Initialize Prometheus metrics and optional pushgateway."""
    global _metrics_initialized
    with _metrics_lock:
        if _metrics_initialized:
            return

        try:
            from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, push_to_gateway

            registry = CollectorRegistry()

            # Latency histograms
            REQUEST_LATENCY = Histogram(
                "winvoice_request_latency_seconds",
                "End-to-end request latency",
                ["stage"],  # kws, vad, asr, llm, tool, tts
                registry=registry,
            )

            # Counters
            REQUEST_TOTAL = Counter(
                "winvoice_requests_total",
                "Total requests processed",
                ["stage", "outcome"],  # success, error, timeout
                registry=registry,
            )

            ACTIVE_STREAMS = Gauge(
                "winvoice_active_streams",
                "Number of active audio streams",
                registry=registry,
            )

            # Store for later use
            import winvoice.logging as logmod
            logmod.REQUEST_LATENCY = REQUEST_LATENCY
            logmod.REQUEST_TOTAL = REQUEST_TOTAL
            logmod.ACTIVE_STREAMS = ACTIVE_STREAMS
            logmod._pushgateway_url = pushgateway_url
            logmod._pushgateway_job = job
            logmod._registry = registry

            # Periodic push
            if pushgateway_url:
                import threading

                def push_loop():
                    while True:
                        try:
                            push_to_gateway(pushgateway_url, job=job, registry=registry)
                        except Exception:
                            pass  # swallow
                        time.sleep(10)

                t = threading.Thread(target=push_loop, daemon=True)
                t.start()

            _metrics_initialized = True
        except ImportError:
            pass  # prometheus_client not installed


def observe_latency(stage: str, seconds: float) -> None:
    if _metrics_initialized:
        try:
            REQUEST_LATENCY.labels(stage=stage).observe(seconds)
        except Exception:
            pass


def inc_request(stage: str, outcome: str) -> None:
    if _metrics_initialized:
        try:
            REQUEST_TOTAL.labels(stage=stage, outcome=outcome).inc()
        except Exception:
            pass


def set_active_streams(count: int) -> None:
    if _metrics_initialized:
        try:
            ACTIVE_STREAMS.set(count)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# Cleanup on exit
# ──────────────────────────────────────────────────────────────

def _cleanup() -> None:
    logging.shutdown()

atexit.register(_cleanup)