#!/bin/bash
set -e

DOMAIN="order.coffeeplace.one"

# Install nginx if missing
if ! command -v nginx &>/dev/null; then
  apt-get update && apt-get install -y nginx
fi

# Install certbot if missing
if ! command -v certbot &>/dev/null; then
  apt-get update && apt-get install -y certbot python3-certbot-nginx
fi

# --- Step 1: HTTP config (needed for certbot to verify domain) ---
cat > /etc/nginx/sites-available/onecp <<NGINX
# Rate limiting zones
limit_req_zone \$binary_remote_addr zone=api_general:10m rate=30r/s;
limit_req_zone \$binary_remote_addr zone=api_auth:10m rate=5r/m;
limit_req_zone \$binary_remote_addr zone=api_admin:10m rate=10r/s;

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    root /onecp;
    index index.html;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;

    # Gzip compression
    gzip on;
    gzip_vary on;
    gzip_min_length 256;
    gzip_types text/plain text/css text/javascript application/javascript application/json image/svg+xml;

    # Cache static files
    location ~* \.(css|js|jpg|jpeg|png|gif|webp|svg|ico|woff2?)$ {
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # Auth endpoints — strict rate limit (5 req/min)
    location /api/auth/ {
        limit_req zone=api_auth burst=3 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        client_max_body_size 1M;
    }

    # Admin endpoints — moderate rate limit
    location /api/admin/ {
        limit_req zone=api_admin burst=20 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        client_max_body_size 10M;
    }

    # General API — rate limit
    location /api/ {
        limit_req zone=api_general burst=50 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        client_max_body_size 10M;
    }

    location /uploads/ {
        alias /onecp/uploads/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location /login {
        try_files \$uri /login/index.html;
    }

    # Admin panel routes
    location ~ ^/(orders|questions|products|sets|calendar|clients|locations|settings)(/|$) {
        try_files \$uri /admin/index.html;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/onecp /etc/nginx/sites-enabled/onecp
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx || systemctl start nginx

# --- Step 2: Obtain SSL certificate (if not already present) ---
if [ ! -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
  certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos --email admin@coffeeplace.one --redirect
else
  # Certificate exists — make sure nginx config has SSL (certbot --nginx updates it)
  certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos --email admin@coffeeplace.one --redirect --keep-until-expiring
fi

# --- Step 3: Auto-renewal via systemd timer (1st of each month) ---
cat > /etc/systemd/system/certbot-renew.service <<'UNIT'
[Unit]
Description=Certbot renewal
[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --quiet --deploy-hook "systemctl reload nginx"
UNIT

cat > /etc/systemd/system/certbot-renew.timer <<'TIMER'
[Unit]
Description=Run certbot renewal monthly
[Timer]
OnCalendar=*-*-01 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now certbot-renew.timer

# Verify final config
nginx -t && systemctl reload nginx

echo "SSL certificate configured for ${DOMAIN} with auto-renewal."

# Nginx log rotation (keep 7 days, max 50MB each)
cat > /etc/logrotate.d/nginx-onecp <<'LOGROTATE'
/var/log/nginx/*.log {
    daily
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    maxsize 50M
    postrotate
        [ -f /var/run/nginx.pid ] && kill -USR1 $(cat /var/run/nginx.pid) 2>/dev/null || true
    endscript
}
LOGROTATE
