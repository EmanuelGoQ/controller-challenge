#!/usr/bin/env python3

import time
import logging
from datetime import datetime, timezone

import config
import state_manager as sm
import aws_client as aws
from logger_setup import setup_logging, log_decision

logger = logging.getLogger("autoscaling-controller")


def run_cycle(state: dict) -> dict:
    """Ejecuta un ciclo completo del controlador. Retorna el estado
    actualizado (para persistirlo despues de la llamada)."""

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # 1. ----------- OBSERVAR -----------

    try:
        instancias = aws.describe_managed_instances()
        instance_ids = [i["instance_id"] for i in instancias]
        salud = aws.get_target_health()
        cpu_por_instancia = aws.get_cpu_utilization_per_instance(
            instance_ids, config.METRIC_LOOKBACK_MINUTES, config.METRIC_PERIOD_SECONDS
        )
        latencia_alb = aws.get_alb_target_response_time(
            config.METRIC_LOOKBACK_MINUTES, config.METRIC_PERIOD_SECONDS
        )

        state["fallos_consecutivos_observacion"] = 0

    except Exception as e:
        state["fallos_consecutivos_observacion"] += 1
        logger.error("Fallo al observar el estado del sistema: %s", e)

        record.update({
            "fase_fallida": "OBSERVAR",
            "error": str(e),
            "decision": "MAINTAIN_CAPACITY",
            "justificacion": (
                "No fue posible obtener metricas/estado desde AWS. "
                "Se mantiene la capacidad actual por seguridad "
                "(fallos consecutivos de observacion: %d)."
                % state["fallos_consecutivos_observacion"]
            ),
            "accion_solicitada": "NONE",
            "resultado_accion": "N/A",
        })
        log_decision(record)

        if state["fallos_consecutivos_observacion"] >= config.MAX_CONSECUTIVE_OBSERVATION_FAILURES:
            logger.critical(
                "%d fallos consecutivos de observacion. Revisar conectividad/"
                "permisos del controlador.",
                state["fallos_consecutivos_observacion"],
            )
        return state

    record["instancias_totales"] = instance_ids
    record["salud_alb"] = salud
    record["cpu_por_instancia"] = cpu_por_instancia
    record["latencia_alb_promedio_ms"] = (
        round(latencia_alb * 1000, 2) if latencia_alb is not None else None
    )


    # 2. ---------- FILTRAR instancias en warm-up ----------

    instancias_validas = []
    instancias_en_warmup_actual = []

    for inst in instancias:
        instance_id = inst["instance_id"]
        es_healthy = salud.get(instance_id) == "healthy"

        if sm.is_instance_warm(state, instance_id, es_healthy, config.WARMUP_GRACE_SECONDS):
            if instance_id in state["instancias_en_warmup"]:
                sm.remove_instance_from_warmup(state, instance_id)
                logger.info("Instancia %s completo su warm-up, entra al analisis.", instance_id)
            instancias_validas.append(instance_id)
        else:
            instancias_en_warmup_actual.append(instance_id)

    record["instancias_validas_para_decision"] = instancias_validas
    record["instancias_en_warmup"] = instancias_en_warmup_actual
    capacidad_actual = len(instancias)  # capacidad total incluye las en warm-up
    record["capacidad_actual"] = capacidad_actual

    if not instancias_validas:
        record.update({
            "decision": "MAINTAIN_CAPACITY",
            "justificacion": "Todas las instancias estan en warm-up; sin datos confiables aun.",
            "accion_solicitada": "NONE",
            "resultado_accion": "N/A",
        })
        log_decision(record)
        return state
