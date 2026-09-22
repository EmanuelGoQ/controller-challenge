import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("autoscaling-controller")

DEFAULT_STATE = {
    "ultima_accion_timestamp": None,
    "ultima_accion_tipo": None,
    "instancias_en_warmup": {},        # instance_id -> iso timestamp de lanzamiento
    "historial_cpu": [],               # lista de {"timestamp": iso, "valor": float}
    "fallos_consecutivos_observacion": 0,
    "fallos_consecutivos_accion": 0,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_state(path):
    """Carga el estado desde disco. Si no existe o esta corrupto, arranca limpio
    (comportamiento seguro: un estado vacio equivale a 'sin cooldown activo,
    sin instancias en warm-up conocidas', lo cual el siguiente ciclo de
    observacion en vivo corrige de todas formas)."""
    if not os.path.exists(path):
        logger.warning("No existe archivo de estado en %s, se crea uno nuevo.", path)
        return dict(DEFAULT_STATE)

    try:
        with open(path, "r") as f:
            data = json.load(f)
        # aseguramos que todas las claves esperadas existan, por si el
        # archivo es de una version anterior del controlador
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Estado corrupto o ilegible (%s). Se reinicia el estado.", e)
        return dict(DEFAULT_STATE)


def save_state(path, state):
    """Guarda el estado de forma atomica (escribe a un archivo temporal y
    renombra) para evitar dejar el archivo corrupto si el proceso muere
    justo durante la escritura."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, path)
    except OSError as e:
        logger.error("No se pudo guardar el estado en %s: %s", path, e)


def is_in_cooldown(state, cooldown_seconds):
    ts = state.get("ultima_accion_timestamp")
    if ts is None:
        return False, 0
    ultima = datetime.fromisoformat(ts)
    ahora = datetime.now(timezone.utc)
    transcurrido = (ahora - ultima).total_seconds()
    restante = max(0, cooldown_seconds - transcurrido)
    return restante > 0, restante


def register_action(state, accion):
    state["ultima_accion_timestamp"] = _now_iso()
    state["ultima_accion_tipo"] = accion


def add_instance_to_warmup(state, instance_id):
    state["instancias_en_warmup"][instance_id] = _now_iso()


def remove_instance_from_warmup(state, instance_id):
    state["instancias_en_warmup"].pop(instance_id, None)


def is_instance_warm(state, instance_id, is_healthy, warmup_grace_seconds):
    """Una instancia se considera 'caliente' (su CPU es confiable para
    decisiones) cuando el ALB la marca healthy Y ademas paso el periodo de
    gracia adicional desde que fue registrada como en warm-up.

    IMPORTANTE: si la instancia no esta siendo trackeada como 'nueva' (no
    aparece en instancias_en_warmup), NO se asume automaticamente que es
    valida: se exige que este healthy en el ALB en este mismo momento. La
    version anterior devolvia True incondicionalmente en ese caso, bajo el
    supuesto de que una instancia 'desconocida' siempre es una instancia
    pre-existente ya sana. Ese supuesto es falso cuando una instancia queda
    huerfana por un fallo parcial durante INCREASE_CAPACITY (lanzada pero
    nunca registrada en el Target Group): sin este chequeo, esa instancia
    huerfana se cuela en el analisis de CPU y puede incluso terminar
    seleccionada como candidata de REDUCE_CAPACITY, arriesgando la
    terminacion de una instancia sana en su lugar."""
    launched_at = state["instancias_en_warmup"].get(instance_id)
    if launched_at is None:
        # no la conocemos como "nueva" en warm-up, pero de todas formas debe
        # estar realmente healthy en el ALB ahora mismo para ser valida
        return is_healthy

    if not is_healthy:
        return False

    transcurrido = (datetime.now(timezone.utc) - datetime.fromisoformat(launched_at)).total_seconds()
    return transcurrido >= warmup_grace_seconds


def push_cpu_datapoint(state, valor, window_size):
    state["historial_cpu"].append({"timestamp": _now_iso(), "valor": valor})
    state["historial_cpu"] = state["historial_cpu"][-window_size:]


def last_n(state, n):
    return state["historial_cpu"][-n:]
