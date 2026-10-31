"""Give the application's own log records somewhere to go.

The container runs ``uvicorn main:app --no-access-log`` with no ``--log-config``,
so uvicorn installs its own ``LOGGING_CONFIG``: that configures only the
``uvicorn*`` loggers and leaves the root logger handler-less. Every application
``log.info`` is therefore dropped - the ``main.py`` startup notice has been
invisible since it landed - and application WARNINGs only reach stderr through
logging's last-resort handler, with no timestamp and no logger name.

``configure_logging`` adds one stderr handler to the root logger, once, so
application records share the shape of uvicorn's own: a timestamp, the level, the
logger name, then the message.
"""
import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Give the root logger a stderr handler, once, without stomping anyone.

    ``force=False`` is deliberate: pytest and uvicorn install their own handlers
    before the app boots, and replacing them would silence the very lines this
    exists to surface (and break caplog). A second call is therefore a no-op.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr, force=False)
