# 在 seres-ai.com/panguverse 上调试 Web 控制台

> 现状：开发调试入口，**不是生产部署**。链路一断，页面就打不开。

## 1. 拓扑

```
浏览器
  │ https://seres-ai.com/panguverse/
  ▼
公网 8.137.118.223 (frps)
  │ frp 隧道
  ▼
Mac mini 192.168.31.3  →  nginx listen 8888 (proxy_protocol)
  │  location /panguverse/  →  proxy_pass http://127.0.0.1:7788/
  ▼
Mac mini 127.0.0.1:7788  ← 反向 SSH 隧道（由 MacBook 发起）
  │
  ▼
MacBook 127.0.0.1:7788  →  python -m panguverse serve
```

关键点：Mac mini **无法直连** MacBook 的 7788（同网段也不通，实测 ping/22/7788 全不通），所以用反向隧道，而不是内网直连。

## 2. 启动（按顺序）

### 2.1 MacBook：跑控制台

```bash
cd ~/PycharmProjects/PanguVerse
source .venv/bin/activate
nohup python -m panguverse serve --host 127.0.0.1 --port 7788 \
  --workspace ~/.panguverse-dev --no-open > /tmp/pv-serve.log 2>&1 &
```

### 2.2 MacBook：建反向隧道

```bash
ssh -N -f -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -R 127.0.0.1:7788:127.0.0.1:7788 codinglog-deploy
```

### 2.3 Mac mini：重载 nginx（需要 root）

```bash
sudo nginx -t && sudo nginx -s reload
```

配置片段在 `nginx/server/https.conf` 的 `location /panguverse/`（Mac mini 上为 `/Users/mac/coding-log-web/nginx/server/https.conf`，被 `/usr/local/etc/nginx/nginx.conf` 的 include 覆盖）。

## 3. 验证

```bash
# Mac mini 上：隧道是否通
curl -s --noproxy '*' -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7788/api/status

# 公网
curl -s -o /dev/null -w '%{http_code}\n' https://seres-ai.com/panguverse/
curl -s https://seres-ai.com/panguverse/api/status
```

## 4. 停止

```bash
pkill -f 'ssh -N -f.*7788'                  # MacBook
pkill -f 'panguverse serve'                 # MacBook
```

## 5. 已知限制

- **子路径**：前端所有资源与接口都用相对路径，`/panguverse/` 必须带尾斜杠；`/panguverse` 由 nginx 301 补斜杠。
- **隧道不持久**：MacBook 休眠/断网后需重跑 2.2；没有做 launchd 常驻。
- **单用户**：`serve` 无认证，当前只经 nginx 暴露在 `/panguverse/` 下；不要把它当成公网服务。
- **工作区**：dev 用 `~/.panguverse-dev`，与仓库的 `.panguverse/` 隔离，避免污染源码目录。
