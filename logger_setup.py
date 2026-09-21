import json
import logging
import os
from datetime import datetime, timezone

import config


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)

    logger = logging.getLogger("autoscaling-controller")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )

    file_handler = logging.FileHandler(config.CONTROLLER_LOG_FILE)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def log_decision(record: dict):
    """Escribe una linea JSON en decisions.log con el registro completo del
    ciclo. record debe incluir al menos: timestamp, metricas, capacidad,
    decision, justificacion, accion, resultado_accion."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with open(config.DECISIONS_LOG_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
