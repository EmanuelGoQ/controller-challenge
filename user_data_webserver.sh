#!/bin/bash
# user_data_webserver.sh
#
# Script de arranque (user-data) para las instancias web administradas por
# el controlador. Instala nginx y publica:
#   - "/"        pagina simple con instance-id y AZ (verificacion visual)
#   - "/health/" endpoint liviano para el health check del ALB
#   - "/stress/" endpoint que realiza trabajo CPU-bound real (hashing
#                repetido), usado durante el experimento de carga para
#                poder estresar la CPU de forma controlada y repetible.
#
# Justificacion del endpoint /stress/: servir contenido estatico simple
# (un archivo HTML de pocos bytes) es computacionalmente casi gratuito
# para nginx, por lo que generar suficiente concurrencia para saturar la
# CPU de una instancia real (incluso t2.micro) requiere volumenes de
# trafico poco practicos de generar desde un cliente de carga corriendo
# en un entorno de laboratorio (limites de red del generador, WSL2, etc).
# El endpoint /stress/ permite controlar la intensidad de la carga por
# request (parametro "n"), haciendo el experimento reproducible sin
# depender de la capacidad del generador de carga.
#
# Pensado para Amazon Linux 2023 (AMI por defecto en AWS Academy Learner Lab).

dnf update -y
dnf install -y nginx python3

TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
AZ=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/availability-zone)

# ---------------------------------------------------------------------
# Pagina principal (verificacion visual de distribucion de trafico)
# ---------------------------------------------------------------------
mkdir -p /usr/share/nginx/html
cat > /usr/share/nginx/html/index.html << HTML
<!DOCTYPE html>
<html>
<head><title>Auto-Scaling Demo</title></head>
<body style="font-family: sans-serif; text-align: center; margin-top: 10%;">
  <h1>Servidor web activo</h1>
  <p>Instance ID: <strong>${INSTANCE_ID}</strong></p>
  <p>Availability Zone: <strong>${AZ}</strong></p>
  <p>Hora de arranque: <strong>$(date -u)</strong></p>
</body>
</html>
HTML

mkdir -p /usr/share/nginx/html/health
echo "OK" > /usr/share/nginx/html/health/index.html

# -------- Endpoint de estres de CPU (servidor Python minimalista en 127.0.0.1:8080) -------

cat > /usr/local/bin/stress_server.py << 'PYEOF'
#!/usr/bin/env python3
"""
Endpoint minimalista que realiza trabajo CPU-bound real por cada request
(hashing SHA-256 repetido), usado unicamente para el experimento de carga
del Auto-Scaling Controller. La intensidad se controla con el parametro
de query "n" (numero de iteraciones de hash), por ejemplo:
    GET /stress/?n=100000
No representa logica de negocio real; existe solo para poder generar
consumo de CPU controlado y reproducible durante las pruebas.
"""
import http.server
import hashlib
from urllib.parse import urlparse, parse_qs

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        try:
            n = int(qs.get("n", ["50000"])[0])
        except ValueError:
            n = 50000
        n = max(1, min(n, 5_000_000))  # limite de seguridad

        data = b"autoscaling-controller-cpu-stress"
        for _ in range(n):
            data = hashlib.sha256(data).digest()

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")

    def log_message(self, format, *args):
        pass  # silenciar logs de acceso (evitar I/O extra durante la prueba)

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8080), Handler)
    server.serve_forever()
PYEOF

chmod +x /usr/local/bin/stress_server.py

cat > /etc/systemd/system/stress-endpoint.service << 'UNITEOF'
[Unit]
Description=Endpoint de estres de CPU para pruebas de auto-scaling
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/stress_server.py
Restart=always
User=nginx

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable stress-endpoint
systemctl start stress-endpoint

# ---------------------------------------------------------------------
# Configuracion de nginx: se sobrescribe nginx.conf con una version
# minima y propia (evita conflictos de "server duplicado" con el
# server-block por defecto que trae el paquete), y se define el server
# real en conf.d/webserver.conf con las tres rutas: /, /health/, /stress/.
# ---------------------------------------------------------------------
cat > /etc/nginx/nginx.conf << 'NGINXCONF'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    sendfile        on;
    keepalive_timeout  65;
    include /etc/nginx/conf.d/*.conf;
}
NGINXCONF

mkdir -p /etc/nginx/conf.d
cat > /etc/nginx/conf.d/webserver.conf << 'SERVERCONF'
server {
    listen 80 default_server;
    server_name _;

    location / {
        root /usr/share/nginx/html;
        index index.html;
    }

    location /health/ {
        root /usr/share/nginx/html;
    }

    location /stress/ {
        proxy_pass http://127.0.0.1:8080/;
    }
}
SERVERCONF

nginx -t
systemctl enable nginx
systemctl restart nginx
