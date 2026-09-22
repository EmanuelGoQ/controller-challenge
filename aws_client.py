import time
import logging
import boto3
from botocore.exceptions import ClientError, BotoCoreError, WaiterError

import config

logger = logging.getLogger("autoscaling-controller")

ec2 = boto3.client("ec2", region_name=config.AWS_REGION)
cloudwatch = boto3.client("cloudwatch", region_name=config.AWS_REGION)
elbv2 = boto3.client("elbv2", region_name=config.AWS_REGION)


def _with_retries(func, *args, **kwargs):
    """Ejecuta una llamada a AWS con reintentos y backoff fijo para errores
    transitorios. Errores de permisos o de parametros invalidos no se
    reintentan (no tiene sentido: fallaran siempre igual)."""
    last_exception = None
    for intento in range(1, config.AWS_API_RETRY_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except ClientError as e:
            codigo = e.response.get("Error", {}).get("Code", "")
            if codigo in ("Throttling", "RequestLimitExceeded", "InternalError"):
                logger.warning(
                    "Error transitorio de AWS (%s), intento %d/%d",
                    codigo, intento, config.AWS_API_RETRY_ATTEMPTS,
                )
                last_exception = e
                time.sleep(config.AWS_API_RETRY_BACKOFF_SECONDS * intento)
                continue
            # error no transitorio (permisos, parametros, etc.): no reintentar
            raise
        except BotoCoreError as e:
            logger.warning(
                "Error de red/BotoCore, intento %d/%d: %s",
                intento, config.AWS_API_RETRY_ATTEMPTS, e,
            )
            last_exception = e
            time.sleep(config.AWS_API_RETRY_BACKOFF_SECONDS * intento)

    raise last_exception


# ---------------------------------------------------------------------------
# OBSERVAR
# ---------------------------------------------------------------------------

def describe_managed_instances():
    """Retorna la lista de instancias administradas por el controlador
    (identificadas por tag), en estado running o pending."""
    response = _with_retries(
        ec2.describe_instances,
        Filters=[
            {"Name": f"tag:{config.INSTANCE_TAG_KEY}", "Values": [config.INSTANCE_TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["running", "pending"]},
        ],
    )
    instancias = []
    for reservation in response["Reservations"]:
        for inst in reservation["Instances"]:
            instancias.append({
                "instance_id": inst["InstanceId"],
                "state": inst["State"]["Name"],
                "launch_time": inst["LaunchTime"],
            })
    return instancias


def get_target_health():
    """Retorna dict {instance_id: 'healthy'|'unhealthy'|'initial'|'draining'|'unused'}."""
    response = _with_retries(
        elbv2.describe_target_health,
        TargetGroupArn=config.TARGET_GROUP_ARN,
    )
    salud = {}
    for desc in response["TargetHealthDescriptions"]:
        instance_id = desc["Target"]["Id"]
        salud[instance_id] = desc["TargetHealth"]["State"]
    return salud


def get_cpu_utilization_per_instance(instance_ids, lookback_minutes, period_seconds):
    """Retorna dict {instance_id: promedio_cpu} usando el ultimo datapoint
    disponible en la ventana de lookback. Si una instancia no tiene datos
    aun (por ejemplo, acaba de lanzarse), simplemente no aparece en el dict."""
    if not instance_ids:
        return {}

    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=lookback_minutes)

    queries = []
    for idx, instance_id in enumerate(instance_ids):
        queries.append({
            "Id": f"cpu_{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                },
                "Period": period_seconds,
                "Stat": "Average",
            },
            "ReturnData": True,
        })

    response = _with_retries(
        cloudwatch.get_metric_data,
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
    )

    resultado = {}
    for idx, instance_id in enumerate(instance_ids):
        serie = next((r for r in response["MetricDataResults"] if r["Id"] == f"cpu_{idx}"), None)
        if serie and serie["Values"]:
            resultado[instance_id] = serie["Values"][0]  # datapoint mas reciente
    return resultado


def get_alb_target_response_time(lookback_minutes, period_seconds):
    """Metrica complementaria de calidad de servicio (no dispara decisiones
    en la version actual, pero se registra en cada log de decision para
    poder correlacionar CPU con latencia real percibida por el usuario)."""
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=lookback_minutes)

    tg_arn_suffix = "/".join(config.TARGET_GROUP_ARN.split(":")[-1].split("/")[-3:])

    try:
        response = _with_retries(
            cloudwatch.get_metric_data,
            MetricDataQueries=[{
                "Id": "latencia",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/ApplicationELB",
                        "MetricName": "TargetResponseTime",
                        "Dimensions": [{"Name": "TargetGroup", "Value": tg_arn_suffix}],
                    },
                    "Period": period_seconds,
                    "Stat": "Average",
                },
                "ReturnData": True,
            }],
            StartTime=start,
            EndTime=end,
        )
        valores = response["MetricDataResults"][0]["Values"]
        return valores[0] if valores else None
    except Exception as e:
        # metrica secundaria: si falla, no se detiene el ciclo (a diferencia
        # de CPU, que es obligatoria para decidir)
        logger.warning("No se pudo obtener TargetResponseTime: %s", e)
        return None


# ---------------------------------------------------------------------------
# ACTUAR
# ---------------------------------------------------------------------------

def launch_instance():
    """Lanza una nueva instancia a partir del Launch Template configurado y
    espera activamente a que EC2 reporte su estado como 'running' antes de
    retornar. Esto es necesario porque la API de Target Groups (ELBv2)
    rechaza con 'InvalidTarget' el registro de una instancia que todavia
    esta en estado 'pending' (run_instances retorna casi de inmediato, pero
    el arranque real de la VM toma varios segundos mas). Sin esta espera,
    register_target() puede fallar por una condicion de carrera, dejando
    una instancia 'huerfana': lanzada, facturando, pero nunca incorporada
    al Target Group ni al seguimiento de warm-up del controlador."""
    response = _with_retries(
        ec2.run_instances,
        LaunchTemplate={"LaunchTemplateId": config.LAUNCH_TEMPLATE_ID},
        MinCount=1,
        MaxCount=1,
        SubnetId=config.SUBNET_ID,
        SecurityGroupIds=[config.SECURITY_GROUP_ID],
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [
                {"Key": config.INSTANCE_TAG_KEY, "Value": config.INSTANCE_TAG_VALUE},
                {"Key": "LaunchedBy", "Value": "autoscaling-controller"},
            ],
        }],
    )
    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("Instancia lanzada: %s, esperando estado 'running'...", instance_id)

    try:
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 5, "MaxAttempts": 24},  # hasta ~2 min
        )
        logger.info("Instancia %s confirmada como 'running'.", instance_id)
    except WaiterError as e:
        # Si la instancia no llega a 'running' a tiempo, propagamos el error
        # para que el ciclo lo trate como fallo de accion (mismo camino que
        # cualquier otro fallo de ACTUAR: no se activa cooldown y se
        # reintenta en el proximo ciclo). No intentamos registrar un target
        # que sabemos que sera rechazado.
        logger.error("Instancia %s no alcanzo 'running' a tiempo: %s", instance_id, e)
        raise

    return instance_id


def register_target(instance_id):
    _with_retries(
        elbv2.register_targets,
        TargetGroupArn=config.TARGET_GROUP_ARN,
        Targets=[{"Id": instance_id, "Port": 80}],
    )


def deregister_target(instance_id):
    _with_retries(
        elbv2.deregister_targets,
        TargetGroupArn=config.TARGET_GROUP_ARN,
        Targets=[{"Id": instance_id, "Port": 80}],
    )


def wait_for_drain(instance_id, timeout_seconds):
    """Espera a que el ALB termine de drenar conexiones activas hacia la
    instancia antes de terminarla (evita cortar requests en curso).
    Ver Seccion 8, pregunta 'como determina el controlador que remover
    capacidad es seguro'."""
    import time as _time
    inicio = _time.time()
    while _time.time() - inicio < timeout_seconds:
        salud = get_target_health()
        estado = salud.get(instance_id)
        if estado in (None, "unused", "draining") and estado != "draining":
            return True
        if estado == "draining":
            _time.sleep(5)
            continue
        return True
    logger.warning(
        "Timeout esperando drenaje de %s, se procede a terminar de todas formas.",
        instance_id,
    )
    return False


def terminate_instance(instance_id):
    _with_retries(ec2.terminate_instances, InstanceIds=[instance_id])
    logger.info("Instancia terminada: %s", instance_id)
