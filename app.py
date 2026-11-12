#!/usr/bin/env python3
"""render-ds-agent: DeepSeek 驱动的工作 agent，部署在 Render 免费服务上。

复用本地 ds-agent (~/workspace/ds-agent/agent.py) 的同一套 agent 循环：
模型 deepseek-flash，8 个工具
web_search / fetch_url / weather / calculate /
write_file / read_file / list_files / run_python，
最多 12 轮，工作目录锁在 app.py 旁边的 work/ 里，
run_python 只用 subprocess 跑 Python、60 秒超时，不开其他 shell。

密钥：只从环境变量 DEEPSEEK_API_KEY 读取，绝不写进代码或仓库，
也不读本地任何 key 文件。在 Render Dashboard → Environment 里设置。

接口：
  GET  /health   {"status": "ok", "model": "deepseek-flash", "key_configured": true/false}
  GET  /         服务说明
  POST /run      {"task": "..."} -> {"result": "...", "model": "deepseek-flash"}
                 未设置 DEEPSEEK_API_KEY 时返回 503，不调用 DeepSeek。
"""
import ast, datetime, html as htmlmod, json, math, operator, os, re, subprocess, sys
import urllib.parse, urllib.request

from flask import Flask, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "work")
API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

os.makedirs(WORK, exist_ok=True)

app = Flask(__name__)

# ---------------- tools（与本地 ds-agent 同一套实现） ----------------
def tool_web_search(query):
    q = urllib.parse.quote(query)
    results = []
    try:
        req = urllib.request.Request(f"https://www.bing.com/search?q={q}", headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "ignore")
        for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S)[:5]:
            tm = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            if tm:
                clean = lambda s: htmlmod.unescape(re.sub(r"<[^>]+>", "", s)).strip()
                results.append({"title": clean(tm.group(2)), "url": tm.group(1),
                                "snippet": clean(sm.group(1))[:300] if sm else ""})
    except Exception:
        pass
    if not results:
        try:
            req = urllib.request.Request(f"https://lite.duckduckgo.com/lite/?q={q}", headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                page = r.read().decode("utf-8", "ignore")
            links = re.findall(r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
            snips = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', page, re.S)
            clean = lambda s: htmlmod.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            for i, (u, t) in enumerate(links[:5]):
                if "uddg=" in u:
                    u = urllib.parse.unquote(u.split("uddg=")[1].split("&")[0])
                results.append({"title": clean(t), "url": u,
                                "snippet": clean(snips[i])[:300] if i < len(snips) else ""})
        except Exception:
            pass
    return results or "没有搜到结果"

def tool_fetch_url(url):
    if not re.match(r"https?://", url):
        return "只支持 http/https 链接"
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost",) or re.match(r"(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host):
        return "不打开内网地址"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(600_000).decode("utf-8", "ignore")
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:4000] or "页面没有可读文本"

WMO = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾", 51: "毛毛雨",
       53: "毛毛雨", 55: "毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨",
       67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "雪", 80: "阵雨",
       81: "阵雨", 82: "暴雨", 85: "阵雪", 86: "阵雪", 95: "雷阵雨", 96: "冰雹雷雨", 99: "冰雹雷雨"}

def tool_weather(city):
    g = json.loads(urllib.request.urlopen(
        "https://geocoding-api.open-meteo.com/v1/search?" +
        urllib.parse.urlencode({"name": city, "count": 1, "language": "zh"}), timeout=15).read().decode())
    if not g.get("results"):
        return f"找不到城市：{city}"
    loc = g["results"][0]
    f = json.loads(urllib.request.urlopen(
        "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "timezone": "Asia/Shanghai", "forecast_days": 3}), timeout=15).read().decode())
    cur = f["current"]
    out = [f"{loc['name']} 现在 {WMO.get(cur['weather_code'], '')}，{cur['temperature_2m']}°C，风速 {cur['wind_speed_10m']} km/h"]
    d = f["daily"]
    for i in range(len(d["time"])):
        out.append(f"{d['time'][i]} {WMO.get(d['weather_code'][i], '')} {d['temperature_2m_min'][i]}~{d['temperature_2m_max'][i]}°C")
    return "\n".join(out)

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "log": math.log, "log10": math.log10, "exp": math.exp, "abs": abs,
          "round": round, "floor": math.floor, "ceil": math.ceil}

def tool_calculate(expression):
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name) and node.id in ("pi", "e"):
            return math.pi if node.id == "pi" else math.e
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("不支持的表达式")
    result = ev(ast.parse(expression, mode="eval"))
    return f"{expression} = {result}"

def _safe_path(name):
    p = os.path.realpath(os.path.join(WORK, name))
    if not (p == WORK or p.startswith(WORK + os.sep)):
        raise ValueError("路径超出工作目录")
    return p

def tool_write_file(name, content):
    p = _safe_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {name}（{len(content)} 字）"

def tool_read_file(name):
    p = _safe_path(name)
    if not os.path.exists(p):
        return f"文件不存在：{name}"
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()[:6000]

def tool_list_files():
    out = []
    for root, _, files in os.walk(WORK):
        for fn in files:
            out.append(os.path.relpath(os.path.join(root, fn), WORK))
    return "\n".join(sorted(out)) or "工作目录是空的"

def tool_run_python(code):
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=WORK, capture_output=True,
                           text=True, timeout=60)
        out = (r.stdout or "")[-3000:]
        err = (r.stderr or "")[-1500:]
        return (out + ("\n[stderr] " + err if err else "")).strip() or "（无输出）"
    except subprocess.TimeoutExpired:
        return "运行超时（60 秒）"

TOOL_FUNCS = {"web_search": tool_web_search, "fetch_url": tool_fetch_url,
              "weather": tool_weather, "calculate": tool_calculate,
              "write_file": tool_write_file, "read_file": tool_read_file,
              "list_files": tool_list_files, "run_python": tool_run_python}

def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

TOOL_SCHEMAS = [
    _fn("web_search", "联网搜索最新信息。需要实时或不确定的信息时先用它查，不要编造。",
        {"query": {"type": "string", "description": "搜索关键词"}}, ["query"]),
    _fn("fetch_url", "打开一个网页链接，读取正文内容。",
        {"url": {"type": "string", "description": "完整 URL"}}, ["url"]),
    _fn("weather", "查询某个城市现在的天气和未来三天预报。",
        {"city": {"type": "string", "description": "城市名，如 中山"}}, ["city"]),
    _fn("calculate", "精确计算数学表达式，支持 + - * / // % ** 和 sqrt/sin/cos/log 等函数。涉及算数必须用它，不要心算。",
        {"expression": {"type": "string", "description": "如 12345*6789"}}, ["expression"]),
    _fn("write_file", "把文本内容写入工作目录里的文件（会覆盖同名文件）。交付成果、整理好的资料都写成文件。",
        {"name": {"type": "string", "description": "文件名，如 report.md"},
         "content": {"type": "string", "description": "文件全文"}}, ["name", "content"]),
    _fn("read_file", "读取工作目录里已有的文件内容。",
        {"name": {"type": "string", "description": "文件名"}}, ["name"]),
    _fn("list_files", "列出工作目录里现有的所有文件。", {}, []),
    _fn("run_python", "在工作目录里运行一段 Python 代码并返回输出。适合数据处理、格式转换、批量计算；只能访问工作目录。",
        {"code": {"type": "string", "description": "Python 代码"}}, ["code"]),
]

SYSTEM = """你是一个工作 agent，部署在 Render 免费服务上，由吉伊（Muse）管理、代 Luo 干活。
规矩：
1. 接到任务后自己规划步骤，用工具把活干完，最后用简体中文总结：做了什么、产出文件叫什么、关键结论是什么。
2. 涉及数字、价格、日期、事实：必须用搜索或计算工具核实，注明出处；查不到就明说查不到，不许编造。
3. 交付成果写成文件放在工作目录里（write_file），不要只在对话里说。
4. 比较东西时用同一套标准；算总数时算全（总价、后续成本）。
5. 干不了的部分如实说，不硬撑。"""

def call_deepseek(key, messages):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOL_SCHEMAS,
                       "max_tokens": 8000}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

def run_task(task, key):
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": task}]
    final = ""
    for round_ in range(12):
        resp = call_deepseek(key, messages)
        msg = resp["choices"][0]["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            final = msg.get("content") or ""
            break
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
                result = TOOL_FUNCS[fn](**args)
            except Exception as e:
                result = f"工具出错：{e}"
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": str(result)[:7000]})
    else:
        final = "（达到最大轮数，任务未完全结束）"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(os.path.join(BASE, "tasks.log"), "a", encoding="utf-8") as f:
            f.write(f"\n=== {ts} ===\nTASK: {task}\nRESULT: {final}\n")
    except Exception:
        pass
    return final

def _key_configured():
    return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "render-ds-agent",
        "model": MODEL,
        "key_configured": _key_configured(),
        "endpoints": {"health": "GET /health", "run": "POST /run {\"task\": \"...\"}"},
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": MODEL, "key_configured": _key_configured()})

@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(silent=True) or {}
    task = (data.get("task") or "").strip()
    if not task:
        return jsonify({"error": "缺少 task 字段：请 POST {\"task\": \"任务描述\"}"}), 400
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return jsonify({"error": "DEEPSEEK_API_KEY 未设置：请在 Render Dashboard → Environment 里添加 DEEPSEEK_API_KEY，不要把密钥写进代码或仓库。"}), 503
    try:
        result = run_task(task, key)
    except Exception as e:
        return jsonify({"error": f"任务执行出错：{e}"}), 500
    return jsonify({"result": result, "model": MODEL})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
