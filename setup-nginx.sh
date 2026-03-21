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

    location / {
        try_files $uri $uri/ =404;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/onecp /etc/nginx/sites-enabled/onecp
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx || systemctl start nginx
