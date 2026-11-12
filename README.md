# render-ds-agent — DeepSeek harness（Render 免费版）

把本地 ds-agent（`~/workspace/ds-agent/agent.py`）改成能在 Render 免费服务上跑的
Flask 服务。同一套东西：模型 `deepseek-flash`，8 个工具
`web_search / fetch_url / weather / calculate / write_file / read_file / list_files / run_python`，
最多 12 轮，工作目录锁在服务自己的 `work/` 里，`run_python` 只用 subprocess
跑 Python、60 秒超时，不开其他 shell。

## 接口

- `GET /health` → `{"status": "ok", "model": "deepseek-flash", "key_configured": true/false}`
- `GET /` → 服务说明
- `POST /run`，body `{"task": "任务描述"}` → `{"result": "...", "model": "deepseek-flash"}`

```bash
curl https://你的服务.onrender.com/health
curl -X POST https://你的服务.onrender.com/run \
  -H "Content-Type: application/json" \
  -d '{"task": "查一下中山未来三天天气，写成 work/ 下的 markdown 文件"}'
```

## 部署（3 步，不用信用卡）

1. **推代码到 GitHub**：新建一个仓库，把本目录的 `app.py`、`requirements.txt`、
   `render.yaml`、`work/.gitkeep` 推上去。不要把密钥放进仓库。
2. **在 Render 建服务**：打开 [render.com](https://render.com)，用 GitHub
   （或 Google / 邮箱）注册登录 → New → Web Service → 选刚才的仓库。
   Render 会读到 `render.yaml`，自动用免费计划（Free）、Python 环境，
   构建命令 `pip install -r requirements.txt`，
   启动命令 `gunicorn app:app --bind 0.0.0.0:$PORT`。点 Create Web Service，
   等部署完成，拿到 `https://ds-agent-xxxx.onrender.com` 这样的地址。
3. **设置密钥**：在 Render Dashboard 打开这个服务 → Environment →
   Add Environment Variable，Key 填 `DEEPSEEK_API_KEY`，Value 填你的
   DeepSeek API key，保存。服务会自动重新部署。

## 密钥安全（重要）

- 密钥**只**从环境变量 `DEEPSEEK_API_KEY` 读取。代码里没有任何密钥，
  也不读本地任何 key 文件。
- 没设置 `DEEPSEEK_API_KEY` 时，`POST /run` 直接返回 503 和中文提示，
  不会调用 DeepSeek。
- 永远不要把密钥写进代码、写进 `render.yaml`、提交到 GitHub，也不要
  发在聊天记录里。

## 免费计划的限制（先知道再用）

- **15 分钟没人访问就休眠**：免费 Web 服务 15 分钟无流量自动睡，
  下次访问要等约 1 分钟唤醒，第一次请求可能超时。不是一直在线的。
  想让它不睡：定时（比如每 10 分钟）访问一次 `/health` 即可，
  也可以接受“用的时候等一分钟”。
- **配置小**：免费实例 512MB 内存 / 0.1 CPU，每月 750 实例小时、
  每月 5GB 流量。超了会暂停到下月，不扣费（没绑卡就扣不了）。
- **文件不持久**：`work/` 里写的文件和 `tasks.log` 存在实例的临时盘上，
  服务重启、重新部署就没了。重要成果要及时下载 / 复制走，
  不要只存在 Render 上。
- **没有后台 worker / 定时任务**：免费版只能跑 Web Service，不能跑
  后台常驻任务和 cron。定时提醒要靠外部定时访问 `/health` 或
  `/run` 来触发。
- **请求会超时**：一个任务最多 12 轮、每轮 DeepSeek 最长等 180 秒，
  大任务可能超过 HTTP 超时。任务尽量拆小，一次只做一件事。

## 本地测试（不调用 DeepSeek）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 不设置 DEEPSEEK_API_KEY，直接启动：
python app.py
curl http://127.0.0.1:5000/health
# 应返回 {"status":"ok",...,"key_configured":false}
curl -X POST http://127.0.0.1:5000/run \
  -H "Content-Type: application/json" -d '{"task": "你好"}'
# 应返回 503 和“DEEPSEEK_API_KEY 未设置”的提示，不会调用 DeepSeek
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `app.py` | Flask 服务 + agent 循环 + 8 个工具 |
| `requirements.txt` | Flask + gunicorn |
| `render.yaml` | Render 免费 Web Service 定义 |
| `work/.gitkeep` | 让 `work/` 目录能进仓库（运行时自动创建） |
| `README.md` | 本文件 |
