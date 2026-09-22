# Auto-Scaling Controller — SI3016 Cloud Computing

Controlador de auto-escalado horizontal construido desde cero (sin usar
Auto Scaling Groups administrados por AWS) para el Challenge Based Learning
No. 1. El controlador observa métricas de Amazon CloudWatch, decide con
lógica propia (umbrales + histéresis + ventanas de observación + cooldown)
y actúa directamente sobre EC2 y el Target Group de un Application Load
Balancer.
---

## 0. Nota sobre IAM en AWS Academy Learner Lab

En un Learner Lab **no puedes crear roles ni políticas IAM propias**: la
consola de IAM está bloqueada y solo existe un rol predefinido, normalmente
llamado `LabRole` (con su instance profile asociado, `LabInstanceProfile`).

Esto significa que el archivo `docs/iam_policy_least_privilege.json` de
este proyecto **no se puede aplicar literalmente** en el Learner Lab.

El requisito de mínimo privilegio (Sección 7, punto 9) se diseñó como la
política adjunta en `docs/iam_policy_least_privilege.json`. Sin embargo,
el entorno de AWS Academy Learner Lab no permite crear roles ni políticas
IAM personalizadas; todas las instancias deben usar el rol predefinido
`LabRole`/`LabInstanceProfile`, que tiene permisos más amplios que los
estrictamente necesarios. Esta es una limitación del entorno de
laboratorio, no una decisión de diseño del controlador.

---

## 1. Requisitos previos

- Cuenta de AWS Academy Learner Lab activa ("Start Lab").
- Un par de llaves SSH (puedes crear uno nuevo desde la consola EC2 →
  Key Pairs, o usar uno existente).
- AWS CLI v2 instalado en tu máquina local (opcional pero recomendado para
  verificar recursos sin entrar a la consola todo el tiempo).
- Herramienta de generación de carga: `ab` (Apache Bench) o `hey`.

---

## 2. Red y seguridad

El Learner Lab ya trae una VPC por defecto (`default VPC`) con subredes en
varias zonas de disponibilidad. La usaremos tal cual, no hace falta crear
una VPC nueva.

### 2.1 Identifica la VPC y subredes por defecto

Consola → **VPC** → "Your VPCs": anota el `VPC ID` marcado como default.
Luego en "Subnets", anota al menos **dos subredes en AZs distintas**
(el ALB exige mínimo 2 AZs).

### 2.2 Crea los Security Groups

Consola → **EC2 → Security Groups → Create security group**. Crea tres:

**SG-ALB** (para el Load Balancer)
- Inbound: HTTP (80) desde `0.0.0.0/0`
- Outbound: todo permitido (default)

**SG-Webserver** (para las instancias nginx)
- Inbound: HTTP (80) **solo desde SG-ALB** (selecciona el security group
  como origen, no un CIDR)
- Inbound: SSH (22) desde tu IP (`My IP`), solo para depuración manual
- Outbound: todo permitido

**SG-Controller** (para la instancia que corre el controlador)
- Inbound: SSH (22) desde tu IP
- Outbound: todo permitido (necesita salir a las APIs de AWS)

---

## 3. Target Group y Load Balancer

### 3.1 Crear el Target Group

Consola → **EC2 → Target Groups → Create target group**
- Tipo: Instances
- Protocolo: HTTP, puerto 80
- VPC: la default
- Health check path: `/health/` (coincide con el endpoint que publica
  `user_data_webserver.sh`)
- Healthy threshold: 2, Unhealthy threshold: 2, Interval: 15s (así el ALB
  detecta instancias sanas/caídas rápido, útil para la demo)
- **No registrar instancias todavía** (el controlador y tú las irán
  registrando manualmente/automáticamente)

Anota el **ARN del Target Group** — lo necesitarás en `.env`.

### 3.2 Crear el Application Load Balancer

Consola → **EC2 → Load Balancers → Create Load Balancer → Application
Load Balancer**
- Scheme: Internet-facing
- VPC: la default, selecciona las 2+ subredes en distintas AZs
- Security group: `SG-ALB`
- Listener: HTTP:80 → forward al Target Group creado en 3.1

Anota el **DNS name** del ALB (algo como
`webservers-alb-123456789.us-east-1.elb.amazonaws.com`): es la URL contra
la que harás las pruebas de carga.

---

## 4. Launch Template para las instancias web

Consola → **EC2 → Launch Templates → Create launch template**
- Nombre: `webserver-template`
- AMI: Amazon Linux 2023 (la más reciente disponible)
- Instance type: `t2.micro` (dentro del free tier / límites del Learner Lab)
- Key pair: el que creaste en el paso 1
- Network settings: no fijes subred aquí (el controlador la especifica al
  lanzar), pero sí selecciona `SG-Webserver` como security group
- **IAM instance profile**: `LabInstanceProfile`
- User data: pega el contenido completo de `user_data_webserver.sh`
- Tags (en "Tag specifications", Resource type = Instances):
  `Role = webserver`

Guarda la plantilla y anota su **Launch Template ID** (`lt-xxxxxxxx`).

---

## 5. Instancia inicial (capacidad mínima)

El reto exige operar entre 1 y 5 instancias, nunca 0. Lanza manualmente
**una primera instancia** desde el Launch Template:

Consola → **EC2 → Launch an instance from template** → selecciona
`webserver-template` → especifica una de las subredes que usa el ALB →
Launch.

Cuando esté `running`, regístrala manualmente en el Target Group:
**Target Groups → tu target group → Register targets** → selecciona la
instancia, puerto 80 → Include as pending below → Register.

Esta es tu capacidad inicial (`MIN_INSTANCES = 1`). A partir de aquí, el
controlador se encarga de las siguientes.

---

## 6. Instancia controladora

Lanza **otra** instancia EC2, separada de las web servers:

- AMI: Amazon Linux 2023
- Instance type: `t2.micro`
- Security group: `SG-Controller`
- IAM instance profile: `LabInstanceProfile`
- Sin user-data especial (la configuras manualmente por SSH)

Conéctate por SSH:

```bash
ssh -i tu-llave.pem ec2-user@<IP-publica-del-controlador>
```

Instala dependencias:

```bash
sudo dnf update -y
sudo dnf install -y python3 python3-pip git
```

Sube el proyecto (puedes usar `scp` desde tu máquina local, o `git clone`
si lo subiste a un repositorio):

```bash
scp -i tu-llave.pem -r autoscaling-controller ec2-user@<IP-publica-del-controlador>:/home/ec2-user/
```

Instala las dependencias de Python:

```bash
cd /home/ec2-user/autoscaling-controller
pip3 install -r requirements.txt --user
```

Configura las variables de entorno:

```bash
cp .env.example .env
nano .env
```

Completa con los valores reales que anotaste en los pasos 2–4:
`TARGET_GROUP_ARN`, `LAUNCH_TEMPLATE_ID`, `SUBNET_ID`, `SECURITY_GROUP_ID`
(usa el ID de `SG-Webserver`, **no** el de `SG-Controller`), y la región
donde trabajas (usualmente `us-east-1` en el Learner Lab).

> **Sobre credenciales**: como la instancia usa `LabInstanceProfile`,
> boto3 obtiene credenciales automáticamente desde el metadata service de
> la instancia (IAM Instance Profile) — **no necesitas poner
> `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`** en ningún archivo. Esto es
> justamente lo que exige el requisito 10.2 del reto ("Credenciales no
> deben quedar en el repositorio").

---

## 7. Prueba manual antes de dejarlo corriendo

Nunca actives el servicio en bucle sin antes verificar que un solo ciclo
funciona correctamente:

```bash
python3 run_once.py
```

Revisa la salida en consola y el archivo `logs/decisions.log`. Si ves un
`FALLO` relacionado con permisos, revisa que `LabInstanceProfile` esté
efectivamente adjunto a la instancia (Consola EC2 → tu instancia →
Security → IAM Role).

Si ves `MAINTAIN_CAPACITY` con la instancia inicial healthy, vas bien.
Ejecuta `run_once.py` un par de veces más (espaciadas ~1 min) para
verificar que el historial de CPU se va acumulando en `state.json`.

---

## 8. Dejar el controlador corriendo como servicio

```bash
sudo cp systemd/autoscaling-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable autoscaling-controller
sudo systemctl start autoscaling-controller
```

Verifica que esté vivo y observando en bucle:

```bash
sudo systemctl status autoscaling-controller
tail -f logs/controller.log
tail -f logs/decisions.log
```

Déjalo correr varios minutos sin carga: deberías ver ciclos consecutivos
con `"decision": "MAINTAIN_CAPACITY"` mientras la CPU esté baja (nginx sin
tráfico apenas consume CPU).

---

## 9. Experimento: generar carga y observar el escalado

### 9.0 Por qué se usa el endpoint `/stress/` y no la página estática `/`

Servir un archivo HTML de ~380 bytes con nginx es una operación casi
gratuita para la CPU. En la práctica, generar suficiente concurrencia
desde un cliente de carga corriendo en un entorno de laboratorio (con las
limitaciones de red de WSL2, NAT doméstica, etc.) para saturar la CPU de
una instancia real contra ese contenido resulta poco práctico: se
necesitan volúmenes de tráfico que exceden lo que un solo proceso `ab`
puede generar de forma confiable.

Por eso, `user_data_webserver.sh` expone un segundo endpoint,
`/stress/?n=<iteraciones>`, que ejecuta trabajo CPU-bound real (hashing
SHA-256 repetido) por cada request. Esto permite controlar con precisión
cuánta CPU consume cada request, haciendo el experimento reproducible sin
depender de la capacidad del generador de carga. Es una carga sintética
—no representa lógica de negocio real— pero es adecuada para el objetivo
del reto: validar que la **lógica de decisión del controlador** reacciona
correctamente ante variaciones de CPU, que es justamente lo que se está
evaluando.

Instala `ab` si no lo tienes:

```bash
sudo apt install apache2-utils   # Debian/Ubuntu/WSL
# o
brew install httpd               # macOS
```

Prueba base (confirmar que el endpoint de estrés responde):

```bash
curl "http://<DNS-del-ALB>/stress/?n=50000"
```

Debe responder `OK` tras una pequeña demora perceptible (si `n` es alto,
notarás el retraso — esa es la señal de que sí está consumiendo CPU real).

### 9.1 Escenario de carga creciente

```bash
ab -k -n 200000 -c 150 "http://<DNS-del-ALB>/stress/?n=80000"
```

Ajusta el valor de `n` (iteraciones de hash por request) según lo que
observes: si la CPU sube demasiado rápido o demasiado lento respecto a lo
esperado, sube o baja `n` y repite. Con `n` bien calibrado, ~150
conexiones concurrentes sostenidas deberían llevar la CPU de una
`t2.micro` por encima del 70% en pocos minutos, ya que ahora cada request
realiza trabajo real, no solo I/O.

Mientras corre, en la instancia controladora observa en vivo:

```bash
tail -f logs/decisions.log
```

Deberías ver la CPU promedio subir, y tras ~2 evaluaciones consecutivas
sobre el umbral (70%), una decisión `INCREASE_CAPACITY`. Verifica en la
consola de EC2 que efectivamente aparece una nueva instancia, y en el
Target Group que entra como `initial` y luego `healthy`.

> **Nota**: si tras varios minutos la CPU se estabiliza en una meseta por
> debajo del umbral (por ejemplo ~35-40%) en vez de seguir subiendo con
> más concurrencia, es señal de que el propio generador de carga (`ab`,
> limitado a un solo hilo, o la capa de red de WSL2) se saturó primero.
> En ese caso, sube el valor de `n` en vez de subir la concurrencia `-c`
> — así cada request individual consume más CPU sin necesitar más
> conexiones simultáneas.

### 9.2 Escenario de caída de carga

Detén `ab` (Ctrl+C) y deja el sistema en reposo. Tras el cooldown y luego
~5 evaluaciones consecutivas con CPU baja, deberías ver `REDUCE_CAPACITY`
y cómo la instancia se desregistra (aparece `draining` en el Target Group)
antes de terminarse.

### 9.3 Escenario de pico transitorio (para validar anti-oscilación)

```bash
ab -k -n 5000 -c 100 "http://<DNS-del-ALB>/stress/?n=80000"
```

Una ráfaga corta (que dure menos que el tiempo de 2 evaluaciones
consecutivas, es decir, menos de ~2 minutos) no debería alcanzar la
ventana requerida para disparar `INCREASE_CAPACITY`. Esto es evidencia
directa para responder la pregunta 3 de la Sección 12 del reto ("¿cómo
distingue el controlador una variación transitoria de un cambio
sostenido?").

Guarda copias de `logs/decisions.log` de cada escenario — es tu evidencia
experimental (Sección 10.3 del reto).

---

## 10. Limpieza (importante en Learner Lab)

Al terminar cada sesión de pruebas, o al finalizar el reto, libera
recursos para no agotar el presupuesto del laboratorio:

1. Detén el servicio: `sudo systemctl stop autoscaling-controller`
2. Termina todas las instancias EC2 (web servers + controlador)
3. Elimina el Load Balancer
4. Elimina el Target Group
5. Elimina el Launch Template (opcional, no genera costo pero es buena
   práctica)
6. Elimina los Security Groups si ya no los vas a reutilizar

---

## 11. Estructura del proyecto

```
autoscaling-controller/
├── controller.py               # ciclo principal observe->analyze->decide->act
├── config.py                   # umbrales, ventanas, cooldown, recursos AWS
├── state_manager.py            # persistencia de estado entre ciclos
├── aws_client.py                # wrappers de boto3 con reintentos
├── logger_setup.py             # logging técnico + log estructurado de decisiones
├── run_once.py                 # ejecuta un solo ciclo (para pruebas manuales)
├── user_data_webserver.sh      # script de arranque de las instancias nginx
├── requirements.txt
├── .env.example                # plantilla de configuración (sin secretos)
├── systemd/
│   └── autoscaling-controller.service
├── docs/
│   └── iam_policy_least_privilege.json   # política IAM de referencia
└── logs/                       # controller.log y decisions.log (generados en runtime)
```
