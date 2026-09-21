#!/usr/bin/env python3

import json
import state_manager as sm
import config
from logger_setup import setup_logging
from controller import run_cycle

if __name__ == "__main__":
    setup_logging()
    state = sm.load_state(config.STATE_FILE_PATH)
    estado_resultante = run_cycle(state)
    sm.save_state(config.STATE_FILE_PATH, estado_resultante)

    print("\n--- Estado del controlador tras el ciclo ---")
    print(json.dumps(estado_resultante, indent=2, default=str))
    print(f"\nRevisa el detalle de la decision en: {config.DECISIONS_LOG_FILE}")
