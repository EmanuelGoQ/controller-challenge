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

    # 3. ----------- ANALIZAR -----------

    valores_validos = [cpu_por_instancia[i] for i in instancias_validas if i in cpu_por_instancia]

    if not valores_validos:
        record.update({
            "decision": "MAINTAIN_CAPACITY",
            "justificacion": "Instancias validas sin datapoints de CPU disponibles aun.",
            "accion_solicitada": "NONE",
            "resultado_accion": "N/A",
        })
        log_decision(record)
        return state

    cpu_promedio_flota = sum(valores_validos) / len(valores_validos)
    sm.push_cpu_datapoint(state, cpu_promedio_flota, config.HISTORY_WINDOW_SIZE)

    ultimas_n_subida = sm.last_n(state, config.SCALE_UP_CONSECUTIVE_EVALS)
    ultimas_n_bajada = sm.last_n(state, config.SCALE_DOWN_CONSECUTIVE_EVALS)

    record["cpu_promedio_flota"] = round(cpu_promedio_flota, 2)
    record["ventana_evaluada_subida"] = ultimas_n_subida
    record["ventana_evaluada_bajada"] = ultimas_n_bajada

    condicion_sobrecarga = (
        len(ultimas_n_subida) == config.SCALE_UP_CONSECUTIVE_EVALS
        and all(dp["valor"] > config.CPU_UPPER_THRESHOLD for dp in ultimas_n_subida)
        and capacidad_actual < config.MAX_INSTANCES
    )
    condicion_subutilizacion = (
        len(ultimas_n_bajada) == config.SCALE_DOWN_CONSECUTIVE_EVALS
        and all(dp["valor"] < config.CPU_LOWER_THRESHOLD for dp in ultimas_n_bajada)
        and capacidad_actual > config.MIN_INSTANCES
    )


    # 4. ------------ DECIDIR (respetando cooldown) ------------

    en_cooldown, restante = sm.is_in_cooldown(state, config.COOLDOWN_SECONDS)

    if en_cooldown:
        decision = "MAINTAIN_CAPACITY"
        justificacion = (
            f"En cooldown desde la ultima accion ({state['ultima_accion_tipo']}); "
            f"faltan {int(restante)}s para poder volver a actuar."
        )
    elif condicion_sobrecarga:
        decision = "INCREASE_CAPACITY"
        justificacion = (
            f"CPU promedio de la flota > {config.CPU_UPPER_THRESHOLD}% en "
            f"{config.SCALE_UP_CONSECUTIVE_EVALS} evaluaciones consecutivas: "
            f"{[dp['valor'] for dp in ultimas_n_subida]}"
        )
    elif condicion_subutilizacion:
        decision = "REDUCE_CAPACITY"
        justificacion = (
            f"CPU promedio de la flota < {config.CPU_LOWER_THRESHOLD}% sostenida en "
            f"{config.SCALE_DOWN_CONSECUTIVE_EVALS} evaluaciones consecutivas: "
            f"{[dp['valor'] for dp in ultimas_n_bajada]}"
        )
    else:
        decision = "MAINTAIN_CAPACITY"
        justificacion = (
            f"CPU promedio actual ({round(cpu_promedio_flota, 2)}%) dentro de la "
            f"zona muerta [{config.CPU_LOWER_THRESHOLD}, {config.CPU_UPPER_THRESHOLD}] "
            f"o la condicion aun no es sostenida en la ventana requerida."
        )

    record["decision"] = decision
    record["justificacion"] = justificacion

    # 5. ------------- ACTUAR -------------

    if decision == "MAINTAIN_CAPACITY":
        record["accion_solicitada"] = "NONE"
        record["resultado_accion"] = "N/A"
        log_decision(record)
        return state

    try:
        if decision == "INCREASE_CAPACITY":
            record["accion_solicitada"] = "run_instances + register_targets"
            nueva_instancia_id = aws.launch_instance()
            aws.register_target(nueva_instancia_id)
            sm.add_instance_to_warmup(state, nueva_instancia_id)
            record["resultado_accion"] = f"OK - instancia lanzada: {nueva_instancia_id}"

        elif decision == "REDUCE_CAPACITY":
            candidata = min(
                ((i, cpu_por_instancia[i]) for i in instancias_validas if i in cpu_por_instancia),
                key=lambda par: par[1],
            )[0]

            record["accion_solicitada"] = f"deregister_targets + terminate_instances ({candidata})"
            aws.deregister_target(candidata)
            aws.wait_for_drain(candidata, config.TARGET_DEREGISTRATION_DRAIN_SECONDS)
            aws.terminate_instance(candidata)
            record["resultado_accion"] = f"OK - instancia terminada: {candidata}"

        sm.register_action(state, decision)
        state["fallos_consecutivos_accion"] = 0

    except Exception as e:
        state["fallos_consecutivos_accion"] += 1
        logger.error("Fallo al ejecutar la accion %s: %s", decision, e)
        record["resultado_accion"] = f"FALLO: {e}"
        record["justificacion"] += (
            " | La decision se tomo pero la ejecucion fallo; no se activa "
            "cooldown para permitir reintento en el proximo ciclo."
        )
        # Deliberadamente NO se llama a sm.register_action(): si la accion
        # fallo, no queremos entrar en cooldown como si hubiera tenido efecto.

        if state["fallos_consecutivos_accion"] >= config.MAX_CONSECUTIVE_ACTION_FAILURES:
            logger.critical(
                "%d fallos consecutivos ejecutando acciones. Revisar permisos "
                "IAM, cuotas de la cuenta o estado del Target Group.",
                state["fallos_consecutivos_accion"],
            )

    log_decision(record)
    return state


def main():
    setup_logging()
    logger.info("Auto-Scaling Controller iniciado.")
    logger.info(
        "Politica: CPU_UPPER=%.1f%% CPU_LOWER=%.1f%% cooldown=%ds warmup_grace=%ds "
        "min=%d max=%d",
        config.CPU_UPPER_THRESHOLD, config.CPU_LOWER_THRESHOLD,
        config.COOLDOWN_SECONDS, config.WARMUP_GRACE_SECONDS,
        config.MIN_INSTANCES, config.MAX_INSTANCES,
    )

    state = sm.load_state(config.STATE_FILE_PATH)

    while True:
        inicio_ciclo = time.time()
        try:
            state = run_cycle(state)
        except Exception as e:
            # ultima red de seguridad: un error no previsto en el ciclo
            # jamas debe matar el proceso del controlador
            logger.exception("Error no manejado en el ciclo: %s", e)
        finally:
            sm.save_state(config.STATE_FILE_PATH, state)

        duracion = time.time() - inicio_ciclo
        espera = max(0, config.EVALUATION_INTERVAL_SECONDS - duracion)
        time.sleep(espera)


if __name__ == "__main__":
    main()
