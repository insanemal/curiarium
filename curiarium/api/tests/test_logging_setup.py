"""The app's own log records must reach stderr even behind uvicorn's config.

The container runs `uvicorn main:app --no-access-log` with no `--log-config`, so
uvicorn's `LOGGING_CONFIG` configures only the `uvicorn*` loggers and leaves the
root logger handler-less. Every application `log.info` was dropped and app
WARNINGs only reached stderr through logging's last-resort handler, without a
timestamp or logger name. `configure_logging` is the one fix, called first in
the lifespan.
"""
import logging
import sys

from logging_setup import configure_logging


def _handlerless_root(monkeypatch) -> logging.Logger:
    """Take the root logger back to the state uvicorn's config leaves it in."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    return root


def test_root_gets_a_stream_handler_at_info(monkeypatch) -> None:
    root = _handlerless_root(monkeypatch)

    configure_logging()

    stream_handlers = [handler for handler in root.handlers if isinstance(handler, logging.StreamHandler)]
    assert len(root.handlers) == 1
    assert len(stream_handlers) == 1
    assert stream_handlers[0].stream is sys.stderr
    assert root.level == logging.INFO


def test_a_second_call_is_a_no_op(monkeypatch) -> None:
    root = _handlerless_root(monkeypatch)

    configure_logging()
    configure_logging()

    assert len(root.handlers) == 1


def test_an_app_info_record_reaches_stderr(monkeypatch, capsys) -> None:
    # The acceptance evidence that `docker logs` sees the line at all.
    _handlerless_root(monkeypatch)

    configure_logging()
    logging.getLogger("library_scan").info("library scan finished in 0.0s")

    assert "library scan finished in 0.0s" in capsys.readouterr().err
