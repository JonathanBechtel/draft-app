import logging, logging.config

def setup_logging(level: str = "INFO", access_log: bool = True):
    level = level.upper()
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                        "datefmt": "%H:%M:%S"},
            # Uvicorn pre-formats access log lines; don't expect extra fields
            "access_simple": {"format": "%(message)s"},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "default"},
            "access":  {"class": "logging.StreamHandler", "formatter": "access_simple"},
        },
        "loggers": {
            "uvicorn.error":  {"level": level, "handlers": ["console"], "propagate": False},
            "uvicorn.access": {"level": ("INFO" if access_log else "WARNING"),
                               "handlers": ["access"], "propagate": False},
        },
        # 👇 this is the key line so your app loggers print
        "root": {"level": level, "handlers": ["console"]},
    })
