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
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    root /onecp;
    index index.html;

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

    location /api/ {
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

# --- Step 3: Auto-renewal cron (certbot installs a systemd timer, but add cron as fallback) ---
CRON_CMD="0 3 * * * certbot renew --quiet --deploy-hook 'systemctl reload nginx'"
( crontab -l 2>/dev/null | grep -v 'certbot renew' ; echo "${CRON_CMD}" ) | crontab -

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
