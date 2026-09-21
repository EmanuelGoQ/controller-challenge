import os
from dotenv import load_dotenv

load_dotenv()

# ------------------- Recursos de AWS (se completan en el archivo .env, ver .env.example) -------------------

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TARGET_GROUP_ARN = os.environ["TARGET_GROUP_ARN"]
LAUNCH_TEMPLATE_ID = os.environ["LAUNCH_TEMPLATE_ID"]
INSTANCE_TAG_KEY = os.environ.get("INSTANCE_TAG_KEY", "Role")
INSTANCE_TAG_VALUE = os.environ.get("INSTANCE_TAG_VALUE", "webserver")
SUBNET_ID = os.environ["SUBNET_ID"]
SECURITY_GROUP_ID = os.environ["SECURITY_GROUP_ID"]

# --------------------- Constraints del reto (Seccion 7 - Scope and Constraints) -----------------------

MIN_INSTANCES = 1
MAX_INSTANCES = 5

# --------------------- Ciclo de evaluacion -----------------------

EVALUATION_INTERVAL_SECONDS = 60      # cada cuanto se ejecuta el ciclo completo
METRIC_PERIOD_SECONDS = 60            # granularidad de los datapoints de CloudWatch
METRIC_LOOKBACK_MINUTES = 6           # cuanta historia se trae en cada consulta

# --------------------- Politica de decision (umbrales + histeresis) -----------------------

CPU_UPPER_THRESHOLD = 70.0            # % promedio -> candidato a scale-up
CPU_LOWER_THRESHOLD = 30.0            # % promedio -> candidato a scale-down

SCALE_UP_CONSECUTIVE_EVALS = 2        # ~2 min sostenidos para subir
SCALE_DOWN_CONSECUTIVE_EVALS = 5      # ~5 min sostenidos para bajar

HISTORY_WINDOW_SIZE = 10              # cuantos datapoints se guardan en memoria/disco

# -------------------- Anti-oscilacion y disponibilidad -----------------------

COOLDOWN_SECONDS = 300                 # 5 min de silencio tras cualquier accion
WARMUP_GRACE_SECONDS = 120             # 2 min extra tras healthy antes de confiar en CPU
TARGET_DEREGISTRATION_DRAIN_SECONDS = 60  # espera de drenaje antes de terminate

# --------------------- Resiliencia -------------------------------

MAX_CONSECUTIVE_OBSERVATION_FAILURES = 3
MAX_CONSECUTIVE_ACTION_FAILURES = 3
AWS_API_RETRY_ATTEMPTS = 3
AWS_API_RETRY_BACKOFF_SECONDS = 5

# --------------------- Rutas locales -----------------------------

STATE_FILE_PATH = os.environ.get("STATE_FILE_PATH", "./state.json")
LOG_DIR = os.environ.get("LOG_DIR", "./logs")
DECISIONS_LOG_FILE = os.path.join(LOG_DIR, "decisions.log")
CONTROLLER_LOG_FILE = os.path.join(LOG_DIR, "controller.log")
