#!/bin/bash
set -e

if ! command -v nginx &>/dev/null; then
  apt-get update && apt-get install -y nginx
fi

cat > /etc/nginx/sites-available/onecp <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
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
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 10M;
    }

    location /uploads/ {
        alias /onecp/uploads/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        try_files $uri $uri/ =404;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/onecp /etc/nginx/sites-enabled/onecp
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx || systemctl start nginx

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
