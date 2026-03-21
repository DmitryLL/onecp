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
