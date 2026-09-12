#!/usr/bin/env bash
# 一键启动：后端 8000 + 前端 5173
set -e
cd "$(dirname "$0")"

echo "==> 生成种子数据（可重复执行）"
(cd backend && python3 seed.py) || true

echo "==> 启动后端 http://localhost:8000"
(cd backend && nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/mt_backend.log 2>&1 &)

echo "==> 启动前端 http://localhost:5173"
(cd frontend && nohup npm run dev > /tmp/mt_frontend.log 2>&1 &)

# 手机 / 其他机器访问必须走 HTTPS，否则浏览器不暴露 navigator.mediaDevices，
# 摄像头与麦克风静默不可用（getUserMedia 只能在安全上下文调用）。
# 首次使用先生成自签证书（IP 写进 SAN，否则 Chrome 仍判不可信）：
#   mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes \
#     -keyout certs/key.pem -out certs/cert.pem -days 365 \
#     -subj "/CN=<你的IP>" -addext "subjectAltName=IP:<你的IP>,DNS:localhost"
if [ -f certs/key.pem ] && [ -f certs/cert.pem ]; then
  echo "==> 启动 HTTPS（手机访问）https://<本机IP>:8443"
  (cd backend && nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8443 \
    --ssl-keyfile ../certs/key.pem --ssl-certfile ../certs/cert.pem \
    > /tmp/mt_https.log 2>&1 &)
else
  echo "==> 跳过 HTTPS（无 certs/key.pem；手机将无法使用摄像头与麦克风）"
fi

sleep 4
echo
echo "后端日志: tail -f /tmp/mt_backend.log"
echo "前端日志: tail -f /tmp/mt_frontend.log"
echo "HTTPS日志: tail -f /tmp/mt_https.log"
echo "打开 http://localhost:5173"
