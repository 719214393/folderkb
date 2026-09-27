# ============================================================
# RAG 后端服务（学习项目）：扫描 → 分块 → 向量化 → 检索 → 注入
# 跑在本机（8001 端口），替浏览器页面干"碰磁盘/打外部 API"的活。
# 启动: python server.py
# ============================================================


import sys
sys.modules.setdefault("server", sys.modules["__main__"])  # 2026-09-23 advisory blocker 修
# 版本守卫（2026-09-27 rapidocr 迁移时加）：项目锁定的依赖链
# （rapidocr 3.9.2 / onnxruntime 1.30.0 / rapid-table）官方只发布
# Python 3.12–3.14 的安装包，区间外 pip 装不上或起不来——提前用中文
# 把原因和出路讲清，别让用户对着一屏 traceback 猜
if not (3, 12) <= sys.version_info[:2] <= (3, 14):
    _v = ".".join(map(str, sys.version_info[:2]))
    print(f"【无法启动】本项目需要 Python 3.12 – 3.14，当前是 {_v}。")
    print("原因：OCR 依赖链（rapidocr / onnxruntime / rapid-table）按 3.12–3.14")
    print("锁定版本，区间外的 Python 装不上或跑不起来。")
    print("出路（任选其一）：")
    print("  1. 用 uv（推荐，自动下载匹配的 Python，无需手动装版本）：")
    print("       uv venv && uv pip install -r requirements.txt")
    print("       启动：.venv\\Scripts\\python.exe server.py（直调 venv 内解释器）")
    print("  2. 手动安装 Python 3.12–3.14：https://www.python.org/downloads/")
    sys.exit(1)
from pathlib import Path
import logging
import logging.handlers
import contextvars
import threading
import shutil  # 四十三修顺带：872/955/1911 行 LibreOffice 路径用全局 shutil（Win 下必炸 NameError），顶层补导入
import json as _json
import time as _time
import uuid as _uuid
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


app = FastAPI()
# ══════════════════════════════════════════════════════════════
# ⑫0 日志基建（2026-09-18 开工）：JSON 结构化 + trace_id + contextvars 自动注入
# 设计依据：日志系列实施清单-升级版.md（业界标准四项：JSON 行/trace_id 对齐
# OpenTelemetry/四级分级/自动注入）+ 查证：日志查询手册.md
#
# 铁律落实：完整记录（轮转归档不删）+ 全链路留痕（每步带 trace_id）+ SMB 落盘
#
# 落盘：logs/ 目录（SMB 主写。本地禁跑铁律（2026-09-19 用户严令）：本地不落
# 任何数据文件——SMB 断连=日志停（可接受），恢复后自动续写，无本地兜底）
import os  # 顶部前置：frozen 日志目录用 os.environ——必须在日志段之前（原 L231 上移）

# 日志目录：dev 模式 = 代码旁 logs/（SMB 主写，本地禁跑铁律）；
# frozen（EXE 打包）= 用户数据目录（安装目录可能只读——Program Files 不可写）
if getattr(sys, 'frozen', False):
    _LOG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "RAGAssistant" / "logs"
else:
    _LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# trace_id 上下文（contextvars：同请求内所有日志自动带 ID，业务代码不手写）
_current_trace_id = contextvars.ContextVar("trace_id", default="-")

def get_trace_id() -> str:
    """业务代码/线程穿透取当前 trace_id 用"""
    return _current_trace_id.get()

class _JsonFormatter(logging.Formatter):
    """⑫0：JSON 结构化——每行一个 JSON（ts/level/trace_id/module/msg + 业务字段）。
    查询按字段过滤（Get-Content|ConvertFrom-Json|Where），不再纯文本 grep"""
    def format(self, record):
        base = {
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(record.created)),
            "level": record.levelname,
            "trace_id": getattr(record, "trace_id", None) or _current_trace_id.get(),
            "module": record.name,
            "msg": record.getMessage(),
        }
        # 业务字段：log 调用方通过 extra 传的键自动并入 JSON（白名单外的内置键排除）
        _builtin = {"name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                    "created", "msecs", "relativeCreated", "thread", "threadName",
                    "processName", "process", "asctime", "taskName", "trace_id"}
        for k, v in record.__dict__.items():
            if k not in _builtin and not k.startswith("_"):
                try:
                    _json.dumps(v)  # 可序列化才收
                    base[k] = v
                except (TypeError, ValueError):
                    base[k] = str(v)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return _json.dumps(base, ensure_ascii=False)

class _TraceIdFilter(logging.Filter):
    """⑫0：trace_id 自动注入——每条日志从 contextvars 读当前请求的 ID 附加。
    业务代码 log.info(...) 不用手写 ID（手工传总会忘，自动注入 100% 无漏）"""
    def filter(self, record):
        record.trace_id = _current_trace_id.get()
        return True

def _make_logger(name: str, filename: str) -> logging.Logger:
    """⑫0：按文件建 logger（六文件分流：api/parse/task/search/sql/chat）。
    轮转：10MB×20 份归档不删（完整记录铁律——轮转旧文件保留）"""
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.addFilter(_TraceIdFilter())
    try:
        fh = logging.handlers.RotatingFileHandler(
            _LOG_DIR / filename, maxBytes=10 * 1024 * 1024,
            backupCount=20, encoding="utf-8")
    except OSError:
        # 本地禁跑铁律（2026-09-19）：SMB 断连不再写本地兜底——
        # 该 logger 挂 NullHandler（日志停但不崩），SMB 恢复后重启进程续写
        fh = logging.NullHandler()
    fh.setFormatter(_JsonFormatter())
    lg.addHandler(fh)
    return lg

# 六文件分流（⑫6 提前随基建建好骨架，各业务项往里写内容）
_lg_api = _make_logger("api", "api.log")        # ⑫1：每请求摘要
_lg_parse = _make_logger("parse", "parse.log")  # ⑫2：解析明细
_lg_task = _make_logger("task", "task.log")     # ⑫3：后台任务
_lg_search = _make_logger("search", "search.log")  # ⑫4：检索链路
_lg_sql = _make_logger("sql", "sql.log")        # ⑫5：SQL 查表路
_lg_chat = _make_logger("chat", "chat.log")     # ⑫5：生成与审计

def _spawn_traced(fn, *args, **kwargs):
    """⑫0 线程穿透：contextvars 不进裸 Thread——ctx=copy_context 再 ctx.run，
    trace_id 跟进后台线程。_run_chunk_job/_run_embed_job/embed worker 三入口
    全换本函数起线程（advisory 实锤：裸 Thread 丢 trace_id=异步失败断链，
    恰是最需要追的路径）。用法：t=_spawn_traced(fn); t.start()"""
    ctx = contextvars.copy_context()
    return threading.Thread(target=lambda: ctx.run(fn, *args, **kwargs), daemon=True)

# 坑3 修复（保留）：解析失败明因日志。旧 _pf 挂 JSON 格式+trace_id 注入
# （⑫2 完成后逐处迁移到 _lg_parse；过渡期 _pf 保持兼容）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("rag_parse.log", encoding="utf-8")],
)
_pf = logging.getLogger("parse_legacy")  # 坑3 旧通道（rag_parse.log）——改名防与 _lg_parse 同名串写

# ══════════════════════════════════════════════════════════════
# 可调参数索引（2026-09-14 建）。参数按功能模块就近放置（改哪个
# 模块就在哪看参数），这里只做总目录——维护时先查此表再跳行号。
# 行号会随代码增删漂移，以参数名为准全文搜索定位。
# ══════════════════════════════════════════════════════════════
# 【扫描】
#   TEXT_EXTS（L28 附近）      文件格式白名单——不在这里的后缀扫描直接跳过
#   SCAN_WORKERS（L52 附近）   扫描线程数 8=实测甜点，更多让 SSD 随机读排队
# 【切块】
#   CHUNK_SIZE（L288 附近）        每块目标字数（调大=块更粗更少，调小=更碎更多；调后⑨评测基线漂移需重跑）
#   CHUNK_OVERLAP（L289 附近）     相邻块重叠字数（防句子被切断丢上下文）
#   CHUNK_BATCH_SIZE（L287 附近）  分块写库缓冲批量（内存/写盘次数平衡）
# 【解析缓存】
#   PARSE_CODE_VERSION（L2273 附近）  解析逻辑版本号——改任何 extract_* 行为必须 +1，否则旧缓存命中旧结果
# 【OCR（PDF 扫描件/图片表格）】
#   OCR_FALLBACK_MIN_CHARS（L1424 附近）  每页 RapidOCR 低于此字数→升级视觉模型兜底
# 【向量化】（配置区 L2507 附近，换服务商只改这里）
#   EMBED_URL / EMBED_MODEL   硅基流动 API 地址 / bge-m3 模型（换模型=已入库向量全部作废需重拍）
#   EMBED_API_KEY             留空=BYOK 前端传 key；自用裸跑临时填回
#   EMBED_BATCH = 32          每批拍指纹块数（服务商限载，硬塞报 413）
#   EMBED_WORKERS = 8         并发工人（函数内 L2620 附近；压测 8 路 282 块/s 零 429，12 路贴限流墙）
#   _slim 截断预算            超长块拍指纹截断（函数内；纯中文 6000 字/40% 数字压 3000——数字串 tokenizer 切得碎）
#   重试退避                  429: 3s/6s/9s；503: 10s/20s/40s（embed_batch 内）
# 【检索】（常量区 L3106 附近，改参数只看一处）
#   RERANK_POOL = 100    重排候选池宽度（五轮步3 60→100：45 FAIL 归因 6 题在 61-100 名可救；业界 Vertex/Azure 100 档。rerank +0.4s 免费）
#   TOP_K = 15           最终返回块数（三轮步1 从 10→15：池内排低题答案在 11-15 名可漏出）
MMR_ON = False      # 4-3 MMR 扩域版开关（2026-09-21 全量实测：净效果 -0.1pp≈0，默认关——功能保留，想启用改 True）（True=开：rerank 前30名多样性选15；False=纯分数前15）
MMR_LAMBDA = 0.7    # 分数/多样性平衡（0.7=分数为主轻度去重；1=纯分数=关闭效果）
#   RRF_K = 60           RRF 融合常数（业界默认 Elasticsearch 同款；第1/10名差距不悬殊）
#   RRF_WEIGHTS = [0.60, 0.40]  RRF 路级权重 [语义路, 关键词路]（三轮扫参定案：0.6:0.4 两批最优，原 0.45:0.55 方向拍反）
# 【前端 app.js 设置弹窗（存 localStorage，用户可改）】
#   chatUrl/chatKey 聊天模型地址+key（公司代理）｜vecUrl/vecKey 向量服务地址+key（硅基流动）
#   sameKey 联动=向量区跟聊天侧配置｜模型 combobox 选聊天模型｜RAG 开关=检索注入 vs 普通聊天
#
TEXT_EXTS = {".txt", ".md", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt"}  # ⑦5b 起收 .ppt（坑2 销号）
app.add_middleware(
    CORSMiddleware,
    # 局域网访问：IP 走动态正则放行，换机器/换网络不用改代码。
    # 任意本机网卡 IP 的 8080 页面均可调后端（localhost 行为不变）
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|(\d{1,3}\.){3}\d{1,3})(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════
# ⑫1 API 请求日志（2026-09-18）：trace_id 中间件 + 每请求一条 JSON
# 慢请求（>1000ms）升 WARN；4xx/5xx 升 ERROR 带完整异常栈（现状 uvicorn
# 只打一行 500 没堆栈查不了）。业务参数记关键项（search 的 q / scan 的
# path / chunk 的 scan_id）——api.log 是查证入口（日志查询手册场景一）
# ══════════════════════════════════════════════════════════════
@app.middleware("http")
async def _api_log_middleware(request: Request, call_next):
    # trace_id：前端带了用前端的（X-Request-Id 头），否则生成（OpenTelemetry 风格短 ID）
    tid = request.headers.get("x-request-id") or f"{_uuid.uuid4().hex[:12]}"
    _current_trace_id.set(tid)
    request.state.trace_id = tid  # 业务代码可从 request.state 拿
    t0 = _time.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception as e:
        # 5xx 未捕获异常：ERROR + 完整堆栈（uvicorn 只打一行，查不了——⑫1 治这个）
        import traceback as _tb
        _lg_api.error("请求异常", extra={
            "method": request.method, "path": request.url.path,
            "q": request.query_params.get("q", "")[:80],
            "exc_type": type(e).__name__, "exc": str(e)[:200],
            "stack": _tb.format_exc()[-1500:]},
            exc_info=False)
        raise
    finally:
        dur_ms = int((_time.time() - t0) * 1000)
        # 关键业务参数（查询手册场景一的过滤字段）
        extra = {
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": dur_ms,
            # 六十四修注记：这两行只覆盖 GET 查询串！POST（/api/agent/chat 等）参数在
            # JSON body——这里永远记空串，别当"没传"证据用（曾据此误判用户没传库）。
            # POST body 的 scan_id/mode 由端点「Agent执行器启动」日志记录（L5568）。
            # 中间件不读 body：Starlette 请求体是单次流，读了端点就读不到（会全体 400）。
            "q": request.query_params.get("q", "")[:80],        # search 的问题（GET only）
            "scan_id": request.query_params.get("scan_id", ""),  # chunk/embed 归属（GET only）
            "path_param": request.path_params.get("scan_id", "") if hasattr(request, "path_params") else "",
        }
        if status >= 400:
            _lg_api.error("请求失败", extra=extra)
        elif dur_ms > 1000:
            _lg_api.warning("慢请求", extra=extra)  # >1s 标 ⚠（手册场景二）
        else:
            _lg_api.info("请求", extra=extra)

# 只收白名单后缀的文件；黑名单目录绝不进去（系统地盘 + 项目垃圾堆）
# 注意：TEXT_EXTS 定义在上方 L28（唯一赋值——历史上曾在 ⑦3b 编辑时出现双定义，
# 旧赋值在后覆盖新赋值，导致 xlsx/xls/csv 静默失效，扫描器收不到表格文件）
SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", "$WinREAgent",
    "node_modules", ".git", "dist", "build", "__pycache__", "venv",
}

import os  # 顶部前置（frozen 日志目录与后续扫描逻辑共用；原 L231 的 import os 上移至此避免 NameError）
import concurrent.futures

# 并行度 8：磁盘 IO 型任务的实测甜点（线程更多会让 SSD 随机读排队打架）
SCAN_WORKERS = 8

import threading
import time
import re
import struct  # ⑦5b 手撕 .ppt 二进制记录树（MS-PPT 记录头解包）
_results_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════
# 步骤① 扫描层
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# 模块 1：文件扫描算法（8 线程并行）
# ══════════════════════════════════════════════════════════════

def scan_dir(root: Path, old_index=None):
    """多线程递归收集 root 下的文本文件，返回 [{path, size, mtime}] 列表。

    old_index = 上次扫描结果 {路径: (大小, 修改时间)}；传了它没变的文件抄旧账，
    不传（None）= 全新扫描。
    """
    results = []
    # 完成判定用"记账板"：任务会生任务（扫一个文件夹发现 N 个子文件夹），
    # 根任务做完不等于全做完。pending[0] 记录未完成任务数，归零才算收工。
    # 套一层列表是因为闭包里只有"改容器内容"对所有人可见，重新赋值不行。
    import threading as _th
    pending = [0]
    # 信号灯：主线程 wait() 睡到全部任务完成（pending 归零时 set()）
    all_done = _th.Event()

    def submit_walk(directory, prefix, buffer):
        # 开票：任务数 +1，任务单交给线程池（谁空谁领）
        pending[0] += 1
        _get_pool().submit(walk, directory, prefix, buffer)

    def walk_finished():
        # 销账：任务数 -1；归零则点灯。必须在 finally 里调（漏销账 = 死等，修过的 bug）
        with _results_lock:
            pending[0] -= 1
            if pending[0] == 0:
                all_done.set()

    def flush_buffer(buffer):
        # 工人的中转筐攒满后整体倒进 results：一次锁搬一批，比逐条搬快千倍
        if not buffer:
            return
        with _results_lock:
            results.extend(buffer)
        buffer.clear()
    def walk(directory, prefix, buffer):
        # 并行的核心：子文件夹不自己递归，而是开新任务单塞回池里，
        # 整棵目录树拆成千万张任务单由 8 个工人同时消化
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    name = entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if name in SKIP_DIRS:
                            continue
                        submit_walk(entry.path, prefix + name + "/", [])
                    elif entry.is_file(follow_symlinks=False):
                        # ~$ 开头是 Office/WPS 打开文档时的锁文件（隐藏临时文件，
                        # 162 字节左右无内容）——不是真文档，跳过（实测混入扫描清单）
                        if name.startswith("~$"):
                            continue
                        ext = os.path.splitext(name)[1].lower()
                        if ext not in TEXT_EXTS:
                            continue
                        rel = prefix + name
                        # size/mtime 是文件的"指纹"，下游分块/向量化靠 mtime
                        # 认出"哪些文件变过"，只对变化的重新处理（Windows 上
                        # stat 随目录列举免费附带，不构成瓶颈）
                        try:
                            st = entry.stat(follow_symlinks=False)
                            size, mtime = st.st_size, int(st.st_mtime)
                        except OSError:
                            size, mtime = 0, 0
                        if old_index and rel in old_index and old_index[rel] == (size, mtime):
                            # 大小+修改时间都没变 -> 内容没变。收集本身便宜照常收，
                            # 真正省活的是下游不用重算它
                            pass
                        buffer.append({"path": rel, "size": size, "mtime": mtime})
                        if len(buffer) >= 1000:
                            flush_buffer(buffer)
                # 这个文件夹过完：筐里零头也倒掉
                flush_buffer(buffer)
        except (PermissionError, OSError):
            # 整个文件夹读不了（没权限/磁盘坏）：放弃它，继续别的
            pass
        finally:
            walk_finished()

    submit_walk(root, "", [])
    all_done.wait()
    return results

_pool = None

def _get_pool():
    global _pool  # 给模块级 _pool 赋值需要声明 global
    if _pool is None:
        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS)
    return _pool


# ══════════════════════════════════════════════════════════════
# 模块 1b：扫描接口 POST /api/scan
# ══════════════════════════════════════════════════════════════

@app.post("/api/scan")
def api_scan(body: dict, request: Request = None):
    """扫描指定目录。body: {"path": "E:/docs"} 或 {"name": "docs"}（模糊找同名目录）"""
    global last_scan_root
    chat_key, chat_url = _chat_from_request(request)

    path = body.get("path", "").strip()
    name = body.get("name", "").strip()

    root = None
    if path:
        # 带完整路径：检查是不是真实存在的文件夹
        candidate = Path(path)
        if candidate.is_dir():
            root = candidate
    elif name:
        # 只报名字（历史遗留用法）：上次扫描的位置 + 各盘根逐个找同名目录
        import string
        candidates = []
        if last_scan_root:
            candidates.append(last_scan_root / name)
        for drive in string.ascii_uppercase:
            candidates.append(Path(f"{drive}:/") / name)
        for c in candidates:
            if c.is_dir():
                root = c
                break

    if root is None:
        return {"error": f"找不到目录: path={path or '-'}, name={name or '-'}, 请输入完整路径"}

    # 并发扫描保护（2026-09-13 500 根因修复）：前一轮分块还在跑时再发扫描，
    # 两个线程同时写 SQLite → database is locked → 500。上一轮没跑完直接拒绝
    # （前端已显示"切块中 X%"，用户等它完成再扫合理）
    with _chunk_jobs_lock:
        running = [sid for sid, j in _chunk_jobs.items() if j.get("status") == "running"]
    if running:
        return {"error": f"上一轮分块还在跑（scan#{running[0]}），等完成后再扫描"}

    # 增量模式：翻旧账——上次扫过同一根目录就捞出 {路径: (大小, mtime)} 字典
    old_scan = db_find_scan_by_root(root)
    old_index = None
    if old_scan:
        old_files = db_get_scan_files(old_scan["id"])
        old_index = {f["path"]: (f["size"], f["mtime"]) for f in old_files}

    import time
    t0 = time.perf_counter()  # 高精度秒表（墙钟会被对时调整，测耗时不可靠）
    files = scan_dir(root, old_index)
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    total_size = sum(f["size"] for f in files)
    last_scan_root = root
    scan_id = db_save_scan(root, files, elapsed_ms)
    # 扫完瞬间自动点火后台分块（和手动点"开始分块"同一条流水线）：
    # 分块只要 1 秒多，用户点"看切块"时早就跑完了；绝不在这里干等
    files_for_chunk = files
    with _db_lock:
        _db.execute("DELETE FROM chunks WHERE scan_id = ?", (scan_id,))
        _db.commit()  # 新扫描新账本，旧块挂不上（保险起见清场）
    with _chunk_jobs_lock:
        _chunk_jobs[scan_id] = {"done": 0, "total": len(files_for_chunk), "status": "running", "chunk_count": None}
    _spawn_traced(_run_chunk_job, scan_id, root, files_for_chunk, chat_key, chat_url).start()  # ⑫0 线程穿透（trace_id 进后台）+⑫3 任务日志在 _run_chunk_job 内

    return {
        "root": str(root),
        "count": len(files),
        "files": files,
        "elapsed_ms": elapsed_ms,
        "total_size": total_size,
        "incremental": bool(old_index),  # True = 这次抄了旧账
        "chunk_count": None,             # 分块后台跑完才有数，前端轮询拿
        "scan_id": scan_id,              # 拉块预览/触发分块向量化时的钥匙
    }


# ══════════════════════════════════════════════════════════════
# 模块 2：目录浏览 GET /api/browse
# ══════════════════════════════════════════════════════════════

import string


@app.get("/api/browse")
def api_browse(path: str = ""):
    """列目录。path 为空 → 列根目录（按平台：Windows=盘符 / macOS=常用根）；否则列子目录"""
    if not path:
        # —— 根目录层（2026-09-25 Mac 兼容）：按平台出根 ——
        entries = []
        if sys.platform == "darwin":
            # macOS 无盘符概念：文件系统从 / 挂载。列用户实际会用到的常用根
            # （advisor 实锤：/Documents 不是顶层真实目录，别列假根）：
            # 家目录、应用程序、外接盘（U盘/移动硬盘/NAS 挂载点全在这）
            _mac_roots = [
                (Path.home(), "🏠 " + Path.home().name + "（家目录）"),
                (Path("/Applications"), "📱 应用程序"),
                (Path("/Users"), "👥 所有用户"),
                (Path("/Volumes"), "💾 外接盘/网络盘"),
                (Path("/tmp"), "📄 临时文件"),
            ]
            for _p, _label in _mac_roots:
                if _p.is_dir():
                    entries.append({"name": _label, "path": str(_p)})
        else:
            # —— Windows：没有"给我盘符清单"的问法，A-Z 逐个问 ——
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:/")
                if drive.is_dir():
                    entries.append({"name": f"{letter}:/", "path": str(drive)})
        return {"current": "", "entries": entries}

    # —— 子目录层：只列文件夹（文件会干扰选择器视线）——
    root = Path(path)
    if not root.is_dir():
        return {"error": f"目录不存在: {path}"}

    entries = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    entries.append({
                        "name": entry.name,
                        "path": str(Path(entry.path)),
                        # . 开头（隐藏）或 $ 开头（系统地盘）→ 前端显示成灰色
                        "hidden": entry.name.startswith(".") or entry.name.startswith("$"),
                    })
    except (PermissionError, OSError):
        return {"error": f"无法访问: {path}"}

    # 名字排序（不区分大小写），列表看起来稳定
    entries.sort(key=lambda e: e["name"].lower())
    return {"current": str(root), "entries": entries}


# ══════════════════════════════════════════════════════════════
# 步骤② 解析分块
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# 模块 3a：分块算法核心（read / pour / chunk_text / build_chunks）
# ══════════════════════════════════════════════════════════════

# ── 冲刺① 表头翻译回填（2026-09-21）──────────────────────────
# 病灶：库内 8379 表格块中 5628 块表头是英文（Amount/Balance Date…），
# 用户用中文问（"期末余额""金额"）→ 语义/关键词两路都命不中英文表头
# （题270 实证：问"期末账面结余"，答案块表头 BALANCE B/FWD 全库仅 1 块）。
# 治法：分块时给英文表头块尾部追加中文对照注释行，检索三路（向量/FTS/
# rerank）都能吃到中文词。词典 header_dict.json 由 LLM 预翻 299 个全库
# 唯一英文表头（deepseek-v4-flash，ERP 财务术语习惯），换库重扫自动适配。
_HEADER_DICT = {}
try:
    _hd_path = Path(__file__).parent / "header_dict.json"
    if _hd_path.exists():
        with open(_hd_path, encoding="utf-8") as _hf:
            _HEADER_DICT = _json.load(_hf)
except Exception as _he:
    _HEADER_DICT = {}  # 词典缺失/损坏不炸分块——退化成无注释（原行为）
    print(f'[冲刺①] 词典加载失败: {type(_he).__name__}: {_he}', flush=True)
else:
    print(f'[冲刺①] 词典加载 {len(_HEADER_DICT)} 词', flush=True)

# ── 冲刺③ 查询词典（2026-09-21）──────────────────────────────
# 病根：MQ 的英文变体是 LLM 现代英语（ending book balance），
# 与库内行业写法（BALANCE B/FWD）语义距离远——变体翻得对不上库。
# 治法：中文术语→库内真实写法词典（query_dict.json，497 条，
# 源头=表头反向+库内语言对照表，判据词零交集已自证）——
# 查询时查表追加精确变体，纯本地零 LLM 成本。
_QUERY_DICT = {}
try:
    _qd_path = Path(__file__).parent / 'query_dict.json'
    if _qd_path.exists():
        with open(_qd_path, encoding='utf-8') as _qf:
            _QUERY_DICT = _json.load(_qf)
except Exception:
    _QUERY_DICT = {}
print(f'[冲刺③] 查询词典加载 {len(_QUERY_DICT)} 词', flush=True)

def _rebuild_header_dict(scan_id, chat_key=None, chat_url=None):
    """冲刺③配套·header_dict 换库重翻（2026-09-21，advisory 债清偿）。
    向量化完成点触发：扫新库全部唯一英文表头 → LLM 批翻 → 原子替换
    header_dict.json。表头注释在下一次分块时自动用新词典（本次不重扫——
    避免全量重嵌入）。无 chat_key 时跳过（下次扫描补）。"""
    global _HEADER_DICT
    if not chat_key or not chat_url:
        return 0
    import re as _re4
    try:
        with _db_lock:
            rows = _db.execute(
                "SELECT text FROM chunks WHERE scan_id=? AND text LIKE '%|---%'",
                (scan_id,)).fetchall()
        # 表头词抽取（与冲刺①同法：分隔行上一行切格，归一化）
        hdr_words = set()
        for (t,) in rows:
            lines = t.splitlines()
            sep_idx = next((i for i, l in enumerate(lines[:8]) if '---|' in l), None)
            if sep_idx is None or sep_idx == 0:
                continue
            hdr = ' '.join(x.strip() for x in lines[:sep_idx] if '|' in x or x.strip())
            for c in hdr.strip('|').split('|'):
                c = ' '.join(c.split())
                if c and _re4.fullmatch(r'[A-Za-z][A-Za-z0-9 /&%()._-]{0,35}', c):
                    hdr_words.add(c)
        # 增量：只翻新词（旧词典已有的跳过——省 LLM 调用）
        new_words = sorted(w for w in hdr_words if w not in _HEADER_DICT)
        if not new_words:
            return 0
        new_map = {}
        for i in range(0, len(new_words), 50):
            batch = new_words[i:i+50]
            raw = _llm_chat(
                '把下列 Excel 表头翻译成简体中文（ERP 财务习惯；货币代码保留原文）。'
                '只输出 JSON 对象，不要解释。' + _json.dumps(batch, ensure_ascii=False),
                api_key=chat_key, api_url=chat_url)
            if not raw:
                continue
            s, e = raw.find('{'), raw.rfind('}')
            try:
                new_map.update(_json.loads(raw[s:e+1]))
            except Exception:
                continue
        if not new_map:
            return 0
        merged = dict(_HEADER_DICT)
        merged.update(new_map)
        _hdp = Path(__file__).parent / 'header_dict.json'
        _tmp = _hdp.with_suffix('.tmp')
        with open(_tmp, 'w', encoding='utf-8') as f:
            _json.dump(merged, f, ensure_ascii=False, indent=1)
        _tmp.replace(_hdp)
        _HEADER_DICT = merged
        _lg_task.info('表头词典已随库重翻', extra={'scan_id': scan_id, 'new': len(new_map), 'total': len(merged)})
        return len(new_map)
    except Exception as e:
        _lg_task.warning('表头词典重翻失败', extra={'scan_id': scan_id, 'exc': str(e)[:120]})
        return 0

def _rebuild_query_dict(scan_id):
    """冲刺③配套（2026-09-21 词典不写死——换库自动重建）。
    扫描分块完成时触发：从新库内容重新生成 query_dict.json。
    源①：库内对照对挖掘——结构判定（点分英文键+相邻中文值），
    不挑文件名不挑列名，任何库通用；源②：header_dict 反向
    （注：header_dict 自身的换库重翻是已知债——见 todo 冲刺①段，
    本函数只吃当前已加载版本，不做 LLM 调用）。"""
    import sqlite3 as _sq
    try:
        # 通用性铁律（2026-09-21 ）：检测规则零语料相关——
        # 不按文件名/列名找对照表，按【结构判定】：任何表格块先全捞，
        # 行内'点分英文键 + 相邻中文值'的形态本身就是对照关系的证据
        with _db_lock:
            rows = _db.execute(
                "SELECT text FROM chunks WHERE scan_id=? AND text LIKE '%|---%'",
                (scan_id,)).fetchall()
        new_dict = {}
        import re as _re3
        for (t,) in rows:
            for line in t.splitlines():
                if not line.startswith('|') or '---' in line:
                    continue
                cells = [c.strip() for c in line.split('|')]
                for i, c in enumerate(cells):
                    if _re3.fullmatch(r'[a-z_]+\.[a-z_.]+', c) and i + 1 < len(cells):
                        zh_val = cells[i + 1]
                        if _re3.search(r'[\u4e00-\u9fa5]', zh_val) and 2 <= len(zh_val) <= 20:
                            cur = new_dict.setdefault(zh_val, [])
                            if c not in cur:
                                cur.append(c)
        # header_dict 反向合并（表头词典也随换库更新）
        for en, zh in _HEADER_DICT.items():
            if zh and zh.lower() != en.lower() and len(zh) >= 2:
                cur = new_dict.setdefault(zh, [])
                if en not in cur:
                    cur.append(en)
        # 原子替换
        _qdp = Path(__file__).parent / 'query_dict.json'
        _tmp = _qdp.with_suffix('.tmp')
        with open(_tmp, 'w', encoding='utf-8') as f:
            _json.dump(new_dict, f, ensure_ascii=False, indent=1)
        _tmp.replace(_qdp)
        global _QUERY_DICT
        _QUERY_DICT = new_dict
        _lg_task.info('查询词典已随库重建', extra={'scan_id': scan_id, 'terms': len(new_dict)})
        return len(new_dict)
    except Exception as e:
        _lg_task.warning('查询词典重建失败', extra={'scan_id': scan_id, 'exc': str(e)[:120]})
        return 0

def _dict_expand_query(q):
    """冲刺③：查词典给查询追加库内写法变体。
    中文术语命中→追加一条含库内英文写法的变体查询（进检索不改判分）。"""
    if not _QUERY_DICT:
        return []
    hits = []
    seen = set()
    for zh, en_list in _QUERY_DICT.items():
        if len(zh) >= 3 and zh in q:  # ≥3 字领域词才命中（'今天→Today'类短日常词不算术语）
            for en in en_list[:2]:
                if en and en not in seen and en.lower() not in q.lower():
                    seen.add(en)
                    hits.append(en)
    if not hits:
        return []
    # 一条变体装全部命中词（控制查询数：最多 2 条）
    v1 = q + " " + " ".join(hits[:3])
    v2 = " ".join(hits[3:6]) + " " + q if len(hits) > 3 else None
    return [v for v in (v1, v2) if v]

def _header_annotation(table_first_lines):
    """冲刺①：给表格头行生成中文对照注释。命中 ≥1 个英文表头才返回行，
    全中文表头/无命中返回 None（不加注）。table_first_lines = 表头行组
    （引导行+表头+分隔行）"""
    if not _HEADER_DICT:
        return None
    # 表头单元格收集：表头可能跨多个物理行（Excel 多行表头被抽成
    # 'Transaction\nDate' 物理断行）——取分隔行之前的所有行拼成一行再切格
    sep_idx = None
    for i, l in enumerate(table_first_lines):
        if "---|" in l and i > 0:
            sep_idx = i
            break
    if sep_idx is not None:
        hdr_line = " ".join(x.strip() for x in table_first_lines[:sep_idx] if "|" in x or x.strip())
    else:
        hdr_line = table_first_lines[0] if table_first_lines else None
    if not hdr_line or "|" not in hdr_line:
        return None
    cells = [" ".join(c.split()) for c in hdr_line.strip("|").split("|")]  # 换行归一：压成空格版
    # 词典命中（精确匹配；大小写不敏感再试一次——LLM 词典是原样大小写）
    pairs = []
    for c in cells:
        if not c:
            continue
        zh = _HEADER_DICT.get(c) or _HEADER_DICT.get(c.lower())
        if zh and zh.lower() != c.lower():  # HTTP=HTTP 这类自译自无价值，跳过
            pairs.append(f"{c}={zh}")
    if not pairs:
        return None
    return "【表头中文对照】" + " | ".join(pairs)


CHUNK_SIZE = 500   # 每块目标字数：调大=块更粗更少（省向量费），调小=更碎更多（检索更细）；调整后⑨评测基线漂移需重跑
CHUNK_OVERLAP = 50 # 相邻块重叠字数：防止答案句子正好被切断丢上下文
TABLE_ROW_GROUP = 10  # ⑨ 三轮步4：表格行组切块——数据行 >10 行即切，每 10 行一组、每组复带表头。
                     # 判据按数据行数（data_line_cnt > TABLE_ROW_GROUP），不按字数——500 行数字表
                     # 仅 ~5000 字（<TABLE_MAX 12000），按字数判永远不会触发，1/500 稀释原样保留
                     # （库内 500-12000 字表格块 1190 个、平均 136 行，全是这个 gap）。稀释度降到
                     # 1/10。10 行是业界甜点（LlamaIndex MarkdownNodeParser 同款）。小表（≤10 数据行）
                     # 仍整表一块；TABLE_MAX 只作单组内的字数保险盖（防单行超长撑爆块）
def _pdf_vector_tables(page, _found=None):
    """⑦4c 增量②（坑4 销号）+性能A（2026-09-13）：pdfplumber 表格抽取。
    性能A改动：_found 参数接收外部 find_tables() 的结果——原来这里调
    extract_tables()（内部检测一遍表格），正文抽取又调 find_tables()
    （再检测一遍）——同一页表格被检测两次，表格检测是 pdfplumber 最贵
    的纯 Python 操作。现在只检测一次，extract 用 Table.extract() 从
    found 对象拿数据，输出与原来逐字节一致（同一引擎同一数据源）。
    返回 (该页管道表文本列表, found 对象)——found 传给正文裁剪复用"""
    found = _found if _found is not None else page.find_tables()
    out = []
    for t in found:
        tbl = t.extract()
        if not tbl or len(tbl) < 2:
            continue
        # fill-down 启发式回填
        filled = [[("" if v is None else str(v).strip()) for v in tbl[0]]]
        for row in tbl[1:]:
            cells = ["" if v is None else str(v).strip() for v in row]
            new = []
            for c, v in enumerate(cells):
                prev = filled[-1][c] if c < len(filled[-1]) else ""
                new.append(prev if (not v and prev) else v)
            filled.append(new)
        n_cols = max(len(r) for r in filled)
        lines = []
        for ri, r in enumerate(filled):
            padded = r + [""] * (n_cols - len(r))
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in padded) + " |")
            if ri == 0:
                lines.append("|" + "---|" * n_cols)
        out.append("\n".join(lines))
    return out, found


def extract_pdf_text(full_path: Path):
    """⑦1+⑦4c PDF 文本层抽取：优先抽矢量表格（extract_tables 结构化，
    合并格启发式回填），表格之外的正文抽纯文本。矢量表格优先的理由：
    线框是矢量对象坐标精确，比纯文本抽取（表格被压平成乱序文字流）和
    OCR 截图路线（猜几何）都强。两层失败/没有文本层返回 None。
    扫描件/图片页仍走 ⑦2/⑦2b（本函数返回 None 后由调用方分派）"""
    try:
        import pdfplumber
        with pdfplumber.open(str(full_path)) as pdf:
            parts = []
            for page in pdf.pages:
                # 性能A（2026-09-13）：find_tables 只检测一次——原来
                # extract_tables() 内部检测一遍、正文裁剪又 find_tables()
                # 一遍，同一页双重检测（pdfplumber 最贵的纯 Python 操作）。
                # 现在 found 传两处共用，输出与原版逐字节一致
                found = page.find_tables()
                tables_md, _ = _pdf_vector_tables(page, _found=found)
                parts.extend(tables_md)
                # 正文只抽表格区域之外的字（found 复用）
                text = ""
                try:
                    if found:
                        rest = page
                        for t in found:
                            rest = rest.crop((0, 0, page.width, t.bbox[1]))
                            below = page.crop((0, t.bbox[3], page.width, page.height))
                        text = (rest.extract_text() or "").strip()
                        text_below = (below.extract_text() or "").strip()
                        text = (text + "\n" + text_below).strip()
                    else:
                        text = (page.extract_text() or "").strip()
                except Exception:
                    text = (page.extract_text() or "").strip()
                if text:
                    parts.append(text)
            full = "\n\n".join(parts).strip()
            return full if full else None
    except Exception as e:
        _pf.warning('pdf抽取失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None
def _process_blips(block, doc, parts):
    """⑦3c+坑7：抽 XML 块里的内嵌图片并识别。段落层（}p）和表格层（}tbl）
    共用——findall 是递归的（.//），表格格子里的段落贴图一样能抓到。
    每张图：related_parts[rid].blob 拿字节 → 双尺度 OCR → 表格判定
    （_looks_like_table）→ 表格走 ⑦2b v2.3 链路出管道表 / 普通图出
    OCR 文字流 → 结果 append 进 parts（调用方保证文档流位置）。
    纯装饰图（OCR 零结果）跳过"""
    # B4 修（error.md）⑥：rid 来源两种——DrawingML a:blip(r:embed) 与 VML
    # v:imagedata(r:id)。老文档（Word 2003 升级件）多用 VML 贴图，漏了
    # 整篇图全丢。dict.fromkeys 去重：AlternateContent 双写件两种标签引
    # 用同一 rid，不去重会同图 OCR 两遍
    _rns = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    rids = [b.get(_rns + 'embed') for b in block.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')]
    rids += [v.get(_rns + 'id') for v in block.findall('.//{urn:schemas-microsoft-com:vml}imagedata')]
    for rid in dict.fromkeys(r for r in rids if r):
        if rid not in doc.part.related_parts:
            continue
        img_part = doc.part.related_parts[rid]
        try:
            img_bytes = img_part.blob
        except Exception:
            continue
        if not img_bytes:
            continue
        boxes = _ocr_multi_scale(img_bytes)
        if not boxes:
            continue  # 纯装饰图：OCR 零结果
        if _looks_like_table(boxes):
            table_md = _table_page_local(img_bytes, boxes)
            if table_md:
                parts.append(table_md)
                continue
        text = "\n".join(b[1] for b in boxes).strip()
        if text:
            parts.append(text)

def extract_docx_text(full_path: Path):
    """⑦3 Word .docx 抽取：python-docx 读段落+表格，表格转 Markdown 管道表。
    docx 合并单元格的福利：python-docx 读合并区时【每行都返回锚点值】
    （与 openpyxl 的锚点有值/覆盖区 None 不同），天然免 ⑦4c 回填——
    横向合并去重（同格同文本只留一）、竖向合并的重复值保留（行自含语义，
    检索友好）。失败返回 None"""
    try:
        from docx import Document
        doc = Document(str(full_path))
        parts = []
        # 段落与表格按文档流顺序抽（python-docx 的 body 元素序 = 顺序）
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        for block in doc.element.body:
            if block.tag.endswith('}p'):
                p = Paragraph(block, doc)
                if p.text.strip():
                    parts.append(p.text.strip())
                # ⑦3c 内嵌图片（坑7 修复版）：段落层与表格层共用 _process_blips
                _process_blips(block, doc, parts)
            elif block.tag.endswith('}tbl'):
                # 坑7 修复：表格格子里的段落贴图（}tbl 内的 }p 里的 blip）也走
                # 同一条 _process_blips——先抽格内图，再抽表格文本
                _process_blips(block, doc, parts)
                t = Table(block, doc)
                rows = []
                for row in t.rows:
                    cells, prev = [], None
                    for c in row.cells:
                        txt = c.text.strip().replace('|', '\\|')
                        # 横向合并：相邻同文本格只留一（python-docx 把合并区每格都给同值）
                        if txt and txt == prev:
                            prev = txt
                            continue
                        cells.append(txt)
                        prev = txt
                    if any(cells):
                        rows.append(cells)
                if not rows:
                    continue
                n_cols = max(len(r) for r in rows)
                lines = []
                for ri, r in enumerate(rows):
                    padded = r + [''] * (n_cols - len(r))
                    if ri == 0:
                        lines.append('| ' + ' | '.join(padded) + ' |')
                        lines.append('|' + '---|' * n_cols)  # 分隔行紧跟表头行（Markdown 语法要求）
                    else:
                        lines.append('| ' + ' | '.join(padded) + ' |')
                parts.append('\n'.join(lines))
        # 2026-09-27 B4 修（error.md）：python-docx 默认 API 只读顶层段落/
        # 表格，漏 7 类文字载体。①-⑤ 存真文字（比 OCR 快且准），⑥⑦ 走
        # OCR 链。docx 是 zip——zipfile 直读部件，pptx 侧 L1828 同款手法
        # ① 文本框/形状文字：w:txbxContent 的 w:t 全抓（孙子层 w:r，
        # Paragraph.text 相对 xpath 摸不到的正是这层）。seen 去重：
        # mc:AlternateContent 的 Choice/Fallback 各含一份相同 txbxContent
        from docx.oxml.ns import qn
        _seen_txbx = set()
        for txbx in doc.element.body.iter(qn('w:txbxContent')):
            texts = [t.text for t in txbx.iter(qn('w:t')) if t.text and t.text.strip()]
            # 去重按内容不按元素对象：mc:AlternateContent 的 Choice/Fallback
            # 是两个不同元素各含一份相同 txbxContent，id() 分不出
            key = ' '.join(texts)
            if texts and key not in _seen_txbx:
                _seen_txbx.add(key)
                parts.append(key)
        # ②③④ zipfile 辅助遍历：SmartArt / 图表 / OLE 嵌入
        #    整段独立 try：zip 部件损坏（CRC 错/截断）时只丢这一小块补抽，
        #    不能让异常冒到外层 except——那会把已抽好的段落/表格整篇丢弃
        import zipfile
        try:
            with zipfile.ZipFile(str(full_path)) as z:
                for name in z.namelist():
                    base = name.rsplit('/', 1)[-1]
                    if name.startswith('word/diagrams/data') and name.endswith('.xml'):
                        # ② SmartArt：<a:t> 正则（pptx 侧 L1833 同款）
                        try:
                            xml = z.read(name).decode('utf-8', errors='ignore')
                            texts = re.findall(r'<a:t>([^<]*)</a:t>', xml)
                            if texts:
                                parts.append('## SmartArt\n\n' + ' '.join(texts))
                        except Exception:
                            pass  # 单个 SmartArt 部件坏（CRC/截断）不影响后续图表/OLE
                    elif name.startswith('word/charts/') and base.endswith('.xml') and 'style' not in base and 'colors' not in base:
                        # ③ 图表：c:pt/c:v 数值 + a:t 文本一把抓（图表 XML 里
                        # 系列名/分类名走 <a:t>、数值走 <c:v>——分开会漏）
                        try:
                            xml = z.read(name).decode('utf-8', errors='ignore')
                            pts = re.findall(r'<c:v>([^<]*)</c:v>', xml)
                            labels = re.findall(r'<a:t>([^<]*)</a:t>', xml)
                            if pts or labels:
                                parts.append('## 图表数据\n\n' + ' | '.join(labels + pts))
                        except Exception:
                            pass  # 单个图表抽不动不影响其余
                    elif name.startswith('word/embeddings/') and (base.endswith('.xlsx') or base.endswith('.docx')):
                        # ④ OLE 嵌入 Excel/Word：落临时文件递归 extract（pptx 侧
                        # OLE 同款手法）；xlsx 走快路临时文件亦同
                        import tempfile, os as _os
                        fd, tmp = tempfile.mkstemp(suffix=Path(base).suffix or '.bin')
                        try:
                            with _os.fdopen(fd, 'wb') as f:
                                f.write(z.read(name))
                            sub = extract_xlsx_text(Path(tmp)) if base.endswith('.xlsx') else extract_docx_text(Path(tmp))
                            if sub:
                                parts.append('## 嵌入文档\n\n' + sub)
                        except Exception:
                            pass  # 嵌入物损坏/抽不动跳过，其余照常
                        finally:
                            try:
                                _os.unlink(tmp)
                            except OSError:
                                pass
        except Exception as e:
            _pf.warning('docx B4 补抽失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        # ⑤ 图片 Alt Text：wp:docPr descr（无障碍描述常含语义关键词）。
        #    注意 chart 的 docPr 也有 descr——图表已在③抽过，此处不跳过
        #    会双抽；但 Alt Text 是作者写的描述、c:pt 是数据值，语义不同
        #    层次，双抽对检索无害（数据有标注总比丢强），不做过滤（过
        #    度设计）。同样独立 try：docPr 遍历异常不牵连 ①-④ 成果
        try:
            for d in doc.element.body.iter(qn('wp:docPr')):
                alt = d.get('descr')
                if alt and alt.strip():
                    parts.append(alt.strip())
        except Exception as e:
            _pf.warning('docx AltText 抽取失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return '\n\n'.join(parts) if parts else None
    except Exception as e:
        _pf.warning('docx抽取失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None
_com_lock = threading.Lock()  # COM 全局串行锁：Word/Excel COM 单实例，并行 DispatchEx 死锁（实测 208 表格文件引爆）
def _convert_doc_to_docx(full_path: Path):
    """⑦3b .doc → .docx 转换（按平台分派）：
    Windows+Word → win32com 驱动真 Word 转换（格式权威实现，表格保真零失真）
    macOS → textutil 直转 txt（表格压平，正文完整）
    都没有 → LibreOffice headless 兜底
    返回转换后 .docx 的临时路径（调用方用完删）；失败返回 None。
    注意：COM 起真实 Word 进程，调用方必须串行（本函数内不并发）"""
    import sys, tempfile, subprocess, os
    full_path = Path(full_path).resolve()  # Word COM 拿裸文件名会在自己的默认
    # 目录找（实测：相对路径静默失败）——进函数先转绝对路径
    tmp = None
    if sys.platform == "win32":
        # COM 全局锁：Word/Excel COM 是单实例进程，8 线程并行读时多个 COM
        # 调用同时 DispatchEx 会死锁（2026-09-13 真实语料 208 个表格文件引爆，
        # 后端整体卡死 GIL 占满）——COM 段全局串行，非 COM 读照常并行
        with _com_lock:
            try:
                import win32com.client
                tmp = tempfile.mktemp(suffix=".docx")
                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                word.DisplayAlerts = 0
                try:
                    doc = word.Documents.Open(str(full_path), ReadOnly=True)
                    doc.SaveAs2(tmp, FileFormat=16)
                    doc.Close(False)
                finally:
                    word.Quit()
                return Path(tmp) if os.path.exists(tmp) and os.path.getsize(tmp) > 0 else None
            except Exception:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
                tmp = None  # COM 失败（没装 Word）→ 落到 LibreOffice
    elif sys.platform == "darwin":
        try:
            tmp = tempfile.mktemp(suffix=".txt")
            subprocess.run(["textutil", "-convert", "txt", "-output", tmp, str(full_path)],
                           check=True, timeout=120, capture_output=True)
            return Path(tmp)
        except Exception:
            return None
    # LibreOffice headless 兜底（跨平台，需安装）
    try:
        lo = "soffice" if sys.platform != "win32" else (shutil.which("soffice") or r"C:\Program Files\LibreOffice\program\soffice.exe")
        if not os.path.exists(lo) and sys.platform == "win32":
            return None  # Windows 上没装 LibreOffice 且 COM 已失败
        outdir = tempfile.mkdtemp()
        subprocess.run([lo, "--headless", "--convert-to", "docx", "--outdir", outdir, str(full_path)],
                       check=True, timeout=180, capture_output=True)
        out = Path(outdir) / (full_path.stem + ".docx")
        return out if out.exists() else None
    except Exception as e:
        _pf.warning('doc转换失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None


def extract_doc_text(full_path: Path):
    """⑦3b .doc 老格式入口：转换成 .docx（或 macOS 直转 txt）后走 ⑦3 同管线。
    失败返回 None（调用方跳过；明因提示等 ⑦7 的通道统一做）"""
    converted = _convert_doc_to_docx(full_path)
    if converted is None:
        return None
    try:
        if converted.suffix == ".docx":
            return extract_docx_text(converted)
        # macOS textutil 产物是 txt：直接读
        return converted.read_text(encoding="utf-8", errors="ignore") or None
    finally:
        try:
            converted.unlink()
        except OSError:
            pass

def _convert_xls_to_xlsx(full_path: Path, _batch_excel=None):
    """⑦4d .xls → .xlsx 转换。性能定稿（2026-09-13）：支持【批量模式】——
    调用方先 _batch_excel_start() 拿一个常驻 Excel 实例传入 _batch_excel，
    23 个文件共用一次进程启动（实测 2.6s/个 → 0.09s/个，29 倍）；
    不传走单文件模式（含自己的 COM 锁+起停）。
    macOS → LibreOffice headless 兜底。返回临时 .xlsx 路径（用完删）"""
    import sys, tempfile, subprocess, os
    # ⑥ .xls 直采补齐（2026-09-22）：COM 线程初始化——分块后台线程调直采时
    # 未 CoInitialize 导致 com_error(-2147221008)，13 个 .xls 全没进 tables
    # （rag_parse.log 实锤）。CoInitialize 幂等（已初始化时返回 S_FALSE 不炸）。
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except ImportError:
        pass  # 非 Windows 无 pythoncom——走 LibreOffice 兜底
    full_path = Path(full_path).resolve()
    if _batch_excel is not None:
        # 批量模式：Excel 已起好（锁由批处理方持有），直接转换
        tmp = tempfile.mktemp(suffix=".xlsx")
        try:
            wb = _batch_excel.Workbooks.Open(str(full_path), ReadOnly=True)
            wb.SaveAs(tmp, FileFormat=51)
            wb.Close(False)
            return Path(tmp) if os.path.exists(tmp) and os.path.getsize(tmp) > 0 else None
        except Exception as e:
            _pf.warning('xls转换(批量)失败 %s: %s: %s', full_path.name, type(e).__name__, e)
            if os.path.exists(tmp):
                os.remove(tmp)
            return None
    tmp = None
    if sys.platform == "win32":
        with _com_lock:  # COM 全局串行（同 doc：并行 DispatchEx 死锁）
            try:
                import win32com.client
                tmp = tempfile.mktemp(suffix=".xlsx")
                excel = win32com.client.DispatchEx("Excel.Application")
                excel.Visible = False
                excel.DisplayAlerts = 0
                try:
                    wb = excel.Workbooks.Open(str(full_path), ReadOnly=True)
                    wb.SaveAs(tmp, FileFormat=51)
                    wb.Close(False)
                finally:
                    excel.Quit()
                return Path(tmp) if os.path.exists(tmp) and os.path.getsize(tmp) > 0 else None
            except Exception as e:
                _pf.warning('xls转换失败 %s: %s: %s', full_path.name, type(e).__name__, e)
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
                tmp = None
    # LibreOffice headless 兜底（macOS/Linux/没装 Excel 的 Windows）
    try:
        lo = "soffice" if sys.platform != "win32" else (shutil.which("soffice") or r"C:\Program Files\LibreOffice\program\soffice.exe")
        if not os.path.exists(lo) and sys.platform == "win32":
            return None
        outdir = tempfile.mkdtemp()
        subprocess.run([lo, "--headless", "--convert-to", "xlsx", "--outdir", outdir, str(full_path)],
                       check=True, timeout=180, capture_output=True)
        out = Path(outdir) / (full_path.stem + ".xlsx")
        return out if out.exists() else None
    except Exception as e:
        _pf.warning('xls转换(LibreOffice)失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None


def _xls_batch_convert(root: Path, rel_paths):
    """批量转换一批 .xls（性能定稿）：一次 DispatchEx 起 Excel → COM 锁内
    逐个转换（常驻实例 0.09s/个）→ 退出。返回 {rel_path: 临时xlsx路径}，
    失败的文件不在返回里（走单文件慢路/跳过）。调用方用完删临时文件"""
    import sys
    if sys.platform != "win32":
        return {}
    results = {}
    try:
        import win32com.client
    except Exception:
        return {}
    with _com_lock:
        excel = None
        try:
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = 0
            for i, rel in enumerate(rel_paths):
                src = (root / rel).resolve()
                out = _convert_xls_to_xlsx(src, _batch_excel=excel)
                if out is not None:
                    results[rel] = out
                # 每 3 个文件歇 1ms 释放 GIL：COM 调用占 GIL 时 HTTP 线程被
                # 饿死（实测批量转换 5s 窗口内 curl 超时）——同 _run_chunk_job
                # 的 i%5 sleep 同款防线
                if i % 3 == 0:
                    time.sleep(0.001)
        except Exception as e:
            _pf.warning('xls批量转换失败: %s: %s', type(e).__name__, e)
        finally:
            if excel is not None:
                try:
                    excel.Quit()
                except Exception:
                    pass
    return results

def _ppt_batch_convert(root: Path, rel_paths):
    """⑦5b 批量转换一批 .ppt（_xls_batch_convert 同构）：一次 DispatchEx 起
    PowerPoint → COM 锁内逐个转（常驻实例免重复起停）→ 退出。返回
    {rel_path: 临时pptx路径}，失败的文件不在返回里（单文件路走
    read_file_text 的 LibreOffice/手撕兜底）。调用方用完删临时文件"""
    import sys
    if sys.platform != "win32":
        return {}
    results = {}
    try:
        import win32com.client, pythoncom
    except Exception:
        return {}
    with _com_lock:
        app = None
        try:
            pythoncom.CoInitialize()
            app = win32com.client.DispatchEx("PowerPoint.Application")
            app.Visible = True  # 同单文件路：部分版本后台窗口 SaveAs 失败
            for i, rel in enumerate(rel_paths):
                src = (root / rel).resolve()
                tmp = None
                try:
                    import tempfile, os as _os
                    pres = app.Presentations.Open(str(src), ReadOnly=True, WithWindow=False)
                    tmp = tempfile.mktemp(suffix=".pptx")
                    pres.SaveAs(tmp, 24)  # ppSaveAsOpenXMLPresentation
                    pres.Close()
                    if _os.path.exists(tmp) and _os.path.getsize(tmp) > 0:
                        results[rel] = Path(tmp)
                    elif tmp:
                        _os.remove(tmp)
                except Exception as e:
                    _pf.warning('ppt批量转换失败 %s: %s: %s', rel, type(e).__name__, e)
                    if tmp and _os.path.exists(tmp):
                        _os.remove(tmp)
                if i % 3 == 0:
                    time.sleep(0.001)  # 释放 GIL，防 HTTP 线程饿死（xls 同款防线）
        except Exception as e:
            _pf.warning('ppt批量转换(启动)失败: %s: %s', type(e).__name__, e)
        finally:
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass
    return results

def extract_xls_text(full_path: Path):
    """⑦4d .xls 老格式入口：转换成 .xlsx 后走 ⑦4 同管线（合并回填+管道表
    全继承，XML 快路吃到转换产物）。失败 None"""
    converted = _convert_xls_to_xlsx(full_path)
    if converted is None:
        return None
    try:
        return extract_xlsx_text(converted)
    finally:
        try:
            converted.unlink()
        except OSError:
            pass


def extract_csv_text(full_path: Path):
    """⑦4d CSV 解析：编码 fallback（UTF-8 → UTF-8-sig(BOM) → GB18030——企业
    导出 GBK 常见，⑪1 同款策略提前落地）+ csv 模块按分隔符切 → 管道表
    （走 chunk_text 表格分支）。无合并单元格概念、免 ⑦4c。失败 None"""
    import csv
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            rows = []
            with open(full_path, newline="", encoding=enc) as f:
                sample = f.read(4096)
                f.seek(0)
                # 嗅探分隔符（逗号/制表符/分号——Excel 各地区导出不同）
                sniffer = csv.Sniffer()
                try:
                    dialect = sniffer.sniff(sample, delimiters=",\t;")
                except csv.Error:
                    dialect = csv.excel  # 嗅探失败退回标准逗号
                for row in csv.reader(f, dialect):
                    cells = [c.strip() for c in row]
                    if any(cells):
                        rows.append(cells)
                        if len(rows) > 5000:
                            break
            if not rows:
                return None
            n_cols = max(len(r) for r in rows)
            lines = []
            for ri, r in enumerate(rows):
                padded = r + [""] * (n_cols - len(r))
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in padded) + " |")
                if ri == 0:
                    lines.append("|" + "---|" * n_cols)
            return "\n".join(lines)
        except UnicodeDecodeError:
            continue  # 换下一个编码
    _pf.warning('csv编码识别失败 %s: utf-8/utf-8-sig/gb18030 都不行', full_path.name)
    return None

def _fill_merged_cells(ws):
    """慢路回填（openpyxl 完整加载模式）：合并区从 ws.merged_cells.ranges 拿，
    算法与 _fill_merged_cells_fast 完全一致。快路（zipfile+read_only）失败时
    的兜底路径——性能优化版编辑时本函数曾被误删（第三次编辑范围事故），
    AST 唯一性检查没覆盖「函数被调但未定义」这一类，validate.py 要补这类"""
    grid = []
    for row in ws.iter_rows(values_only=True):
        grid.append(["" if v is None else str(v).strip() for v in row])
    for rng in ws.merged_cells.ranges:
        min_col, min_row, max_col, max_row = rng.bounds
        if min_row - 1 >= len(grid):
            continue
        anchor = grid[min_row - 1][min_col - 1] if min_col - 1 < len(grid[min_row - 1]) else ""
        if not anchor:
            continue
        for r in range(min_row - 1, max_row):
            while len(grid) <= r:
                grid.append([])
            while len(grid[r]) < max_col:
                grid[r].append("")
            for c in range(min_col - 1, max_col):
                grid[r][c] = anchor
    return grid
def _xlsx_xml_grid(path, max_rows=5000):
    """性能优化定稿（2026-09-13）：纯 zipfile XML 解析 xlsx——合并区+单元格值
    一把抓，openpyxl 零参与。实测 180 文件 3.6s（原 openpyxl 600s，166 倍）。
    兼容性（逐字节对比验证全过）：openpyxl 写盘（rels 属性 Target 在 Id 前）/
    WPS 写盘（inlineStr 内联值）/ Excel 导出（sharedStrings）三种形态；
    空行按原始行号占位（合并区坐标依赖行号对齐，漏占位会洒错行——积分表
    实测踩过）；中文数字实体 html.unescape。
    返回 [(sheet名, [merge ref], grid), ...]；解析失败抛异常由调用方走慢路"""
    import zipfile, re, html
    with zipfile.ZipFile(str(path)) as z:
        wb_xml = z.read('xl/workbook.xml').decode('utf-8')
        # sheet 节点属性序不保证：openpyxl 写 name 在前，标准写 xmlns:r 在前——双序配
        sheets = re.findall(r'<sheet[^>]*?name="([^"]*)"[^>]*?r:id="(rId\d+)"', wb_xml)
        if not sheets:
            sheets = [(n, r) for r, n in re.findall(r'<sheet[^>]*?r:id="(rId\d+)"[^>]*?name="([^"]*)"', wb_xml)]
        rels_xml = z.read('xl/_rels/workbook.xml.rels').decode('utf-8')
        rid2t = {}
        for rid, tgt in re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', rels_xml):
            rid2t[rid] = tgt
        for tgt, rid in re.findall(r'Target="([^"]+)"[^>]*Id="(rId\d+)"', rels_xml):
            rid2t.setdefault(rid, tgt)  # 双序：openpyxl 写 Target 在 Id 前
        sst = []
        if 'xl/sharedStrings.xml' in z.namelist():
            ss = z.read('xl/sharedStrings.xml').decode('utf-8', errors='ignore')
            for si in re.findall(r'<si>(.*?)</si>', ss, re.S):
                sst.append(html.unescape(''.join(re.findall(r'<t[^>]*>([^<]*)</t>', si))))
        out = []
        for sname, rid in sheets:
            t = rid2t.get(rid, '')
            xp = t.lstrip('/') if t.startswith('/') else 'xl/' + t
            try:
                xml = z.read(xp).decode('utf-8', errors='ignore')
            except KeyError:
                out.append((sname, [], []))
                continue
            merges = re.findall(r'<mergeCell ref="([^"]+)"', xml)
            grid = []
            for rm in re.finditer(r'<row[^>]*?r="(\d+)"[^>]*>(.*?)</row>', xml, re.S):
                rnum = int(rm.group(1))
                cells = {}
                # 单元格四种值形态：t="s"(共享串下标) / t="inlineStr"(<is><t>)
                # / t="b"(布尔) / 无 t(数字)。属性里 s= 样式号任意位置
                for cm in re.finditer(
                        r'<c r="([A-Z]+)\d+"([^>]*)>(?:<f>[^<]*</f>)?'
                        r'(?:<is><t[^>]*>([^<]*)</t></is>)?(?:<v>([^<]*)</v>)?', rm.group(2)):
                    col, attrs, inline, val = cm.groups()
                    typ = (re.search(r't="([^"]*)"', attrs) or [None, None])[1]
                    v = inline if inline is not None else val
                    if v is None or v == '':
                        continue
                    if typ == 's' and val is not None and inline is None:
                        idx = int(val)
                        v = sst[idx] if idx < len(sst) else ''
                    elif typ == 'b':
                        v = 'TRUE' if v == '1' else 'FALSE'
                    cells[col] = html.unescape(str(v)).strip()
                def c2n(c):
                    n = 0
                    for ch in c:
                        n = n * 26 + (ord(ch) - 64)
                    return n
                while len(grid) < rnum:
                    grid.append([])  # 空行占位：行号对齐（合并区坐标靠它）
                if not cells:
                    continue
                maxc = max(c2n(c) for c in cells)
                row = [''] * maxc
                for c, v in cells.items():
                    row[c2n(c) - 1] = v
                grid[rnum - 1] = row
                if rnum >= max_rows:
                    break
            out.append((sname, merges, grid))
        return out


def _xlsx_serialize(sheets_data):
    """XML 快路序列化：回填→两级表头扁平化→去重→管道表。与慢路（openpyxl
    完整加载）同构——四文件逐字节对比验证输出一致"""
    from openpyxl.utils import range_boundaries
    parts = []
    for sname, merges, grid in sheets_data:
        hmr = any((lambda b: b[1] == 1 and b[3] == 1 and b[2] > b[0])(range_boundaries(ref)) for ref in merges)
        for ref in merges:
            mc, mr, xc, xr = range_boundaries(ref)
            if mr - 1 >= len(grid):
                continue
            anchor = grid[mr - 1][mc - 1] if mc - 1 < len(grid[mr - 1]) else ''
            if not anchor:
                continue
            for r in range(mr - 1, xr):
                while len(grid) <= r:
                    grid.append([])
                while len(grid[r]) < xc:
                    grid[r].append('')
                for c in range(mc - 1, xc):
                    grid[r][c] = anchor
        if hmr and len(grid) >= 2:
            r1, r2 = grid[0], grid[1]
            if len(r1) < len(r2):
                r1 = r1 + [''] * (len(r2) - len(r1))
            flat = []
            for c in range(len(r2)):
                top, sub = r1[c].strip(), r2[c].strip()
                if top and sub and top != sub:
                    flat.append(f"{top}-{sub}")
                elif top and not sub:
                    flat.append(top)
                else:
                    flat.append(sub or top)
            grid = [flat] + grid[2:]
        dd = []
        for r in grid:
            if dd and r == dd[-1]:
                continue
            dd.append(r)
        rr = [r for r in dd if any(r)][:5000]
        if not rr:
            continue
        nc = max(len(r) for r in rr)
        ls = [f'## Sheet：{sname}', '']
        for ri, r in enumerate(rr):
            p = r + [''] * (nc - len(r))
            ls.append('| ' + ' | '.join(c.replace('|', '\\|') for c in p) + ' |')
            if ri == 0:
                ls.append('|' + '---|' * nc)
        parts.append('\n'.join(ls))
    return '\n\n'.join(parts) if parts else None

def _grid_to_tables(sname, merges, grid):
    """⑨ 五轮 B：结构化网格 → (cols, rows)。多级表头逐层压扁（从网格原始行
    直判，不靠管道文本反猜）：合并区回填后，从首行起连续的「全短文本无数字」
    行都是表头层，逐层并入列名直到数据行。与 _xlsx_serialize 的两级扁平化
    同构但不限层数，且零文本往返损耗——列名错位/反猜失真的根治点。
    返回 None 表示该 sheet 无有效表。"""
    from openpyxl.utils import range_boundaries
    # 1) 合并区回填（与 _xlsx_serialize 同算法：锚值铺满整个合并区）
    for ref in merges:
        try:
            mc, mr, xc, xr = range_boundaries(ref)
        except Exception:
            continue
        if mr - 1 >= len(grid):
            continue
        anchor = grid[mr - 1][mc - 1] if mc - 1 < len(grid[mr - 1]) else ''
        if not anchor:
            continue
        for r in range(mr - 1, xr):
            while len(grid) <= r:
                grid.append([])
            while len(grid[r]) < xc:
                grid[r].append('')
            for c in range(mc - 1, xc):
                grid[r][c] = anchor
    # 2) 行规整：去空行、去连续重复行、列数对齐、上限 5000
    rows = [r for r in grid if any(str(c).strip() for c in r)][:5000]
    if len(rows) < 2:
        return None
    dd = []
    for r in rows:
        if dd and list(r) == dd[-1]:
            continue
        dd.append([str(c) if c is not None else '' for c in r])
    if len(dd) < 2:
        return None
    nc = max(len(r) for r in dd)
    dd = [r + [''] * (nc - len(r)) for r in dd]
    # 3) 多级表头逐层压扁（通用结构判据，语言无关）：表头行=全短文本(≤30)且
    # 无数字且非空占比过半；数据行=含数字或有长文本。逐层并入直到数据行。
    # 四个坑（冒烟+真实语料实测）：
    # ①竖合并锚（A2:A3='科目编码'）把列名铺满下层——吸收层引入 0 个新词视为
    #   占位回显，不再吸收；
    # ②子串包含误判——'余额' in '科目余额表' 为 True 会吞掉真子列，判据改整格相等；
    # ③首行全格同值=表标题（A1:E1 合并），跳过；
    # ④页眉行（'编制单位：xxx' '单位：AFN' 这类报表元信息）非空格仅 1-2 个
    #   占位远多于内容——真表头行非空占比过半，页眉行不过半，跳过不吸收
    #   （真实语料实锤：不加此判据列名变 '编制单位：100402-…-科目编码-科目编码'）。
    def _hdr(cells):
        return (cells and all(len(c) <= 30 and not any(ch.isdigit() for ch in c) for c in cells)
                and sum(1 for c in cells if c.strip()) >= max(2, len(cells) // 2))
    def _meta_row(cells):
        """页眉行判定（语言无关结构信号）。坑（真实语料实锤）：借/贷子表头行
        只有 2 个不同值（借方×4/贷方×4），按 uniq<len/2 会被误判页眉 → 吸收
        循环退出 → 列名丢子层变'期初余额_2'。区分：页眉行的值是【长而各不同
        的元信息】（编制单位：xxx/2026-02/单位：xxx），子表头行的值是【极短
        的重复词】（借方/贷方×4）。判据：非空值平均长度 ≥6 才像页眉——
        '借方'(2字)/'贷方'(2字) 短词铺满是子表头，'编制单位：100402…'是页眉"""
        ne = [c.strip() for c in cells if c.strip()]
        if len(cells) < 4 or not ne:
            return False
        if len(ne) < len(cells) // 2:
            return True  # ①稀疏态（未铺满的页眉）
        avg_len = sum(len(c) for c in ne) / len(ne)
        uniq = len(set(ne))
        return uniq < len(cells) // 2 and avg_len >= 6  # ②铺满态：词少且词长（元信息）才页眉；短词铺满是子表头
    # 标题行：非空格全部同值 → 是表标题，跳过（表名已在 sheet 名里）。
    # 两种形态：合并区回填后多格同值（A1:E1）+ 单格独占一行（无合并信息时）
    start = 0
    while start < len(dd):
        nonempty = [c.strip() for c in dd[start] if c.strip()]
        if nonempty and len(set(nonempty)) == 1 and (
                len(nonempty) >= 2          # 多格同值=合并标题
                or (len(nonempty) == 1 and len(dd[start]) > 1)):  # 单格+行内还有空位=独占标题
            start += 1
        else:
            break
    if start >= len(dd) - 1:
        return None  # 全是标题/表头没有数据
    # 页眉区：表标题之后、真表头之前的稀疏行（编制单位/期间/单位：元）逐行跳过
    while start < len(dd) - 1 and _meta_row(dd[start]):
        start += 1
    # 表头行横向填充：Excel 横向合并表头的标准语义——'期末余额'合并跨 G:H 两列
    # 意味着两列都属期末余额。合并区回填只铺合并区本身，但表头行里非合并的空位
    # （如 row3 只写了首列）需要继承左边最近的非空表头词，否则吸收子层后该列
    # 变裸 '贷方' 丢父级名（真实语料：科目余额表 8 列表头 3/4 靠继承）
    hdr_row = [c.strip() for c in dd[start]]
    filled = []
    last = ''
    for c in hdr_row:
        if c:
            last = c
        filled.append(last)
    cols = filled
    body = dd[start + 1:]
    while body and _hdr(body[0]) and not _meta_row(body[0]):
        nxt = [c.strip() for c in body[0]]
        fresh = 0
        merged = []
        for c1, c2 in zip(cols, nxt):
            if c2 and c2 != c1:
                # 复合列名 c1-c2（如 期初余额-借方）。不做字符级子串包含判断
                # ——'余额' in '科目余额表' 为 True 会把真子列当重复吞掉（中文
                # 表头高频踩中）；只按整格相等和'-'分词判重复
                words1 = c1.split('-') if c1 else []
                if not c1:
                    merged.append(c2)
                elif c2 in words1:
                    merged.append(c1)  # c2 是 c1 的既有分词（占位回显）——保 c1
                else:
                    merged.append(f"{c1}-{c2}")
                fresh += 1
            else:
                merged.append(c1 or c2)
        if fresh == 0:
            break  # 该层全是既有列名的回显（竖合并锚占位）——不是新表头层
        cols = merged
        body = body[1:]
    if not body:
        return None
    # 列名空位补通用序号 + 重复列名加序号（dict(zip(cols,r)) 后键覆盖前键，
    # 重复列会丢一整列数据——'科目名称' 横跨 B2:C2 铺满后必现）
    seen = {}
    final_cols = []
    for i, c in enumerate(cols):
        c = c or f"col_{i+1}"
        if c in seen:
            seen[c] += 1
            final_cols.append(f"{c}_{seen[c]}")  # 科目名称_2（seen 已自增=出现序号）
        else:
            seen[c] = 1
            final_cols.append(c)
    return final_cols, body


def _store_tables_direct(scan_id, rel_path, sheets_data):
    """⑨ 五轮 B：xlsx 网格直采入库（跳过管道文本反猜）。sheets_data 来自
    _xlsx_xml_grid / openpyxl 慢路网格。先清该文件旧表再插（增量正确性——
    重扫/重抽时防新旧并存）。"""
    with _db_lock:
        _db.execute("DELETE FROM tables WHERE scan_id=? AND file=?", (scan_id, rel_path))
        _db.commit()
    n = 0
    for sname, merges, grid in sheets_data:
        got = _grid_to_tables(sname, merges, grid)
        if got is None:
            continue
        cols, body = got
        with _db_lock:
            _db.execute(
                "INSERT INTO tables (scan_id, file, sheet, cols, rows_json, row_count) VALUES (?,?,?,?,?,?)",
                (scan_id, rel_path, sname or "Sheet",
                 json.dumps(cols, ensure_ascii=False),
                 json.dumps(body, ensure_ascii=False), len(body)))
            _db.commit()
        n += 1
    return n


def extract_xlsx_text(full_path: Path):
    """⑦4 xlsx 抽取（性能定稿版 2026-09-13）：快路 = 纯 zipfile XML 解析
    （合并区+值一把抓，180 文件 3.6s，实测 166 倍）；异常回退慢路 =
    openpyxl 完整加载（原 ⑦4 实现）。两路序列化同构，四文件逐字节验证"""
    return _xlsx_parse_impl(full_path)

def _store_grid_tables(scan_id, rel_path, root, text, low):
    """⑨ 五轮 B 统一入口：按扩展名分发表格直采。
    - .xlsx：_xlsx_xml_grid 网格直采（快路）；失败回退 openpyxl 慢路网格
    - .xls：COM 转 xlsx 后同 .xlsx（临时文件用完即删）
    - .csv：管道文本重切网格（csv 无表头层级/合并区，文本零损耗，重切即精确）
    入库前先清该文件旧表（增量正确性，与 _extract_tables_to_db 同语义）"""
    with _db_lock:
        _db.execute("DELETE FROM tables WHERE scan_id=? AND file=?", (scan_id, rel_path))
        _db.commit()
    full = root / rel_path
    if low.endswith(".csv"):
        return _store_csv_tables(scan_id, rel_path, text)
    tmp = None
    try:
        if low.endswith(".xls"):
            tmp = _convert_xls_to_xlsx(full)
            if tmp is None:
                _pf.warning("⑥诊断3 xls转换None: %s", full.name)  # ⑥ 补齐排查
                return 0
            full = tmp
        try:
            sheets = _xlsx_xml_grid(full)
        except Exception:
            sheets = _openpyxl_grid(full)
        return _store_tables_direct(scan_id, rel_path, sheets)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _openpyxl_grid(full_path):
    """⑨ 五轮 B：openpyxl 慢路网格（快路异常的兜底）。与 _xlsx_xml_grid 同构
    返回 [(sheet名, [merge ref], grid), ...]——合并区由 _grid_to_tables 统一回填"""
    import openpyxl
    wb = openpyxl.load_workbook(str(full_path), data_only=True, read_only=False)
    out = []
    try:
        for ws in wb.worksheets:
            if ws.max_row < 1 or ws.max_column < 1:
                continue
            merges = [str(r) for r in ws.merged_cells.ranges]
            out.append((ws.title, merges, _fill_merged_cells(ws)))
    finally:
        wb.close()
    return out


def _store_csv_tables(scan_id, rel_path, text):
    """⑨ 五轮 B：csv 管道文本重切网格直采。csv 本就是平面表（无合并区无
    多级表头），文本零损耗——重切即精确结构。表头行=首行，数据=其余行"""
    rows = []
    for l in (text or "").split("\n"):
        l = l.strip()
        if not l.startswith("|"):
            continue
        if "---|" in l:
            continue  # 分隔行
        cells = [c.strip().replace("\\|", "|") for c in l.strip("|").split("|")]
        if any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return 0
    nc = max(len(r) for r in rows)
    rows = [r + [""] * (nc - len(r)) for r in rows]
    cols = [c or f"col_{i+1}" for i, c in enumerate(rows[0])]
    body = rows[1:5001]
    with _db_lock:
        _db.execute(
            "INSERT INTO tables (scan_id, file, sheet, cols, rows_json, row_count) VALUES (?,?,?,?,?,?)",
            (scan_id, rel_path, Path(rel_path).stem, json.dumps(cols, ensure_ascii=False),
             json.dumps(body, ensure_ascii=False), len(body)))
        _db.commit()
    return 1


def _extract_tables_to_db(scan_id, rel_path, text):
    """⑨ 四轮步3：从解析文本里抽 sheet 表（## Sheet：X 引导行 + 管道表）
    存进 tables 元数据库。只认「| 分隔行」结构；重扫时先清该文件旧表
    （增量正确性）。SQL 查询路（精确值题）的数据源。"""
    with _db_lock:
        _db.execute("DELETE FROM tables WHERE scan_id=? AND file=?", (scan_id, rel_path))
        _db.commit()
    sheets = re.split(r"^## Sheet：", text, flags=re.M)
    n = 0
    for seg in sheets[1:]:
        lines = seg.split("\n")
        sheet_name = lines[0].strip() or "Sheet"
        # 找表头行：第一个含 | 且下一行含 ---| 的
        head_idx = None
        for i, l in enumerate(lines):
            if "|" in l and i + 1 < len(lines) and "---|" in lines[i + 1]:
                head_idx = i
                break
        if head_idx is None:
            continue  # 该段无管道表（纯文本 sheet）
        cols = [c.strip() for c in lines[head_idx].strip().strip("|").split("|")]
        data_start = head_idx + 2  # 跳过分隔行
        # 通用二级表头识别（语言无关结构判据）：首行数据全为短文本、无数字、
        # 且与表头列数一致，而下一行起含数字/更长值 → 首行实为二级表头（多级表头表
        # 在 extract_xlsx_text 扁平化时只并了第一级，第二级漏成数据行——
        # Account-Asset 实锤：cols='Account-Prepared By…'，二级='Account Code'）
        def _looks_like_header_row(cells):
            return (len(cells) == len(cols) and cells
                    and all(len(c) <= 30 and not any(ch.isdigit() for ch in c) for c in cells)
                    and sum(1 for c in cells if c.strip()) >= max(2, len(cells) // 2))
        raw_rows = []
        for l in lines[data_start:]:
            l = l.strip()
            if not l.startswith("|"):
                break
            cells = [c.strip() for c in l.strip("|").split("|")]
            if len(cells) != len(cols):
                continue
            raw_rows.append(cells)
        # 多级表头循环吸收（通用判据）：连续的表头样式行（全短文本、与数据行
        # 模式不同）逐层并入列名，直到遇到数据行。三级表（科目余额表）实锤：
        # [一级拼接串]→[科目编码|科目名称|期初余额...]→[...借方|贷方]→[数据]
        # 合成列名=「期初余额-借方」这类复合——去重相邻重复（科目编码|科目编码）
        while (raw_rows and _looks_like_header_row(raw_rows[0])
               and len(raw_rows) >= 1
               and (len(raw_rows) < 2 or raw_rows[1] is None
                    or any(any(ch.isdigit() for ch in c) or len(c) > 12 for c in raw_rows[1])
                    or _looks_like_header_row(raw_rows[1]))):
            merged = []
            for c1, c2 in zip(cols, raw_rows[0]):
                if c2 and c2 != c1 and c2 not in c1:
                    merged.append(f"{c1}-{c2}" if c1 else c2)  # 复合列名保父子（期末余额-贷方）
                else:
                    merged.append(c1 or c2)
            cols = merged
            raw_rows = raw_rows[1:]
        rows = raw_rows
        if rows:
            with _db_lock:
                _db.execute(
                    "INSERT INTO tables (scan_id, file, sheet, cols, rows_json, row_count) VALUES (?,?,?,?,?,?)",
                    (scan_id, rel_path, sheet_name, json.dumps(cols, ensure_ascii=False),
                     json.dumps(rows, ensure_ascii=False), len(rows)))
                _db.commit()
            n += 1
    return n

def _xlsx_parse_impl(full_path: Path):
    """⑦4 xlsx 抽取实现（extract_xlsx_text 委托到此）：快路 zipfile XML +
    慢路 openpyxl 回退"""
    try:
        try:
            return _xlsx_serialize(_xlsx_xml_grid(full_path))
        except Exception as e:
            _pf.warning('xlsx快路失败(回退慢路) %s: %s: %s', full_path.name, type(e).__name__, e)
        import openpyxl
        wb = openpyxl.load_workbook(str(full_path), data_only=True, read_only=False)
        parts = []
        for ws in wb.worksheets:
            if ws.max_row < 1 or ws.max_column < 1:
                continue
            header_merged_row1 = any(
                r.min_row == 1 and r.max_row == 1 and r.max_col > r.min_col
                for r in ws.merged_cells.ranges
            )
            grid = _fill_merged_cells(ws)
            if header_merged_row1 and len(grid) >= 2:
                r1, r2 = grid[0], grid[1]
                if len(r1) < len(r2):
                    r1 = r1 + [""] * (len(r2) - len(r1))
                flat = []
                for c in range(len(r2)):
                    top, sub = r1[c].strip(), r2[c].strip()
                    if top and sub and top != sub:
                        flat.append(f"{top}-{sub}")
                    elif top and not sub:
                        flat.append(top)
                    else:
                        flat.append(sub or top)
                grid = [flat] + grid[2:]
            dedup = []
            for r in grid:
                if dedup and r == dedup[-1]:
                    continue
                dedup.append(r)
            rows_raw = [r for r in dedup if any(r)][:5000]
            if not rows_raw:
                continue
            n_cols = max(len(r) for r in rows_raw)
            lines = [f"## Sheet：{ws.title}", ""]
            for ri, r in enumerate(rows_raw):
                padded = r + [""] * (n_cols - len(r))
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in padded) + " |")
                if ri == 0:
                    lines.append("|" + "---|" * n_cols)
            parts.append("\n".join(lines))
        wb.close()
        return "\n\n".join(parts) if parts else None
    except Exception as e:
        _pf.warning('xlsx抽取失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None

def _pptx_table_md(tbl):
    """⑦5-② PPT 原生表格 → 管道表。合并单元格：实测 python-pptx 与 docx 不同——
    被合并覆盖的格（is_spanned）text 为空，锚点值只在 merge origin 格（行为像
    openpyxl 不像 python-docx）。回填：is_merge_origin + span_height/span_width
    锚点值铺格（⑦4 纯数组回填同构），铺完横向相邻同文本去重（回填后合并区
    出现相邻重复）；竖向合并重复值保留（行自含语义，docx 同款决策）。失败 None"""
    try:
        # cell 文本取法（WPS 实测坑）：c.text 只返回第一段——WPS 造表每格
        # 插多个 <a:p>（豆包AI生成器就这样），"85"和单位/换行会分家。取全段：
        # text_frame.paragraphs 逐段拼（段内 run 已由 .text 合并）
        def _cell_text(c):
            try:
                return ' '.join(p.text for p in c.text_frame.paragraphs if p.text).strip().replace('|', '\\|')
            except Exception:
                return ''
        grid = [[_cell_text(c) for c in row.cells] for row in tbl.rows]
        # 合并回填（在纯数组上铺，不碰 worksheet）
        for ri, row in enumerate(tbl.rows):
            for ci, c in enumerate(row.cells):
                if not c.is_merge_origin:
                    continue
                anchor = grid[ri][ci] if ci < len(grid[ri]) else ''
                if not anchor:
                    continue
                for r in range(ri, ri + c.span_height):
                    while len(grid) <= r:
                        grid.append([])
                    while len(grid[r]) < ci + c.span_width:
                        grid[r].append('')
                    for cc in range(ci, ci + c.span_width):
                        grid[r][cc] = anchor
        rows = []
        for r in grid:
            cells, prev = [], None
            for txt in r:
                if txt and txt == prev:
                    prev = txt
                    continue
                cells.append(txt)
                prev = txt
            if any(cells):
                rows.append(cells)
        if not rows:
            return None
        n_cols = max(len(r) for r in rows)
        lines = []
        for ri, r in enumerate(rows):
            padded = r + [''] * (n_cols - len(r))
            lines.append('| ' + ' | '.join(padded) + ' |')
            if ri == 0:
                lines.append('|' + '---|' * n_cols)
        return '\n'.join(lines)
    except Exception:
        return None
def _pptx_image_parts(shape, parts, ctx=""):
    """⑦5-③ 嵌图（⑦3 docx _process_blips 同款链路）：picture blob →
    双尺度 OCR → _looks_like_table 判定 → 表格图走 ⑦2b v2.3 还原链出管道表 /
    普通图出 OCR 文字流。纯装饰图（OCR 零结果）跳过。
    ctx=页标题引导行（坑14 同款语义锚——2026-09-14 销售部检索失灵根因：
    页4 的表格是贴图（PICTURE），走本函数；原生表格路有 _ctx 引导行而
    图片表格路没有，纯数字管道表裸奔进库，embedding 无语义锚，查
    「销售部工作效率」命不中；同一文件页5 原生表格（客服部）带引导行
    命中正常——同病不同命实锤）"""
    try:
        img_bytes = shape.image.blob
    except Exception:
        return
    if not img_bytes:
        return
    boxes = _ocr_multi_scale(img_bytes)
    if not boxes:
        return
    if _looks_like_table(boxes):
        table_md = _table_page_local(img_bytes, boxes)
        if table_md:
            parts.append(ctx + table_md)
            return
    text = "\n".join(b[1] for b in boxes).strip()
    if text:
        parts.append(text)

def extract_pptx_text(full_path: Path):
    """⑦5 PPT .pptx 全量抽取（八个子项）。slide.shapes 统一分派器：
    文本框/表格/图片/图表各归其路；组合形状递归（组内 shape 同一分派器，
    组里的表格/图片同样出得来）。按 shape top/left 排近似阅读序（PPT 无
    文档流）。备注先判 has_notes_slide（notes_slide 属性会在无备注时
    自动创建空页，幻影空页混进切块）。SmartArt/批注/OLE 嵌入走 zipfile
    直读（python-pptx 无 diagram/comments/embeddings API）。
    诚实边界：母版/版式样板字（页脚/公司落款/页码）不抽——每页重复的
    模板 chrome 会稀释语义，这是设计选择不是漏抽。失败 None（明因走 _pf）"""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    try:
        prs = Presentation(str(full_path))
        parts = []
        for si, slide in enumerate(prs.slides, 1):
            # top/left 排序近似阅读序：标题在上、正文按从上到下从左到右
            shapes = sorted(slide.shapes, key=lambda s: (s.top or 0, s.left or 0))
            page_bits = []
            # 语义引导行（2026-09-14 检索失灵根因③）：表块/图表块只有数字
            # 网格，'工作效率'等语义住在页标题和表头里——embedding/rerank 对
            # 「职能部工作效率怎么样」类问题天然匹配不上纯数字表。抽页内最近
            # 的标题 shape（top 最小的文本框≈页标题）作为表块前缀，表块自己
            # 长出可检索的语义锚
            _slide_title = ''
            for s in shapes:
                if s.shape_type != MSO_SHAPE_TYPE.PICTURE and s.has_text_frame:
                    t = s.text_frame.text.strip()
                    if t and len(t) <= 60:
                        _slide_title = t
                        break
            def _ctx(prefix_type):
                # prefix_type 如「表格」；引导行 = 页标题 + 类型，无标题时退类型
                return f"{_slide_title}（{prefix_type}）\n" if _slide_title else ""
            def _dispatch(shape):
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    for sub in shape.shapes:
                        _dispatch(sub)
                    return
                if shape.has_table:
                    md = _pptx_table_md(shape.table)
                    if md:
                        page_bits.append(_ctx('数据表格') + md)
                    return
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    # ctx 引导行随图走（表格贴图路补语义锚——见函数 docstring）
                    _pptx_image_parts(shape, page_bits, ctx=_ctx('数据表格'))
                    return
                if getattr(shape, 'has_chart', False):
                    try:
                        for plot in shape.chart.plots:
                            cats = [str(c) for c in plot.categories]
                            rows = [['类别'] + [s.name for s in plot.series]]
                            for i, c in enumerate(cats):
                                vals = []
                                for s in plot.series:
                                    v = s.values[i] if i < len(s.values or []) else None
                                    vals.append('' if v is None else str(v))
                                rows.append([str(c)] + vals)
                            n_cols = max(len(r) for r in rows)
                            lines = []
                            for ri, r in enumerate(rows):
                                padded = r + [''] * (n_cols - len(r))
                                lines.append('| ' + ' | '.join(v.replace('|', '\\|') for v in padded) + ' |')
                                if ri == 0:
                                    lines.append('|' + '---|' * n_cols)
                            page_bits.append(_ctx('图表数据') + '\n'.join(lines))
                    except Exception:
                        pass  # 图表抽不动（损坏/空数据）跳过，页面其余部分照常
                    return
                if shape.has_text_frame:
                    t = shape.text_frame.text.strip()
                    if t:
                        page_bits.append(t)
            for shape in shapes:
                _dispatch(shape)
            # ⑦5-④ 演讲者备注：先判 has_notes_slide（防自动创建空页）
            if slide.has_notes_slide:
                nt = slide.notes_slide.notes_text_frame.text.strip()
                if nt:
                    page_bits.append(nt)
            if page_bits:
                # 标记与内容分段（\n\n）发——孤儿标记由 chunk_text 表格分支统一
                # 折并（短标记桶折进表格块首）。抽取层不做前缀融合：融合会破坏
                # para.startswith("|")，表格失灵掉进切句路径，巨表被劈行
                parts.append(f"## 幻灯片 {si}\n\n" + "\n\n".join(page_bits))
        # ⑦5-⑤+⑦ pptx 是 zip：SmartArt/批注/OLE 嵌入直接读 XML 部件。
        # <a:t> 前缀匹配丢弃命名空间声明差异（_xlsx_xml_grid 同款手法）
        import zipfile
        with zipfile.ZipFile(str(full_path)) as z:
            for name in z.namelist():
                if name.startswith('ppt/diagrams/data') and name.endswith('.xml'):
                    xml = z.read(name).decode('utf-8', errors='ignore')
                    texts = re.findall(r'<a:t>([^<]*)</a:t>', xml)
                    if texts:
                        parts.append('## SmartArt\n\n' + ' '.join(texts))
                elif name.startswith('ppt/comments') and name.endswith('.xml'):
                    xml = z.read(name).decode('utf-8', errors='ignore')
                    texts = re.findall(r'<a:t>([^<]*)</a:t>', xml)
                    if texts:
                        parts.append('## 批注\n\n' + ' '.join(texts))
                elif name.startswith('ppt/embeddings/'):
                    # OLE 嵌入的活 Office 文档：直接喂已有解析链（xlsx→⑦4 / docx→⑦3）
                    lower = name.lower()
                    try:
                        if lower.endswith('.xlsx'):
                            # 嵌入 xlsx 走 tempfile + extract_xlsx_text（⑦4 完整链：
                            # zipfile 快路+合并回填+两级表头），不内联 openpyxl 副本
                            # ——副本代码会与 ⑦4 漂移，且绕过 166 倍快路
                            import tempfile, os as _os
                            blob = z.read(name)
                            fd, tmp = tempfile.mkstemp(suffix='.xlsx')
                            try:
                                _os.write(fd, blob)
                                _os.close(fd)
                                t = extract_xlsx_text(Path(tmp))
                                if t:
                                    parts.append('## 嵌入表格\n\n' + t)
                            finally:
                                _os.unlink(tmp)
                        elif lower.endswith('.docx'):
                            import tempfile, os as _os
                            blob = z.read(name)
                            fd, tmp = tempfile.mkstemp(suffix='.docx')
                            try:
                                _os.write(fd, blob)
                                _os.close(fd)
                                t = extract_docx_text(Path(tmp))
                                if t:
                                    parts.append('## 嵌入文档\n\n' + t)
                            finally:
                                _os.unlink(tmp)
                        else:
                            # 老式 OLE .bin（Excel 97 等）：需要 olefile/COM，明因跳过
                            _pf.info('pptx嵌入对象跳过(老式OLE) %s: %s', full_path.name, name)
                    except Exception as e:
                        _pf.warning('pptx嵌入对象抽取失败 %s %s: %s: %s',
                                    full_path.name, name, type(e).__name__, e)
        return '\n\n'.join(parts) if parts else None
    except Exception as e:
        _pf.warning('pptx抽取失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None
# ---------- ⑦5b PPT 老格式 .ppt（PowerPoint 97-2003 二进制） ----------
# 三级路线（用户定标 2026-09-14：不给用户机器装任何软件；pip 依赖可装）：
#   ① 本机已装 PowerPoint → COM 转 .pptx 走 ⑦5 全管线（格式最保真）
#   ② 本机已装 LibreOffice → headless 转换（macOS/Linux 主路）
#   ③ 兜底：olefile（纯 Python 库）+ struct 手撕 MS-PPT 二进制记录树——
#      裸机零 Office 也能出字；能力边界：文本/表格格文本/组合形状/备注全出，
#      图片不 OCR、表格网格不重建（blob/PP9Table 结构复杂，交给 COM 路补齐）
def _convert_ppt_to_pptx(full_path: Path):
    """⑦5b .ppt → .pptx 转换（⑦3b/⑦4d 同款三段式）。返回临时 .pptx 路径
    （调用方用完删）；失败 None。COM 段持全局锁（PowerPoint 同 Word/Excel
    是单实例 COM 服务器，并行 DispatchEx 死锁——坑9 同款）"""
    import sys, tempfile, subprocess, os
    # 预检：COM 起 PowerPoint 前先验 OLE 魔数——坏/假 .ppt 会让 PowerPoint
    # 弹恢复对话框等人工点击（ReadOnly/WithWindow 防不住，实测测试卡死
    # 180s 超时），一个坏文件能卡死整个后端。不是合法 OLE 直接 None
    try:
        import olefile
        if not olefile.isOleFile(str(full_path)):
            _pf.warning('ppt预检跳过 %s: 非OLE2文件（损坏/假后缀）', full_path.name)
            return None
    except ImportError:
        pass  # olefile 未装则跳过预检（COM 自己会失败）
    full_path = Path(full_path).resolve()  # COM 拿相对路径会在自己默认目录找（⑦3b 踩坑）
    tmp = None
    if sys.platform == "win32":
        with _com_lock:
            try:
                import win32com.client, pythoncom
                pythoncom.CoInitialize()
                app = None
                # DispatchEx 瞬态失败：前一个 PowerPoint 实例 Quit 后 COM
                # class table 注销有延迟（实测连续转换时 RPC_E_CALL_REJECTED
                # "正在使用中"），退避重试即可通——Word/Excel 无此病
                for _attempt in range(4):
                    try:
                        app = win32com.client.DispatchEx("PowerPoint.Application")
                        break
                    except Exception:
                        if _attempt == 3:
                            raise
                        time.sleep(1.5 * (_attempt + 1))
                app.Visible = True  # PPT COM 与 Word 不同：部分版本后台窗口
                # SaveAs 失败，必须可见窗口（实测 12.0）
                try:
                    app.DisplayAlerts = 1  # ppAlertsNone——损坏文件的修复
                    # 对话框非确定性挂起（实测同文件一次秒败一次卡 180s），
                    # 一个坏文件卡死 COM 锁会冻结全部 doc/xls/ppt 转换
                except Exception:
                    pass  # 旧版本无此属性
                try:
                    pres = app.Presentations.Open(str(full_path), ReadOnly=True, WithWindow=False)
                    tmp = tempfile.mktemp(suffix=".pptx")
                    pres.SaveAs(tmp, 24)  # ppSaveAsOpenXMLPresentation
                    pres.Close()
                finally:
                    app.Quit()
                return Path(tmp) if os.path.exists(tmp) and os.path.getsize(tmp) > 0 else None
            except Exception as e:
                _pf.warning('ppt转换(COM)失败 %s: %s: %s', full_path.name, type(e).__name__, e)
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
                tmp = None
    # LibreOffice headless（跨平台兜底；没装直接跳过——不要求用户装软件）
    try:
        lo = "soffice" if sys.platform != "win32" else (shutil.which("soffice") or r"C:\Program Files\LibreOffice\program\soffice.exe")
        if not os.path.exists(lo) and sys.platform == "win32":
            return None
        outdir = tempfile.mkdtemp()
        subprocess.run([lo, "--headless", "--convert-to", "pptx", "--outdir", outdir, str(full_path)],
                       check=True, timeout=180, capture_output=True)
        out = Path(outdir) / (full_path.stem + ".pptx")
        return out if out.exists() else None
    except Exception as e:
        _pf.warning('ppt转换(LibreOffice)失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None


_PPT_RT = {  # 本解析器用到的记录类型（MS-PPT 官方编号）
    'SLIDE': 0x03EE, 'NOTES': 0x03F0, 'NOTES_ATOM': 0x03F1,
    'PPDRAW': 0x040C, 'GROUP': 0xF003, 'SHAPE': 0xF004, 'DRAW': 0xF002,
    'ANCHOR': 0xF010, 'TEXTBODY': 0xF00D, 'TEXTCHARS': 0x0FA0, 'TEXTBYTES': 0x0FA8,
}


def _ppt_bin_children(buf, off, end):
    """迭代 [off,end) 内一层 MS-PPT 子记录。记录头 8 字节：
    verInstance(2B) + recType(2B) + recLen(4B)；声称超界即停（损坏文件防越界）"""
    out = []
    while off + 8 <= end:
        vi, rt, rl = struct.unpack_from("<HHI", buf, off)
        if off + 8 + rl > end:
            break
        out.append((rt, vi & 0xF, off + 8, off + 8 + rl))
        off += 8 + rl
    return out


def _ppt_bin_shape(buf, s_off, s_end):
    """一个 shape/组：返回 (top, left, [文本...])。组递归（组内成员坐标是
    相对组锚的，但兜底路只按坐标聚类重建网格，绝对值不参与语义——够用）"""
    top = left = 0
    texts = []
    for rt, ver, o, e in _ppt_bin_children(buf, s_off, s_end):
        if rt == _PPT_RT['ANCHOR']:
            top, left = struct.unpack_from("<ii", buf, o)[0:2]
        elif rt == _PPT_RT['TEXTBODY']:
            for rt2, v2, o2, e2 in _ppt_bin_children(buf, o, e):
                if rt2 == _PPT_RT['TEXTCHARS']:
                    t = buf[o2:e2].decode('utf-16-le', errors='replace')
                    if t and t.strip() and t.strip() != '*':
                        texts.append(t)
                elif rt2 == _PPT_RT['TEXTBYTES']:
                    # TextBytesAtom 单字节（官方 cp1252）；中文文件走 UTF-16
                    # 路（TEXTCHARS），这条路英文老文件常见
                    t = buf[o2:e2].decode('cp1252', errors='replace')
                    if t and t.strip() and t.strip() != '*':
                        texts.append(t)
        elif rt == _PPT_RT['GROUP']:
            for rt3, v3, o3, e3 in _ppt_bin_children(buf, o, e):
                if rt3 == _PPT_RT['SHAPE']:
                    _t, _l, sub_texts = _ppt_bin_shape(buf, o3, e3)
                    texts += sub_texts  # 组内文本平铺；坐标丢弃（网格重建不需要）
    return top, left, texts


def _ppt_bin_walk_drawing(buf, c_off, c_end, out):
    """容器下钻到 shape 为止。实测链：PPDRAW(0x040C) > DRAW(0xF002) >
    GROUP(0xF003) > SHAPE(0xF004)；表格是嵌套组（GROUP > GROUP > SHAPE，
    每格一个带锚点的 shape）。DRAW/GROUP 都透明递归，撞到 SHAPE 才收集"""
    for rt, ver, o, e in _ppt_bin_children(buf, c_off, c_end):
        if rt in (_PPT_RT['DRAW'], _PPT_RT['GROUP']):
            _ppt_bin_walk_drawing(buf, o, e, out)
        elif rt == _PPT_RT['SHAPE']:
            top, left, texts = _ppt_bin_shape(buf, o, e)
            for t in texts:
                out.append((top, left, t))


def _ppt_bin_rebuild_page(found):
    """shape 文本列表 → 页内容。诚实边界（2026-09-14 实测定案）：.ppt 表格
    的格 shape 嵌在 GROUP>GROUP 里且无独立锚点（0xF010 只在外层 frame 出现，
    格坐标藏在 PP9Table 二进制扩展），几何网格重建此路不通——格文本按
    文档序（=行优先）平铺，语义可检索，行列对齐丢失。COM 路（装了
    PowerPoint 的机器）无此损失，全结构继承 ⑦5"""
    return "\n\n".join(t for _t2, _l2, t in found)


def _ppt_bin_extract(buf):
    """手撕主入口：PowerPoint Document 流字节 → 文本（幻灯片标题/正文/
    表格格文本/组合形状/备注）。返回 str 或 None（零文本=解析失败）。
    顶层白名单只认 SLIDE/NOTES 容器——母版 0x03F8 天然不在名单里，
    母版样板字免过滤自动跳过（⑦5 同款设计选择：模板 chrome 稀释语义）。
    诚实边界：表格网格结构不重建（行列信息在 PP9Table 二进制扩展里，
    属另一个格式章节，解析成本无限大）——格文本按文档序平铺，语义
    可检索，行列对齐丢失。图片不 OCR（blob 结构复杂，COM 路补齐）"""
    parts = []
    slide_no = 0
    off = 0
    n = len(buf)
    while off + 8 <= n:
        vi, rt, rl = struct.unpack_from("<HHI", buf, off)
        if (vi & 0xF) != 0xF or off + 8 + rl > n:
            break  # 非 container / 声称越界 = 损坏文件，安全停
        if rt == _PPT_RT['SLIDE']:
            slide_no += 1
            found = []
            for rt2, v2, o2, e2 in _ppt_bin_children(buf, off + 8, off + 8 + rl):
                if rt2 == _PPT_RT['PPDRAW']:
                    _ppt_bin_walk_drawing(buf, o2, e2, found)
            if found:
                # 阅读序（top 排序）+ 表格网格重建（⑦2b 同款几何思路）
                found.sort(key=lambda x: (x[0], x[1]))
                parts.append(f"## 幻灯片 {slide_no}\n\n" + _ppt_bin_rebuild_page(found))
        elif rt == _PPT_RT['NOTES']:
            # 备注母版判别：NotesAtom.slideIdRef 高位 0x80000000（实测 12.0 版）。
            # 两遍扫：先判完母版标记再抽文本（记录顺序无保证）
            is_master = False
            kids = _ppt_bin_children(buf, off + 8, off + 8 + rl)
            for rt2, v2, o2, e2 in kids:
                if rt2 == _PPT_RT['NOTES_ATOM'] and e2 - o2 >= 4:
                    if struct.unpack_from("<I", buf, o2)[0] & 0x80000000:
                        is_master = True
                        break
            if not is_master:
                for rt2, v2, o2, e2 in kids:
                    if rt2 == _PPT_RT['PPDRAW']:
                        found = []
                        _ppt_bin_walk_drawing(buf, o2, e2, found)
                        for _t, _l, txt in sorted(found, key=lambda x: (x[0], x[1])):
                            if txt.strip():
                                parts.append(txt)
        off += 8 + rl
    return '\n\n'.join(parts) if parts else None


def extract_ppt_text(full_path: Path):
    """⑦5b .ppt 老格式入口（三级分派，用户定标：不要求用户装软件）：
    ① COM 转 .pptx → ⑦5 全管线（本机已装 PowerPoint 时格式最保真，
    图片/图表/OLE 全继承）② LibreOffice headless（恰装则用）③ 手撕
    二进制兜底（olefile 纯 Python——裸机零 Office 也能出字）。失败 None"""
    converted = _convert_ppt_to_pptx(full_path)
    if converted is not None:
        try:
            return extract_pptx_text(converted)
        finally:
            try:
                converted.unlink()
            except OSError:
                pass
    # ③ 手撕兜底（COM/LO 都不可用的机器）
    try:
        import olefile
        with olefile.OleFileIO(str(full_path)) as ole:
            if not ole.exists('PowerPoint Document'):
                _pf.warning('ppt手撕跳过 %s: 无 PowerPoint Document 流', full_path.name)
                return None
            buf = ole.openstream('PowerPoint Document').read()
        return _ppt_bin_extract(buf)
    except Exception as e:
        _pf.warning('ppt手撕抽取失败 %s: %s: %s', full_path.name, type(e).__name__, e)
        return None


def read_file_text(root: Path, rel_path: str, chat_key=None, chat_url=None):
    """读一个文件的内容（txt/md 直读；PDF 文本层+OCR；docx 抽段落表格；
    doc 老格式转换链；xlsx 合并区回填+管道表），返回文件全文；读不了返回
    None，调用方跳过——但每个 None 都留一行明因日志（坑3）。
    chat_key/chat_url：⑦2 视觉兜底用的聊天侧配置（BYOK，从 /api/scan 请求头透传下来）
    性能定稿③：重格式（pdf/docx/doc/xlsx/xls）先查 parse_cache（mtime 没变
    直接复用上次解析结果，PDF OCR 29s→0），txt/md 太快不值得查缓存"""
    full_path = root / rel_path
    suf = full_path.suffix.lower()
    _t0 = _time.time()
    _cache_hit = False
    if suf in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt"):
        cached = cache_get_text(full_path)
        if cached is not None:
            _cache_hit = True
            _lg_parse.info("解析完成", extra={  # ⑫2：缓存命中也记（手册场景三对账）
                "file": rel_path, "fmt": suf, "cache": True,
                "duration_ms": int((_time.time() - _t0) * 1000),
                "chars": len(cached) if cached != " " else 0, "ok": cached != " "})
            return cached if cached != " " else None  # 负缓存=已知失败，跳过重试
    try:
        text = None
        if suf == ".pdf":
            text = extract_pdf_text(full_path)
            if text is None:
                text = ocr_pdf_pages(full_path, chat_key, chat_url)
        elif suf == ".pptx":
            text = extract_pptx_text(full_path)
        elif suf == ".ppt":
            text = extract_ppt_text(full_path)
        elif suf == ".docx":
            text = extract_docx_text(full_path)
        elif suf == ".doc":
            text = extract_doc_text(full_path)
        elif suf == ".xlsx":
            text = extract_xlsx_text(full_path)
        elif suf == ".xls":
            text = extract_xls_text(full_path)
        elif suf == ".csv":
            text = extract_csv_text(full_path)
        else:
            # ⑪1 编码 fallback（2026-09-23 补）：utf-8 失败回退 GBK/GB18030——
            # 中文 Windows 的 txt 常是 GBK，原来直接 None 静默跳过（文档实锤坑）
            text = None
            for _enc in ("utf-8", "gb18030", "gbk"):
                try:
                    with open(full_path, encoding=_enc) as f:
                        text = f.read(1024 * 1024)
                    if _enc != "utf-8":
                        _lg_parse.info("编码回退", extra={
                            "file": rel_path, "fmt": suf, "encoding": _enc})
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                _lg_parse.warning("解析失败", extra={
                    "file": rel_path, "fmt": suf, "exc": "UnicodeDecodeError(三编码全败)"})
                return None
        if suf in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt"):
            # 成功和失败都写缓存——失败文件（BadZipFile 假 xlsx 等）每次重试
            # COM/慢路兜底是稳态 13.9s 的大头；负缓存（空文本）跳过重试。
            # 失败标记存单空格（cache_get 判 None 才算未命中，空串算命中跳过）
            cache_put_text(full_path, text if text is not None else " ")
        # ⑫2：每文件一条明细（路径/格式/耗时/字符数/成败——换库后块数不对在这对账）
        _dur = int((_time.time() - _t0) * 1000)
        _lg_parse.info("解析完成" if text else "解析为空", extra={
            "file": rel_path, "fmt": suf, "cache": False,
            "duration_ms": _dur, "chars": len(text) if text else 0,
            "ok": bool(text)})
        if _dur > 5000:
            _lg_parse.warning("解析超时", extra={  # >5s 的慢解析标警（COM 转换/OCR 大头）
                "file": rel_path, "fmt": suf, "duration_ms": _dur})
        return None if text == " " else text
    except Exception as e:
        _lg_parse.error("解析失败", extra={  # ⑫2：异常版（原 _pf.warning 迁 JSON+堆栈）
            "file": rel_path, "fmt": suf,
            "exc_type": type(e).__name__, "exc": str(e)[:200]})
        _pf.warning("解析失败[%s] %s: %s: %s", suf, rel_path, type(e).__name__, e)  # 旧通道兼容保留
        return None

# ---------- ⑦2 PDF 扫描件/图片页 OCR（双链路） ----------
# 文本层抽不出（<100字/页）的页：本地 RapidOCR 识别 → 仍 <200 字/页 →
# 聊天侧视觉模型（qwen3.8-plus，公司代理）兜底。硅基流动只管向量/重排，不掺和。
# 聊天 key 由前端随 /api/scan 请求头传入（X-Chat-Key/X-Chat-Url），BYOK 用完即弃。
OCR_FALLBACK_MIN_CHARS = 200   # RapidOCR 结果低于此字数/页 → 升级视觉模型
_ocr_engine = None             # RapidOCR 单例（模型加载 ~1s，全进程复用）
_ocr_lock = threading.Lock()


def _get_ocr():
    """懒加载 RapidOCR 单例（rapidocr 3.x 模型内置在安装包里，离线可用；
    2026-09-27 从 rapidocr-onnxruntime 1.4.4 迁移——旧包停更，官方
    Requires-Python <3.13，堵死 3.13/3.14）"""
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            from rapidocr import RapidOCR
            _ocr_engine = RapidOCR()
    return _ocr_engine
def _render_page_image(pdfium_page, scale=2.0):
    """pypdfium2 把 PDF 页渲染成 PNG 字节流（OCR 的眼睛）。scale=2 ≈144dpi：
    清晰度够 OCR 认字，图又不会大到拖慢识别。注意：这是 pdfium 的页对象
    （有 render() 方法），不是 pypdf 的 PageObject——两家 API 别混"""
    bitmap = pdfium_page.render(scale=scale)
    pil = bitmap.to_pil()
    import io
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


def _pdf_page_images(full_path: Path, scale=2.0):
    """逐页产出 (页码, PNG字节)。pypdfium2 打开文档逐页渲染——生成器省内存"""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(full_path))
    try:
        for i in range(len(doc)):
            yield i, _render_page_image(doc[i], scale)
    finally:
        doc.close()

def _pdf_embedded_images(full_path: Path):
    """⑦2b 修复关键：抽 PDF 里嵌入的原始图片（截图拼贴 PDF 的表格截图）。
    嵌入原图 = 最高分辨率的源头；页面渲染是它的缩放贴图，缩放会放大 OCR
    识别盲区（实测：原生分辨率漏识瘦字符「1」，1.5x 放大即出）。
    用 pdfplumber 的 stream 取原始字节，仅收 PNG/JPEG。失败返回空表"""
    try:
        import pdfplumber
        out = []
        with pdfplumber.open(str(full_path)) as pdf:
            for page in pdf.pages:
                for img in page.images:
                    stream = img.get("stream")
                    if stream is None:
                        continue
                    try:
                        data = stream.get_data()
                    except Exception:
                        continue
                    # PNG 头 / JPEG 头嗅探（有些流解出来还是压缩态，跳过）
                    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
                        out.append(data)
        return out
    except Exception:
        return []


def _ocr_multi_scale(png_bytes, scales=(1.0, 1.5)):
    """双尺度 OCR + 框结果合并：RapidOCR 对原生分辨率的瘦字符（如「1」）有
    识别盲区，放大 1.5 倍重扫可补回。两轮框按几何 IoU 去重（同一位置同文字
    只留一次），坐标统一归一化回 1.0 尺度，供 RapidTable / 聚类共用"""
    from PIL import Image
    import io
    # B2 修（error.md）：EMF/WMF 等格式在无 handler 的平台 Image.open 直接抛，
    # 原样上浮会毁整篇 docx——隔离成空结果，调用方按纯装饰图跳过。
    # load() 强制解码：PIL 惰性，截断文件拖到 load 才抛，一并关进本闸
    try:
        base = Image.open(io.BytesIO(png_bytes))
        base.load()
    except Exception:
        return []
    merged = []  # [(归一化中心x, 归一化中心y, w, h, text, conf)]
    for s in scales:
        if s == 1.0:
            img_bytes = png_bytes
        else:
            resized = base.resize((int(base.width * s), int(base.height * s)), Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            img_bytes = buf.getvalue()
        boxes = _ocr_page_boxes(img_bytes)
        if not boxes:
            continue
        for b in boxes:
            xs = [p[0] / s for p in b[0]]
            ys = [p[1] / s for p in b[0]]
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            # 去重（v2.1 修）：按【位置】判重——两框中心距离 < 半格即视为
            # 同一目标。文字相同跳过；文字不同（多尺度识别差异，如「工时」
            # vs「阳工」）保置信度高者——v1 只按"同位置同文字"判重，识别
            # 差异的两个框全留下，表头出现「阳工 工时」双写
            dup = False
            for idx, m in enumerate(merged):
                if abs(m[0] - cx) < max(w, 8) and abs(m[1] - cy) < max(h, 8):
                    if b[2] > m[5]:  # 新框置信度更高 → 顶掉旧框
                        merged[idx] = (cx, cy, w, h, b[1].strip(), b[2])
                    dup = True
                    break
            if not dup:
                merged.append((cx, cy, w, h, b[1].strip(), b[2]))
    # 还原成 _ocr_page_boxes 同构格式 [[4点坐标, 文字, 置信度], ...]
    out = []
    for cx, cy, w, h, text, conf in merged:
        pts = [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2],
               [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]]
        out.append([pts, text, conf])
    return out

def _ocr_page_boxes(png_bytes):
    """RapidOCR 识别并返回框级结果 [[4点坐标, 文字, 置信度], ...]（表格检测和
    RapidTable 都要吃框级数据，不能只拿拼好的纯文本）。失败或没认出字返回 None。
    rapidocr 3.x 返回单对象 RapidOCROutput：.boxes 是 4 点坐标数组、
    .txts / .scores 是元组——这里摊平回旧包的 [[坐标, 文字, 置信度], ...]
    列表，下游 _ocr_multi_scale / _looks_like_table / RapidTable 沿用旧格式零改动"""
    try:
        out = _get_ocr()(png_bytes)
        if out is None or out.boxes is None or len(out.boxes) == 0:
            return None
        return [[b.tolist(), t, float(s)]
                for b, t, s in zip(out.boxes, out.txts, out.scores)]
    except Exception:
        return None


def _looks_like_table(boxes):
    """⑦2b 表格版式检测（不数字数）：X 聚成 ≥3 列簇 + 框数 ≥6 + 行对齐约束。
    纯文字页的框 X 基本全对齐（1 簇）；表格页的字框天然按列对齐。启发式，
    宁可多报给 RapidTable 验证——但 ⑦5 实测架构图（slide7）会骗过纯 X 聚类，
    故加第二维约束：真表格的行内框必须落在列位置上（行×列网格对齐），
    架构图/流程图的标签散乱不齐列"""
    if not boxes or len(boxes) < 6:
        return False
    # 每个框取中心 X：RapidOCR 框是 4 点坐标 [[x,y]×4]（不是 [x1,y1,x2,y2]），
    # 取 4 点 X 的均值当中心
    xs = sorted(sum(p[0] for p in b[0]) / len(b[0]) for b in boxes)
    if len(xs) < 2:
        return False
    # 聚类阈值修正（⑦5 实测踩坑）：原 span/8 在"页宽大列距小"的紧凑表格图
    # 上失效——WPS 表格图 955px 宽 9 列、列距 ~100px < span/8=103px，9 列
    # 被并成 2 簇判非表格，整表静默丢失（研发部效率对比表就这么丢的）。
    # 改自适应：对相邻 X 间隔做统计，取"大间隔"（≥中位间隔 3 倍且 ≥8px）为
    # 列界——列距天然均匀，真分界处的间隔显著大于列内抖动
    gaps = [xs[i] - xs[i-1] for i in range(1, len(xs))]
    gaps.sort()
    med_gap = gaps[len(gaps) // 2] if gaps else 0
    gap_threshold = max(med_gap * 3, 8)
    col_edges = [xs[0]]
    for i in range(1, len(xs)):
        if xs[i] - xs[i-1] > gap_threshold:
            col_edges.append(xs[i])
    if len(col_edges) < 3:
        return False
    # 行框数众数判据（⑦5 slide7 实测三轮淘汰撞列法后定稿）：真表格每行
    # 一框一列，行框数众数 ≈ 列数（表格图 9 列 → 众数 9）；架构图/流程图
    # 的标签垂直堆叠，行框数众数只有 2-3。判据：众数 ≥3 才进 RapidTable
    # （3 列小表——名称|数量|金额——行框数众数 3，不能误杀）。
    # 坑：statistics.mode 无唯一众数时抛 StatisticsError 会炸整条抽取链——
    # 用 Counter.most_common 平局取任一，恒不抛
    from collections import Counter
    def _center(b):
        pts = b[0]
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    centers = sorted((_center(b) for b in boxes), key=lambda c: c[1])
    heights = sorted(max(p[1] for p in b[0]) - min(p[1] for p in b[0]) for b in boxes)
    row_thresh = max(heights[len(heights) // 2] * 0.6, 8)
    rows, cur_row = [], [centers[0]]
    for c in centers[1:]:
        if c[1] - cur_row[-1][1] <= row_thresh:
            cur_row.append(c)
        else:
            rows.append(cur_row)
            cur_row = [c]
    rows.append(cur_row)
    if len(rows) < 2:
        return False
    counts = [len(r) for r in rows if r]
    mode = Counter(counts).most_common(1)[0][0]
    return mode >= 3


_table_engine = None
_table_lock = threading.Lock()


def _get_table():
    """RapidTable 单例（首次用才下模型 7.4MB，之后离线复用）"""
    global _table_engine
    with _table_lock:
        if _table_engine is None:
            from rapid_table import RapidTable
            _table_engine = RapidTable()
    return _table_engine


def _html_table_to_markdown(html):
    """RapidTable 输出的 <td> HTML 表转 Markdown 管道表（现有 chunk_text 的
    表格分支只认 | 开头的管道表）。含 rowspan/colspan 的合并信息 HTML 里有，
    但管道表表达不了——后续 ⑦4b/⑦4c 统一转「列名：值」序列化时再处理，
    这里先保真转换结构"""
    import re
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    if not rows:
        return None
    # 先按行抽单元格
    all_rows = []
    for row in rows:
        cells = [c.strip().replace("|", "\\|") for c in re.findall(r"<td.*?>(.*?)</td>", row, re.S)]
        if cells:
            all_rows.append(cells)
    if not all_rows:
        return None
    # SLANet 幻影列剔除：某列在所有行都空（右侧留白被误判成列）→ 整列删掉。
    # 从最右往左扫，只剔尾部连续全空列（中间隔空列不动，那是真合并单元格留下的）
    n_cols = max(len(r) for r in all_rows)
    while n_cols > 1 and all(len(r) >= n_cols and r[n_cols - 1] == "" for r in all_rows):
        all_rows = [r[:n_cols - 1] for r in all_rows]
        n_cols -= 1
    lines = []
    for i, cells in enumerate(all_rows):
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + "---|" * len(cells))
    return "\n".join(lines) if lines else None


def _box_grid_table(boxes, cell_bboxes, logic_points):
    """⑦2b 修复（v2.3 定稿）：全 OCR 几何——列切线 = 框 X 聚类（文字框天然
    按列堆叠，大间隙=列界；实测比 SLANet cell_bboxes 聚合准，后者曾把
    功能+功能模块 并列、无表头图偏移），行切线 = 框 Y 聚类（行距规整）。
    合并单元格的值落锚点行；全空幻影列剔除；无表头表全按数据行。纯本地"""
    # 列切线：X 中心聚类，阈值 60px（截图表格列间距远大于此）。
    # 跨列表头剔除（2026-09-14 坑15）：「2025年度」跨 4 列的标签框中心恰好
    # 落在 Q2/Q3 之间，被当独立列边 → 数据行 Q2Q3 挤一格（销售部 | 92 91 |）。
    # 判据：框宽 > 框宽中位数 × 2.5 = 跨列标签（数据格框宽 ≈ 列宽，跨列标签
    # 宽 ≥2 列）——先聚类一遍算出框宽中位，再剔除宽框重聚类
    def _bw(b):
        return max(p[0] for p in b[0]) - min(p[0] for p in b[0])
    widths = sorted(_bw(b) for b in boxes)
    med_w = widths[len(widths) // 2] if widths else 0
    col_boxes = [b for b in boxes if _bw(b) <= med_w * 2.5] if med_w else boxes
    xs_sorted = sorted((min(p[0] for p in b[0]) + max(p[0] for p in b[0])) / 2 for b in col_boxes)
    x_edges = []
    for i in range(1, len(xs_sorted)):
        if xs_sorted[i] - xs_sorted[i - 1] > 60:
            x_edges.append((xs_sorted[i - 1] + xs_sorted[i]) / 2)
    n_c = len(x_edges) + 1
    # 行切线：Y 中心聚类，阈值 = 框高中位数 × 0.6
    heights = sorted(max(p[1] for p in b[0]) - min(p[1] for p in b[0]) for b in boxes)
    med_h = heights[len(heights) // 2] if heights else 20
    row_thresh = max(med_h * 0.6, 8)
    pts = sorted(boxes, key=lambda b: (min(p[1] for p in b[0]) + max(p[1] for p in b[0])) / 2)
    rows = []
    for b in pts:
        cy = (min(p[1] for p in b[0]) + max(p[1] for p in b[0])) / 2
        if rows and cy - rows[-1]["cy_last"] <= row_thresh:
            rows[-1]["boxes"].append(b)
            rows[-1]["cy_last"] = cy
        else:
            rows.append({"boxes": [b], "cy_last": cy})
    if len(rows) < 2 or n_c < 2:
        return None
    def which(v, edges):
        for i, e in enumerate(edges):
            if v < e:
                return i
        return len(edges)
    # 先按行列铺开所有行，再剔幻影列（v2.2）：①全空列删（含表头占位符
    # 「列N」不算内容——v2.1 首版被占位符骗过）②无表头表处理：首行不含
    # 表头特征词（功能/模块/说明/工时/备注/名称/类型/编号/日期）→ 不设
    # 表头行，全部按数据行输出（实测会员管理表无表头，首行就是数据）
    grid_rows = []
    for row in rows:
        cells = [""] * n_c
        for b in sorted(row["boxes"], key=lambda b: min(p[0] for p in b[0])):
            xs = [p[0] for p in b[0]]
            c = min(which((min(xs) + max(xs)) / 2, x_edges), n_c - 1)
            cells[c] = (cells[c] + " " + b[1].strip()).strip()
        if any(cells):
            grid_rows.append(cells)
    if len(grid_rows) < 2:
        return None
    keep = [c for c in range(n_c) if any(r[c] for r in grid_rows)]
    grid_rows = [[r[c] for c in keep] for r in grid_rows]
    n_c = len(keep)
    HEADER_HINTS = ("功能", "模块", "说明", "工时", "备注", "名称", "类型", "编号", "日期")
    first_is_header = sum(1 for c in grid_rows[0] if any(h in c for h in HEADER_HINTS)) >= 2
    lines = []
    for ri, cells in enumerate(grid_rows):
        if ri == 0 and first_is_header:
            header = [c if c else f"列{i+1}" for i, c in enumerate(cells)]
            lines.append("| " + " | ".join(h.replace("|", "\\|") for h in header) + " |")
            lines.append("|" + "---|" * n_c)
        else:
            lines.append("| " + " | ".join((c or " ").replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines) if len(lines) > 2 else None



def _table_page_local(png_bytes, boxes):
    """⑦2b 主链路 (a)：RapidTable 还原表格结构 → Markdown 管道表。
    修复史（2026-09-12）：HTML 行分组对复杂合并表不可靠 → 框坐标启发式
    三轮失败 → 定稿：cell_bboxes+logic_points 切线聚合重排（_box_grid_table，
    纯本地）为主路；HTML 直转为备路。SLANet 几何输出是准的，丢的只是
    HTML 渲染层的合并信息"""
    try:
        import numpy as np
        ocr_results = [(
            np.array([b[0] for b in boxes]),
            tuple(b[1] for b in boxes),
            tuple(b[2] for b in boxes),
        )]
        out = _get_table()(png_bytes, ocr_results=ocr_results)
        if not out.pred_htmls or "<td" not in out.pred_htmls[0]:
            return None
        # 主路：切线聚合重排（实测积分表 31 行错 16 → 工时全部归位）
        if out.cell_bboxes is not None and out.logic_points is not None:
            md = _box_grid_table(boxes, out.cell_bboxes, out.logic_points)
            if md:
                return md
        # 备路：HTML 直转 + 质量闸门（空格率>40% = 网格对位失败 → 触发视觉兜底）。
        # 注意 "| a | b |".split("|") 首尾有两个语法边界空串，必须 [1:-1] 剥掉，
        # 否则满表也会被误判高空格率（实测简单表 67% 假空率）
        md = _html_table_to_markdown(out.pred_htmls[0])
        if not md:
            return None
        data_lines = [l for l in md.split("\n") if l.startswith("|") and "---" not in l][1:]
        if data_lines:
            empty_cells = 0
            total_cells = 0
            for l in data_lines:
                cells = l.split("|")[1:-1]
                empty_cells += sum(1 for c in cells if not c.strip())
                total_cells += len(cells)
            if total_cells and empty_cells / total_cells > 0.4:
                return None
        return md
    except Exception:
        return None


def _table_page_vision(png_bytes, chat_key, chat_url):
    """⑦2b 兜底链路 (b)：视觉 LLM 直读表格页 → Markdown 管道表。
    复用 ⑦2 的视觉调用，只换提示词。失败返回 None"""
    if not chat_key:
        return None
    try:
        import base64
        b64 = base64.b64encode(png_bytes).decode()
        payload = json.dumps({
            "model": "qwen3.8-plus",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "把这张图片里的表格完整转成 Markdown 管道表（|列1|列2|格式），合并单元格的值在覆盖的每一行都重复写出。只输出表格，不要解释。"},
                ],
            }],
        }).encode("utf-8")
        req = urllib.request.Request(
            (chat_url or "").rstrip("/") + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + chat_key},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"].strip()
        # 校验输出真的是管道表（LLM 偶尔不听话）
        return content if "|" in content and content.count("\n") >= 1 else None
    except Exception:
        return None

def _ocr_page_vision(png_bytes, chat_key, chat_url):
    """API 兜底链路：图片 base64 塞进多模态消息发聊天侧视觉模型（qwen3.8-plus）。
    没配聊天 key / 请求失败返回 None（调用方当这页没识别出来，不硬扛）"""
    if not chat_key:
        return None
    try:
        import base64
        b64 = base64.b64encode(png_bytes).decode()
        payload = json.dumps({
            "model": "qwen3.8-plus",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "识别这张图片里的全部文字，按原布局逐行输出，不要解释。"},
                ],
            }],
        }).encode("utf-8")
        req = urllib.request.Request(
            (chat_url or "").rstrip("/") + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + chat_key},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
def ocr_pdf_pages(full_path: Path, chat_key=None, chat_url=None):
    """⑦2+⑦2b 编排。路径分派（2026-09-12 定稿）：
    ① PDF 内有嵌入原图（截图拼贴型 PDF——表格截图直接贴页）→ 逐嵌入图处理。
       嵌入图是 PDF 字节流里自带的原始分辨率图，比页面渲染缩放图清晰，
       OCR 识别率高（实测：渲染图漏识「1」，嵌入原图 1.5x 稳定识别）
    ② 无嵌入图（真扫描件）→ pypdfium2 页面渲染逐页处理
    每张图/页内：双尺度 OCR（原生+1.5x，补瘦字符盲区）→ 表格判定（版式
    特征，框 X 聚簇，不数字数）→ 表格走 RapidTable+切线重排（本地），
    失败才视觉兜底；普通页纯文本 + <200字视觉兜底"""
    def process_image(png_bytes):
        """串行页处理薄壳。2026-09-27 修：原复制版漏写 return text——
        纯扫描 PDF（无嵌入图）的非表格页文本全部静默丢失，None 被
        调用方 if t: 过滤。与并行版 _process_ocr_boxes 统一实现，防再分叉"""
        return _process_ocr_boxes(png_bytes, _ocr_multi_scale(png_bytes), chat_key, chat_url)
    def _process_ocr_boxes(png_bytes, boxes, chat_key, chat_url):
        """OCR 已在外层并行完成，这里串行做表格重排/兜底（onnx 不抢线程池）"""
        if not boxes:
            # 2026-09-27 B5 修（OCR 永远主通道）：OCR 零结果才试云端
            # 视觉兜底；云端也失败（无 key/请求失败返 None）才放弃本页
            vis = _ocr_page_vision(png_bytes, chat_key, chat_url)
            return vis.strip() if vis else ""
        if _looks_like_table(boxes):
            table_md = _table_page_local(png_bytes, boxes)
            if not table_md:
                table_md = _table_page_vision(png_bytes, chat_key, chat_url)
            return table_md or "\n".join(b[1] for b in boxes)
        text = "\n".join(b[1] for b in boxes).strip()
        if len(text) < OCR_FALLBACK_MIN_CHARS:
            better = _ocr_page_vision(png_bytes, chat_key, chat_url)
            if better and len(better) > len(text):
                text = better
        return text

    texts = []
    embedded = _pdf_embedded_images(full_path)
    if embedded:
        # 性能B（2026-09-13 实测定稿）：OCR 阶段图间并行（ThreadPool——
        # onnxruntime 推理时释放 GIL，实测 2 图 8.7-12.4s → 6.3-6.6s），
        # 表格重排/视觉兜底仍串行（RapidTable 与 RapidOCR 同为 onnx，
        # 同时推理会争抢 onnxruntime 全局线程池反而退化，实测 12.5s 比
        # 串行 9.4s 还慢——只并行 OCR 不并行重排，唯一不退化的组合）
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(embedded))) as ex:
            all_boxes = list(ex.map(_ocr_multi_scale, embedded))
        for img_bytes, boxes in zip(embedded, all_boxes):
            t = _process_ocr_boxes(img_bytes, boxes, chat_key, chat_url)
            if t:
                texts.append(t)
    else:
        for _i, png in _pdf_page_images(full_path):
            t = process_image(png)
            if t:
                texts.append(t)
    full = "\n\n".join(t for t in texts if t).strip()
    return full if full else None
def pour_into_bucket(item, item_len, bucket, bucket_len, chunks):
    """把一个段落/句子碎片装进桶；桶装不下时封桶成块、带 50 字重叠开新桶。
    返回 (bucket, bucket_len)：桶必须通过返回值交还调用者（闭包外重新赋值不生效）"""
    # 贪心装桶：装得下就只管 append（块大上下文全，块少 embedding 省调用）
    if bucket_len + item_len + 1 > CHUNK_SIZE and bucket:  # 装不下且桶里有货 -> 先封桶
        chunk = "\n\n".join(bucket)     # 封桶：桶里的段落用空行拼成一块
        chunks.append(chunk)
        # 开新桶：从刚封的块末尾抄 50 字起头——跨边界那句话在两块里各有一份
        # 完整拷贝，检索时命中哪块都能读到完整句子
        tail = chunk[-CHUNK_OVERLAP:]
        bucket = [tail]
        bucket_len = len(tail)
    # 这两行在 if 外：无论封没封桶，这次来的东西都装进"当前"桶
    bucket.append(item)
    bucket_len += item_len + 1
    return bucket, bucket_len
def chunk_text(text):
    """把一段长文字切成 ~500 字的块列表（"往桶里装段落"算法）。

    1. 按空切段落（空行 = 天然语义边界）
    2. 段落逐个装桶；桶满封桶成块，新桶带 50 字重叠
    3. 表格段落整表成块（巨表按行组切，每组带表头）
    4. 超长段落先切句，单句还超就硬切，碎片也走装桶
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks = []
    bucket = []
    bucket_len = 0
    for para in paragraphs:

        # 表格感知：Markdown 表格（| 开头、行间无空行）被按空切段成一个段落。
        # 表格是结构化数据，从中间劈开会把表头和数据行分家（两块语义都残废，
        # 检索"谁负责的"命不中含人名的行）。规则：小表整表一块；巨表按行组
        # 切（绝不劈行内），每组复抄表头
        # 表格判定（2026-09-14 引导行配套改 + OCR 表兼容）：原
        # para.startswith("|") 被引导行打破；只认 "\n|---" 又漏了 OCR 表
        # （图片表还原路出的管道表无分隔行，|部门||Q1|… 直接数据行起）。
        # 双条件：有分隔行 = 表；无分隔行但连续 ≥3 行 | 开头 = 表（OCR 表
        # 的结构铁证是管道列对齐，分隔行只是 Markdown 语法装饰）
        if "\n|---" in para:
            is_table = True
        else:
            pipe_lines = sum(1 for l in para.split("\n") if l.strip().startswith("|"))
            is_table = pipe_lines >= 3
        if is_table:
            # ⑨ 三轮步4 补充：巨表判定从 TABLE_MAX 字数改为行数——300 行短列表
            # 仅 5500 字（<12000），原判定走"小表整表"分支，答案行稀释 1/300。
            # 库内 1000-12000 字表格块 1601 个都是这个 gap。行数判据：数据行
            # > TABLE_ROW_GROUP 才切（≤10 行的小表保持整表一块）
            data_line_cnt = sum(1 for l in para.split("\n") if l.strip().startswith("|"))
            if bucket:
                # 桶里攒着普通段落先封掉——普通段落和表格分家
                chunks.append("\n\n".join(bucket))
                bucket, bucket_len = [], 0
            # 孤儿标记折并（⑦5 千遍检查端到端逮到）：表格前的短标记段
            # （「## 幻灯片 N」「## Sheet：X」）单独成块会稀释检索。统一折进
            # 表格块首——条件：前一块是短纯标记（##/【 开头、无换行、<60字）。
            # 放块首不放块尾：块尾非 | 行会混进行组切分
            _marker = None
            if chunks and isinstance(chunks[-1], str):
                _prev = chunks[-1]
                # 前一块是短标记/短标题（无句末标点、<60字、非表格）→ 折进
                # 当前表格块首。覆盖三种形态：纯标记（## Sheet：X）、标记+
                # 短标题（## 幻灯片 N + 页题）、连续双标记。有句号的正文段不折
                if (len(_prev) < 60 and "---|" not in _prev
                        and not re.search(r"[。！？；]", _prev)):
                    _marker = chunks.pop()
            table_lines = para.split("\n")
            # 巨表上限：bge-m3 单条上限 8192 token（中文约 16000 字），超了 API
            # 直接 400 拒收（实测 94KB 映射总表就炸过）。安全线 12000 字留足余量
            TABLE_MAX = 12000
            if _marker and len(para) + len(_marker) + 2 <= TABLE_MAX:
                para = _marker + "\n" + para
                table_lines = para.split("\n")
            if len(para) <= TABLE_MAX and data_line_cnt <= TABLE_ROW_GROUP:
                # 四轮步2 线性化已回退（用户拍板 B，2026-09-16）：实测净回退 1.4pp
                # （73.8%<75.2%），多级表头表被渲染成「标题=值」垃圾句（36% 块污染，
                # 301-400 段 -7pp）——线性化收益（简单表 +1pp）盖不过污染伤害。
                # 回退为步1 管道形态：小表整表一块，原文不动
                # 冲刺①：注释插分隔行之后（头部）——不能放块尾：_slim 拍嵌入指纹
                # 从头部截 3000-6000 字，尾注在超预算长块上会被切掉（advisory 逮住）
                _annot = _header_annotation(table_lines[:8])
                if _annot:
                    _sep_i = next((i for i, l in enumerate(table_lines[:8]) if "---|" in l), None)
                    _ins = (_sep_i + 1) if _sep_i is not None else 2  # OCR 表无分隔行→首2行后
                    para = "\n".join(table_lines[:_ins] + [_annot] + table_lines[_ins:])
                chunks.append(para)  # 小表格：整表 = 一块
                continue
            # ⑨ 三轮步4：中表/巨表统一走行组切分——数据行 >TABLE_ROW_GROUP 即切，
            # 每 TABLE_ROW_GROUP 行一组带表头（原仅 >TABLE_MAX 的巨表才切）
            # 巨表头定位（坑14 修复，2026-09-14）：不能假设表头在前两行——
            # ①引导行在前（table_lines[0]=引导行 [1]=表头 [2]=数据行）②OCR 表
            # 无分隔行。动态找第一个含 ---| 的行；找不到（无分隔行 OCR 表）退
            # 首 2 行（OCR 表头 2 行是双级表头）
            sep_idx = next((i for i, l in enumerate(table_lines[:8]) if "---|" in l), None)
            head_n = (sep_idx + 1) if sep_idx is not None else 2
            header = "\n".join(table_lines[:head_n])  # 引导行+表头+分隔，跟着每一组
            # 四轮步2 线性化已回退（用户拍板 B，2026-09-16）——数据行恢复管道原样
            # （线性化实测净回退 1.4pp：多级表头污染 36% 块，301-400 段 -7pp，
            #  简单表收益仅 +1pp。垃圾句块签名「## Sheet：X=Y」6299 块已入档）
            if _marker:
                header = _marker + "\n" + header
            # 冲刺①：注释并入 header（表头区每组复带，且天然落头部——_slim
            # 嵌入截断从头部截，头部注释永不被切掉；行组内块本身 ≤12000 字）
            _annot = _header_annotation(table_lines[:8])
            if _annot:
                header = header + "\n" + _annot
            # ⑨ 三轮步4：行组切块——数据行每 TABLE_ROW_GROUP 行一组、每组复带表头
            group, group_len = list(table_lines[:head_n]), len(header)
            for line in table_lines[head_n:]:
                data_rows = len(group) - head_n
                too_long = group_len + len(line) + 1 > TABLE_MAX
                if (data_rows >= TABLE_ROW_GROUP or too_long) and len(group) > head_n:
                    chunks.append(header + "\n" + "\n".join(group[head_n:]))
                    group, group_len = list(table_lines[:head_n]), len(header)
                group.append(line)
                group_len += len(line) + 1
            if len(group) > head_n:               # 收尾：最后不满一组的数据行也封
                chunks.append(header + "\n" + "\n".join(group[head_n:]))
            continue
        if len(para) > CHUNK_SIZE:
            # 切句用 re.split + 零宽断言（每个句末标点正后方下刀），全文线性扫一遍。
            # 原 findall 写法在"整段没有中文句末标点"的段落上会平方级回溯
            # （2 万字段落 ≈ 2 亿次比较，实测 1260 文件切 49 秒），且会丢掉
            # 最后一个标点之后的尾巴；split 又快又一句不丢
            sentences = [s for s in re.split(r"(?<=[。！？])", para) if s]
            if not sentences:  # 整段没标点：整段当"一句话"交给硬切，防整段丢失
                sentences = [para]
            # 单句仍超长 -> 按 CHUNK_SIZE 硬切，相邻片留重叠
            pieces = []
            for s in sentences:
                while len(s) > CHUNK_SIZE:
                    pieces.append(s[:CHUNK_SIZE])
                    s = s[CHUNK_SIZE - CHUNK_OVERLAP:]  # 从第 450 字起，前 50 字和上一片重叠
                if s:
                    pieces.append(s)
            for piece in pieces:
                bucket, bucket_len = pour_into_bucket(piece, len(piece), bucket, bucket_len, chunks)
            continue

        # 常规段落：直接装桶
        bucket, bucket_len = pour_into_bucket(para, len(para), bucket, bucket_len, chunks)

    # 最后那桶零头也要封（不封就丢了）
    if bucket:
        chunks.append("\n\n".join(bucket))

    return chunks
def build_chunks(root: Path, files, chat_key=None, chat_url=None):
    """对一份扫描清单逐个读内容+切块。返回 [{file, seq, text}]：哪个文件、第几块、内容"""
    all_chunks = []
    # 并行读的收益不在磁盘而在 Windows Defender：每个 open() 都要过实时杀毒，
    # 串行读 = 一个文件的杀毒等完才开下一个；并行读让等待互相重叠
    def read_one(f):
        return f["path"], read_file_text(root, f["path"], chat_key, chat_url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as read_pool:
        results = list(read_pool.map(read_one, files))

    for rel_path, text in results:
        if text is None:
            continue  # 读不了的文件跳过，不拖累整批
        pieces = chunk_text(text)
        # seq 序号之后检索命中时能按"文件名+第几块"报出处
        for seq, piece in enumerate(pieces):
            all_chunks.append({
                "file": rel_path,
                "seq": seq,
                "text": piece,
            })
    return all_chunks
# ══════════════════════════════════════════════════════════════
# 模块 3b：异步分块任务（后台批缓冲 + 进度轮询）
# ══════════════════════════════════════════════════════════════

CHUNK_BATCH_SIZE = 1000  # 分块写库缓冲批量：满 1000 块一个事务写库（摊薄磁盘同步成本，内存恒定 ≤几MB）
_chunk_jobs = {}
_chunk_jobs_lock = threading.Lock()
def _run_chunk_job(scan_id, root, files, chat_key=None, chat_url=None):
    """后台线程跑的分块任务（分批缓冲写）。

    读文件 -> 切块 -> 攒缓冲区；满 1000 块写库一次（一个事务）-> 清空继续。
    内存占用恒定（≤1000 块 ≈ 几 MB），磁盘写入次数 = 总块数/1000，重启不丢已写批次。
    """
    _lg_task.info("分块任务启动", extra={"scan_id": scan_id, "files": len(files)})  # ⑫3 生命周期
    try:
        chunk_count = 0
        buffer = []
        # 性能定稿①：.xls 预批量转换——pool.map 之前一次 Excel 实例转全部
        # （实测 23个: 逐个起停 2.6s/个=60s → 常驻实例 0.09s/个≈3s），
        # 转换产物传给读取循环直接用（read_file_text 不再单独 COM）
        # 性能B修正（2026-09-13）：xls 也走 parse_cache——先查哪些 xls 缓存
        # 未命中，只对未命中的做批量 COM 转换（此前批量分支绕过缓存，二扫
        # 23 个 xls 全量重转 60s+ 是 167s 的大头）
        xls_files = [f["path"] for f in files if f["path"].lower().endswith(".xls")]
        xls_uncached = [rel for rel in xls_files
                        if cache_get_text((root / rel).resolve()) is None]
        xls_converted = _xls_batch_convert(root, xls_uncached) if xls_uncached else {}
        # ⑦5b 同款：.ppt 预批量转换（PowerPoint COM 起停 ~2.5s/个，N 个
        # .ppt 串行起停是分钟级——与 xls 同构的常驻实例批量模式）
        ppt_files = [f["path"] for f in files if f["path"].lower().endswith(".ppt")]
        ppt_uncached = [rel for rel in ppt_files
                        if cache_get_text((root / rel).resolve()) is None]
        ppt_converted = _ppt_batch_convert(root, ppt_uncached) if ppt_uncached else {}
        # 性能定稿②：读进度实时上报——pool.map 全量提交后 done 在读完前
        # 恒为 0（用户看到假死）。改 submit + as_completed：每读完一个文件
        # done+1，前端"自动分块中 X%"实时可见
        import concurrent.futures as _cf
        # 注意：绝不能用 `with _get_pool() as _p:`——with 退出会 shutdown
        # 全局池（500 根因：第一次分块线程退出关池，第二次扫描 submit 炸
        # RuntimeError: cannot schedule new futures after shutdown）。
        # 全局池就该全局活着，只 submit 不管理生命周期
        _p = _get_pool()
        futures = {}  # ⑦7 编辑事故补回：此行曾被吞（NameError: futures
        # is not defined——语法合法但语义缺失，用户点切块才炸）
        for idx, f in enumerate(files):
            if f["path"] in xls_converted:
                def _read_converted(p=root, r=f["path"], c=xls_converted[f["path"]]):
                    if not c.exists():
                        return None
                    t = extract_xlsx_text(c)
                    # 转换产物写缓存（key=原始 .xls 路径）——下次不再 COM。
                    # ⑦6 补洞：失败也写负缓存（单空格）——转换成功但抽取
                    # 失败的文件，下次扫描不再白白重转一遍 COM（稳态大头）
                    cache_put_text((p / r).resolve(), t if t is not None else " ")
                    return t
                fut = _p.submit(_read_converted)
            elif f["path"] in ppt_converted:
                def _read_ppt(p=root, r=f["path"], c=ppt_converted[f["path"]]):
                    if not c.exists():
                        return None
                    t = extract_pptx_text(c)
                    if t is not None:
                        cache_put_text((p / r).resolve(), t)
                    else:
                        # ⑦6 负缓存补洞（同上）
                        cache_put_text((p / r).resolve(), " ")
                    return t
                fut = _p.submit(_read_ppt)
            else:
                fut = _p.submit(read_file_text, root, f["path"], chat_key, chat_url)
            futures[fut] = f
        done_count = 0
        failed_files = []  # ⑦7 解析失败明因：[{file, reason}]——坑2 病根
        # （哪些文件被跳过用户无感知）。原因从 parse 日志抓不到（线程内
        # 异常被 except 吞），所以这里自己记：text=None 就是"读不出"
        # （具体原因在 rag_parse.log，文件名可 grep）
        for fut in _cf.as_completed(futures):
            f = futures[fut]
            done_count += 1
            try:
                text = fut.result()
            except Exception as e:
                # 读线程异常与"解析为空"互斥登记（⑦7 双重登记 bug 修复：
                # 原版 except 里 append 后 if text is None 又 append——同一
                # 文件进两次且原因被覆盖。改 try/except/else 各记一次）
                text = None
                failed_files.append({"file": f["path"], "reason": f"读线程异常: {type(e).__name__}"})
            else:
                if text is None:
                    # 解析失败的明因分类：负缓存命中=已知失败；其余=解析返回空
                    # （损坏/加密/编码，细因在 rag_parse.log 按文件名查）
                    cached = cache_get_text((root / f["path"]).resolve())
                    reason = "此前解析失败（负缓存）" if cached == " " else "解析为空（损坏/加密/无文本层，详见 rag_parse.log）"
                    failed_files.append({"file": f["path"], "reason": reason})
            if text is not None:
                # ⑨ 五轮 B：表格直采——xlsx/xls 结构化网格直接入 tables（跳过
                # 管道文本反猜，多级表头错位根治）；csv 从管道文本重切网格（本就
                # 无表头层级，损耗为零）；其余格式保留 _extract_tables_to_db
                # 文本反解（docx/pdf 表格来自文档模型，先不动）
                low = f["path"].lower()
                if low.endswith((".xlsx", ".xls", ".csv")):
                    try:
                        _store_grid_tables(scan_id, f["path"], root, text, low)
                    except Exception as _te:
                        _pf.warning("tables 直采跳过 %s: %s", f["path"], type(_te).__name__)
                elif low.endswith((".docx", ".pdf")):
                    try:
                        _extract_tables_to_db(scan_id, f["path"], text)
                    except Exception as _te:
                        _pf.warning("tables 抽取跳过 %s: %s", f["path"], type(_te).__name__)
                pieces = chunk_text(text)
                for seq, piece in enumerate(pieces):
                    buffer.append((scan_id, f["path"], seq, piece))
                chunk_count += len(pieces)
            if len(buffer) >= CHUNK_BATCH_SIZE:
                db_append_chunks(buffer)
                buffer = []
            if done_count % 5 == 0:
                time.sleep(0.001)
                with _chunk_jobs_lock:
                    _chunk_jobs[scan_id]["done"] = done_count
                    # ⑦6 进度带格式名：用户看到"分块中 45/268"时不知道在啃
                    # 什么格式——COM 转换（doc/xls/ppt）慢时尤其需要（30s 停
                    # 留在同一个数字会像卡死）。记最近完成的文件名+扩展名，
                    # 前端轮询展示"正在处理 xxx.docx"
                    _chunk_jobs[scan_id]["current_file"] = f["path"]
                    _chunk_jobs[scan_id]["current_fmt"] = (f["path"].rsplit(".", 1)[-1] or "?").lower()
        # xls 批量转换的临时文件清理
        for tmp in xls_converted.values():
            try:
                tmp.unlink()
            except OSError:
                pass
        # 缓冲区最后一批不满的零头也要写库（不写就丢）
        if buffer:
            db_append_chunks(buffer)
        with _chunk_jobs_lock:
            _chunk_jobs[scan_id]["done"] = len(files)
            _chunk_jobs[scan_id]["status"] = "done"
            _chunk_jobs[scan_id]["chunk_count"] = chunk_count
        _lg_task.info("分块任务完成", extra={"scan_id": scan_id, "chunks": chunk_count})
        # 冲刺③（2026-09-21 词典不写死）：分块完成即重建查询词典——
        # 换库扫描后词典自动跟新库（语言对照块挖掘），零人工零停机
        try:
            _rebuild_query_dict(scan_id)
        except Exception as _qe:
            _lg_task.warning('词典重建跳过', extra={'scan_id': scan_id, 'exc': str(_qe)[:80]})  # ⑫3
        try:
            with _db_lock:
                _na = _db.execute("SELECT COUNT(*) FROM chunks WHERE scan_id=? AND text LIKE '%表头中文对照%'", (scan_id,)).fetchone()[0]
            print(f'[冲刺①] scan{scan_id} 注释块数: {_na}', flush=True)
        except Exception as _e:
            print(f'[冲刺①] 注释计数失败: {_e}', flush=True)
    except Exception as e:
        _lg_task.error("分块任务失败", extra={"scan_id": scan_id, "exc_type": type(e).__name__, "exc": str(e)[:200]})  # ⑫3
        _chunk_jobs[scan_id]["error"] = str(e)


def _chat_from_request(request):
    """⑦2 BYOK：前端随扫描/分块请求带 X-Chat-Key/X-Chat-Url 头（聊天侧配置，
    OCR 视觉兜底用），传给后台分块线程，用完即弃不落盘。
    十三修：同 _vec_from_request 的 None 防护"""
    if request is None:
        return None, None
    return request.headers.get("x-chat-key"), request.headers.get("x-chat-url")

@app.post("/api/kb/{scan_id}/chunk")
def api_chunk_start(scan_id: int, request: Request = None):
    """启动某次扫描的分块任务。立刻返回（不等待），后台线程慢慢干"""
    chat_key, chat_url = _chat_from_request(request)
    scan = db_find_scan_by_id(scan_id)
    if not scan:
        return {"error": f"没有这次扫描: {scan_id}"}
    root = Path(scan["root"])
    files = db_get_scan_files(scan_id)
    # 先清旧块（重跑分块时旧块全部作废）
    with _db_lock:
        _db.execute("DELETE FROM chunks WHERE scan_id = ?", (scan_id,))
        _db.commit()
    with _chunk_jobs_lock:
        _chunk_jobs[scan_id] = {"done": 0, "total": len(files), "status": "running", "chunk_count": None}
    t = _spawn_traced(_run_chunk_job, scan_id, root, files, chat_key, chat_url)  # ⑫0 穿透
    t.start()
    return {"started": True, "total": len(files)}
@app.get("/api/kb/{scan_id}/chunk/status")
def api_chunk_status(scan_id: int):
    """查分块进度。前端每隔一两秒调一次，拿 done/total 显示百分比"""
    with _chunk_jobs_lock:
        job = _chunk_jobs.get(scan_id)
    if job is None:
        # 没在跑？看库里有没有已完成的历史块（可能早就分好了）
        with _db_lock:
            n = _db.execute("SELECT COUNT(*) FROM chunks WHERE scan_id = ?", (scan_id,)).fetchone()[0]
        return {"status": "done" if n > 0 else "not_started", "chunk_count": n, "done": None, "total": None}
    return job
# ══════════════════════════════════════════════════════════════
# 模块 4：SQLite 存储层（scans / files / chunks 三表 + db_* 函数）
# ══════════════════════════════════════════════════════════════

import sqlite3
import time as _time
import numpy as np  # 向量批量运算（余弦矩阵化）+ float32 BLOB 编解码；运算释放 GIL，并发检索不再排队

# 数据库文件位置（2026-09-25 「不准写死路径」+「任何拷贝命令都禁止」）：
# 落点 = 环境变量 RAG_DB_PATH 覆盖（最高优先）→ 平台标准目录（默认）。
#   RAG_DB_PATH 指哪就读哪（原地读，零拷贝零移动，不搬一个字节）；
#   不设 = 按平台标准：win %APPDATA%/RAGAssistant、mac ~/Library/Application
#   Support/RAGAssistant、linux ~/.local/share/RAGAssistant（新机器/新系统用）。
# frozen（打包）与 dev（python server.py）共用同一规则。
# ⛔ 零拷贝铁律（2026-09-16 定案+2026-09-25 重申「任何拷贝命令都禁止」）：
# 代码不做任何 copy/move/迁移——库在哪就在哪原地读。
import sys as _sys

def _db_path():
    """库文件路径（唯一真源）：RAG_DB_PATH 环境变量 > 平台标准目录。
    2026-09-25 用户红线重申：不准任何写死路径——旧库优先判断版已删（违规）。"""
    _env = os.environ.get("RAG_DB_PATH")
    if _env:
        p = Path(_env)
        if p.suffix != ".db":  # 给的是目录 → 拼库名
            p = p / "rag_index.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    # 平台标准目录（_DATA_DIR 供 agent_tools 等目录级引用——env 覆盖时取父目录）
    if _sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "RAGAssistant"
    elif _sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", str(Path.home()))) / "RAGAssistant"
    else:
        d = Path.home() / ".local" / "share" / "RAGAssistant"
    d.mkdir(parents=True, exist_ok=True)
    global _DATA_DIR
    _DATA_DIR = d
    return d / "rag_index.db"

DB_PATH = _db_path()
_DATA_DIR = DB_PATH.parent  # env 覆盖时=env 目录；标准时=平台目录（两路统一取库父目录）

# check_same_thread=False：FastAPI 每次请求可能派不同线程来用同一个连接，
# 配合 _db_lock 保证不打架
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db_lock = threading.Lock()

# WAL：写不阻塞读（后台分块/向量化写库时检索照常跑）；busy_timeout：写撞车等 5s 再报错
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("PRAGMA busy_timeout=5000")

# scans（每扫一次一行）+ files（每个文件一行，scan_id 对上号）
_db.executescript("""
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 编号：自动 1、2、3 往上涨
    root TEXT NOT NULL,                    -- 扫描的根目录
    file_count INTEGER NOT NULL,           -- 文件数
    total_size INTEGER NOT NULL,           -- 总字节数
    elapsed_ms INTEGER NOT NULL,           -- 扫描耗时（毫秒）
    created_at TEXT NOT NULL               -- 扫描时间
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,              -- 属于哪次扫描（对上 scans 表的编号）
    path TEXT NOT NULL,                    -- 相对路径
    size INTEGER NOT NULL,                 -- 字节数
    mtime INTEGER NOT NULL DEFAULT 0,      -- 文件内容最后一次被改的时间（增量更新的关键）
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);
""")

# 老库升级：try 着加列，报"已存在"就忽略（新库建的表本来就带）
try:
    _db.execute("ALTER TABLE files ADD COLUMN mtime INTEGER NOT NULL DEFAULT 0")
except sqlite3.OperationalError:
    pass
_db.commit()

# chunks（块清单）：数量远多于文件（1 文件 ≈ 5~20 块），是 embedding 的原料
_db.executescript("""
CREATE TABLE IF NOT EXISTS chunks (

    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 块的编号（自动 1、2、3 往上涨）
    scan_id INTEGER NOT NULL,              -- 属于哪次扫描（对上 scans 表的编号）
    file TEXT NOT NULL,                    -- 来自哪个文件（相对路径，检索命中后能报出处）
    seq INTEGER NOT NULL,                  -- 这个文件里的第几块（从 0 数，出处更精确）
    text TEXT NOT NULL,                    -- 块的正文（500 字左右的一段文字）
    vector TEXT                            -- 块的向量指纹（BAAI/bge-m3，NULL=还没向量化）
);
""")

# 解析缓存（性能定稿③）：path+mtime+size 三键指纹，二次扫描同文件直接复用
_db.executescript("""
CREATE TABLE IF NOT EXISTS parse_cache (
    path TEXT NOT NULL,                   -- 文件绝对路径（跨 scan 复用）
    mtime INTEGER NOT NULL,               -- 解析时的文件 mtime（变了就失效）
    size INTEGER NOT NULL,                -- 同上（双保险指纹）
    text TEXT NOT NULL,                   -- 解析出的全文（read_file_text 的返回值）
    PRIMARY KEY (path)
);
""")
_db.commit()

# ⑨ 四轮步3（2026-09-16）：tables 元数据库——SQL 查询路的数据源。
# xlsx/csv 扫描入库时把结构化数据（sheet+列名+行）抽出来单独存，
# 精确值题（"科目1002的期末余额"）直接查这张表，不走语义相似度。
# 换库自动适配：重扫时自动重建，零人工配置
_db.executescript("""
CREATE TABLE IF NOT EXISTS tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,              -- 属于哪次扫描
    file TEXT NOT NULL,                    -- 源文件相对路径
    sheet TEXT NOT NULL,                   -- sheet 名（csv 为文件名）
    cols TEXT NOT NULL,                    -- 列名 JSON 数组
    rows_json TEXT NOT NULL,               -- 行数据 JSON（每行一个数组）
    row_count INTEGER NOT NULL             -- 行数（查询路由先看规模）
);
CREATE INDEX IF NOT EXISTS idx_tables_scan ON tables(scan_id);
""")
_db.commit()
# ⑦6 code_version 缓存版本号（挂账销号：解析逻辑升级后旧缓存不失效——改了
# _pptx_table_md 的合并回填，268 个旧缓存还按老逻辑命中，内容错到下版才发现）。
# 机制：列进指纹比对（mtime+size+version 三键），版本不匹配=未命中=重新解析。
# bump 规则：任何 extract_* 解析函数的行为变更（回填逻辑/编码fallback/新格式）
# 都必须 +1。老库升级：ALTER 加列，存量行 version 默认 0 ≠ 当前 1 → 首次
# 扫描全量重解析一次（一次性成本，换来旧脏缓存自动清场）
PARSE_CODE_VERSION = 2  # v2（2026-09-14）：PPT 表格贴图路补页标题引导行（销售部检索失灵根因：页4 贴图表无语义锚，同文件页5 原生表有引导行命中正常）
try:
    _db.execute("ALTER TABLE parse_cache ADD COLUMN code_version INTEGER NOT NULL DEFAULT 0")
    _db.commit()
except Exception:
    pass  # 列已存在（二次启动）——DEFAULT 0 是存量行的版本，比对自然淘汰


def cache_get_text(full_path: Path):
    """缓存命中则返回上次解析的全文，否则 None。⑦6 起指纹四键：
    path+mtime+size+code_version（解析逻辑升级自动失效旧缓存）"""
    try:
        st = full_path.stat()
        with _db_lock:
            row = _db.execute(
                "SELECT text FROM parse_cache WHERE path=? AND mtime=? AND size=? AND code_version=?",
                (str(full_path), int(st.st_mtime), st.st_size, PARSE_CODE_VERSION),
            ).fetchone()
        if not row:
            return None
        return row[0]  # 负缓存（空格）也返回——调用方判 falsy 跳过（B13：删恒等三目）
    except Exception:
        return None


def cache_put_text(full_path: Path, text):
    """解析成功后写缓存（UPSERT）。⑦6 起带 code_version——旧版本行被
    UPSERT 覆盖（同 path 主键），版本指纹自然换代"""
    try:
        st = full_path.stat()
        with _db_lock:
            _db.execute(
                "INSERT INTO parse_cache (path, mtime, size, text, code_version) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, text=excluded.text, code_version=excluded.code_version",
                (str(full_path), int(st.st_mtime), st.st_size, text, PARSE_CODE_VERSION),
            )
            _db.commit()
    except Exception:
        pass  # 缓存写失败不影响主流程


def db_find_scan_by_id(scan_id):
    """按编号找一次扫描的档案（异步分块要用：从编号翻出根目录和文件清单）"""
    with _db_lock:
        row = _db.execute(
            "SELECT id, root FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "root": row[1]}

# 索引：DELETE 按 scan_id 找行，没索引 = 全表逐行翻（块上万行时越来越慢，
# 之前 E 盘超时的帮凶之一）
try:
    _db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_scan ON chunks(scan_id)")
except sqlite3.OperationalError:
    pass


def db_save_scan(root, files, elapsed_ms):
    """把一次扫描的结果存进库。返回新扫描的编号"""
    with _db_lock:
        with _db:  # 事务：几笔账要么全记成功，中途出错全回滚
            cur = _db.execute(
                # 永远用 ? 占位（参数化），绝不把内容拼进 SQL——防注入
                "INSERT INTO scans (root, file_count, total_size, elapsed_ms, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(root), len(files), sum(f["size"] for f in files), elapsed_ms,
                 _time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            scan_id = cur.lastrowid
            _db.executemany(
                "INSERT INTO files (scan_id, path, size, mtime) VALUES (?, ?, ?, ?)",
                [(scan_id, f["path"], f["size"], f.get("mtime", 0)) for f in files],
            )
        return scan_id

def db_append_chunks(rows):
    """追加一批块到 chunks 表（不清旧块——开工时已清过场）。
    rows = [(scan_id, file, seq, text), ...]。一个事务一次 commit，
    把磁盘同步成本摊到整批上"""
    with _db_lock:
        with _db:
            _db.executemany(
                "INSERT INTO chunks (scan_id, file, seq, text) VALUES (?, ?, ?, ?)",
                rows,
            )
    return len(rows)


def db_save_chunks_incremental(scan_id, file_path, pieces):
    """流式写库：追加一个文件的块（不清旧块，只往里加）。
    调用方保证顺序：任务开始时已清空过该 scan_id 的旧块"""
    with _db_lock:
        with _db:
            _db.executemany(
                "INSERT INTO chunks (scan_id, file, seq, text) VALUES (?, ?, ?, ?)",
                [(scan_id, file_path, seq, p) for seq, p in enumerate(pieces)],
            )


def db_save_chunks(scan_id, chunks):
    """把一次分块的结果存进 chunks 表（先清后存：每次分块都是全量重算，
    改了 CHUNK_SIZE 重跑时旧块全部作废）。返回存了多少块"""
    with _db_lock:
        with _db:
            _db.execute("DELETE FROM chunks WHERE scan_id = ?", (scan_id,))
            _db.executemany(
                "INSERT INTO chunks (scan_id, file, seq, text) VALUES (?, ?, ?, ?)",
                chunks,
            )
    return len(chunks)


def db_find_scan_by_root(root):
    """按根目录找最近一次扫描的档案（增量更新翻旧账用）"""
    with _db_lock:
        row = _db.execute(
            "SELECT id, root, file_count, total_size, elapsed_ms, created_at FROM scans WHERE root = ? ORDER BY id DESC LIMIT 1",
            (str(root),),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "root": row[1], "count": row[2], "total_size": row[3],
            "elapsed_ms": row[4], "created_at": row[5]}


# 块预览接口：分块质量肉眼不可见，看到实际内容/边界/有没有劈半句子
# 才能判断分块好不好（调参依据）
@app.get("/api/kb/{scan_id}/chunks")
def api_kb_chunks(scan_id: int, limit: int = 50, offset: int = 0):
    """拉某次扫描的块清单。limit = 最多回多少块（默认50，防一次拉爆），
    offset = 跳过前面多少块（翻页用）。路径参数自动转数字"""
    with _db_lock:
        total = _db.execute(
            "SELECT COUNT(*) FROM chunks WHERE scan_id = ?", (scan_id,)
        ).fetchone()[0]
        rows = _db.execute(
            "SELECT file, seq, LENGTH(text), text FROM chunks WHERE scan_id = ? "
            "ORDER BY file, seq LIMIT ? OFFSET ?",
            (scan_id, limit, offset),
        ).fetchall()
    # 块内容截前 200 字（预览够用，省流量）。⑦7 ext=文件扩展名（大写徽章，
    # 前端格式徽章用——路径里就有，免得前端每个块再 split 一次）
    return {
        "total": total,
        "chunks": [
            {"file": r[0], "seq": r[1], "len": r[2], "preview": r[3][:200],
             "ext": (r[0].rsplit(".", 1)[-1] if "." in r[0] else "").upper()}
            for r in rows
        ],
    }


def db_get_scan_files(scan_id):
    """按扫描编号读回文件清单"""
    with _db_lock:
        rows = _db.execute(
            "SELECT path, size, mtime FROM files WHERE scan_id = ?", (scan_id,)
        ).fetchall()
    return [{"path": r[0], "size": r[1], "mtime": r[2]} for r in rows]


def db_list_scans():
    """列出所有扫描历史（新的排前面）"""
    with _db_lock:
        rows = _db.execute(
            "SELECT id, root, file_count, total_size, elapsed_ms, created_at FROM scans ORDER BY id DESC"
        ).fetchall()
    return [
        {"id": r[0], "root": r[1], "count": r[2], "total_size": r[3],
         "elapsed_ms": r[4], "created_at": r[5]}
        for r in rows
    ]


# ═══ 动态限流器（令牌桶·2026-09-20 开工）═══
# 治实测踩过的坑：评测高频打 embed/rerank 撞 429（坑16 两轮深夜 429 风暴实录）。
# 设计：每秒 N 令牌 + 突发桶容量；取不到令牌就排队（不拒绝——评测要跑完）。
# 自适应：遇 429 自动降速（N 减半，下限 1/s），连续 5 分钟无 429 逐步恢复。
class _TokenBucket:
    def __init__(self, rate, burst):
        self._rate = float(rate)      # 每秒令牌数
        self._burst = float(burst)    # 桶容量（允许突发）
        self._tokens = float(burst)
        self._last = time.time()
        self._lock = threading.Lock()
        self._base_rate = float(rate)  # 基准速率（恢复用）

    def acquire(self):
        """取一个令牌——没有就排队等（阻塞式限流）"""
        while True:
            with self._lock:
                now = time.time()
                self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(min(wait, 0.5))  # 分段睡，响应降速变化

    def on_429(self):
        """遇 429：速率减半（下限 1/s）——自适应降速"""
        with self._lock:
            self._rate = max(1.0, self._rate / 2)
            _metrics_event("rate_limited")

    def recover(self):
        """无 429 一段时间后逐步恢复基准速率"""
        with self._lock:
            if self._rate < self._base_rate:
                self._rate = min(self._base_rate, self._rate * 2)

# 硅基免费档实测：embed 8 并发稳定 → 速率 6/s（留余量）突发 8
_SILICON_LIMITER = _TokenBucket(rate=6, burst=8)

def _silicon_acquire():
    """硅基调用前取令牌（embed/rerank 共用）"""
    _SILICON_LIMITER.acquire()

# ═══ ⑫7 监控指标采集（2026-09-20 业界标准三层·指标层）═══
# RED 指标 + 业务计数——内存计数器（进程级，重启清零；单机版不引 Prometheus，
# 量级大了再接：计数器接口不变，导出到 Prometheus client 即可）
import collections as _collections
_METRICS = {
    "start_time": time.time(),
    "counters": _collections.Counter(),      # {endpoint:count} {event:count}
    "errors": _collections.Counter(),         # {endpoint:error_count}
    "latencies": _collections.defaultdict(list),  # {endpoint:[ms,...]}（保留最近100条）
    "kb": {"chunks": 0, "vectors": 0, "last_scan": None},
}
_METRICS_LOCK = threading.Lock()
_RECENT_ERRORS = _collections.deque(maxlen=20)  # ⑫7d ERROR 速览（环形 20 条）

class _ErrorRingHandler(logging.Handler):
    """⑫7d：ERROR 级日志进环形缓冲——/api/stats 的 recent_errors 数据源"""
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            _RECENT_ERRORS.append({
                "ts": time.strftime("%H:%M:%S"),
                "module": record.name,
                "msg": str(record.getMessage())[:120],
            })
_ring = _ErrorRingHandler()
_ring.setLevel(logging.ERROR)
logging.getLogger().addHandler(_ring)

def _metrics_record(endpoint, ms, is_error=False):
    """api_search/api_chat 等端点埋点调用——p50/p90/p99 由 /api/stats 现算"""
    with _METRICS_LOCK:
        _METRICS["counters"][endpoint] += 1
        if is_error:
            _METRICS["errors"][endpoint] += 1
        lat = _METRICS["latencies"][endpoint]
        lat.append(round(ms, 1))
        if len(lat) > 100:
            del lat[:len(lat) - 100]

def _metrics_event(event):
    """业务事件计数：rerank降级/embed重试/断路器跳闸/缓存命中"""
    with _METRICS_LOCK:
        _METRICS["counters"][event] += 1

def _pct(sorted_lat, p):
    """百分位（输入已排序）"""
    if not sorted_lat:
        return None
    k = min(int(len(sorted_lat) * p / 100), len(sorted_lat) - 1)
    return sorted_lat[k]

@app.get("/api/stats")
def api_stats():
    """⑫7b 指标端点——RED 指标 + 业务计数 + 知识库状态
    业界对照：Prometheus /metrics 的 JSON 轻量版（Grafana 前端健康页直接读它）"""
    with _METRICS_LOCK:
        out = {
            "uptime_s": round(time.time() - _METRICS["start_time"]),
            "endpoints": {},
            "events": {},
        }
        for ep in ("search", "sql_route", "chat"):
            lat = sorted(_METRICS["latencies"].get(ep, []))
            if lat:
                out["endpoints"][ep] = {
                    "count": _METRICS["counters"].get(ep, 0),
                    "errors": _METRICS["errors"].get(ep, 0),
                    "error_rate": round(_METRICS["errors"].get(ep, 0) / max(_METRICS["counters"].get(ep, 1), 1) * 100, 1),
                    "p50_ms": _pct(lat, 50), "p90_ms": _pct(lat, 90), "p99_ms": _pct(lat, 99),
                }
        for ev in ("rerank_degraded", "embed_retry", "breaker_trip", "cache_hit", "rate_limited"):
            if _METRICS["counters"].get(ev):
                out["events"][ev] = _METRICS["counters"][ev]
        # 知识库状态
        try:
            with _db_lock:
                kb = _db.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL").fetchone()[0]
                last = _db.execute("SELECT id, created_at FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            out["kb"] = {"vectors": kb, "last_scan": last[1] if last else None}
        except Exception:
            out["kb"] = {"error": "db"}
        # ⑫7c 告警判定（面板红黄牌数据源）
        alerts = []
        for ep, d in out["endpoints"].items():
            if d.get("error_rate", 0) > 5:
                alerts.append({"level": "red", "msg": f"{ep} 错误率 {d['error_rate']}% > 5%"})
        if out["events"].get("breaker_trip"):
            alerts.append({"level": "red", "msg": "断路器已跳闸（rerank/embed/chat）"})
        for ep, d in out["endpoints"].items():
            if d.get("p50_ms") and d["p50_ms"] > 10000:
                alerts.append({"level": "yellow", "msg": f"{ep} 平均耗时 {d['p50_ms']}ms > 10s"})
        out["alerts"] = alerts
        out["recent_errors"] = list(_RECENT_ERRORS)  # ⑫7d ERROR 速览
        return out

@app.get("/health")
def api_health():
    """⑫5① 提前落地 + 看门狗探测端点（用户定标 2026-09-14：/health+NSSM，
    Mac 兼容——launchd 探测的也是它）。三层检查：
    ①进程活着：能返回本 JSON 就是活的（HTTP 层自证）
    ②DB 连通：SELECT 1 探 SQLite——页面能开不等于库没锁死
    ③在跑任务：chunk/embed 账本扫描（看门狗/NSSM 重启决策用——
      在跑任务时自动重启会腰斩向量化，坑13；返回清单让人来决策）
    纯 Python 跨平台（Windows/macOS 同一份代码）"""
    import time as _t
    db_ok = True
    try:
        with _db_lock:
            _db.execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False
    running = []
    with _chunk_jobs_lock:
        for sid, job in _chunk_jobs.items():
            if job.get("status") == "running":
                running.append(f"chunk#{sid} {job.get('done', 0)}/{job.get('total', '?')}")
    with _embed_jobs_lock:
        for sid, job in _embed_jobs.items():
            if job.get("status") == "running":
                running.append(f"embed#{sid} {job.get('done', 0)}/{job.get('total', '?')}")
    # ⑫7a 就绪探针（可查的进程态四维；chat/vec key 是 BYOK 每请求头携带——
    # 无进程级配置，key 就绪由前端"已配置"徽标负责，不在 health 里重复）
    _ready = {"db": db_ok}
    try:
        with _db_lock:
            _vec_n = _db.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL").fetchone()[0]
        _ready["vectors"] = _vec_n > 0
    except Exception:
        _ready["vectors"] = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "ready": _ready,  # ⑫7a 就绪探针（db/vectors 两维可查态；key 为 BYOK 请求头由前端徽标负责）
        "running_tasks": running,
        "time": _t.strftime("%Y-%m-%d %H:%M:%S"),
    }

@app.get("/api/logs")
def api_logs(file: str = "all", trace_id: str = "", level: str = "", q: str = "", limit: int = 200):
    """⑫8 日志查询面板（2026-09-21 页面看日志——问错了能查全链路）。
    手册五大场景的界面化：按文件/trace_id/级别/关键词过滤任意日志。
    只读 SMB logs/ 目录——不碰别处。"""
    LOG_FILES = {"api", "search", "chat", "sql", "parse", "task"}
    files = [file] if file in LOG_FILES else sorted(LOG_FILES)
    out = []
    try:
        for fname in files:
            p = _LOG_DIR / f"{fname}.log"
            if not p.exists():
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-2000:]  # 每文件尾 2000 行（内存护栏）
            for ln in reversed(lines):  # 新的在前
                ln = ln.strip()
                if not ln:
                    continue
                # 过滤条件（JSON 行——字段级匹配；非 JSON 行——全文匹配）
                ok = True
                if trace_id and trace_id not in ln:
                    ok = False
                if ok and level and f'"level": "{level}"' not in ln and f'[{level}]' not in ln.upper():
                    ok = False
                if ok and q and q not in ln:
                    ok = False
                if not ok:
                    continue
                # 来源标记
                try:
                    obj = _json.loads(ln)
                    obj["_src"] = fname
                    out.append(obj)
                except Exception:
                    out.append({"_src": fname, "raw": ln[:500]})
        # ⑫8（advisory 终修）：不做收集期早停（文件序收集会让 api.log 偏置）——
        # 每文件 2000 行护栏已有界，全收齐再排序再截断，跨文件时间序无偏
        out.sort(key=lambda o: o.get("ts", "9999"))
        if trace_id:
            out = out[:limit * 2]  # trace_id 查询给双倍（全链路通常 8+ 步）
            truncated = False
        else:
            truncated = len(out) > limit
            out = out[-limit:]
        return {"logs": out[::-1], "truncated": truncated, "files_scanned": files}
    except Exception as e:
        return {"error": str(e)[:200]}

@app.get("/api/ingest-log")
def api_ingest_log(file: str = "", limit: int = 30):
    """⑫8 C4 入库日志查询（详情弹窗跳转——这个文件当初怎么被切块/直采的）"""
    try:
        if not file or len(file) > 200:
            return {"error": "bad file"}
        fname = file.replace("\\", "/").split("/")[-1][:80]
        out = []
        for lf in ("parse.log", "task.log"):
            p = _LOG_DIR / lf
            if not p.exists():
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                for ln in f.readlines()[-5000:]:
                    if fname[:40] in ln:
                        try:
                            obj = _json.loads(ln.strip())
                            obj["_src"] = lf.replace(".log", "")
                            out.append(obj)
                        except Exception:
                            out.append({"_src": lf, "raw": ln.strip()[:300]})
                    if len(out) >= limit:
                        break
        return {"logs": out[:limit]}
    except Exception as e:
        return {"error": str(e)[:150]}

@app.get("/api/detail")
def api_detail_get(trace_id: str = "", q: str = "", limit: int = 20):
    """⑫8 C1/C3 明细档读取——点击日志行弹详情 / 按问题搜明细档。
    trace_id 直查单档；q 搜历史明细（返回命中的问题列表）"""
    try:
        if trace_id:
            if "/" in trace_id or "\\" in trace_id:
                return {"error": "bad id"}
            p = _DETAIL_DIR / (trace_id + ".json")
            if p.exists():
                return {"detail": _json.loads(p.read_text(encoding="utf-8"))}
            # SQL 明细档（trace_id 后缀 _sql）
            p2 = _DETAIL_DIR / (trace_id + "_sql.json")
            if p2.exists():
                return {"detail": _json.loads(p2.read_text(encoding="utf-8"))}
            return {"error": "明细档不存在（该次提问早于明细档上线，或评测未落档）"}
        # 按问题搜（C3）：扫全部明细档的 q 字段
        if q:
            out = []
            for f in sorted(_DETAIL_DIR.glob("*.json"), key=lambda x: -x.stat().st_mtime)[:500]:
                try:
                    d = _json.loads(f.read_text(encoding="utf-8"))
                    if q in d.get("q", ""):
                        out.append({"trace_id": f.stem, "q": d.get("q", "")[:80],
                                    "ts": d.get("ts", "")})
                    if len(out) >= limit:
                        break
                except Exception:
                    continue
            return {"matches": out}
        return {"error": "缺 trace_id 或 q"}
    except Exception as e:
        return {"error": str(e)[:150]}

@app.post("/api/detail/append")
async def api_detail_append(request: Request):
    """⑫8 B2 明细档答案段补写（两段式第二段——答案在代理侧生成，
    前端拿着 trace_id POST 回来合并进同一份明细档）"""
    try:
        body = await request.json()
        tid = str(body.get("trace_id", ""))[:60]
        if not tid or "/" in tid or "\\" in tid:
            return {"error": "bad trace_id"}
        p = _DETAIL_DIR / f"{tid}.json"
        doc = {}
        if p.exists():
            try:
                doc = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                doc = {}
        doc["answer"] = {
            "model": str(body.get("model", ""))[:60],
            "latency_ms": body.get("latency_ms"),
            "text": str(body.get("text", ""))[:20000],  # 答案全文（上限 2 万字）
        }
        p.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)[:150]}

@app.post("/api/log")
async def api_client_log(request: Request):
    """⑫5 前端两步上报端点：改写触发/审计触发这两步在前端决定（后端无痕），
    必须上报落盘才能全链路留痕（全链路留痕铁律）。前端带 trace_id 传上来，
    后端写进 chat.log——同一次提问的全链由此完整"""
    try:
        body = await request.json()
        event = str(body.get("event", ""))[:40]      # 事件类型：rewrite/audit/…
        tid = str(body.get("trace_id", ""))[:32] or "-"
        if tid != "-":
            _current_trace_id.set(tid)  # 对齐前端会话的 trace_id
        _lg_chat.info(event, extra={
            "q": str(body.get("q", ""))[:60],
            "detail": str(body.get("detail", ""))[:1000],  # ⑫8 放宽（答案正文上报用——200 塞不下）
            "source": "frontend"})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)[:100]}


@app.get("/api/kb")
def api_kb_list():
    """知识库列表：页面加载时调用，秒回（只查库不扫盘）"""
    return {"scans": db_list_scans()}


@app.get("/api/kb/{scan_id}/files")
def api_kb_files(scan_id: int):
    """拉某次扫描的文件清单"""
    return {"files": db_get_scan_files(scan_id)}
# ══════════════════════════════════════════════════════════════
# 步骤③ 向量化
# ══════════════════════════════════════════════════════════════
# embedding = 把一段文字交给大模型，换回 1024 个小数的向量。意思相近的文字
# 向量挨得近，检索时拿"问题向量"和"每块向量"比距离找最相关的几块。
# 分块是本地活（秒级），向量化是网络活（分钟级），两种节奏分开跑互不拖累。

# ══════════════════════════════════════════════════════════════
# 模块 5a：向量化核心（配置 + 调 API + 存向量）
# ══════════════════════════════════════════════════════════════

import urllib.request
import urllib.error
import json

# —— 配置区（换模型/换服务商只改这里）——
EMBED_URL = "https://api.siliconflow.cn/v1/embeddings"  # 硅基流动的 embedding 接口地址
EMBED_MODEL = "BAAI/bge-m3"  # 免费多语言 embedding 模型（中英混排文档都能拍指纹）
EMBED_API_KEY = ""  # 留空：BYOK 模式下 key 由前端每次请求传入（X-Vec-Key 头）。自用裸跑时临时填回即可
EMBED_BATCH = 32  # 一批最多 32 条（服务商限载，硬塞报 413）
# 重排（rerank）独立配置——和 embedding 分开：换家服务商端点/模型名/账号都可能
# 不同（免费厂里 bge-m3 和 bge-reranker 恰好同厂同账号，但不能假设用户也这样）
RERANK_URL = "https://api.siliconflow.cn/v1"  # rerank 基址（内部拼 /rerank）
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
RERANK_API_KEY = ""  # 留空：BYOK 由前端 X-Rerank-Key 传入；没配则回退向量 Key

# 老库升级：给 chunks 表加 vector 列装指纹（已有就忽略）
try:
    _db.execute("ALTER TABLE chunks ADD COLUMN vector TEXT")
except sqlite3.OperationalError:
    pass
_db.commit()
# ---------- BYOK：从前端请求头取用户的向量服务配置（每次请求自带，不落盘） ----------
def _vec_from_request(request):
    """返回 (api_key, api_url)。前端调 /search、/embed 时在请求头带上
    X-Vec-Key / X-Vec-Url；没带则回退空（embed_batch 内部再退回常量）。
    十三修（2026-09-23）：request=None 防护——工具链/内部调用的
    无 Request 场景返回 (None, None) 而不是 AttributeError"""
    if request is None:
        return None, None
    return request.headers.get("x-vec-key"), request.headers.get("x-vec-url")


# ---------- BYOK：重排/嵌入模型名的独立配置（与向量 Key 可分开） ----------
def _rerank_from_request(request):
    """返回 (api_key, api_url, model)。三者都可能为 None——调用方按
    「重排配置 -> 向量配置 -> 常量」逐级回退，老用户（只填过一把向量 Key）
    无需改动就能继续用重排。"""
    if request is None:
        return None, None, None
    h = request.headers
    return h.get("x-rerank-key"), h.get("x-rerank-url"), h.get("x-rerank-model")


def _embed_model_from_request(request):
    """嵌入模型名（X-Embed-Model 头）。None 表示用 EMBED_MODEL 常量。
    与 key/url 分开取是因为向量化后台任务也要把它带进线程"""
    if request is None:
        return None
    return request.headers.get("x-embed-model")


def embed_batch(texts, api_key=None, api_url=None, label=None, model=None):
    """texts = 最多 32 条文本的列表。返回向量列表（和输入一一对应）。
    api_key/api_url：BYOK 模式下由前端每次请求传入（用完即弃，不落盘）；
    不传时退回 EMBED_* 常量（自用兼容）。一通电话换一批，比一条一通快 32 倍
    model：BYOK 嵌入模型名（X-Embed-Model 头）；不传退回 EMBED_MODEL"""
    # 前端传的是 base（如 https://api.siliconflow.cn/v1），拼成完整端点；
    # 传完整端点（以 /embeddings 结尾）则原样用
    if api_url:
        url = api_url.rstrip("/") + ("" if api_url.rstrip("/").endswith("/embeddings") else "/embeddings")
    else:
        url = EMBED_URL
    key = api_key or EMBED_API_KEY
    if not key:
        raise ValueError("缺少向量 API Key——请在前端「⚙ 设置」里配置")
    # 韧性三件套·断路器（2026-09-19 业界补齐）：embed 域熔断期直接明确报错——
    # 不让用户陪着重试干等（原：挂了每题重试3次×退避=18s+ 才失败）
    if _svc_tripped("embed", key):
        raise RuntimeError("向量服务暂时不可用（熔断保护中，约10分钟自动恢复）——请稍后再试")
    payload = json.dumps({"model": model or EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,  # 带了 data 自动用 POST
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    # 重试升级（2026-09-14 明因日志定案：失败全是 TimeoutError 读超时——
    # 局域网出公网慢+8工人挤管道，60s 读不完 32 块响应。原只重试 429）：
    # ①超时 60→120s（慢网余量）②TimeoutError/URLError 与 429 同等重试
    # ③退避加长（3s/6s/9s），让挤在管道里的其他工人先过
    _eb_t0 = _time.time()  # ⑫3：批次耗时（429 静默重试现在有痕——手册场景四）
    _retries = 0
    for attempt in range(4):
        try:
            _silicon_acquire()  # 动态限流：取令牌（429 防护·2026-09-20）
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            # 429 限流 / 503 服务过载（2026-09-14 实测：bge-m3 免费通道
            # 503 故障期 8 工人全军覆没、批次连坐全灭）——都值得退避重试；
            # 503 退避翻倍加长（10s/20s/40s），等服务端回血
            if e.code in (429, 503) and attempt < 3:
                if e.code == 429:
                    _SILICON_LIMITER.on_429()  # 动态限流：429 自适应降速（速率减半）
                wait = (3 * (attempt + 1)) if e.code == 429 else (10 * (2 ** attempt))
                _retries += 1
                _lg_task.warning("embed退避重试", extra={"code": e.code, "wait_s": wait,
                    "attempt": attempt + 1, "batch_label": label or ""})  # ⑫3
                _pf.warning("embed%s %d 退避 %ds 重试（attempt %d）", f"[{label}]" if label else "", e.code, wait, attempt + 1)
                time.sleep(wait)
                continue
            _svc_fail("embed", key)  # 429/503 重试耗尽或其他 HTTP 错——断路器记账
            raise
        except (TimeoutError, urllib.error.URLError) as e:
            # 读超时/连接抖动：可重试的瞬时错（服务器可能已在处理，重试
            # 只是多算几次配额——比整批 32 块永久 NULL 便宜）
            if attempt < 3:
                _retries += 1
                _lg_task.warning("embed超时重试", extra={"exc": type(e).__name__,
                    "attempt": attempt + 1, "batch_label": label or ""})  # ⑫3
                time.sleep(3 * (attempt + 1))
                continue
            _svc_fail("embed", key)  # 重试全败——断路器记账
            raise
    _svc_ok("embed", key)  # 成功复位
    _lg_task.info("embed批次完成", extra={"chunks": len(texts),
        "duration_ms": int((_time.time() - _eb_t0) * 1000),
        "retries": _retries, "batch_label": label or ""})  # ⑫3：每批一条
    # 按 index 排序对回输入顺序（不依赖对方返回顺序）
    data = sorted(body["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def vec_to_blob(vec):
    """数字列表 -> float32 二进制 BLOB（1024 维 = 4096 字节，比 JSON 文本小 40%
    且 np.frombuffer 零解析还原）"""
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vec(blob):
    """BLOB -> 数字列表（老接口兼容用；检索热路径不用它，直接矩阵化）"""
    return np.frombuffer(blob, dtype=np.float32).tolist()


def db_save_vectors(rows):
    """rows = [(vector_blob, chunk_id), ...]（float32 BLOB）。和存块同一套'批发'思路：
    一个事务一批，摊薄磁盘同步成本"""
    with _db_lock:
        with _db:
            _db.executemany(
                "UPDATE chunks SET vector = ? WHERE id = ?",
                rows,
            )
    return len(rows)


# ══════════════════════════════════════════════════════════════
# 模块 5b：异步向量化任务（后台批处理 + 进度轮询）
# ══════════════════════════════════════════════════════════════
# 和分块任务同一张图纸：接口秒回 -> 后台线程干 -> 前端轮询进度

# ═══ 坑13 修复（2026-09-20 开工）：embed 任务状态持久化 ═══
# 实况：数据层已断点续传（vector IS NULL 捞漏）✓，但进度账本是内存——
# 重启后前端进度条丢显示。修：状态轻量落 scans 表，重启恢复显示。
def _embed_persist(scan_id, done, total, status, error=None):
    """embed 状态落库（启动/收尾各一次+每百批——库级轻写）"""
    try:
        with _db_lock:
            _db.execute("ALTER TABLE scans ADD COLUMN embed_status TEXT")
            _db.execute("ALTER TABLE scans ADD COLUMN embed_done INTEGER")
            _db.execute("ALTER TABLE scans ADD COLUMN embed_total INTEGER")
            _db.execute("ALTER TABLE scans ADD COLUMN embed_error TEXT")
            _db.commit()
    except Exception:
        pass  # 列已存在
    try:
        with _db_lock:
            _db.execute("UPDATE scans SET embed_status=?, embed_done=?, embed_total=?, embed_error=? WHERE id=?",
                        (status, done, total, error, scan_id))
            _db.commit()
    except Exception:
        pass  # 落库失败不挡任务（内存账本仍是主源）

_embed_jobs = {}                      # 进度账本：{scan_id: {done, total, status, error}}
_embed_jobs_lock = threading.Lock()

def _run_embed_job(scan_id, vec_key=None, vec_url=None, embed_model=None):
    """后台线程跑的向量化任务（多工人并发版）。

    发号员先把全部待拍批次静态分好 -> 工人们从队列领批次，各自打 API
    拍指纹、回填、记账，领空收工。
    【断点续传】捞的条件是 vector IS NULL——中途崩了重跑只补漏的，
    已拍过的不重拍（免费配额也是钱）。
    """
    # 8 工人：压测（每档 25 秒）4路 169 块/s | 6路 250 | 8路 282 | 12路 325，
    # 8 路后增速急衰减且 12 路离限流墙太近，定 8 零 429；429/503 自动退避
    #（embed_batch 兜底，503 用 10s/20s/40s 翻倍退避等服务端回血）
    EMBED_WORKERS = 8
    try:
        # 开工先对账：总共多少块、还剩多少没指纹（重跑时拍过的直接记账）
        with _db_lock:
            total = _db.execute(
                "SELECT COUNT(*) FROM chunks WHERE scan_id = ?", (scan_id,)
            ).fetchone()[0]
            remaining = _db.execute(
                "SELECT COUNT(*) FROM chunks WHERE scan_id = ? AND vector IS NULL",
                (scan_id,),
            ).fetchone()[0]
        done = total - remaining
        with _embed_jobs_lock:
            _embed_jobs[scan_id]["done"] = done
        # 发号员：开工前静态分好全部批次（[(块id, 正文)] x 32 一批）。
        # 如果边跑边查 NULL 捞批，两个工人可能捞到同一批白烧配额
        with _db_lock:
            rows_all = _db.execute(
                "SELECT id, text FROM chunks WHERE scan_id = ? AND vector IS NULL "
                "ORDER BY id",
                (scan_id,),
            ).fetchall()
        batches = [rows_all[i:i + EMBED_BATCH] for i in range(0, len(rows_all), EMBED_BATCH)]
        next_batch = [0]  # 认领游标（闭包里改内容要包一层列表）
        cursor_lock = threading.Lock()

        # 修复丢批 bug（2026-09-10）：原版工人抛异常整人退出，领的后续批次
        # 全部失踪但主线程无脑写 done。修两处：① 工人异常不退出，记失败数
        # 继续领；② 终态按库里实账定（join 后亲自数 NULL），不信工人嘴
        failed = [0]

        def worker():
            """工人的一生：领批次 -> 打电话拍指纹 -> 回填 -> 再领，领空收工。
            批次失败不死人：记一笔继续干（终态由主线程按库实账裁决）"""
            while True:
                # 领号：看游标、领走批次号、游标 +1，三步上锁一气呵成
                with cursor_lock:
                    idx = next_batch[0]
                    next_batch[0] += 1
                if idx >= len(batches):
                    return
                batch = batches[idx]
                # 超长块瘦身（2026-09-14 实测根因修复）：12000 字数字表格经
                # bge-m3 tokenizer 碎切成 20000+ token，超 8192 上限被 400
                # 拒——「重跑补齐」补不动这种块（期初模板 384 块反复失败）。
                # 拍指纹用截断文本（保块原文不动，检索命中仍回全文），但截断
                # 点选在行边界（表块不劈行内、正文按句末标点），前 6000 字
                # 语义足够代表整块（表格前部=表头+首批数据）
                def _slim(text):
                    # 清洗（2026-09-14 根因②：李明 PDF 抽出文本含 \x00 NUL——
                    # JSON 序列化非法，embedding API 直接拒收，一批 32 块连坐全灭）。
                    # 控制字符对语义零贡献，拍指纹前剔净
                    if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                        text = ''.join(c for c in text if ord(c) >= 32 or c in '\n\r\t')
                    # 预算按数字密度自适应（2026-09-14 二轮实测：6000 字 40%
                    # 数字的期初模板块仍被 400 拒——数字串经 tokenizer 切得极碎，
                    # 5882 字实际 token 远超字数。数字占比越高预算越紧：
                    # 纯中文 1 字≈1 token 吃满 6000；数字表压缩到 1/4）
                    digit_ratio = sum(c.isdigit() for c in text[:2000]) / min(len(text), 2000)
                    budget = 6000 if digit_ratio < 0.15 else (4500 if digit_ratio < 0.3 else 3000)
                    if len(text) <= budget:
                        return text
                    cut = text.rfind('\n', 0, budget)
                    if cut < budget // 2:  # 单行超长（无换行）：按句末标点退
                        cut = max(text.rfind('。', 0, budget), text.rfind('！', 0, budget), text.rfind('？', 0, budget))
                    if cut < budget // 2:  # 兜底：硬截（极罕见，一整块无标点无换行）
                        return text[:budget]
                    return text[:cut + 1]  # +1 含边界字符（句号/换行）——rfind 是索引不是长度
                try:
                    vectors = embed_batch([_slim(r[1]) for r in batch], api_key=vec_key, api_url=vec_url,
                                          label=f"scan{scan_id}", model=embed_model)
                except Exception as e:
                    # 明因日志（坑3 同款哲学，2026-09-14 补——此前静默吞错只记数，
                    # 连续三轮猜 token 预算不如一行真实 HTTP 状态）：429=限流 /
                    # 400=超长或非法字符 / 401=key 错——首块特征一起打（id+长度+
                    # 数字密度），下次失败看日志即知根因
                    _first = batch[0]
                    _dr = sum(c.isdigit() for c in _first[1][:2000]) / max(min(len(_first[1]), 2000), 1)
                    _pf.error("embed批次失败[scan%d]: %s 块%d块 | 首块 id=%d len=%d 数字占比%.0f | %s: %s",
                              scan_id, len(batch), len(batch), _first[0], len(_first[1]), _dr * 100,
                              type(e).__name__, str(e)[:200])
                    with _embed_jobs_lock:
                        failed[0] += len(batch)
                    continue
                # 向量存 float32 BLOB（存取走 vec_to_blob）；float32 自带 ~7 位
                # 有效数字，余弦精度绰绰有余，体积比 JSON 文本再省 40%
                db_save_vectors([
                    (vec_to_blob(vec), row[0])
                    for row, vec in zip(batch, vectors)
                ])
                with _embed_jobs_lock:
                    _embed_jobs[scan_id]["done"] += len(batch)

        threads = [_spawn_traced(worker) for _ in range(EMBED_WORKERS)]  # ⑫0 穿透（第四处：embed 工人——批次日志 trace_id 不断链）
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 终态裁决：亲自数库里的 NULL 实账
        with _db_lock:
            still_null = _db.execute(
                "SELECT COUNT(*) FROM chunks WHERE scan_id = ? AND vector IS NULL",
                (scan_id,),
            ).fetchone()[0]
        with _embed_jobs_lock:
            if still_null == 0:
                _lg_task.info("向量化完成", extra={"scan_id": scan_id})  # ⑫3
                _embed_jobs[scan_id]["status"] = "done"
                _embed_persist(scan_id, _embed_jobs[scan_id].get("done", 0), total, "done")  # 坑13：收尾落库
                # 冲刺③（2026-09-21 词典不写死·换库零人工）：
                # 向量化全部完成后，新库表头词增量 LLM 重翻——下次分块自动用新词典
                try:
                    _rebuild_header_dict(scan_id, chat_key=vec_key, chat_url=vec_url)
                except Exception:
                    pass  # 重翻失败不挡向量化收尾（下次扫描补）
            else:
                # 有批次失败：报 partial_done + 失败数，重跑断点续传补齐。
                # 失败原因不含服务重启腰斩（2026-09-14 实测：后端重启杀掉
                # embed 线程，领走的 13 批 384 块留 NULL——连续 id 带，头尾
                # 批中间截断，与内容无关）；超长块/NUL 已由 _slim 治，
                # 剩余多为瞬时网络错或限流重试 3 次仍败
                _lg_task.warning("向量化未完成", extra={"scan_id": scan_id, "still_null": still_null})  # ⑫3
                _embed_jobs[scan_id]["status"] = "partial_done"
                _embed_jobs[scan_id]["error"] = f"{still_null} 块未完成（网络中断/限流/服务重启），再点一次向量化自动补齐缺的"
    except Exception as e:
        # 逃生舱：错误记进账本，不让后端陪葬
        with _embed_jobs_lock:
            _embed_jobs[scan_id]["status"] = "error"
            _embed_persist(scan_id, _embed_jobs[scan_id].get("done", 0), total, "error", _embed_jobs[scan_id].get("error"))  # 坑13
            _lg_task.error("向量化任务失败", extra={"scan_id": scan_id, "exc_type": type(e).__name__, "exc": str(e)[:200]})  # ⑫3
            _embed_jobs[scan_id]["error"] = str(e)


@app.post("/api/kb/{scan_id}/embed")
def api_embed_start(scan_id: int, request: Request = None):
    """启动某次扫描的向量化任务。立刻返回，后台线程慢慢打 API。
    BYOK：X-Vec-Key/X-Vec-Url 头里的用户配置随任务带进后台线程
    （X-Embed-Model 头可选——不传用 EMBED_MODEL）"""
    vec_key, vec_url = _vec_from_request(request)
    embed_model = _embed_model_from_request(request)
    if not vec_key and not EMBED_API_KEY:
        return {"error": "缺少向量 API Key——请在前端「⚙ 设置」里配置后再点向量化"}
    # 分块还在跑就别开工——块还在陆续入库，现在开工会漏掉后来的块
    with _chunk_jobs_lock:
        chunk_running = _chunk_jobs.get(scan_id, {}).get("status") == "running"
    if chunk_running:
        return {"error": "分块还在跑，等几秒块齐了再点向量化"}
    with _db_lock:
        total = _db.execute(
            "SELECT COUNT(*) FROM chunks WHERE scan_id = ?", (scan_id,)
        ).fetchone()[0]
    if total == 0:
        return {"error": "这个扫描还没有块——先分块再向量化"}
    # 防连点重复开工
    with _embed_jobs_lock:
        job = _embed_jobs.get(scan_id)
        if job and job.get("status") == "running":
            return {"started": True, "total": job["total"]}
        _embed_jobs[scan_id] = {"done": 0, "total": total, "status": "running", "error": None}
        _embed_persist(scan_id, 0, total, "running")  # 坑13：启动落库
    _spawn_traced(_run_embed_job, scan_id, vec_key, vec_url, embed_model).start()  # ⑫0 穿透+⑫3 在 job 内记生命周期
    return {"started": True, "total": total}


@app.get("/api/kb/{scan_id}/embed/status")
def api_embed_status(scan_id: int):
    # 坑13：重启恢复——内存账本没有时读库里的持久化状态
    with _embed_jobs_lock:
        if scan_id not in _embed_jobs:
            try:
                with _db_lock:
                    _r = _db.execute("SELECT embed_status, embed_done, embed_total, embed_error FROM scans WHERE id=?", (scan_id,)).fetchone()
                if _r and _r[0]:
                    return {"done": _r[1] or 0, "total": _r[2] or 0, "status": _r[0], "error": _r[3]}
            except Exception:
                pass  # 无持久化列（老库）走原逻辑
    """查向量化进度。账本格式和分块进度同一套，前端可以套用同一套渲染"""
    with _embed_jobs_lock:
        job = _embed_jobs.get(scan_id)
    if job is None:
        # 没在跑？看库里的实账（可能历史任务早跑完，或压根没开始）
        with _db_lock:
            total = _db.execute(
                "SELECT COUNT(*) FROM chunks WHERE scan_id = ?", (scan_id,)
            ).fetchone()[0]
            done = _db.execute(
                "SELECT COUNT(*) FROM chunks WHERE scan_id = ? AND vector IS NOT NULL",
                (scan_id,),
            ).fetchone()[0]
        if total == 0:
            return {"status": "not_chunked", "done": 0, "total": 0}
        return {
            "status": "done" if done == total else "not_started",
            "done": done,
            "total": total,
        }
    return job

# ══════════════════════════════════════════════════════════════
# 步骤④ 检索
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# 模块 6a：检索核心（余弦相似度 + Top-K 挑选）
# ══════════════════════════════════════════════════════════════


def cosine(a, b):
    """余弦相似度：看两个向量的"方向"夹角。1.0 = 方向相同；0.0 = 垂直不相关。
    公式：cos = (a·b) / (|a| * |b|)。
    用余弦不用直线距离：embedding 向量的"长度"没有意义（同一句话写长写短
    指纹会整体放大缩小），只有方向编码语义，余弦正好剥掉长度差"""
    dot = sum(x * y for x, y in zip(a, b))
    len_a = sum(x * x for x in a) ** 0.5
    len_b = sum(y * y for y in b) ** 0.5
    if len_a == 0 or len_b == 0:  # 全零向量没有方向，判不相似
        return 0.0
    return dot / (len_a * len_b)


def _decode_stored_vec(raw):
    """库里的向量还原成 float32 数组。兼容两种存量格式：
    bytes = 新 BLOB（零解析直取）；str = 老 JSON 文本（迁移前的历史数据）"""
    if isinstance(raw, bytes):
        return np.frombuffer(raw, dtype=np.float32)
    return np.asarray(json.loads(raw), dtype=np.float32)


def db_search_chunks(scan_id, question_vec, k=5):
    """语义路：捞出全部指纹 -> numpy 一次矩阵算完所有余弦 -> 取前 K。
    返回 [{file, seq, score, text}] 按相似度从高到低。
    矩阵化后 2 万块 ~50ms（原逐块纯 Python 循环 1 秒），且 numpy 批量
    运算释放 GIL——多人同时检索不再互相排队"""
    with _db_lock:
        rows = _db.execute(
            "SELECT id, file, seq, vector, text FROM chunks "
            "WHERE scan_id = ? AND vector IS NOT NULL",
            (scan_id,),
        ).fetchall()
    if not rows:
        return []  # 没指纹的库（还没向量化），调用方好提示
    # 问题向量规整成行向量；预除模长，后面矩阵乘一次出全部分数
    q = np.asarray(question_vec, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []
    q_unit = q / q_norm
    # 逐块还原成 (N,1024) 大矩阵（BLOB 零解析直取；老 JSON 逐条解析）
    vecs = np.empty((len(rows), q.shape[0]), dtype=np.float32)
    for i, r in enumerate(rows):
        vecs[i] = _decode_stored_vec(r[3])
    norms = np.linalg.norm(vecs, axis=1)
    norms[norms == 0] = 1.0  # 全零向量：分数自然为 0，不用特判
    scores = (vecs @ q_unit) / norms  # (N,1024)@(1024,) -> N 个余弦，C 级速度
    # argpartition 取前 K 的下标再精排（比全量 sort 快，K << N）
    top = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
    top = top[np.argsort(-scores[top])]
    return [
        {"file": rows[i][1], "seq": rows[i][2],
         "score": round(float(scores[i]), 4), "text": rows[i][4]}
        for i in top
    ]


# ══════════════════════════════════════════════════════════════
# 模块 8：混合检索（Hybrid Search，企业级标配）
# ══════════════════════════════════════════════════════════════
# 为什么纯向量不够："BASE-04 谁负责"（编号没语义）语义检索排 30 名，关键词
# 检索秒中第 1；"菜单混挂的根因"这种口语问题关键词查不到，语义一捞一个准。
# 两路各有盲区天生互补，跑两路再融合（RRF）：
#   问题 ─┬─ 路A 关键词（FTS5 全文索引，毫秒级，擅长编号/人名/表名）
#         └─ 路B 语义（向量余弦，秒级，擅长口语/换说法/模糊语义）
#              └→ RRF 融合（只比排名不比分数）→ Top-K

# ─────────────────────────────────────────────————————
# 8a-1：FTS5 全文索引（trigram 分词 + 触发器自动同步）
# ─────────────────────────────────────────————————————
# FTS5 默认分词器按空格切词，中文整句成一个 token（实测全灭）；trigram =
# 每 3 个连续字符一个索引项，中文子串能命中。软肋：2 字词（"张伟"）比滑窗短，
# 检索函数里用 LIKE 兜底（实测 1.4 万块 LIKE 只要 0.048 秒）
_db.executescript("""
-- 全文索引虚表：镜像 chunks 表的 text 列（content= 表示"内容存在别处"，
-- 索引里只存分词→行号 的映射，省一半空间）
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, tokenize='trigram', content='chunks', content_rowid='id'
);
-- 触发器三件套：chunks 表怎么动，索引跟着怎么动（自动同步，不用手动喂）
-- ① 新块入库 -> 索引加一行（分块任务跑批时每块自动登记）
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
-- ② 块被删（重分块先清场）-> 索引跟着删，不然搜到幽灵行
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
-- ③ 块被改（暂时没有这个场景，留对称防将来）
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
""")
_db.commit()


def db_rebuild_fts(force=False):
    """全量重建全文索引。触发器只管"以后"的增删改，"过去"已入库的块
    要手动灌一次（重分块后跑一次，几千块 ~1 秒）。
    2026-09-11 修复：无条件 rebuild 在百万块库上要 4 分钟且启动锁库——
    改为行数对账（chunks vs chunks_fts 差异行才 rebuild），一致则秒过。
    2026-09-15 修复 import 挂死（20 分钟级实锤）：COUNT(*) 在 UNC 网络盘
    对 20 万行 FTS 表是分钟级全表扫——评测器/第二进程 import server 全部
    卡死在这。改 max(rowid) O(1) 元数据对账（实测 0.05s）：行号连续递增
    场景下 max(rowid) 等价于行数（chunks.id 自增主键/fts rowid 同源），
    精度足够触发"差异才 rebuild"的判据；确有删除导致的空洞时偏差只会
    引发一次多余的 rebuild（代价 1 次重建），不会漏重建（低侧触发）"""
    with _db_lock:
        n_main = _db.execute("SELECT MAX(id) FROM chunks").fetchone()[0] or 0
        n_fts = _db.execute("SELECT MAX(rowid) FROM chunks_fts").fetchone()[0] or 0
        if not force and n_main == n_fts:
            return False  # 已同步（触发器维护着），跳过
        with _db:
            _db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    return True

# 启动时对账：不一致（历史脏数据/触发器缺失时代的存量）才全量重建，一致秒过
db_rebuild_fts()


# ─────────────────────────────────────────———————————
# 8a-2：关键词检索（FTS5 MATCH + 2 字词 LIKE 兜底）
# ─────────────────────────——————————————————————————————————
def db_keyword_search(scan_id, q, k=10):
    """关键词路：把问题当词组去全文索引里精确匹配。
    返回 [{file, seq, score, text}]，格式和语义路完全一致（方便融合）。

    三个实测踩出来的细节：
    1. 每个词用 "双引号" 包成短语——FTS5 里 - AND OR 是保留字，
       不包的话 "BASE-04" 会报 no such column: 04
    2. 词与词之间用 OR 连接——关键词路要召回宽（漏了语义路补），
       排序交给 RRF 融合层操心
    3. ≤2 字的词（张伟/汇率）trigram 窗口匹配不到 -> LIKE 全表兜底

    ⑨ 优化第1+3步（2026-09-15，61.5%→95% 冲刺）：
    a. BM25 排序替代命中计数（D2 实锤：「11402.35」命中 1 个文档和「对账单」
       命中 40 个在计数眼里同权，精确数字被泛词挤出池——bm25() 给罕见词
       自动加权 IDF，SQLite FTS5 内置零依赖，业界标准打分）
    b. 停用词过滤 + LIKE 兜底词频排序（二轮刀1+2：D556 实锤"用于/记录/全称"
       问法虚词命中几百块纯噪声；LIKE 无排序 74 块取前 10 等于抽签）
    """
    import re as _re
    # 通用性改造①②（2026-09-23 ）：写死的三路分词 + 48 中文虚词 →
    # multilingual_tokenizer（多语言停用词 + 库内高频自动降权 + 语言分流分词）。
    # 中文库行为兼容（jieba 双路 + 标点 + 英文短语全保留——零回归），
    # 日韩 bigram / 纯拉丁空格分词为新支持。异常回退原逻辑（不炸主路）。
    words = None
    _dw = {}  # 库内高频降权映射（2026-09-23 advisory：降权≠硬删）
    try:
        from multilingual_tokenizer import tokenize as _ml_tokenize, get_domain_weights as _gdw
        words = _ml_tokenize(q)
        try:
            _dw = _gdw(str(DB_PATH))
        except Exception:
            _dw = {}
    except Exception:
        words = None
    if words is None:
        # 回退：原三路分词（模块缺失/异常时保持原行为）
        import jieba
        _STOP = {"多少", "对应", "是什么", "哪些", "是否", "可以", "什么", "怎么",
                 "如何", "请问", "一下", "这个", "那个", "分别", "具体", "给出",
                 "还是", "以及", "然后", "一个", "有没有", "用于", "记录", "字段",
                 "全称", "体现", "显示", "数字", "业务", "相关", "信息", "数据",
                 "内容", "说明", "情况", "进行", "通过", "需要", "应该", "名称",
                 "其中", "以上", "以下", "本次", "当前", "所有", "各种", "出现"}
        jieba_words = [w for w in jieba.cut(q)
                       if len(w.strip()) >= 2 and w not in _STOP
                       and not _re.match(r"^[\s，。？！、,.;:?!()（）]+$", w)]
        search_words = [w for w in jieba.cut_for_search(q)
                        if len(w.strip()) >= 2 and w not in _STOP
                        and w not in jieba_words]
        import re as _re2
        _en_phrases = [m for m in _re2.findall(r'[A-Za-z][A-Za-z0-9/.\-\[\]]{3,}', q)
                       if any(c.isupper() for c in m) or '/' in m or '[' in m]
        punct_words = [w for w in _re.split(r"[\s，。？！、,.;:?!()（）]+", q)
                       if len(w) >= 2 and w not in _STOP]
        seen_w = set()
        words = []
        for w in jieba_words + punct_words + _en_phrases + search_words:
            if _re.search(r'[A-Za-z]', w) and _re.search(r'[\u4e00-\u9fa5]', w):
                continue
            if w not in seen_w:
                seen_w.add(w)
                words.append(w)
    fts_words = [w for w in words if len(w) >= 3]
    short_words = [w for w in words if len(w) == 2]
    bm25_rows = []   # (bm25分数, id) —— BM25 打分行（bm25 负值越负越相关）
    like_rows = []   # LIKE 兜底行（(freq, id)——刀1 后带词频）
    with _db_lock:
        # ⑨ 二轮刀3 v5 终版（A/B 定案法）：文件名锚 + 稀有度门槛 + 可开关。
        # v1→v4.2 五版样本身调不收敛（advisory 定谳：修一题破另一题）。
        # v5 判据可解释：单词锚要求该词在文件名中足够稀有（≤3 个文件含它
        # ——'现金'1个✓ '余额'34个✗ '凭证'6个✗），多词共现≥2 无稀有度
        # 要求（多词本身就过滤了噪声）。数据锚：现金1/库存现金1/考勤1/
        # AccountStatement1 vs 余额34/凭证6。开关：FILENAME_ANCHOR=False
        # 可整体关闭（全量 A/B 用）
        FILENAME_ANCHOR = True  # A/B 定案（2026-09-15）：True=71.4% vs False=70.8%，锚净贡献 +0.6pp——保留
        if FILENAME_ANCHOR:
            _anchor_words = [w for w in words if not w.replace('.', '').isdigit()]
            # 预查每个词的文件名稀有度（一次 GROUP BY 拿全）
            _fname_counts = {}
            for (fname,) in _db.execute(
                "SELECT DISTINCT file FROM chunks WHERE scan_id = ?", (scan_id,)
            ).fetchall():
                for w in _anchor_words:
                    if w in fname.rsplit('.', 1)[0]:
                        _fname_counts[w] = _fname_counts.get(w, 0) + 1
            for rid, rfile in _db.execute(
                "SELECT id, file FROM chunks WHERE scan_id = ?", (scan_id,)
            ).fetchall():
                _stem = rfile.rsplit('.', 1)[0]
                _hits = [w for w in _anchor_words if w in _stem]
                if len(_hits) >= 2 or any(len(w) >= 2 and _fname_counts.get(w, 99) <= 3 for w in _hits):
                    bm25_rows.append((-50.0, rid))
        if fts_words:
            match_expr = " OR ".join(f'"{w}"' for w in fts_words)
            try:
                bm25_rows += _db.execute(
                    "SELECT bm25(chunks_fts) AS score, c.id FROM chunks_fts f "
                    "JOIN chunks c ON c.id = f.rowid "
                    "WHERE chunks_fts MATCH ? AND c.scan_id = ? "
                    "ORDER BY score LIMIT ?",
                    (match_expr, scan_id, k * 3),
                ).fetchall()
            except sqlite3.OperationalError:
                pass  # 查询语法炸了（极端符号）-> 这路放弃，不拖垮整体
        # 路A2：2 字词 LIKE 兜底（全表扫，1.4 万块 0.05s）。
        # ⑨ 二轮刀1（2026-09-15）：LIKE 命中加词频排序——原版按 rowid 顺序
        # 取前 k，74 块含"现金"时等于抽签（D14 实锤：目标块落选 Top10）。
        # 词频降序 = 手写 BM25 代理（短词上 IDF 逻辑同样成立：词在块内
        # 出现越多越可能是该块的主题）。SQL 版：length(text)-length(replace)
        # 算出现次数，一次查询带排序
        for w in short_words:
            safe_w = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[").replace("]", "\\]")  # 方括号补转义（五轮步3 归因实锤）：SQLite LIKE 里 [Cash] 是字符类通配符，'Bank Deposit[Cash]' 这类精确值永远匹配不到字面
            # 2026-09-23 通用性：泛词降权（advisory blocker 落地）——库内高频词
            # freq 乘权重（0.2-0.5）。泛词主要伤害在 LIKE freq 路（BM25 的 IDF
            # 天然压高频），精准在此乘。词不删（召回保住），只压排序分。
            _wgt = _dw.get(w, 1.0)
            _rows = _db.execute(
                "SELECT (length(text) - length(replace(text, ?, ''))) / length(?) AS freq, id "
                "FROM chunks WHERE scan_id = ? AND text LIKE ? ESCAPE '\\' "
                "ORDER BY freq DESC LIMIT 100",  # 召回宽度（advisory 定案）：
                # 原按 k=30 每词截断，双词低频块（贷方7+期初5=12分）两词
                # Top30 都不进、求和轮空——放宽 100 后 Python 求和取 Top
                (safe_w, safe_w, scan_id, f"%{safe_w}%"),
            ).fetchall()
            like_rows += [(f * _wgt, i) for f, i in _rows]  # freq × 降权
    if not bm25_rows and not like_rows:
        return []
    # 融合两路 id：BM25 行优先（有真实相关度分），LIKE 行按词频补位。
    from collections import defaultdict
    if bm25_rows:
        top_ids = [i for _, i in sorted(bm25_rows)[:k]]
        # 多 2 字词求和（advisory 逮到 v1 覆盖 bug：'现金'freq15+'期初'freq1
        # 后写覆盖只留 1——多词命中应累加）
        freq_sum = defaultdict(int)
        for f, i in like_rows:
            freq_sum[i] += f
        for i, fs in sorted(freq_sum.items(), key=lambda kv: -kv[1])[:k]:
            if i not in top_ids and len(top_ids) < k:
                top_ids.append(i)
        score_map = {i: s for s, i in bm25_rows}
    else:
        freq_sum = defaultdict(int)
        for f, i in like_rows:
            freq_sum[i] += f
        top_ids = [i for i, _ in sorted(freq_sum.items(), key=lambda kv: -kv[1])[:k]]
        score_map = dict(freq_sum)
    with _db_lock:
        details = _db.execute(
            "SELECT id, file, seq, text FROM chunks WHERE id IN (%s)"
            % ",".join("?" * len(top_ids)),
            top_ids,
        ).fetchall()
    dmap = {d[0]: d for d in details}
    # score 仅路内排序用；RRF 融合时只看排名（两路分数量纲不同不能直接比）
    return [
        {"file": dmap[i][1], "seq": dmap[i][2], "score": score_map.get(i, 0), "text": dmap[i][3]}
        for i in top_ids if i in dmap
    ]


# ─────────────────────────————————————————————————
# 检索可调常量区（2026-09-15 函数内默认值全部提取成模块常量，
# 与顶部参数索引配套——改参数只看一处。必须定义在使用它们的函数之前）
RERANK_POOL = 100   # 五轮步3 池位归因定案（2026-09-18）：60→100——45 FAIL 实测 6 题答案块在 61-100 名直接救活；业界 Vertex/Azure 100 档。rerank token +67%（bge 免费，延迟 +0.4s）
                    # 扩到 60 治其中 8 个（31-60 名实测）。业界 Vertex/Azure 池宽 50-100，30 偏紧。rerank 0.66→1.10s（+0.44s，bge 免费）
TOP_K = 15          # 最终返回块数（三轮步1 从 10 调 15——池内排低题答案在 11-15 名可漏出）
RRF_WEIGHTS = [0.60, 0.40]  # RRF 路级权重 [语义路, 关键词路]（三轮步1扫参定案：两批200题 0.6:0.4 两批最优 88%/78%，原 0.45:0.55 两批最差 75%/60%——一轮拍的权重方向反了）
RRF_K = 60          # RRF 融合常数（业界默认 Elasticsearch 同款；第1/10名差距不悬殊）

def rrf_fuse(result_lists, k=TOP_K, rrf_k=RRF_K, weights=RRF_WEIGHTS):
    """多路结果融合成一份 Top-K。
    result_lists = [某路的hits列表, 另一路的hits, ...]（每路已各自排好序）

    为什么不能直接把两路的分数相加：语义路是 0~1 的余弦，关键词路是命中词数
    1~5——量纲不同，直接加等于让关键词路永远当分母。RRF 只用"排名"：
       score(块) = Σ 1/(60 + rank)
    某块在某路排第 1 名得 1/61、第 2 名得 1/62…两路都命中的块分数叠加。
    60 是业界默认常数（Elasticsearch 同款）：让第 1 名和第 10 名差距不至于
    太悬殊，单路冠军压不死双路都进前五的块

    ⑨ 优化第2步（2026-09-15）：weights 路级加权。表格密集语料（本项目
    74% Excel）业界经验关键词路要提权——传 [0.5, 0.5] 平权起步，
    评测集扫 0.6:0.4 / 0.5:0.5 / 0.4:0.6 定终值。不传=平权（向后兼容，
    现有调用零改动）。加权语义：某路的排名分乘以该路权重再加总——
    关键词路 0.6 时它的第 1 名贡献 0.6/61，语义路 0.4 的第 1 名 0.4/61"""
    scores = {}  # {(file, seq): [rrf分, text]} —— file+seq 是块的唯一身份证
    for li, hits in enumerate(result_lists):
        w = weights[li] if weights and li < len(weights) else 1.0
        for rank, h in enumerate(hits):
            key = (h["file"], h["seq"])
            s = w * 1.0 / (rrf_k + rank + 1)  # +1 把 0 基排名转成 1 基
            if key in scores:
                scores[key][0] += s  # 双路都认 = 真相关，叠加
            else:
                scores[key] = [s, h["text"]]
    ranked = sorted(scores.items(), key=lambda kv: -kv[1][0])[:k]
    return [
        {"file": f, "seq": s, "score": round(v[0], 6), "text": v[1]}
        for (f, s), v in ranked
    ]

# ─────────────────────────————————————————————————
# 8c：重排层（Cross-Encoder Reranker，三段式检索的最后一块拼图）
# ─────────────────────────————————————————————————
# 召回（语义+关键词）是"分开看"：快但粗；重排是"一起看"——"问题+文档"拼成
# 一对喂进专用模型逐字读关系再打分，慢但准（实测答案块 0.397 分 vs 噪声块
# 0.001 分）。位置在召回和生成之间：宽进（召回 Top-30 宁滥勿缺）-> 严出
# （重排精算取 Top-10）。ES/Algolia/OpenAI file search 同款骨架。

# 重排候选池大小：30 是甜点（候选越多 rerank 越慢越贵、收益边际递减；
# 实测 30 篇 0.66s / 8k token，50 篇 1.1s / 13k token）

def rerank(query, documents, top_n=TOP_K, api_key=None, api_url=None, model=None):
    """query = 用户问题；documents = 候选文档列表；top_n = 精算后取前几。
    api_key/api_url/model：BYOK 模式由调用方传入（缺省时 key 回退到向量 Key、
    url/model 回退到 RERANK_* 常量——用户没单独配重排也能跑）。
    一次 API 重排整池（30 对打包一个请求，不是 30 通电话）"""
    key = api_key or RERANK_API_KEY or EMBED_API_KEY
    if not key:
        raise ValueError("缺少重排 API Key——请在前端「⚙ 设置」里配置")
    payload = json.dumps({
        "model": model or RERANK_MODEL,   # 换家服务商（Cohere/Jina）改这里或前端配置
        "query": query,
        "documents": [d[:1500] for d in documents],  # 单篇截 1500 字：rerank 有长度上限，超了报错
    }).encode("utf-8")
    _silicon_acquire()  # 动态限流：取令牌（429 防护）——仅排队不取返回值
    # url 剥尾（与 embed_batch 同款）：BYOK 头可能传 /v1/embeddings 完整端点，
    # rerank 要的是 /v1/rerank——不剥会拼出 /v1/embeddings/rerank 404
    _rb = (api_url or RERANK_URL).rstrip("/")
    if _rb.endswith("/embeddings"):
        _rb = _rb[: -len("/embeddings")]
    req = urllib.request.Request(
        _rb + "/rerank",
        data=payload,
        headers={
            # 走重排 Key；用户没单独配时上游已回退到向量 Key（同账号场景）
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    # ★ 韧性三件套·rerank 域补齐（2026-09-19 业界标准——原只有异常降级裸序）：
    # ① 断路器入口闸：熔断期直接 raise（调用方降级裸序+用户可见提示，不再静默）
    # ② 重试：429/503/超时 退避重试 2 次（3s/6s——比 embed 轻，检索是交互路）
    # ③ 成功复位/失败记账（连续5败跳闸10分钟）
    if _svc_tripped("rerank", key):
        raise RuntimeError("排序服务熔断保护中（约10分钟自动恢复）——本次降级为粗排")
    body = None
    _t0_rr = time.time()
    for _att in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            _lg_search.info("rerank调用", extra={  # ⑫8 外部API留痕（大模型/排序调用可见）
                "pool": len(documents), "latency_ms": int((time.time() - _t0_rr) * 1000),
                "model": "bge-reranker-v2-m3"})
            break
        except urllib.error.HTTPError as _ce:
            if _ce.code in (429, 503) and _att < 2:
                time.sleep(3 * (_att + 1))
                continue
            _svc_fail("rerank", key)
            raise
        except (TimeoutError, urllib.error.URLError):
            if _att < 2:
                time.sleep(3)
                continue
            _svc_fail("rerank", key)
            raise
    _svc_ok("rerank", key)
    # ⑫8 A9 rerank 完整序留痕（100 名全程——明细档/排障用，
    # "答案块排第几被挤掉"的直接证据）
    global _LAST_RERANK_FULL
    _LAST_RERANK_FULL = [(r["index"], round(r["relevance_score"], 4)) for r in body["results"]]
    # 返回 {results: [{index, relevance_score}, ...]} 已按分数从高到低排好
    return [
        {"index": r["index"], "relevance_score": r["relevance_score"]}
        for r in body["results"][:top_n]
    ]

def _is_table_block(t):
    """语言无关判据：管道表行结构（| 分隔行 + ---| 分隔行）占比过 1/3"""
    ls = t.split("\n")
    return sum(1 for l in ls if l.startswith("|") or "---|" in l) >= max(2, len(ls) // 3)


def exact_match_boost(q, scan_id, pool=None, pool_size=25):
    """⑨ 五轮步3 方案1（业界标准 Exact Match Boosting——Elasticsearch
    constant_score / identifier search）：问题里的精确值（字母编号/金额/
    ≥4位数字，结构信号语言无关）LIKE 直查全库，命中块置于池头。
    生产 api_search 与评测器 eval_pipeline 共用本函数——抽共享防口径漂移
    （分桶配额那次"只改生产评测器没同步"的教训实录）。
    返回：精确命中块列表（可能为空）。调用方负责与 RRF 池合并去重。"""
    import re as _re
    exact_vals = _re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}|\d[\d,]*\.\d+|\d{4,}", q)
    if not exact_vals:
        return []
    hits = []
    seen_ids = set()
    with _db_lock:
        for v in exact_vals[:4]:  # 每题最多 4 个精确值，防长尾
            v_esc = (v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                      .replace("[", "\\[").replace("]", "\\]"))
            try:
                rows = _db.execute(
                    "SELECT id, file, seq, text FROM chunks WHERE scan_id=? AND text LIKE ? ESCAPE '\\' "
                    "LIMIT 20", (scan_id, f"%{v_esc}%")).fetchall()
                for rid, f, s, t in rows:
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        hits.append({"file": f, "seq": s, "score": 1.0, "text": t})
            except Exception:
                pass
    return hits[:pool_size]

_LAST_RERANK_FULL = []  # ⑫8 A9：最近一次 rerank 完整序（100 名全程——明细档用）

# ── ⑫8 B1/B3 明细档（2026-09-21 一次提问一个 JSON——零截断全现场） ──
_DETAIL_DIR = Path(__file__).parent / "logs" / "details"
_DETAIL_MAX_BYTES = 500 * 1024 * 1024  # B5：总大小上限 500MB（超删最老）

def dump_search_detail(scan_id, q, queries, sem_all, kw_all, pool, rerank_full, hits, second_pass=None):
    """⑫8 明细档共享 helper（生产 api_search 与评测 eval_one 共用）。
    一次提问一个 logs/details/<trace_id>.json：
    问题全文/MQ 全变体/双路每路 top100/RRF 池100/rerank100 分+最终15块全文。
    前端生成完成后 POST /api/detail/append 补写答案段（B2 两段式）。"""
    import traceback
    try:
        _DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        tid = get_trace_id()
        if not tid or tid == "-":
            return None
        def _simplify(hs, with_text=False, cap=100):
            out = []
            for h in (hs or [])[:cap]:
                d = {"file": h.get("file", ""), "seq": h.get("seq"), "score": h.get("score")}
                if with_text:
                    d["text"] = (h.get("text") or "")[:1500]
                out.append(d)
            return out
        doc = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_id": scan_id,
            "q": q,
            "queries": queries or [q],
            "semantic_top": _simplify(sem_all),
            "keyword_top": _simplify(kw_all),
            "rrf_pool": _simplify(pool),
            "rerank_full": [{"idx": i, "score": s} for i, s in (rerank_full or [])],
            "final_hits": _simplify(hits, with_text=True),
            "second_pass": second_pass,
        }
        p = _DETAIL_DIR / f"{tid}.json"
        if p.exists():  # 两段式：答案段先落过则保留
            try:
                old = _json.loads(p.read_text(encoding="utf-8"))
                old.update(doc)
                doc = old
            except Exception:
                pass
        p.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(p)
    except Exception:
        try:
            _lg_search.warning("明细档写入失败", extra={"exc": traceback.format_exc()[-120:]})
        except Exception:
            pass
        return None

def _detail_cleanup():
    """B5：明细档清理——按周删旧+总大小上限双保险（启动时跑一次）"""
    import time as _t
    try:
        if not _DETAIL_DIR.exists():
            return
        files = sorted(_DETAIL_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
        week_ago = _t.time() - 7 * 86400
        for f in files:
            if f.stat().st_mtime < week_ago:
                f.unlink()
        files = sorted(_DETAIL_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        while total > _DETAIL_MAX_BYTES and files:
            f = files.pop(0)
            total -= f.stat().st_size
            f.unlink()
    except Exception:
        pass

_detail_cleanup()  # 启动清一次

class _RerankDegraded(Exception):
    """rerank 降级哨兵（2026-09-19 每请求信号——advisary 定案：全局 flag 在 FastAPI
    线程池并发下有错标窗口（A 成功被 B 降级污染）+ 跳过 rerank 的请求读到残留值。
    改为哨兵异常：api_search 本地捕获——信号绑定本次请求，零串扰）"""

def rerank_category_aware(query, pool, k=TOP_K, api_key=None, api_url=None, model=None):
    """⑨ 五轮步3 案4 v3（2026-09-18 两轮降分复盘定稿）：格式感知重排——
    全局 rerank 序为主，仅"表格块=0"时保底补位。
    v1 封顶 bug：59→49（表格密集题被封 6 席）；v2 类别主序 bug：101-200 段
    88→21（tbl+txt 拼接把表格块全排文本块前——答案在 md 文本块的段全灭）。
    v3 逻辑（业界 category-aware 正确形态）：默认【全局 rerank 分数序】取
    Top-k（与无分桶完全一致）；仅当结果里表格块数 < TABLE_MIN 时，从池里
    按分数取表格块补位/交换进 Top-k——单一干预点，两边段都不伤。
    生产/评测器三处共用。API 失败降级回 RRF 池序"""
    TABLE_MIN = 0  # 表格块保底席【定稿：撤】（2026-09-18 全量对照定案）：撤保底
                   # 全量净 83.5%（1284/1537 剔A直算）vs 带保底 83.0%（1276/1537）——保底
                   # 在两个表格密集段赚 1-2 分，但六个普通段各亏 1-4 分，全卷
                   # 净 -0.5pp。纯全局分数序定稿，少一个干预点。
    try:
        ranked = rerank(query, [h["text"] for h in pool], top_n=len(pool),
                        api_key=api_key, api_url=api_url, model=model)
        by_idx = {r["index"]: r["relevance_score"] for r in ranked}
        order = sorted(range(len(pool)), key=lambda i: -by_idx.get(i, 0))
        def _mk(i):
            return {"file": pool[i]["file"], "seq": pool[i]["seq"],
                    "score": round(by_idx.get(i, 0), 4), "text": pool[i]["text"]}
        top = [order[i] for i in range(min(k, len(order)))]
        tbl_in_top = [i for i in top if _is_table_block(pool[i]["text"])]
        if len(tbl_in_top) >= TABLE_MIN:
            return [_mk(i) for i in top]  # 表格块够——纯全局序（与旧全量一致）
        # 表格块不足：从池里按 rerank 分数取表格块，替换 Top 尾部最低分块
        tbl_pool = [i for i in order if _is_table_block(pool[i]["text"])][:TABLE_MIN]
        if not tbl_pool:
            return [_mk(i) for i in top]
        need = TABLE_MIN - len(tbl_in_top)
        # 替换 Top 尾部（保留表格位不重复）
        tbl_set = set(tbl_in_top)
        for ti in tbl_pool:
            if ti in tbl_set:
                continue
            if len(top) >= k:
                top[-1] = ti  # 顶掉尾部最低分（top 本身按分排序，尾部最低）
            else:
                top.append(ti)
            tbl_set.add(ti)
            need -= 1
            if need <= 0:
                break
        return [_mk(i) for i in top]
    except _RerankDegraded:
        # 本层降级信号向上抛（api_search 捕获写响应体）——哨兵只管信号
        raise
    except Exception as _re:
        # 重排 API 挂了/超时 -> 降级回 RRF 排序（不能因为最后一层挂了就不回答）
        _lg_search.warning("rerank降级裸序", extra={"exc": type(_re).__name__,
            "hint": "排序服务降级中——本次用粗排（质量可能下降，约10分钟自动恢复）"})
        raise _RerankDegraded(str(_re))  # 哨兵：每请求信号（api_search 本地捕获）
# ② LLM 兜底：结构特征不足时，把问题+库里实际表结构喂给 LLM 判"查表题还是语义题"
#    ——LLM 看的是当前库的真实内容，换库自动适配，零硬编码
_STRUCT_SIGNAL = re.compile(r"[A-Za-z]*-?\d{3,}|\d+\.\d+")  # 1002/546949.1166/FKD0004（编号与数值，语言无关）

# ══════════════════════════════════════════════════════════════
# ⑨ 五轮步 4-2：检索查询增强四选手（2026-09-18，选型赛）
# 业界出处：①Multi-Query=LlamaIndex MultiQueryRetriever/LangChain 标配
# ②HyDE=论文 Gao et al. 2022/LlamaIndex HyDEQueryTransform
# ③查询分解=LlamaIndex SubQuestionQueryEngine ④组合路由=Agentic RAG 标准
# 设计：共享 LLM 调用基建（与 SQL 意图同款 _llm_chat），返回统一格式
# [查询列表]——调用方（评测器 --enhance 参数）拿列表逐个检索后 RRF 合并。
# 假想文档（HyDE）只是检索探针不进答案——四层防幻觉不受影响（已向用户确认）
# ══════════════════════════════════════════════════════════════

# ★ 4-2B 刀1 计算工具化·业界正解版（2026-09-19 重写）：
# LLM 吐结构化 {op, operands}（temperature 0——多语言天然支持、操作数由语义定）
# → 代码确定性计算 → 注入【系统已核算】。替代已废弃的正则词表版
# （词表版四缺陷：中文硬编码违通用性/加减靠猜/无乘除/"74块2毛5"拆成 74+2+5）。
# 安全闸：LLM 预调用挂/JSON 烂/运算不支持 → 不注入（宁缺勿错）。
def mmr_diversify(query_vec, pool, top_n=TOP_K, lambda_param=0.7, candidate_pool=30, scan_id=None, vec_lookup=None):
    """MMR（Maximal Marginal Relevance，最大边际相关性）扩域版——五轮步4-3。

    治【同文件挤占型挂题】：多行相似块文件（考勤表/银行对账单）的块向量几乎
    相同，纯分数取 Top15 会被同文件双胞胎块灌满，真答案块被挤出。
    MMR 在分数与多样性间平衡：每轮选「rerank 分数高 × 与已选块相似度低」
    的块，同文件相似块自然限席。

    纯本地计算（零模型调用）：块向量已存 chunks.vector，相似度=点积。
    lambda_param=0.7：分数为主、轻度去重（多跳题答案在同表相邻块的场景
    不会被过度压掉——λ 可调，0=纯多样性 1=纯分数）。

    query_vec：原题向量（与 pool 里的块向量同域）；
    pool：rerank 后的候选（已按分数降序，取前 candidate_pool 参选）；
    返回：MMR 选出的 top_n 块（保持选中顺序）。
    2026-09-20 四轮步4-3 开工实现。"""
    if not pool or len(pool) <= top_n:
        return pool
    import numpy as _np
    cands = pool[:candidate_pool]
    # 块向量从库里取（chunks.vector 已存——不重复向量化）
    # 向量来源（2026-09-20 死锁根治·advisory 定案）：优先【内存查表函数】——
    # 语义路启动时已把全库矩阵载内存，vec_lookup(file,seq)→ndarray 零 DB 查询；
    # 生产（无内存矩阵）退批量 SELECT（套 _db_lock——2 线程并发游标死锁实录）。
    vecs = []
    for h in cands:
        v = h.get("_vec")
        if v is None and vec_lookup is not None:
            v = vec_lookup(h["file"], h["seq"])
        vecs.append(v)
    if any(v is None for v in vecs) and scan_id is not None and vec_lookup is None:
        keys = {(h["file"], h["seq"]) for h in cands}
        vmap = {}
        with _db_lock:
            for f, s, v in _db.execute(
                "SELECT file, seq, vector FROM chunks WHERE scan_id=? AND vector IS NOT NULL",
                (scan_id,)):
                if (f, s) in keys and v:
                    vmap[(f, s)] = _np.frombuffer(v, dtype=_np.float32)
        vecs = [h.get("_vec") or vmap.get((h["file"], h["seq"])) for h in cands]
    # 没有向量的候选退纯分数序（兼容降级）
    if any(v is None for v in vecs):
        return cands[:top_n]
    V = _np.stack(vecs).astype(_np.float32)
    _nrm = _np.linalg.norm(V, axis=1, keepdims=True)
    V = V / (_nrm + 1e-9)  # 行归一（numpy 无 normalize——手写）
    qv = _np.asarray(query_vec, dtype=_np.float32)
    qv = qv / (_np.linalg.norm(qv) + 1e-9)
    sim_q = V @ qv                      # 与问题的相似度（= 分数代理）
    sim_mm = V @ V.T                    # 块间相似度矩阵
    scores = _np.array([h.get("score", 0) for h in cands], dtype=_np.float32)
    # 分数归一到 0-1（与相似度同量纲）
    smin, smax = scores.min(), scores.max()
    rel = (scores - smin) / (smax - smin + 1e-9) if smax > smin else sim_q
    selected, remaining = [], list(range(len(cands)))
    while len(selected) < top_n and remaining:
        best_i, best_v = None, -1e9
        for i in remaining:
            div = 1.0 - (max(sim_mm[i][j] for j in selected) if selected else 0.0)
            v = lambda_param * rel[i] + (1 - lambda_param) * div
            if v > best_v:
                best_v, best_i = v, i
        selected.append(best_i)
        remaining.remove(best_i)
    return [cands[i] for i in selected]


def calc_tool(q, chat_key=None, chat_url=None):
    """LLM 结构化意图 + 代码计算（业界 Structured Output + Tool Use 组合）。
    返回核算结果文本或 None（非计算题/任何失败都不注入）。"""
    if not chat_key:
        return None
    # 零成本数字闸（业界生产惯例 2026-09-19 用户拍板）：问题无数字直接跳过——
    # 连 LLM 判定都不调（省 1 次往返）；有数字才让 LLM 判意图
    import re as _re_gate
    if not _re_gate.search(r"\d", q):
        return None
    raw = _llm_chat(
        "判断这个问题是否需要算术计算（加减乘除）。如果需要，提取运算符和操作数，"
        '输出 JSON：{"need": true, "op": "+|-|*|/", "operands": [数1, 数2, ...]}'
        '（op 只能是四则之一；operands 是问题原文里的数字，含小数；'
        '口语金额如"74块2毛5"要归一成 74.25）。不需要计算输出 {"need": false}。'
        "只输出 JSON，不要解释。\n\n问题: " + q, api_key=chat_key, api_url=chat_url)
    if not raw:
        return None
    # 剥可能的 markdown 栅栏
    raw = raw.strip().strip("`").replace("```json", "").replace("```", "").strip()
    try:
        intent = json.loads(raw)
    except Exception as _e:
        return None  # JSON 烂不注入
    if not isinstance(intent, dict) or not intent.get("need"):
        return None
    op = intent.get("op")
    operands = intent.get("operands")
    if op not in ("+", "-", "*", "/") or not isinstance(operands, list) or len(operands) < 2:
        return None
    try:
        nums = [float(x) for x in operands]
    except (TypeError, ValueError):
        return None
    # 确定性计算（减法：首数减其余；除法：首数除以其余；加乘：全操作数）
    if op == "+":
        result = sum(nums)
    elif op == "-":
        result = nums[0] - sum(nums[1:])
    elif op == "*":
        result = 1
        for x in nums: result *= x
    elif op == "/":
        result = nums[0]
        for x in nums[1:]:
            if x == 0: return None  # 除零不注入
            result /= x
    def _fmt(x):
        s = f"{x:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    expr = (" + " if op == "+" else " - " if op == "-" else " × " if op == "*" else " ÷ ").join(_fmt(x) for x in nums)
    return (f"【系统已核算——问题中的数值由代码计算，直接引用此结果，禁止自己心算】\n"
            f"{expr} = {_fmt(result)}")

def enhance_multiquery(q, api_key=None, api_url=None):
    """4-2① Multi-Query：LLM 把问题改写成 3 个不同角度的变体（返回含原题 4 条）。
    角度覆盖：口语化改述 / 专业术语版 / 英文版（中英双表语料对症）"""
    raw = _llm_chat(
        "把这个问题改写成3个用于知识库检索的变体查询。要求：变体1用专业术语重新表述；"
        "变体2用更口语化的说法；变体3翻译成英文。每行一个查询，不要编号不要解释。\n\n"
        f"原始问题: {q}", api_key=api_key, api_url=api_url)
    if not raw:
        out = [q]
    else:
        variants = [v.strip() for v in raw.strip().split("\n") if v.strip() and len(v.strip()) > 1][:3]
        out = [q] + variants if variants else [q]
    out += _dict_expand_query(q)  # 冲刺③：词典精确扩展变体（中文术语命中才加，纯本地零 LLM 成本）
    # 去重（LLM 变体与词典变体可能重复）+ 保条数上限 6（原题+3 LLM+2 词典）
    _seen = set()
    _out = []
    for v in out:
        k = v.strip().lower()
        if k not in _seen:
            _seen.add(k)
            _out.append(v)
    return _out[:6]

def enhance_hyde(q, api_key=None, api_url=None):
    """4-2② HyDE：LLM 编一段假想答案文档（返回 [原题, 假想文档]）——拿假想文档
    的语义去检索（文档查文档，解决'问题短文档长'的嵌入错配）。假想文档只是
    检索探针，不进 Top15、不给模型看——答案照常来自真实块"""
    hypo = _llm_chat(
        "假设你是知识库里的文档。写一段50字以内的假想答案文档（包含可能的表格字段名、"
        "数值格式、术语），用于语义检索定位。只输出文档内容，不要解释。\n\n"
        f"问题: {q}", api_key=api_key, api_url=api_url)
    return [q, hypo] if hypo and len(hypo.strip()) > 5 else [q]

def enhance_decompose(q, api_key=None, api_url=None):
    """4-2③ 查询分解：多跳问题拆成子问题（返回 [原题, 子问题1, 子问题2, ...]）。
    触发条件：问题问了两件及以上事物的属性（'A的X和B的Y'型）"""
    raw = _llm_chat(
        "这个问题需要从多个位置分别找信息吗？如果需要，把它拆成独立的子问题"
 "（每个子问题只问一件事）。如果单点可答，原样返回。每行一个查询，不要编号。\n\n"
        f"问题: {q}", api_key=api_key, api_url=api_url)
    if not raw:
        return [q]
    subs = [v.strip() for v in raw.strip().split("\n") if v.strip() and v.strip() != q][:3]
    return [q] + subs if subs else [q]

def enhance_combo(q, api_key=None, api_url=None):
    """4-2④ 组合智能路由：LLM 判单跳/多跳 → 单跳走 MQ+HyDE，多跳走 分解+每子问题 MQ。
    按题型花钱（单跳不上分解的 API），成本介于单用与完全体之间"""
    verdict = _llm_chat(
        "这个问题需要从多个不同位置各找一部分信息才能完整回答吗"
        "（比如问两个事物的属性/对比/合计）？只答 A 或 B。A=单点可答 B=需拆解。\n\n"
        f"问题: {q}", api_key=api_key, api_url=api_url)
    if verdict and "B" in verdict.upper()[:3]:
        # 多跳：分解 + 每个子问题 MQ（子问题各自变体检索）
        subs = enhance_decompose(q, api_key, api_url)
        queries = []
        for sq in subs:
            queries += enhance_multiquery(sq, api_key, api_key and api_url)[:2]  # 每子题取1个变体+自身
        return queries[:5]
    else:
        # 单跳：MQ + HyDE（变体 + 假想文档一起上）
        mq = enhance_multiquery(q, api_key, api_url)[:2]
        hy = enhance_hyde(q, api_key, api_url)[1:]
        return (mq + hy)[:4]


def route_query(q: str, scan_id=None, vec_key=None, vec_url=None) -> str:
    # 注：vec_key/vec_url 形参名是历史遗留——实际传【公司代理 chat 配置】（api_search 已改传 chat_key）
    """返回 'sql' 或 'search'。
    第一层：结构信号（语言无关）——问句带编号/精确数值 → sql（查表大概率有解）
    第二层：LLM 动态判定——结构不足时让 LLM 看问题+当前库表结构现场决策。
    无表库（纯文档语料）→ tables 空 → 直接 search，LLM 都不用问"""
    if _STRUCT_SIGNAL.search(q):
        return "sql"
    # 库里没表 → SQL 路无意义，直接检索
    if scan_id is not None:
        with _db_lock:
            n_tables = _db.execute(
                "SELECT COUNT(*) FROM tables WHERE scan_id=?", (scan_id,)
            ).fetchone()[0]
        if n_tables == 0:
            return "search"
    # ── 冲刺⑥ 路由提权·第三层：表名信号（2026-09-22）──────────────
    # 病根（94 道召回问题三分类归因）：54 道期望表格文件的题被判成向量路——
    # LLM 判定偏保守（"余额是多少"判成语义检索）。治法：问题词命中
    # 库内真实文件名词干（对账单/科目/余额/凭证…——从 tables 元数据
    # 动态提取，零硬编码换库自适应）≥2 词时强判 sql——这些词出现在
    # 表格文件名里，说明用户在问表格内容。
    try:
        with _db_lock:
            _fname_words = _db.execute(
                "SELECT DISTINCT file FROM tables WHERE scan_id=?", (scan_id,)).fetchall()
        # 文件名+列名词干分词（列名含中文表头——治"问中文表名"的跨语言鸿沟：
        # 问"对账单余额"命不中英文文件名 AccountStatement，但命得中
        # 中文列名"余额/借方/科目"——2026-09-22 实测题284/728）
        import re as _re6
        _corpus_words = set()
        _cols_rows = _db.execute(
            "SELECT file, cols FROM tables WHERE scan_id=?", (scan_id,)).fetchall() if False else None
        for (fn,) in _fname_words:
            stem = _re6.sub(r'[-_()\d.]+', ' ', str(fn).rsplit('.', 1)[0])
            for w in stem.split():
                if len(w) >= 2 and not w.isdigit():
                    _corpus_words.add(w)
        with _db_lock:
            for (fn2, _cols) in _db.execute(
                    "SELECT file, cols FROM tables WHERE scan_id=?", (scan_id,)).fetchall():
                try:
                    for c in (_json.loads(_cols) if isinstance(_cols, str) else _cols):
                        for w in _re6.split(r'[\s:：/|·]+', str(c)):
                            w = w.strip()
                            if len(w) >= 2 and not w.isdigit():
                                _corpus_words.add(w)
                except Exception:
                    continue
        _q_words = set(q)
        _hits = sum(1 for w in _corpus_words if w in q)
        if _hits >= 2:
            return "sql"
    except Exception:
        pass

    # LLM 动态判定（零词表）：给出问题样例，让 LLM 分类——不依赖任何语料词
    verdict = _llm_chat(
        # 2026-09-23 通用性修（零语料写死）：例词抽象化——不带业务域词，
        # 换任何库（财务/HR/游戏/医学）路由判断同效
        "判断这个提问更适合哪种处理方式。A=查表取精确值（问表格/清单中的具体数值、"
        "金额、数量、编码、日期，或某行某列内容、编号与名称的对应关系）；"
        "B=语义检索（问原因/流程/怎么做/设计思路/总结归纳）。"
        "拿不准时倾向 A（结构化数据查表更快更准）。只回答 A 或 B。"
        "\n\n提问: " + q, api_key=vec_key, api_url=vec_url)
    if verdict and verdict.strip().upper().startswith("A"):
        return "sql"
    return "search"

def sql_route_search(scan_id: int, q: str, vec_key=None, vec_url=None):
    # 注：vec_key/vec_url 形参名是历史遗留——实际传【公司代理 chat 配置】
    """⑨ 四轮步3：SQL 查询路——路由判 sql 的题走这里。把 tables 元数据库的
    表结构（列名+样例行）喂 LLM 生成 pandas 表达式，本地执行返回精确行。
    任何失败返回 None（调用方回退检索路）——SQL 路是增强，不是主链路。"""
    # 1) 找相关表：问题与 sheet 名/列名做词面匹配（表数量多时先粗筛）
    with _db_lock:
        tables = _db.execute(
            "SELECT file, sheet, cols, rows_json FROM tables WHERE scan_id=?", (scan_id,)
        ).fetchall()
    if not tables:
        return None
    # 粗筛：问题里的词在 sheet/列名/文件名里出现 → 候选表
    # 通用切词（语言无关）：中文 jieba + 英文/数字连续段——粗筛用，比整句 token 细
    import jieba as _jb
    q_tokens = set(w for w in _jb.cut(q) if len(w.strip()) >= 2)
    q_tokens |= set(re.findall(r"[A-Za-z][A-Za-z0-9]{2,}|\d+", q))
    scored = []
    for file, sheet, cols_j, rows_j in tables:
        cols = json.loads(cols_j)
        # 粗筛面=文件名+sheet+列名+前5行数据（值也是特征——'yxck'是数据不是列名）
        _rows = json.loads(rows_j)
        blob = f"{file} {sheet} {' '.join(cols)} {' '.join(' '.join(r) for r in _rows[:5])}"
        overlap = sum(1 for t in q_tokens if t in blob)
        # 数字信号加权（通用）：问题里的 3 位以上数字在表数据前 5 行出现 → +3/词
        # （'1002' 出现在数据里的表才是答案表——列名/文件名撞词的表压不过它）
        _data_head = ' '.join(' '.join(r) for r in _rows[:5])
        overlap += sum(3 for t in q_tokens if t.isdigit() and len(t) >= 3 and t in _data_head)
        if overlap > 0:
            scored.append((overlap, file, sheet, cols, _rows))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return None
    # 2) 取 Top-3 候选表喂 LLM 生成结构化筛选意图（非代码——安全：LLM 输出
    # JSON 意图，本地固定解释器执行，零 eval 零注入面。通用性：意图是
    # {列,值,条件} 结构，语言无关，任何库任何语言同构）
    schema_parts = []
    for i, (ov, file, sheet, cols, rows) in enumerate(scored[:3]):  # 3 张名片（瘦身：10→3，正确表基本在粗筛前3）
        sample = [[str(c)[:15] for c in r] for r in rows[:1]]  # 瘦身：1 行样例，每格截 15 字
        # 列名瘦身：保末两段复合名（"科目余额表-…-期末余额-借方"→"期末余额-借方"）——
        # advisory 实锤：split[-1] 会把期初/期末余额-借方都砍成"借方"，判别信息全丢
        slim_cols = ['-'.join(c.split('-')[-2:])[:16] for c in cols]
        schema_parts.append(
            f"表{i+1}: 文件={file} sheet={sheet}\n列名: {json.dumps(slim_cols, ensure_ascii=False)}\n样例: {json.dumps(sample, ensure_ascii=False)}")
    schema = "\n\n".join(schema_parts)  # 瘦身版拼接（三名片×一短行 ≈1200 字 vs 原 6229）
    prompt = (
        "你是数据查询助手。根据用户问题和表结构，输出一个 JSON 查询意图（不要代码不要解释，必须是合法 JSON 对象，禁止只输出 None 或 null）。\n"
        '格式: {"table": 表序号(1-3), "filters": [{"col": "列名", "val": "匹配值", "exact": true或false}], "target_col": "要取值的列名，留空表示整行"}\n'
        "filters 的匹配值必须是问题里出现的原文。启发式：纯数字/编码样式的值优先选值看起来都是短代码的列（如样例里全是编号的那列），文本值选内容是名称/描述的列。exact=true 精确等于，false 包含即可。\n\n"
        f"用户问题: {q}\n\n{schema}")
    raw = _llm_chat(prompt, api_key=vec_key, api_url=vec_url)
    if not raw:
        return None
    # 3) 解析意图 JSON（容错：剥 markdown 栅栏）+ 硬闸（advisory：
    # "None"/"42"/"[]" 能过 json.loads 但类型错——必须 dict 校验，
    # 否则 intent.get 在 try 外炸 AttributeError 又一种静默死）
    try:
        intent = json.loads(raw.strip().strip("`").replace("```json", "").replace("```", "").strip())
        if not isinstance(intent, dict):
            _lg_sql.warning("SQL意图非dict", extra={"q": q[:60], "raw": raw[:100]})  # ⑫5
            return None
    except Exception:
        _lg_sql.warning("SQL意图解析失败", extra={"q": q[:60], "raw": raw[:100]})  # ⑫5
        return None
    t_idx = int(intent.get("table", 1)) - 1
    if t_idx < 0 or t_idx >= len(scored):
        return None
    filters = intent.get("filters") or []
    target_col = intent.get("target_col")
    # 4) 固定解释器执行（无 eval）+ 容错矩阵：
    # A. col 对齐——LLM 幻觉列名时找子串最近的真列名
    # B. 多表重试——LLM 选的表查空时依次试其余候选表（Top10）
    # C. col 对齐失败的 filter 降级为任意列匹配（不废弃整表）
    def _align_col(c, col_set):
        if c in col_set:
            return c
        for real in col_set:  # 子串对齐：LLM col 是真列名的一部分或反之
            if c and (c in real or real in c):
                return real
        return None
    # ── 冲刺⑥ A 类：值格式归一（2026-09-22 定位修复——此前定义丢失致 NameError，
    # SQL 路全程崩溃回退，route=None 的真凶）──────────────────
    # 病根（题533）：问题"11月11日" vs 表数据"11-11-2025"字面不匹配。
    # 治法：两边提取数字串前缀包含比对（1111 vs 11112025 → 前缀含即命中）。
    def _norm_digits(s):
        ds = ''.join(ch for ch in str(s) if ch.isdigit())
        return ds if len(ds) >= 3 else None
    def _val_match(v, cell, exact):
        v_s, c_s = str(v), str(cell)
        if exact:
            if c_s.strip() == v_s:
                return True
        else:
            if v_s in c_s or c_s.strip() == v_s:
                return True
        # 数字归一路（治日期"11月11日"↔"11-11-2025"）——
        # ⑥ bug 修正（2026-09-22 advisory 实锤）：纯数字编码（4-8 位、
        # 两边都是纯数字串）只做精确相等不做前缀包含——
        # 否则 1001 ⊂ 1001001 被当命中，SQL 把 1001 行错答给 1001001 查询
        vd, cd = _norm_digits(v_s), _norm_digits(c_s)
        if vd and cd:
            v_pure = v_s.replace(',', '').replace('.', '').replace(' ', '').strip()
            c_pure = c_s.replace(',', '').replace('.', '').replace(' ', '').strip()
            both_code = v_pure.isdigit() and c_pure.isdigit() and 3 <= len(v_pure) <= 10 and 3 <= len(c_pure) <= 10
            if both_code:
                return vd == cd  # 编码类：精确相等（1001 ≠ 1001001）
            if vd.startswith(cd) or cd.startswith(vd):
                return True  # 日期类：前缀包含（1111 ⊂ 11112025 ✓）
        return False
    for t_try in [t_idx] + [i for i in range(min(10, len(scored))) if i != t_idx]:
        ov, file, sheet, cols, rows = scored[t_try]
        col_set = set(cols)
        aligned = []
        viable = True
        for f in filters:
            c = _align_col(f.get("col"), col_set)
            v = str(f.get("val", "")).strip().strip("，。、；：-_=（）()[]【】 ").strip()
            if not v:
                viable = False
                break
            aligned.append((c, v, bool(f.get("exact", True))))  # c=None → 任意列匹配
        if not viable:
            continue
        result_rows = []
        for r in rows:
            row = dict(zip(cols, r))
            ok = True
            for c, v, exact in aligned:
                if c is None:  # C. 任意列匹配（col 对齐失败的降级路）
                    if not any(_val_match(v, cell, False) for cell in row.values()):  # ⑥ A 类归一
                        ok = False
                        break
                    continue
                cell = str(row.get(c, ""))
                if not _val_match(v, cell, exact):  # ⑥ A 类：数字归一（日期/千分位/编号格式变体）
                    ok = False
                    break
            if ok:
                result_rows.append(row)
        if not result_rows:  # D. 行值回退（advisory 根治法）：列匹配全空时，
            # 退化为"值在整行任意单元格出现即命中"——治 LLM 猜错近义列
            # （借/贷方向、期初/期末这类语义不可判的歧义），语言无关零词表
            for r in rows:
                row = dict(zip(cols, r))
                if all(any(_val_match(v, cell, False) for cell in row.values())  # ⑥ A 类归一
                       for _, v, _ in aligned):
                    result_rows.append(row)
        if result_rows:
            # 5) 结果序列化（target_col 有值则只取该列，否则整行）
            text = f"查询结果（来自 {file} / {sheet}）：\n"
            for r in result_rows[:10]:
                if target_col and isinstance(target_col, str) and target_col in r:
                    text += json.dumps({target_col: r[target_col]}, ensure_ascii=False) + "\n"
                else:
                    text += json.dumps(r, ensure_ascii=False) + "\n"
            _lg_sql.info("SQL路命中", extra={"file": file[:60], "sheet": sheet, "q": q[:60]})  # ⑫5
            # ⑫8 B4 SQL 路明细档（完整 SQL 现场：问题/意图/表结构/命中的行全文）
            try:
                _DETAIL_DIR.mkdir(parents=True, exist_ok=True)
                _tid4 = get_trace_id()
                if _tid4 and _tid4 != "-":
                    _p4 = _DETAIL_DIR / (_tid4 + "_sql.json")
                    _p4.write_text(_json.dumps({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "q": q,
                        "file": file, "sheet": sheet,
                        "matched_rows": text.strip()[:5000],
                    }, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            return [{
                "file": file, "seq": f"sql:{sheet}", "score": 1.0, "text": text.strip()
            }]
    _lg_sql.warning("SQL路三表全空", extra={"q": q[:60]})  # ⑫5
    return None

# ★ 韧性三件套基建（2026-09-19 业界标准补齐——resilience4j/Hystrix 模式）：
# 超时（timeout）+ 重试（retry+退避）+ 断路器（breaker）——每个故障域一套。
# 三个故障域：chat（已有 breaker）/ embed（本轮补 breaker）/ rerank（本轮补全三件）
# 通用小断路器：按 (服务, key) 隔离，连续 N 败跳闸 T 秒——成功复位
_SVC_BREAKERS = {}  # {(svc, key): [fails, tripped_ts]}
_SVC_BREAK_AT = 5   # 连续失败阈值（与 chat 断路器同款）
_SVC_BREAK_COOLDOWN = 600  # 冷却 10 分钟（比 chat 的 30min 短——云抖动恢复更快）

def _svc_tripped(svc, key):
    """断路器是否跳闸中（True=熔断期，调用方应直接走降级）。
    冷却到期时清状态（否则 st[1] 恒非零，_svc_fail 的 not st[1] 闸住
    永不再跳闸——一次性断路器 bug，advisory 2026-09-19 实锤）"""
    st = _SVC_BREAKERS.get((svc, key))
    if not st or not st[1]:
        return False
    if _time.time() - st[1] < _SVC_BREAK_COOLDOWN:
        return True
    _SVC_BREAKERS[(svc, key)] = [0, 0.0]  # 冷却到期复位（下轮可再跳闸）
    return False

def _svc_fail(svc, key):
    """记一次失败——连续达阈值跳闸"""
    st = _SVC_BREAKERS.setdefault((svc, key), [0, 0.0])
    st[0] += 1
    if st[0] >= _SVC_BREAK_AT and not st[1]:
        st[1] = _time.time()
        _lg_search.warning("服务断路器跳闸", extra={"svc": svc, "fails": st[0],
            "cooldown_s": _SVC_BREAK_COOLDOWN, "key": (key or "")[:8]})

def _svc_ok(svc, key):
    """成功复位"""
    _SVC_BREAKERS[(svc, key)] = [0, 0.0]

# ⑨ 断路器（2026-09-17）：SQL 路 LLM 连续失败达阈值 → 跳闸整轮关闭
# （四轮步3 全量实测白烧 1287 次失败调用≈40 分钟——每题硬打限流墙）
_SQL_LLM_BREAK_AT = 5    # 连续 5 次失败跳闸（按 key 隔离）
_SQL_LLM_STATE = {}      # {api_key: [fails, tripped_ts]}——BYOK 多用户互不串扰

def _llm_chat(prompt, api_key=None, api_url=None):
    """轻量 LLM 调用（SQL 意图/路由兜底/judge 用）。
    ★ 用户架构（2026-09-17 定谳）：聊天模型一律走公司代理（X-Chat-Key/X-Chat-Url），
    硅基流动只做向量和排序——四轮借 vecKey 打硅基流动 chat 是配置错误（免费 key
    打收费端点必 429，浪费两天排查）。api_key/api_url 参数由调用方传【公司代理】配置。
    断路器按 key 隔离：同一把 key 连续 5 次失败 → 跳闸 30 分钟"""
    _t0_llm = time.time()  # ⑫8 LLM 调用计时
    _k = api_key or "none"  # 断路器按 key 隔离（未传 key 用哨兵值）
    _st = _SQL_LLM_STATE.get(_k)
    if _st and _st[1]:
        if time.time() - _st[1] > 1800:  # 冷却复位
            _SQL_LLM_STATE[_k] = [0, 0.0]
        else:
            return None
    else:
        if _st is None:
            _SQL_LLM_STATE[_k] = [0, 0.0]
    if not api_key or not api_url:
        return None  # ★ 没有公司代理配置就不跑聊天调用（不退 vecKey——那是向量专用）
    # 客户端节流（2026-09-17 实测：评测密度下 411 发 334 失败=81% 限流——SQL 被掐死）：
    # 同一把 key 的 chat 调用强制间隔 1.5s，把速率压回代理限流线下
    _st = _SQL_LLM_STATE.setdefault(_k, [0, 0.0])
    if len(_st) < 3:
        _st.append(0.0)  # [fails, tripped_ts, last_call_ts]
    elif time.time() - _st[2] < 1.5:
        time.sleep(1.5 - (time.time() - _st[2]))
    _st[2] = time.time()
    base = api_url.rstrip("/")
    if base.endswith("/embeddings"):
        base = base[: -len("/embeddings")]
    url = base + "/chat/completions"
    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "temperature": 0, "max_tokens": 200,
        # 关思考（2026-09-17 实测定案）：默认时模型把思考写进 reasoning_content、
        # 正文 content 返回空串（评测里'LLM raw: None'的真凶）；关掉后 1.6s
        # 返回合法 JSON（3/3 命中），比 qwen3.8-max 思考 13s 快 8 倍
        "enable_thinking": False,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    try:
        # 韧性三件套·重试（2026-09-19 业界补齐）：chat 域原只有超时+断路器+节流
        # ——缺重试。429/503/超时 退避重试 2 次（聊天是交互式：轻退避快败，
        # 不像 embed 批处理可以长等——用户体验优先）
        _chat_body = None
        for _att in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90) as _cr:
                    _chat_body = json.loads(_cr.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as _ce:
                if _ce.code in (429, 503) and _att < 2:
                    time.sleep(2 * (_att + 1))  # 2s/4s 轻退避
                    continue
                raise
            except (TimeoutError, urllib.error.URLError):
                if _att < 2:
                    time.sleep(2)
                    continue
                raise
        d = _chat_body
        _SQL_LLM_STATE[_k] = [0, 0.0]  # 成功复位
        # ⑫8 大模型调用留痕（2026-09-21 何时调了大模型要可见）
        _lg_chat.info("LLM调用", extra={
            "model": "deepseek-v4-flash", "latency_ms": int((time.time() - _t0_llm) * 1000),
            "chars": len(d["choices"][0]["message"]["content"] or "")})
        return d["choices"][0]["message"]["content"]
    except Exception as e:
        _st = _SQL_LLM_STATE.setdefault(_k, [0, 0.0])
        _st[0] += 1
        if _st[0] >= _SQL_LLM_BREAK_AT:
            _st[1] = time.time()
            _pf.warning("SQL路断路器跳闸[key=%s...]（连续%d次失败，冷却30分钟）", _k[:8], _st[0])
        _pf.warning("SQL路 LLM 调用失败(%d/%d): %s", _st[0], _SQL_LLM_BREAK_AT, type(e).__name__)
        _lg_chat.warning("LLM调用失败", extra={"exc": type(e).__name__, "latency_ms": int((time.time() - _t0_llm) * 1000)})
        return None



@app.get("/api/kb/{scan_id}/search")
def api_search(scan_id: int, q: str, k: int = TOP_K, use_hybrid: bool = True, use_rerank: bool = True, request: Request = None, pool: int = None):
    # pool（2026-09-23 通用性③）：重排候选池深度——请求级覆盖 RERANK_POOL
    # （前端档位：快60/均衡100/深150；None=用默认 100）
    # 2026-09-23 修（advisory blocker：函数内对 RERANK_POOL 赋值会把它变成
    # 整函数局部变量——pool=None 路径读未赋值名 → UnboundLocalError → 搜索 500。
    # 与当年 eval_pipeline SKIP 事故同款作用域病。改独立局部名 _pool）
    _pool = RERANK_POOL if pool is None else max(30, min(int(pool), 300))
    import time as _t_m; _t_m0 = _t_m.time()  # ⑫7b 指标埋点
    """q = 用户的问题（query）；k = 取最相关的几块。
    use_hybrid = True（默认）跑混合检索；False 回退纯语义（对比实验用）。
    use_rerank = True（默认）召回后加 Cross-Encoder 重排；False 只到 RRF。
    三段式：召回（语义+关键词 RRF 宽进 Top-30）-> 重排（精算严出 Top-k）
    BYOK：X-Vec-Key / X-Vec-Url 请求头携带用户自己的向量服务配置（用完即弃）；
    重排可用 X-Rerank-Key / X-Rerank-Url / X-Rerank-Model 单独配置，
    不配则回退到上面的向量配置（老用户零改动）"""
    vec_key, vec_url = _vec_from_request(request)
    embed_model = _embed_model_from_request(request)
    rerank_key, rerank_url, rerank_model = _rerank_from_request(request)
    # 重排 -> 向量 逐级回退：只填过一把向量 Key 的用户重排照样生效
    rerank_key, rerank_url = rerank_key or vec_key, rerank_url or vec_url
    q = q.strip()
    if not q:
        return {"error": "问题不能为空"}
    # ★ 用户架构（2026-09-17 定谳）：聊天类调用（SQL 意图/路由兜底/judge）一律走
    # 公司代理（X-Chat-Key/X-Chat-Url）——设置界面①的配置；硅基流动（②）只做向量
    # 和排序。四轮借 vecKey 打硅基流动 chat 是配置错误（免费 key 打收费端点必 429）。
    chat_key, chat_url = _chat_from_request(request)
    # ── 冲刺⑥ A 类修复（2026-09-22）：表名信号前置到 api_search ──
    # 病根：原第三层在 route_query 内部，而 api_search 的调用链是
    # route_query(...) if chat_key else "search"——chat_key 为空时整条
    # 路跳过，纯本地的列名信号也跟着死。治法：列名信号提到这里（零 LLM
    # 依赖、零 chat_key 依赖——只读 tables 元数据），命中≥2 词直接判 sql。
    try:
        import re as _re7
        with _db_lock:
            _t_files = _db.execute("SELECT DISTINCT file FROM tables WHERE scan_id=?", (scan_id,)).fetchall()
            _t_cols = _db.execute("SELECT file, cols FROM tables WHERE scan_id=?", (scan_id,)).fetchall()
        _corpus = set()
        for (fn,) in _t_files:
            for w in _re7.sub(r'[-_()\d.]+', ' ', str(fn).rsplit('.', 1)[0]).split():
                if len(w) >= 2 and not w.isdigit():
                    _corpus.add(w)
        for _fn2, _c2 in _t_cols:
            try:
                for c in _json.loads(_c2) if isinstance(_c2, str) else _c2:
                    for w in _re7.split(r'[\s:：/|·]+', str(c)):
                        w = w.strip()
                        if len(w) >= 2 and not w.isdigit():
                            _corpus.add(w)
            except Exception:
                continue
        _hits_n = sum(1 for w in _corpus if w in q)
        _table_signal = _hits_n >= 2
        print(f'[⑥诊断] scan={scan_id} corpus={len(_corpus)} hits={_hits_n} sig={_table_signal} q={q[:30]}', flush=True)
    except Exception:
        _table_signal = False
    # ⑨ 四轮步3：查询路由——精确值题先走 SQL 查表路（失败回退检索，增强非主链路）
    # 刀1 calc 意图前置判定（2026-09-19 advisory 定谳的正确形状——只判一次）：
    # need=true（纯算术题）→ 跳过 SQL 路由直接走检索路+calc（"单价35×3"该算不该查表）
    # need=false/None → 正常进 SQL 路由（"1002科目余额"走查表，编码数字不进 calc）
    _calc = calc_tool(q, chat_key=chat_key, chat_url=chat_url) if chat_key else None
    route_mode = ("search" if _calc else
                 ("sql" if _table_signal else
                  (route_query(q, scan_id=scan_id, vec_key=chat_key, vec_url=chat_url) if chat_key else "search")))
    print(f'[⑥诊断2] _calc={_calc is not None} _table_signal={_table_signal} chat_key={bool(chat_key)} route_mode={route_mode}', flush=True)
    sql_hits = None  # ⑥ C：默认无 SQL 命中（search 路也定义——防空引用）
    if route_mode == "sql":
        try:
            sql_hits = sql_route_search(scan_id, q, vec_key=chat_key, vec_url=chat_url)
            if sql_hits:
                pass  # ⑥ C：不再短路返回——落到主返回处两路合流（见下方 hits 合并段）
        except Exception as _re:
            _pf.warning("SQL路异常回退检索: %s", type(_re).__name__)
    # 没指纹就提示先做步骤③，别白打一次 API
    with _db_lock:
        n = _db.execute(
            "SELECT COUNT(*) FROM chunks WHERE scan_id = ? AND vector IS NOT NULL",
            (scan_id,),
        ).fetchone()[0]
    if n == 0:
        # ⑬ 入库即用（2026-09-20 开工）：全库无向量不再硬拦——
        # 关键词路（FTS 分块完成即有索引）先行可用，语义路等向量后台补齐
        _n_total = _db.execute("SELECT COUNT(*) FROM chunks WHERE scan_id = ?", (scan_id,)).fetchone()[0]
        if _n_total > 0:
            kw_only = db_keyword_search(scan_id, q, k)  # FTS 由触发器自动同步（分块完即有）
            if kw_only:
                return {"query": q, "total_chunks": _n_total, "hits": kw_only,
                        "mode": "keyword-only",  # ⑬ 关键词先行（向量未生成）
                        "vector_pending": True, "vector_total": _n_total,
                        "kw_hit_count": len(kw_only)}
        return {"error": "这个知识库还没有切块——先选文件夹扫描"}
    # ★ 4-2 查询增强定案（2026-09-19 D桶对决胜者进生产）：Multi-Query
    # 3变体并行检索——LLM 把问题改写成 3 个角度变体（专业术语版/口语版/英文版），
    # 每条各自过语义路+关键词路，全部结果 RRF 融合（LangChain MultiQueryRetriever 同款）。
    # D桶 160 道真挂题实测：净救 11 道（云口径，vs 基线抖动放行 7 道）。
    # 每题成本 +1 次 LLM 改写（公司代理 chat key）；增强挂了自动退单查询（不中断）
    queries = [q]
    if chat_key and use_hybrid and use_rerank:  # 纯语义/对比实验模式不增强
        # 零成本 MQ 闸（业界生产惯例）：极短问题（≤4 字，如"你好"）跳过改写——
        # 这类问题改写无增益纯耗 1 次 LLM；正常问题才增强
        if len(q) > 4:
            try:
                queries = enhance_multiquery(q, api_key=chat_key, api_url=chat_url) or [q]
            except Exception:
                queries = [q]  # 改写挂了退原题（增强是增益不是依赖）
    try:
        qvecs = embed_batch(queries, api_key=vec_key, api_url=vec_url, model=embed_model)
        question_vec = qvecs[0]
    except Exception as e:
        return {"error": f"问题指纹失败: {e}"}
    # 多查询各自的语义路（原题 + 变体都查——多角度捞块）
    sem_lists = [db_search_chunks(scan_id, qv, _pool) for qv in qvecs]
    sem_hits = sem_lists[0]
    # ⑫4：检索链路 JSON（同 trace_id 串全链——query/各路/融合/rerank）
    _lg_search.info("语义路召回", extra={"q": q[:80], "pool": _pool,
        "mq_count": len(queries),
        # ⑫8 修正：top3 统一真文件名（原 split('.')[-1] 只显扩展名）
        "top3": [f"{h['file'].replace(chr(92), '/').split('/')[-1][:20]}#{h['seq']}({h['score']:.3f})" for h in sem_hits[:3]]})  # ⑫4
    if not use_hybrid:
        # 纯语义模式（对比实验用）
        return {"query": q, "total_chunks": n, "hits": sem_hits[:k], "mode": "semantic"}
    # 混合模式：关键词路 + RRF 融合（宽进 Top-30，宁滥勿缺——垃圾候选
    # 重排时会得低分沉底，漏掉的真相救不回来）+ 重排（严出 Top-k）
    kw_lists = [db_keyword_search(scan_id, qq, _pool) for qq in queries]
    kw_hits = kw_lists[0]
    _lg_search.info("关键词路召回", extra={"q": q[:80], "mq_count": len(queries),
        "top3": [f"{h['file'].split('.')[-1][:6]}#{h['seq']}({h['score']:.3f})" for h in kw_hits[:3]]})  # ⑫4
    # 4-2 Multi-Query 融合：原题两路 + 各变体的两路全进 RRF（权重均分——
    # 评测器融合 bug 修复后的同款口径：主查询关键词路保留，多路平等融合）
    all_lists = sem_lists + kw_lists
    _w = RRF_WEIGHTS if len(all_lists) == 2 else [0.5 / len(all_lists)] * len(all_lists)
    pool = rrf_fuse(all_lists, k=_pool, weights=_w)
    _lg_search.info("RRF融合", extra={"weights": list(_w),  # 实跑权重（MQ 均权/单查询扫参定案权重）——留痕与实跑一致
        # ⑫8 修正：top3 真文件名（进池名单可见）
        "top3": [f"{h['file'].replace(chr(92), '/').split('/')[-1][:20]}#{h['seq']}" for h in pool[:3]]})  # ⑫4
    # ⑨ 五轮步3 精确值直查【已撤】（2026-09-18 用户定案）：全量实测 79.8% vs
    # 83.0%（-3.2pp，16 段里 14 段降）——本语料标识符不唯一（出现在几十个
    # 文档标题），直查捞回的是"提到过"不是"答案在"，强占池头 25 席挤掉排好的。
    # 共享函数 exact_match_boost 保留（评测器 EXACT_BOOST=1 可开）——若改造成
    # "仅唯一命中（≤1 块）时启用"可再验（401-500 金额密集段 +4 的启示）
    # ⑨ 五轮步3 案4（用户定案 2026-09-18：业界企业搜索方案——格式感知重排）：
    # 共享函数 rerank_category_aware（生产/评测器三处同款口径，抽共享防漂移）
    _rerank_degraded = False  # 每请求信号（哨兵捕获——并发零串扰）
    if use_rerank and len(pool) > 1:
        try:
            _hits_full = rerank_category_aware(q, pool, max(k * 2, 30),
                                               rerank_key, rerank_url, rerank_model)
        except _RerankDegraded:
            _hits_full = pool[:max(k * 2, 30)]  # 降级裸序（rerank_category_aware 内部已记日志）
            _rerank_degraded = True
        # 4-3 MMR 扩域版（2026-09-20）：rerank 出 30 名 → MMR 多样性选 15。
        # 治同文件挤占型挂题（考勤表/对账单双胞胎块灌满 Top15）。
        # 纯本地零模型；MMR_ON 开关（评测对照/随时可撤）
        if MMR_ON and not _rerank_degraded:
            try:
                hits = mmr_diversify(question_vec, _hits_full, top_n=k, scan_id=scan_id)
            except Exception as _mmr_e:
                # MMR 挂了退重排前 15（精排结果保住——不退 RRF 裸序）
                _pf.warning("MMR 异常退重排序: %s", type(_mmr_e).__name__)
                hits = _hits_full[:k]
        else:
            hits = _hits_full[:k]
    else:
        hits = pool[:k]  # use_rerank=false（对比实验）或池子太小：直接切前 k
    _lg_search.info("检索完成", extra={  # ⑫4：最终结果（同 trace_id 可拉全链 6 步）
        "q": q[:80], "mode": "hybrid+rerank" if use_rerank else "hybrid",
        "kw_count": len(kw_hits), "final_k": len(hits),
        # ⑫8 修正（2026-09-21 要能看到命中的文件名和内容）——
        # 原格式 split('.')[-1] 取到的是扩展名（xlsx/md），认不出哪个文件
        "top5": [f"{h['file'].replace(chr(92), '/').split('/')[-1][:24]}#{h['seq']}({h['score']:.3f})" for h in hits[:5]],
        "top1_preview": (hits[0]["text"].replace(chr(10), " ")[:80]) if hits else ""})  # 首块内容摘要

    # ⑫8 B1 明细档落盘（一题一 JSON——生产路；评测同 helper）
    try:
        _sem_flat = [h for lst in sem_lists for h in lst] if 'sem_lists' in dir() else []
        _kw_flat = [h for lst in kw_lists for h in lst] if 'kw_lists' in dir() else []
        dump_search_detail(scan_id, q, queries, _sem_flat, _kw_flat, pool,
                           getattr(_LAST_RERANK_FULL, 'copy', lambda: [])(), hits)
    except Exception:
        pass
    # 刀1 计算注入：用前置判定的 _calc（一次判定两条路共用——不重复调 LLM）
    # ⑥ C 两路合流（2026-09-22 用户拍板·advisory 修正版）：SQL 命中
    # 走独立 sql_hits 字段【不混入 hits】——sql 的 seq 是 "sql:sheet" 字符串
    # 混进向量块（整数 seq）会破 [N] 引用溯源与前端渲染。
    # 向量路照跑（短路已拆），两路各自返回，前端/生成侧拼装。
    _metrics_record("search", (_t_m.time() - _t_m0) * 1000)  # ⑫7b
    return {
        "query": q,
        "total_chunks": n,
        "hits": hits,
        # ⑥ C：SQL 命中独立字段（不混 hits——防破 [N] 溯源；前端拼装）
        **({"sql_hits": sql_hits[:3]} if sql_hits else {}),
        "mode": ("sql+" if sql_hits else "") + ("hybrid+rerank" if use_rerank else "hybrid"),
        "kw_hit_count": len(kw_hits),
        # rerank 降级标记（2026-09-19 前端闭环·每请求信号）：True=本次是粗排结果
        "rerank_degraded": _rerank_degraded,
        **({"calc": _calc} if _calc else {}),
    }

# ══════════════════════════════════════════════════════════════
# 步骤⑤ 注入：检索片段拼进聊天提示词
# ══════════════════════════════════════════════════════════════
# 普通聊天闭卷瞎编；RAG 聊天先检索（步骤④）拿最相关的 K 块拼进 system 提示词，
# 模型变"开卷精读"。注入位置在前端 app.js（调 /search 拿块、拼 system 消息、
# 走原有聊天链路），后端只提供"拼资料"纯函数 + 预览接口。

# ══════════════════════════════════════════════════════════════
# 模块 7：提示词拼装（纯函数，前端注入同款格式）
# ══════════════════════════════════════════════════════════════


def build_rag_context(hits):
    """hits = api_search 返回的 [{file, seq, score, text}]。
    返回拼好的参考资料文本（每块带编号+出处，模型能引用来源）。
    4-2B 刀3 上下文工程（Anthropic Context Engineering 指南）：
    ① 表格块（## Sheet: 管道表格式）转规整 Markdown 表格——列对齐渲染
       治模型读表串行/抄错行
    ② 相关性标注——命中分数映射"最相关/相关/背景"，模型知道优先看哪块
    ③ hits 已按 rerank 分数降序（api_search 保证）——首块即最相关"""
    parts = []
    for i, h in enumerate(hits):
        text = h["text"]
        # 表格块规整化：## Sheet 行 + 管道表 → Markdown 表头分隔行补齐
        if text.startswith("## Sheet") and "|" in text:
            lines = text.split("\n")
            fixed = []
            header_done = False
            for ln in lines:
                if ln.startswith("|") and not header_done and ln.count("|") >= 2:
                    fixed.append(ln)
                    # 补 Markdown 表头分隔行（|---|---|）——无它渲染器当纯文本
                    ncols = ln.count("|") - 1
                    fixed.append("|" + "|".join(["---"] * ncols) + "|")
                    header_done = True
                else:
                    fixed.append(ln)
            text = "\n".join(fixed)
        # 相关性标注（score 域：rerank 归一分>0.5 高相关；保守阈值）
        s = h.get("score", 0) or 0
        tag = "最相关" if s >= 0.5 else ("相关" if s >= 0.3 else "背景")
        parts.append(f"【资料{i + 1}·{tag}】出处: {h['file']} 第{h['seq']}块\n{text}")
    return "\n\n".join(parts)


def build_rag_system_prompt(hits):
    """把资料文本 + 行为约束拼成一条 system 消息（聊天时排在最前面）。
    约束三条：优先用资料、没资料就说不知道、回答带出处——防幻觉三板斧"""
    context = build_rag_context(hits)
    return (
        "你是知识库问答助手。下面是从项目知识库检索到的参考资料。\n\n"
        + context
        + "\n\n回答要求：\n"
        "1. 优先依据参考资料回答，并在回答中注明出处（如：据资料2）。\n"
        "2. 参考资料里没有的内容，明确说\"资料里没有\"，不要编造。"
        "注意：字段存在不等于答案存在——不要根据字段名猜测答案"
        "（如资料有 Account 字段不代表它是手机号）。\n"
        "3. 如果资料之间矛盾，指出矛盾并列出两处出处。\n"
        "4. 涉及金额/数量计算（加减、汇总、差额）：先列出每个原始数值"
        "及其出处，再写出算式，最后给出计算结果。禁止只罗列数字不计算。\n"
        "5. 表格类问题（查某科目/某人的某项数据）：先核对行"
        "（科目代码或名称要与问题完全对准），再读该行指定列的值。禁止跳行取数。"
    )


@app.get("/api/kb/{scan_id}/inject-preview")
def api_inject_preview(scan_id: int, q: str, k: int = 10, request: Request = None):
    """检索 + 拼提示词，返回完整 system 文本。用于肉眼检查注入质量：
    资料块是不是相关的、拼出来的格式好不好读"""
    result = api_search(scan_id, q, k, request=request)
    if "error" in result:
        return result
    return {
        "query": q,
        "system_prompt": build_rag_system_prompt(result["hits"]),
        "hit_count": len(result["hits"]),
    }

# ══════════════════════════════════════════════════════════════
# 启动开关
# ══════════════════════════════════════════════════════════════
# ---------- 桌面版静态托管：FastAPI 直接伺服前端页面（单端口，免 8080 静态服务） ----------
# 开发模式仍可用 8080 的 http.server（两种都通）；打包版只靠这个
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 静态目录：开发模式 = server.py 旁边；打包模式（frozen，onedir 结构）= exe 旁的 _internal/
# （PyInstaller 把 datas 摊进 _internal；FileResponse 要读真实磁盘文件）
_FROZEN = getattr(_sys, 'frozen', False)
_STATIC_DIR = (Path(_sys.executable).parent / '_internal') if _FROZEN else Path(__file__).parent

@app.get("/", include_in_schema=False)
def serve_index():
    # 三十五修：no-cache——浏览器每次校验（防版本号 bump 不生效的 304 缓存坑）
    return FileResponse(_STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

# CDN 之外的本地静态文件（app.js）；marked/DOMPurify 走公网 CDN
_static_mounted = False
if (_STATIC_DIR / "app.js").exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # app.js 在根路径引用（<script src="app.js">），加个根级路由转发
    @app.get("/app.js", include_in_schema=False)
    def serve_app_js():
        # 三十五修：no-cache——同上（app.js 改动必须即时到达浏览器）
        return FileResponse(_STATIC_DIR / "app.js", headers={"Cache-Control": "no-cache"})
    _static_mounted = True


last_scan_root = None


# ═══════════════════════════════════════════════════════════════
# 阶段二·服务端 Agent 执行器（2026-09-23 开工·演进方案路线一）
# POST /api/agent/chat（SSE 流式）：工具循环从前端搬到后端。
# 设计：语义对齐前端 callChatWithTools（5 轮上限+降级作答）——
# 评测器可直调（不再复刻前端逻辑）、会话可持久化、断线续跑。
# 工具：agent_tools 注册表五工具（search_kb/sql_query/calc/
# list_kb_files/read_chunk）——一处注册处处用。
# 通用性铁律：提示词零语料词。
# ═══════════════════════════════════════════════════════════════
import context_manager as _cm  # 4.2 上下文管理三件套（纯函数模块——SSE 端点接线 2026-09-24）

_SEARCH_CACHE = {}  # 6.2 会话级检索缓存（LRU 50——进程生命周期）

_FOLLOWUP_CANDIDATE_MAX = 30  # D-3.1 三阶之一：候选判定字数门槛（对齐旧链 L1678 的 30 字）


def _intent_classify(question, history, chat_key, chat_url, has_attachment):
    """D-3.1 + D-3.3 复合意图判定（2026-09-24）：一次 LLM 调用出两个结果——
    ① followup 类型：FOLLOWUP（依赖上文）| NEW（换话题）——三阶判定链第二阶
    ② attach_intent：SEARCH（让我检索知识库）| ANALYZE（分析附件本身）——附件意图路由
    兜底（拍板版·对齐旧链 app.js L1610 fail→FOLLOWUP）：
      - 调用失败/输出不合法 → ("FOLLOWUP", "ANALYZE")——追问宁带上文（TREC CAsT：
        不改写的追问近零召回），附件宁直答（宁可漏检索不污染）
    返回 (followup_kind, attach_intent, rewritten_query)：
      - 非 FOLLOWUP 时 rewritten_query=None（原词检索）
      - FOLLOWUP 时 rewritten_query=改写结果（改写失败→原词——同样拍板版兜底）"""
    import json as _jx
    recent = history[-4:] if history else []
    ctx = "\n".join(f"{'用户' if m.get('role') == 'user' else '助手'}: {str(m.get('content') or '')[:400]}"
                    for m in recent)[:1500]
    att_hint = "用户本次附带图片或文档。" if has_attachment else ""
    sys_prompt = (
        "判定用户消息的两个意图维度，输出 JSON（不要解释）：\n"
        '{"followup": "FOLLOWUP 或 NEW", "rewrite": "改写后的完整问题或空串", "attach": "SEARCH 或 ANALYZE"}\n'
        "· followup：最新问题是否依赖上文才完整（如\"小李呢\"\"那市场部呢\"=FOLLOWUP；"
        "语义完整的换话题问题如\"销售部的工作效率如何\"=NEW）。无上文时输出 NEW。\n"
        "· rewrite：仅当 followup=FOLLOWUP 时，把问题结合上文改写成离开上文也完整的独立问题"
        "（如\"小李呢\"→\"小李负责什么工作\"）；否则输出空串。\n"
        f"· attach：{att_hint}用户是想让你【检索知识库找相关内容】（如\"帮我查找/搜索/查一下相关…\"=SEARCH），"
        "还是【直接分析图片/文档本身】（如\"分析这张图\"\"总结这份文档\"=ANALYZE）。无附件时输出 ANALYZE。")
    out = _llm_chat(f"{sys_prompt}\n\n对话上文：\n{ctx or '（无）'}\n\n用户最新消息：{question}",
                    chat_key, chat_url)
    if not out:
        return ("FOLLOWUP", "ANALYZE", None)  # 失败兜底（拍板版）
    try:
        raw = out.strip().strip("`").replace("```json", "").replace("```", "").strip()
        d = _jx.loads(raw)
        fk = "FOLLOWUP" if str(d.get("followup", "")).upper().startswith("F") else "NEW"
        ai = "SEARCH" if str(d.get("attach", "")).upper().startswith("S") else "ANALYZE"
        rw = str(d.get("rewrite") or "").strip()[:300]
        if fk != "FOLLOWUP":
            rw = None  # NEW 不补全（防跨话题污染——旧链 L1687 语义）
        return (fk, ai, rw or None)
    except Exception:
        return ("FOLLOWUP", "ANALYZE", None)  # 输出不合法兜底


@app.post("/api/agent/chat")
async def api_agent_chat(body: dict, request: Request):
    """服务端 Agent 执行器（SSE）。请求体：
    {question, scan_id, history: [{role, content}...](可选),
     chat_key, chat_url, model(可选默认 deepseek-v4-flash),
     vec_key, vec_url, embed_model(可选·嵌入模型名),
     rerank_key, rerank_url, rerank_model(可选·缺省回退向量配置)}
    SSE 事件流：
      event: round   data: {"round": N}                    —— 每轮开始
      event: tool    data: {"name", "args", "secs", "ok"}  —— 工具调用完成
      event: delta   data: {"text": "..."}                 —— 最终答案流式增量
      event: done    data: {"trace_id", "rounds", "kb_calls", "secs"} —— 收尾
    """
    import time as _t
    import json as _j
    import urllib.request as _ur
    import asyncio  # 2026-09-23 七修+十一修：裸名导入——调用点用 asyncio.sleep（曾误导为 _asyncio 名字不匹配 NameError 复发）
    import agent_tools as _at
    from fastapi.responses import StreamingResponse

    t0 = _t.time()
    question = str(body.get("question") or "").strip()
    scan_id = body.get("scan_id")
    chat_key = body.get("chat_key") or request.headers.get("X-Chat-Key")
    chat_url = body.get("chat_url") or request.headers.get("X-Chat-Url")
    # 十修：vec 双 key 也取（sameKey=false 时与 chat 不同——rerank/语义路用）
    vec_key = body.get("vec_key") or request.headers.get("X-Vec-Key") or chat_key
    vec_url = body.get("vec_url") or request.headers.get("X-Vec-Url") or chat_url
    # 三模型独立配置：嵌入模型名 + 重排三件套（body 优先，头部兜底）。
    # 重排缺省回退向量配置——老用户只填过一把向量 Key 也能照常重排
    embed_model = body.get("embed_model") or request.headers.get("X-Embed-Model")
    rerank_key = body.get("rerank_key") or request.headers.get("X-Rerank-Key") or vec_key
    rerank_url = body.get("rerank_url") or request.headers.get("X-Rerank-Url") or vec_url
    rerank_model = body.get("rerank_model") or request.headers.get("X-Rerank-Model")
    if not chat_url:
        return {"error": "chat_url 必填（body.chat_url 或 X-Chat-Url 头——BYOK 设计）"}
    model = body.get("model") or "deepseek-v4-flash"
    history = body.get("history") or []
    # ── D-1 统一请求契约（2026-09-24 · 迁移 Phase 0）──
    # mode: "auto"(默认·服务端按 scan_id 自行路由) | "rag"(强制检索) | "chat"(纯聊天)
    # attachments: [{type:"image", data}, {type:"doc", name, content}]（D-2 多模态直答用——本步只收字段）
    mode = (body.get("mode") or "auto").lower()
    attachments = body.get("attachments") or []
    # D-3.0b 图片模型兜底（advisor 抓的真缺口）：前端模型列表动态拉取（前端不知
    # 谁多模态），selectedModel 可能是纯文本模型（deepseek 系）——图片轮打纯文本
    # 模型必 400。服务端兜底：附件含图片且 model 在纯文本名单 → 自动切视觉模型。
    # 名单是「实测纯文本」模型（todo 架构决定：qwen3.7-plus/qwen3.8-max 实测支持图片）。
    _ATT_IMAGE_MODELS = ("qwen", "glm-4v", "gpt-4o", "claude-", "gemini", "vl", "vision")  # 多模态关键词
    _has_img_att = any(isinstance(a, dict) and a.get("type") == "image" and a.get("data")
                       for a in (attachments or []))
    if _has_img_att and model and not any(k in model.lower() for k in _ATT_IMAGE_MODELS):
        _steps_log_preview = f"图片轮模型兜底: {model} 不支持图片 → 切 qwen3.7-plus"
        model = "qwen3.7-plus"
    # ── D-2 附件解析（2026-09-24 · 附件下沉）──
    # 旧链等价语义（app.js callChatWithTools L1452-1478 实况为准）：
    # · 图片：user 消息 content 数组（text + image_url base64×N——OpenAI 多模态标准）
    # · 文档：全文直接拼在问题后（无围栏——L1457 围栏注释掉了，如实对齐）
    # · 检索不跳过：RAG 开+有库照样预检索命中块拼 system（含拒答闸/calc 全套）
    # · tools 同权：附件轮照样可补查（旧链 ragPath 时不分附件与否）
    _att_imgs = []
    _att_doc = None
    for _a in (attachments or []):
        if not isinstance(_a, dict):
            continue
        if _a.get("type") == "image" and _a.get("data"):
            _att_imgs.append(str(_a["data"]))
        elif _a.get("type") == "doc" and _a.get("content"):
            _att_doc = {"name": str(_a.get("name") or "文档"), "content": str(_a["content"])}
    _has_attachment = bool(_att_imgs or _att_doc)
    # chat 路由判定（9.2 根治 + C 阶段活体第一次 FAIL 根因）：
    # 显式 mode=chat 或（auto 且无库）→ 纯聊天——不检索、不挂工具、单轮直答。
    # 旧行为（劣化）：无库时 rag_sys 仍提示「先换词查一次」+ 下发 5 工具 →
    # 模型空转检索烧穿墙钟（实测 378s/5 次 kb_calls 全白烧）。
    _is_chat = (mode == "chat") or (mode == "auto" and not scan_id) or (mode == "rag" and not scan_id)
    if not question:
        return {"error": "question 必填"}
    if not chat_key:
        return {"error": "chat_key 必填（body 或 X-Chat-Key 头）"}
    trace_id = f"ag_{_t.time_ns():x}"[:20]
    # 六十六修（advisor blocker 实锤·两套编号不互通）：_TraceIdFilter 会用
    # _current_trace_id（中间件设的 12 位随机码）覆盖 extra 里的 trace_id——
    # 导致 chat.log 记的编号与 done 事件回传的 ag_… 编号对不上号，日志断言
    # 全灭。修：端点开头把上下文编号设为本轮 ag_…——本轮全部日志（含工具层）
    # 统一用它，面板按 done 回传编号一查一个准。
    _current_trace_id.set(trace_id)
    try:
        _lg_chat.info("Agent执行器启动", extra={
            "trace_id": trace_id, "q": question[:80],
            "scan_id": scan_id, "mode": mode,   # 六十四修（用户实锤）：请求参数指纹入日志
            # ——「未指定知识库」排障时日志里查不到 scan_id 传没传（api.log 中间件只读
            # query_params，POST body 全丢）——排障铁律：入口参数必须可查
            "attachments": len(attachments), "history_turns": len(history) // 2})
    except Exception:
        pass

    # ── 预检索（与前端同款——预检索块拼 system，模型可直接用可补查）──
    # 三十修：step 事件流（调用过程逐步显示——与旧链 addProcessStep 同款文案）
    async def _step(text):
        yield_lock = True
        # 生成器外的 step 不能直接 yield——改用队列模式太重。
        # 简方案：step 信息收集后随 round 事件附带。
        _steps_log.append(text)
    _steps_log = []

    # ── D-3.1 三阶判定链 + D-3.3 附件意图路由（2026-09-24 · 预检索前）──
    # 三阶（旧链 app.js L1674-1690 等价下沉）：① 30 字候选闸 ② LLM 分类
    # FOLLOWUP/NEW ③ 仅 FOLLOWUP 改写。附件意图（用户拍板 D-3.3）：
    # SEARCH（帮我查相关）→ 走检索；ANALYZE（分析附件）→ 直答。
    # 复合判定一次 LLM 出双结果（省调用）；兜底拍板版：失败→FOLLOWUP+ANALYZE。
    _search_query = question  # 检索用问题（FOLLOWUP 改写后替换）
    _att_search_intent = False
    _intent_note = ""
    if len(history) >= 2 and len(question) <= _FOLLOWUP_CANDIDATE_MAX:
        try:
            _fk, _ai, _rw = _intent_classify(question, history, chat_key, chat_url, _has_attachment)
            if _fk == "FOLLOWUP" and _rw:
                _search_query = _rw
                _intent_note = f"补全追问语境... | 检索问题: {_rw[:60]}"  # 五十修：文案对齐旧链两步展示（截图比对）
                _lg_chat.info("追问判定", extra={"trace_id": trace_id,
                    "q": question[:40], "detail": "判定=FOLLOWUP"})  # ⑫8 A10 同款
                _lg_chat.info("检索词补全", extra={"trace_id": trace_id,
                    "q": question[:40], "detail": f"补全后={_rw[:100]}"})  # ⑫8 A11 同款
            elif _fk == "NEW":
                _intent_note = "新话题——纯净检索（上文不进检索词）"
                _lg_chat.info("追问判定", extra={"trace_id": trace_id,
                    "q": question[:40], "detail": "判定=NEW"})  # ⑫8 A10 同款
            if _has_attachment and _ai == "SEARCH" and scan_id:
                _att_search_intent = True
                _intent_note += " | 附件意图: 检索知识库"
            elif _has_attachment:
                _intent_note += " | 附件意图: 直接分析附件"
        except Exception:
            _intent_note = "意图判定失败——按原词检索（FOLLOWUP 兜底）"
    elif _has_attachment and scan_id:
        # 附件轮长问题/无历史——意图路由仍要判定（D-3.3 独立于追问三阶）
        try:
            _, _ai, _ = _intent_classify(question, history, chat_key, chat_url, True)
            if _ai == "SEARCH":
                _att_search_intent = True
                _intent_note = "附件意图: 检索知识库"
            else:
                _intent_note = "附件意图: 直接分析附件"
        except Exception:
            _intent_note = "附件意图判定失败——直答（安全侧）"
    # D-3.3 附件意图消费（必须在 if _is_chat 分支判定之前——旧位置在两分支
    # 之后翻转=两头落空，kb_calls 未定义 done 事件 NameError，断言④实锤）：
    # 附件轮 + 分析意图（非 SEARCH）→ 跳过检索直接分析附件（复用 chat 直答路）；
    # 检索意图（SEARCH）→ 照走 auto 检索（旧链等价 + 用户拍板）。
    if not _is_chat and _has_attachment and not _att_search_intent:
        _is_chat = True
        _steps_log.append("附件意图=分析——跳过检索，直接分析附件")
    if _is_chat:
        # D-1 chat 路由：纯聊天——预检索整段跳过（无库检索必空手）、rag_sys 换
        # 纯聊天提示、不挂工具。单轮直答（第 1 轮模型直接作答，无 tool_calls）。
        _steps_log.append("纯聊天模式（无检索——直接回答）")
        if _has_attachment:
            # D-3.3 附件分析直答（意图=ANALYZE 或无库附件轮）——提示词面向附件
            rag_sys = ("用户随消息附带了图片或文档。请直接分析附件内容本身回答："
                       "图片看图作答，文档依据全文作答。忠实于附件内容，"
                       "不确定就说不确定，不要编造。")
        else:
            rag_sys = ("你是用户的智能助手。当前为普通对话模式（未连接知识库）。"
                       "基于对话上下文直接回答；涉及之前聊过的内容时参考【此前对话摘要】。"
                       "不确定就说不确定，不要编造。")
        _shaped_history = _cm.slide_history(
            history, keep_recent=12, max_summary_chars=600,
            summarize_fn=lambda txt: _llm_chat(
                "把以下对话历史压缩成一段不超过300字的摘要，保留：用户问过的问题要点、"
                "已确认的事实答案、涉及的关键文件名/编号/数值。只压缩已有信息，不要新增任何内容。\n\n" + txt[:6000],
                chat_key, chat_url))
        # D-2 附件组装（旧链等价 L1452-1478）：文档=全文直拼问题后（无围栏）；
        # 图片=content 数组（text + image_url×N）；图文并存=文档全文放 text + 图数组
        _user_content = question
        if _att_doc:
            _user_content = (question or "请总结这份文档") + _att_doc["content"]
            _steps_log.append(f"文档注入（{_att_doc['name']}，{len(_att_doc['content'])} 字）")
        if _att_imgs:
            _user_content = [{"type": "text", "text": _user_content if isinstance(_user_content, str) else (question or "请分析这张图片")}
                             ] + [{"type": "image_url", "image_url": {"url": u}} for u in _att_imgs]
            _steps_log.append(f"多模态直答（{len(_att_imgs)} 张图片）")
        messages = ([{"role": "system", "content": rag_sys}] + _shaped_history
                    + [{"role": "user", "content": _user_content}])
        tools_json = []  # 不挂工具——工具循环第 1 轮模型无 tool_calls 即直答
        MAX_ROUNDS, kb_calls = 1, 0  # 单轮（auto 模式的 5 轮上限对纯聊天无意义）
        _tok_total, _tok_calls = 0, 0
        _Q_WALL = 120  # 纯聊天无检索，2 分钟墙钟足够
        _no_presearch = True
        _src_files = []    # done 事件引用源清单（chat 模式为空——纯聊天无引用）
        _src_details = []  # 块内容摘要（[N] 浮窗用——chat 模式为空）
        _vote_winner = None  # 六十二修（2026-09-25 用户实锤复现）：chat 分支没初始化——
        # _stream 读 _vote_winner 判投票短路时 NameError（free variable 未绑定），
        # 表现为「执行器异常 NameError」空答案。auto 分支 L5835 有、这里漏。
    # D-1 公共前置（chat/auto 两路都要——run_in_threadpool 工具执行用、
    # _src_files/_src_details done 事件引用【chat 模式为空列表】）
    from fastapi.concurrency import run_in_threadpool
    import agent_tools as _at_pre
    if not _is_chat:
        _no_presearch = False
        # ── auto/rag 路径：预检索 + RAG system + 5 轮工具循环 ──
        if _intent_note:
            _steps_log.append(_intent_note)  # D-3.1 判定留痕（前端过程区可见）
        # D-5.3 前置：预检索请求对象/块数/缓存键提前定义（D-5.4 自动向量化与缓存命中分支都要用）
        _pre_req = _at_pre._fake_request(chat_key, chat_url, vec_key, vec_url,
                                         rerank_key, rerank_url, rerank_model, embed_model)
        _pre_k = _cm.dynamic_presearch_k(len(history) // 2)
        # D-5.4 检索前自动向量化（2026-09-24 · 旧链 ensureEmbedded 等价下沉 L597-629）：
        # 新库首次问答——指纹没生成过就自动触发后台向量化（不等），关键词路先行
        # （api_search 的 keyword-only 降级已有）。分块没完成按普通聊天答（旧链同款）。
        try:
            with _db_lock:
                _tot = _db.execute("SELECT COUNT(*) FROM chunks WHERE scan_id=?", (scan_id,)).fetchone()[0]
                _dn = _db.execute("SELECT COUNT(*) FROM chunks WHERE scan_id=? AND vector IS NOT NULL", (scan_id,)).fetchone()[0]
            if _tot > 0 and _dn == 0:
                _steps_log.append("首次使用：后台自动生成语义指纹（大库约 2 分钟）——本次先用关键词模式检索...")
                try:
                    await run_in_threadpool(api_embed_start, scan_id, _pre_req)
                except Exception:
                    pass  # 触发失败不硬扛：关键词模式照常答（旧链同款容错）
            elif _tot == 0:
                _steps_log.append("知识库正在准备（分块中），本次按普通聊天回答")
        except Exception:
            pass  # 自动向量化检查失败不阻塞检索（旧链同款容错）
        _cache_key = (scan_id, _search_query[:200])  # D-3.1：缓存键用 _search_query（FOLLOWUP 改写后的完整问题）
        _cached = _SEARCH_CACHE.get(_cache_key)
        if _cached is not None:
            _steps_log.append("检索缓存命中（秒回——同问题已查过）")
            r = _cached
        else:
            _steps_log.append("RAG 检索知识库（预检索）...")
            try:
                # 4.4 长对话不超上下文③：预检索块数按【轮】数动态下调（15-12-10-8）
                # _pre_req/_pre_k 已提前定义（上方 D-5.3 前置段）
                r = await run_in_threadpool(api_search, scan_id, _search_query, _pre_k, True, True, _pre_req) if scan_id else None
                # 6.2：写缓存（原始响应 dict——hits/sql_hits 都在）
                if r is not None:
                    if len(_SEARCH_CACHE) >= 50:
                        _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)))  # LRU 淘汰最旧
                    _SEARCH_CACHE[_cache_key] = r
            except Exception:
                r = None
        hits = (r or {}).get("hits") or []
        sql_h = (r or {}).get("sql_hits") or []

        # ── D-5.3 同义词第 2 路检索 + 两路合并（2026-09-24 · 旧链 synonymize 等价下沉）──
        # 旧链语义（app.js L1698-1744 实况）：embedding 对近义词有盲区（"购买模块"
        # 0.52 vs "采购模块" 0.58——词面差决定答案块进不进 Top-K）。第 1 路（原词/
        # 改写后的完整问题）检索后：Top1 分数 <0.4 → 反馈改写（喂命中块给 LLM 参考
        # 库里用词）；≥0.4 → 同义词改写（凭空换专业词）。改写出变体 → 第 2 路检索
        # → 同一块（file+seq）留分数高者，合并排序取 Top15。
        try:
            _top1 = hits[0].get("score", 0) if hits else 0
            _feedback = hits if _top1 < 0.4 else None
            _syn_sys = ("把问题改写成知识库检索用的最佳查询：1) 口语词换专业词（购买→采购）；"
                        "2) 补上语料可能用的术语、实体名、领域词（如工作包编号、表名、字段名、"
                        "\"移交\"\"负责人\"等文档常用词）。只输出改写后的查询，不要解释。")
            if _feedback:
                _refs = "\n".join(
                    f"文件名: {(h.get('file') or '').split('/')[-1]}｜内容开头: {str(h.get('text') or '')[:80]}"
                    for h in _feedback[:5])
                _syn_sys = ("知识库检索命中不佳，需要改写查询重新检索。以下是知识库里实际存在的文档"
                            "（含文件名和内容开头）——请参考这些文档的用词和表述，把用户的问题重新表述成"
                            f"最匹配这些文档的检索查询。只输出改写后的查询，不要解释。\n\n参考文档：\n{_refs}")
            _variant = _llm_chat(f"{_syn_sys}\n\n用户问题: {_search_query}", chat_key, chat_url)
            if _variant:
                _variant = _variant.strip().strip('"“”\'')[:200]
            if _variant and _variant != _search_query:
                _steps_log.append(f"RAG 检索（第 2 路：{'反馈' if _feedback else '同义词'}改写）: {_variant[:40]}")
                _lg_chat.info("rewrite_trigger", extra={"trace_id": trace_id,
                    "q": _search_query[:60], "top1": round(_top1, 3),
                    "mode": "feedback" if _feedback else "synonym", "variant": _variant[:80]})  # ⑫8 同款留痕
                _r2 = await run_in_threadpool(
                    api_search, scan_id, _variant, _pre_k, True, True, _pre_req)
                _h2 = (_r2 or {}).get("hits") or []
                _seen = {}
                for _h in list(hits) + list(_h2):
                    _k2 = f"{_h.get('file')}#{_h.get('seq')}"
                    if _k2 not in _seen or (_h.get("score") or 0) > (_seen[_k2].get("score") or 0):
                        _seen[_k2] = _h
                hits = sorted(_seen.values(), key=lambda x: -(x.get("score") or 0))[:15]
                _lg_chat.info("两路合并", extra={"trace_id": trace_id,
                    "q": _search_query[:60], "variant": _variant[:60],
                    "merged": len(hits)})  # ⑫8 A7 同款留痕
        except Exception:
            pass  # 同义词这路挂了不影响主路（旧链同款容错）

        # ── D-5.5 拒答硬闸强化（2026-09-24 · 旧链 L1783-1790 等价下沉）──
        # 旧链零命中提示词 vs 服务端旧版差异：补「通用知识必须标注非资料来源」
        # 硬要求 + 禁止不标注给具体数字/编号/日期（防幻觉第二层）。
        if sql_h:
            _steps_log.append(f"SQL 直查命中 {len(sql_h)} 行（已并入资料）")
        if hits:
            _steps_log.append(f"RAG 命中 {len(hits)} 块（已注入提示词）")
        parts = []
        _src_files = []  # 二十六修：引用溯源——done 事件带回来源文件清单
        _src_details = []  # 二十九修：块内容摘要（[N] 浮窗用——file/seq/text 前 200 字）
        _low_top1 = (hits[0].get("score") or 0) if hits else 0  # Top1 分数（低分预警判据）
        for i, h in enumerate(hits):
            fname = (h.get("file") or "").replace("\\", "/").split("/")[-1]
            _sf = f"{fname} · 第{h.get('seq')}块"
            if _sf not in _src_files:
                _src_files.append(_sf)
                _src_details.append({"file": fname, "seq": h.get('seq'), "text": (h.get('text') or '')[:800],
                                     "score": round(h.get("score") or 0, 3)})  # 分数亮给用户（六十一修）
            parts.append(f"【资料{i+1}】出处: {fname} 第{h.get('seq')}块\n{(h.get('text') or '')[:400]}")
        if hits and _low_top1 < 0.3:
            # 六十一修（2026-09-25 用户拍板·分数透明）：Top1 < 0.3 低分命中——过程区
            # 明示「这些块可能不相关」，用户自己判断。不做硬闸（行为变更留待全量
            # 评测定阈值——advisor 实锤：hits 层过滤会动 [N] 三对齐，非纯展示）。
            _steps_log.append(f"⚠ 检索命中分数偏低（Top1={_low_top1:.3f}）——这些资料可能与问题不相关，答案若称「资料里没有」属正常")
        if sql_h:
            parts.append("\n".join(f"【SQL直查】{(h.get('text') or '')[:300]}" for h in sql_h))
        if parts:
            rag_sys = ("以下是从项目知识库预检索的参考资料（可能与问题相关也可能不完整）：\n\n"
                       + "\n\n".join(parts)
                       + "\n\n你有检索工具可自主查知识库。使用指引：\n"
                         "· 预检索资料已含答案 → 直接引用回答（用 [1][3] 标记编号）\n"
                         "· 问了两件事（多跳）且资料只覆盖一件 → 对另一件调工具补查\n"
                         "· 资料与问题不相关 → 换用文档原文表述的关键词调 search_kb 重查\n"
                         "· 需要精确查表数值 → 用 sql_query；需要计算 → 用 calc\n"
                         "· 检索结果不够 → 最多再换词查 2 次\n"
                         "· 资料和检索都没有 → 明确说\"资料里没有\"，不要编造。")
        else:
            # D-5.5 拒答硬闸（零命中 → 强制拒答 + 通用知识标注硬要求）
            _lg_chat.info("reject_gate", extra={"trace_id": trace_id,
                "q": question[:60], "query": _search_query[:60]})  # ⑫8 A13 同款留痕
            rag_sys = ("知识库检索未命中任何相关内容。请明确回答\"知识库资料里没有相关内容\"。"
                       "如你的通用知识能部分解答，可补充说明并标注\"（以下为模型通用知识，非知识库资料）\"。"
                       "禁止不标注来源就给出具体数字、编号、日期等事实性内容。")

        # 4.4 长对话不超上下文①：历史滑窗+摘要（默认 12 条=6 轮 verbatim，更早压
        # 摘要——LLM 摘要可注入）。摘要回调复用 _llm_chat（三参——断路器/节流/关思考
        # 全继承，模块自注释「复用 _llm_chat 不引新依赖」的设计原意）；失败返 None
        # → slide_history 本地首尾拼接兜底。4.2 件②在工具落链处、件③在预检索处。
        _shaped_history = _cm.slide_history(
            history, keep_recent=12, max_summary_chars=600,
            summarize_fn=lambda txt: _llm_chat(
                "把以下对话历史压缩成一段不超过300字的摘要，保留：用户问过的问题要点、"
                "已确认的事实答案、涉及的关键文件名/编号/数值。只压缩已有信息，不要新增任何内容。\n\n" + txt[:6000],
                chat_key, chat_url))
        # D-2 附件组装（与 chat 分支同款——旧链等价：附件轮照样检索不跳过）
        _user_content = question
        if _att_doc:
            _user_content = (question or "请总结这份文档") + _att_doc["content"]
            _steps_log.append(f"文档注入（{_att_doc['name']}，{len(_att_doc['content'])} 字）")
        if _att_imgs:
            _user_content = [{"type": "text", "text": _user_content if isinstance(_user_content, str) else (question or "请分析这张图片")}
                             ] + [{"type": "image_url", "image_url": {"url": u}} for u in _att_imgs]
            _steps_log.append(f"多模态直答（{len(_att_imgs)} 张图片）")
        messages = ([{"role": "system", "content": rag_sys}] + _shaped_history
                    + [{"role": "user", "content": _user_content}])
        tools_json = _at.get_tools_json()

        # ── D-4.1 计算题投票路（2026-09-24 · 旧链 needsVote+sample 等价下沉）──
        # 旧链语义（app.js L1574-1848 实况）：
        # · 触发：数字预筛（十六修——「科目编码1001」含数字但非计算，先筛词再问 LLM）
        #   + LLM 判定 YES（金额/数量计算）→ 3 次采样投票
        # · 素材冻结：检索只 1 次（上面的预检索已完成的 rag_sys/messages）——
        #   3 次采样复用同一份 messages，仅温度不同（0/0.7/0.7），不重检索不进工具循环
        # · 多数决：值提取（数字去重排序前 6 拼串）≥2 票相同胜出；3 票各异取第 1 遍
        # · 失败：判定挂→单答（保守不拖累）；采样挂→回退单次直答
        # · 交互一致：投票完成步骤提示 + 「答题投票/算术判定」事件留痕（⑫8 A8/A12）
        _vote_winner = None
        # D-4.1 计数初始化（advisor 实锤的 UnboundLocalError：_tok_calls 先用后定义
        # ——被 except 吞成「假失败」，白烧一次采样。初始化必须挪到投票块前。）
        MAX_ROUNDS, kb_calls = 5, 0
        _tok_total, _tok_calls = 0, 0
        _Q_WALL = 360  # 单题墙钟 6 分钟（与评测器同款）
        _CALC_WORDS = ("多少", "合计", "总共", "一共", "差额", "剩余", "平均", "求和", "汇总",
                       "+", "-", "×", "÷", "*", "/")  # 数字预筛词表（旧链十六修同款）
        _calc_pre = any(w in question for w in _CALC_WORDS) and any(c.isdigit() for c in question)
        if _calc_pre:
            _nv = _llm_chat("判断这个问题是否涉及金额/数量的计算（加减、汇总、求和、剩余、差额、"
                            "多步数值推理）。只回答 YES 或 NO。不要任何解释。\n\n问题: " + question,
                            chat_key, chat_url)
            _needs_vote = bool(_nv and _nv.strip().upper().startswith("YES"))
            _steps_log.append("算术判定: " + ("计算题→投票" if _needs_vote else "普通题→单答"))
            if _needs_vote:
                _steps_log.append("计算类问题——启用 3 次采样投票（更稳，多等几秒）...")
                try:
                    import re as _re_v
                    def _extract_val(t):
                        nums = sorted(set(s.replace(",", "") for s in
                                          _re_v.findall(r"-?\d[\d,]*(?:\.\d+)?", t or "")))[:6]
                        return "N:" + ",".join(nums) if nums else (t or "")[:40]
                    def _sample(temp):
                        b = {"model": model, "messages": messages, "temperature": temp}
                        req = _ur.Request(chat_url.rstrip("/") + "/chat/completions",
                            data=_j.dumps(b).encode(),
                            headers={"Authorization": "Bearer " + chat_key, "Content-Type": "application/json"})
                        d = _j.loads(_ur.urlopen(req, timeout=120).read())
                        return ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    _a1 = _sample(0)
                    _votes, _answers = [_extract_val(_a1)], [_a1]
                    _tok_calls += 1
                    for _t_v in (0.7, 0.7):
                        try:
                            _av = _sample(_t_v)
                            _answers.append(_av); _votes.append(_extract_val(_av)); _tok_calls += 1
                        except Exception:
                            pass  # 采样挂了容错（旧链同款——票少不少路）
                    _cnt = {}
                    for _v in _votes:
                        _cnt[_v] = _cnt.get(_v, 0) + 1
                    _top_v = sorted(_cnt.items(), key=lambda x: -x[1])[0]
                    _vote_winner = _a1
                    if _top_v[1] >= 2:
                        _wi = _votes.index(_top_v[0])
                        if _wi >= 0:
                            _vote_winner = _answers[_wi]
                    _steps_log.append(f"投票完成：{len(_votes)} 次采样，胜出答案已确定")
                    try:
                        _lg_chat.info("答题投票", extra={"trace_id": trace_id,
                            "q": question[:60], "votes": " | ".join(_votes)[:120],
                            "winner": str(_vote_winner)[:80]})  # ⑫8 A8 留痕
                    except Exception:
                        pass
                except Exception as _e_v:
                    _steps_log.append(f"投票采样失败，回退单次直答: {type(_e_v).__name__}")
                    _vote_winner = None

        # （MAX_ROUNDS/_tok_* 已在投票块前初始化——投票短路在 _stream 内消费：
        # _vote_winner 非 None 时流式发出胜出答案并定稿，跳过工具循环与降级。）

    async def _llm(msgs, with_tools, on_delta=None):
        """三十一修：真流式 LLM 调用（stream:true SSE 解析——逐 token 经 on_delta 回调
        直通前端 yield，与旧链 fetch stream:true 完全同款交互）。
        on_delta(text, type)：reasoning/answer 逐 token。
        返回 (content, tool_calls, usage, reasoning)——同原签名（调用方不变）。"""
        b = {"model": model, "messages": msgs, "temperature": 0.3, "stream": True}
        if with_tools and tools_json:  # D-1：chat 模式 tools_json=[] 不塞（空数组有被代理 400 风险）
            b["tools"] = tools_json
        for _attempt in range(2):
            try:
                req = _ur.Request(chat_url.rstrip("/") + "/chat/completions",
                    data=_j.dumps(b).encode(),
                    headers={"Authorization": "Bearer " + chat_key, "Content-Type": "application/json"})
                resp = _ur.urlopen(req, timeout=120)
                _content = ""
                _reasoning = ""
                _tool_calls = []
                _usage = {}
                _buf = ""
                _raw = ""      # 三十三修：<think> 标签解析缓冲
                _in_think = False
                _done = False
                # 四十三修：增量 UTF-8 解码器。resp.read(512) 按字节切块，汉字（UTF-8 三字节）
                # 可能劈在两个块边界；旧写法逐块 decode(errors="replace") 会把劈开的半字
                # 直接替换成 U+FFFD（用户截图正文里的  乱码），再经 SSE 原样下发。
                # 增量解码器缓存块尾不完整字节、拼到下一块再解，彻底消除切块乱码。
                import codecs as _codecs
                _dec = _codecs.getincrementaldecoder("utf-8")("replace")
                while not _done:
                    chunk = resp.read(512)
                    if not chunk:
                        _buf += _dec.decode(b"", True)  # 流尾 flush 缓存的尾字节
                        break
                    _buf += _dec.decode(chunk)
                    while "\n" in _buf:
                        line, _buf = _buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            _done = True
                            break
                        try:
                            d = _j.loads(data_str)
                        except Exception:
                            continue
                        if d.get("usage"):
                            _usage = d["usage"]
                        delta = (d.get("choices") or [{}])[0].get("delta") or {}
                        rc = delta.get("reasoning_content")
                        if rc:
                            _reasoning += rc
                            if on_delta:
                                on_delta(rc, "reasoning")
                        c = delta.get("content")
                        if c:
                            # 三十三修：deepseek 思考走 <think> 标签（非 reasoning_content
                            # 字段——实测 delta reasoning=0 answer=86 且 answer 首块是
                            # "<think>The user asks..."）。解析标签：think 内→reasoning
                            _raw += c
                            while _raw:
                                if _in_think:
                                    _close = _raw.find("</think>")
                                    if _close >= 0:
                                        _think_part = _raw[:_close]
                                        if _think_part:
                                            _reasoning += _think_part
                                            if on_delta:
                                                on_delta(_think_part, "reasoning")
                                        _raw = _raw[_close + 8:]
                                        _in_think = False
                                    else:
                                        # 还没收全 </think>——全段是思考（留尾巴防标签劈半）
                                        _keep = _raw[-8:] if len(_raw) > 8 else ""
                                        _think_part = _raw[:len(_raw) - len(_keep)]
                                        if _think_part:
                                            _reasoning += _think_part
                                            if on_delta:
                                                on_delta(_think_part, "reasoning")
                                        _raw = _keep
                                        break
                                else:
                                    _open = _raw.find("<think>")
                                    if _open >= 0:
                                        _ans_part = _raw[:_open]
                                        if _ans_part:
                                            _content += _ans_part
                                            if on_delta:
                                                on_delta(_ans_part, "answer")
                                        _raw = _raw[_open + 7:]
                                        _in_think = True
                                    else:
                                        # 无标签——留尾巴防 "<thi" 劈半
                                        _keep = _raw[-7:] if len(_raw) > 7 else ""
                                        _ans_part = _raw[:len(_raw) - len(_keep)]
                                        if _ans_part:
                                            _content += _ans_part
                                            if on_delta:
                                                on_delta(_ans_part, "answer")
                                        _raw = _keep
                                        break
                        # 尾部残余 flush（流结束时）
                        if not chunk and _raw:
                            if _in_think:
                                _reasoning += _raw
                                if on_delta:
                                    on_delta(_raw, "reasoning")
                            else:
                                _content += _raw
                                if on_delta:
                                    on_delta(_raw, "answer")
                            _raw = ""
                        tcs = delta.get("tool_calls")
                        if tcs:
                            for tc in tcs:
                                idx = tc.get("index", 0)
                                while len(_tool_calls) <= idx:
                                    _tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if tc.get("id"):
                                    _tool_calls[idx]["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    _tool_calls[idx]["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    _tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                return _content, _tool_calls, _usage, _reasoning
            except _ur.URLError:
                if _attempt == 0:
                    await asyncio.sleep(2)
                else:
                    raise

    async def _stream():
        nonlocal kb_calls, _tok_total, _tok_calls  # 十二修：token 记账双变量漏 nonlocal
        final_answer = ""
        try:
            _lg_chat.info("LLM生成开始", extra={"trace_id": trace_id,
                "q": question[:60]})  # ⑫8 同款留痕（旧链 L1881）
        except Exception:
            pass
        try:
            # 投票已定稿 → 流式发出胜出答案 + final_answer 定稿。
            # ① 赢家真正生效（旧 bug：MAX_ROUNDS=0 空转后 final_answer 空，
            #   降级分支再调一次 LLM 覆盖投票结果）
            # ② 工具循环被 range(0) 跳过，降级分支被 final_answer 非空门住——
            #   投票结果直达 done 事件，与旧链 finalizeTurn(voteWinnerText) 同语义。
            if _vote_winner is not None:
                _sent_steps_n = len(_steps_log)  # 五十修·三修（advisor 重推实锤）：投票路全量推完记账——尾截只推审计新增行，防全量重推两遍
                yield f"event: round\ndata: {{\"round\": 1, \"steps\": {_j.dumps(_steps_log, ensure_ascii=False)}}}\n\n".encode()
                for i in range(0, len(_vote_winner), 80):
                    yield ("event: delta\ndata: " + _j.dumps({"text": _vote_winner[i:i+80]}, ensure_ascii=False) + "\n\n").encode()
                    await asyncio.sleep(0)
                final_answer = _vote_winner
            else:
                # 非投票路：正常工具循环 + 降级（与投票路互斥——advisor 结构修正：
                # 投票 yield 完不穿透到循环，避免二次作答）
                _sent_steps_n = 0  # 五十修：已推送 steps 计数（增量推送用）
                _wall_hit = False  # C 阶段活体验收实锤的洞：墙钟 break 路径空答案收尾
                for rnd in range(MAX_ROUNDS):
                    # 五十修（2026-09-25 用户实锤截图）：非投票路也要推 round 事件——
                    # 旧链 addProcessStep 逐步展示（补全语境→第1路→SQL→第2路→命中N块→轮次），
                    # 投票路 L6038 有、这里漏了：步骤攒在 _steps_log 不发，前端过程区光秃。
                    # 每轮推一次增量（_sent_steps 之前发过的不重发）——旧链边做边显示同款效果。
                    # 五十修补丁（advisor 重复展示实锤）：过滤「模型请求调用/工具返回」——
                    # 这两行已由 event: tool 单独推送（🔧 name (secs)），round 里再推=同一调用 3 条近似文案。
                    # 只推判定/检索/命中类步骤（补全语境/第1路/SQL/第2路/命中N块/轮次）。
                    _new_steps = [s for s in _steps_log[_sent_steps_n:]
                                  if not s.startswith("模型请求调用") and not s.startswith("工具返回")]
                    if _new_steps:
                        yield ("event: round\ndata: " + _j.dumps(
                            {"round": rnd + 1, "steps": _new_steps}, ensure_ascii=False) + "\n\n").encode()
                    _sent_steps_n = len(_steps_log)  # 计数以全量为准（被过滤的行也标记已读防重推）
                    # 三十一修：流式回调——delta 逐 token 直通 SSE 流（yield 在闭包内不可行——
                    # 用 buffer 收集 + 主循环 flush 的方式）
                    _delta_q = []
                    def _on_delta(text, dtype):
                        _delta_q.append((text, dtype))
                    content, tool_calls, usage, _reasoning = await _llm(messages, True, on_delta=_on_delta)
                    # flush 收集的 delta（逐 token——前端逐字渲染）
                    for _dt, _dtp in _delta_q:
                        yield ("event: delta\ndata: " + _j.dumps({"text": _dt, "type": _dtp}, ensure_ascii=False) + "\n\n").encode()
                    _tok_total += int(usage.get("total_tokens") or 0); _tok_calls += 1
                    if not tool_calls:
                        final_answer = content
                        _lg_chat.info("LLM生成完成", extra={"trace_id": trace_id,
                            "q": question[:60], "detail": f"{round(_t.time()-t0,1)}s | 答案: {str(content)[:200]}"})  # ⑫8 同款（旧链 L2153）
                        # 三十一修：真流式已逐 token 发过（on_delta 直通）——这里只收尾
                        break  # 正常完成（_wall_hit 保持 False——不进降级）
                    messages.append({"role": "assistant", "content": content or "",
                        "tool_calls": [{"id": tc["id"], "type": "function",
                                        "function": {"name": tc["function"]["name"],
                                                     "arguments": tc["function"]["arguments"]}}
                                       for tc in tool_calls]})
                    for tc in tool_calls[:4]:  # 每轮 tool_call 上限 4（评测器同款限流）
                        if _t.time() - t0 > _Q_WALL:
                            _wall_hit = True  # 工具段墙钟到限——降级作答（活体验收实锤：
                            # 原实现 break 后空答案收尾，模型 5 次检索全白烧）
                            messages.append({"role": "tool", "tool_call_id": tc["id"],
                                             "content": "检索时间到——请基于已有资料回答"})
                            continue
                        try:
                            args = _j.loads(tc["function"]["arguments"])
                        except Exception:
                            args = {}
                        if tc["function"]["name"] == "search_kb":
                            kb_calls += 1
                        # 2026-09-23 九修（advisory 破案：工具链检索没带 BYOK key →
                        # route_query 降级 search 路——无 SQL/MQ 增强，比端点直调弱一档）：
                        # chat_key/chat_url 随工具注入（kwargs 通道同 _scan_id 模式）
                        _steps_log.append(f"模型请求调用: {tc['function']['name']}({str(args)[:60]})")
                        result, meta = await run_in_threadpool(
                            _at.execute_tool, tc["function"]["name"], args,
                            trace_id, scan_id, chat_key, chat_url, vec_key, vec_url,
                            rerank_key, rerank_url, rerank_model, embed_model)
                        _steps_log.append(f"工具返回: {tc['function']['name']}（{meta.get('secs','?')}s）")
                        yield ("event: tool\ndata: " + _j.dumps(
                            {"name": tc["function"]["name"], "args": str(args)[:100],
                             "secs": meta.get("secs"), "ok": meta.get("ok")}, ensure_ascii=False) + "\n\n").encode()
                        # 2026-09-23 诊断：tool result 进链前留痕（长度+开头——查模型为何说没有）
                        print(f"[SSE工具结果] {tc['function']['name']} len={len(result)} | {result[:200]!r}", flush=True)
                        # 4.2 件②：工具结果分级截断（search_kb 6000/sql 3000/calc 800/…）
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                                         "content": _cm.clip_tool_result(tc["function"]["name"], result)})
                    if _wall_hit:
                        break  # 工具段墙钟到限——跳出外层循环走统一降级
                if not final_answer:
                    # 降级作答（5 轮耗尽 or 墙钟到限统一路径——与前端修1 同款：基于已有
                    # 资料给部分答案，不空手收尾。2026-09-24 C 阶段活体验收实锤修复）
                    messages.append({"role": "user", "content":
                        "已达检索轮次上限。请基于以上已有资料给出最终回答：已确认的部分直接答，"
                        "未找到的部分明确说明\"资料中未找到\"。不要再调用工具。"})
                    _lg_chat.info("Agentic降级作答", extra={"trace_id": trace_id,
                        "q": question[:40], "detail": "5轮到限，强制部分回答"})  # ⑫8 同款（旧链 L2193）
                    content, _, _, _ = await _llm(messages, False)  # 降级轮非流式（一次性答完）
                    final_answer = content
                    _lg_chat.info("LLM生成完成", extra={"trace_id": trace_id,
                        "q": question[:60], "detail": f"{round(_t.time()-t0,1)}s | 答案: {content[:200]}"})  # ⑫8 同款（旧链 L2153）
                    for i in range(0, len(content), 80):
                        yield ("event: delta\ndata: " + _j.dumps({"text": content[i:i+80]}, ensure_ascii=False) + "\n\n").encode()
                        await asyncio.sleep(0)
        except Exception as e:
            import traceback as _tb
            _stk = _tb.format_exc()[-260:].replace("\n", " | ")
            try:
                _lg_chat.error("SSE执行器异常", extra={"trace_id": trace_id, "stk": _stk})
            except Exception:
                pass
            yield ("event: delta\ndata: " + _j.dumps(
                {"text": f"执行器异常: {type(e).__name__}: {str(e)[:100]}——请重试"}, ensure_ascii=False) + "\n\n").encode()
        # ── D-5.6 数字溯源审计（2026-09-25 · 旧链 auditGrounding 等价下沉 L2205-2238）──
        # 触发条件（旧链 L2165 同款）：RAG 路且命中块>0。抽答案数字（≥3 位，含小数），
        # 字面找不到的交 LLM 审计（可能换算/加总等价）——不可支撑 → 答案尾标注
        # ⚠（不删答案：企业场景宁可标注不静默改写）。审计挂了不拦答案。
        if hits and final_answer:
            try:
                _nums = [m for m in re.findall(r"\d[\d,]*\.?\d*", final_answer)]
                _nums = [n.replace(",", "").replace("，", "") for n in _nums
                         if len(n.replace(",", "").replace("，", "").replace(".", "")) >= 3]
                _corpus = "\n".join(str(h.get("text") or "") for h in hits)
                _missing = [n for n in _nums if n not in _corpus and n.replace(".", "") not in _corpus]
                if _missing:
                    _steps_log.append(f"答案核验：{len(_missing)} 个数字正在核对资料出处...")
                    _lg_chat.info("audit_trigger", extra={"trace_id": trace_id,
                        "q": question[:60], "detail": f"答案{len(final_answer)}字，块{len(hits)}个——数字溯源审计"})  # ⑫5 同款留痕
                    _ctx = "\n".join(f"【资料{i+1}】{str(h.get('text') or '')[:400]}"
                                     for i, h in enumerate(hits[:8]))
                    _v = _llm_chat(
                        "你是答案审计员。回答里的数字若能从参考资料的原文、换算或加总得出答 YES，"
                        "完全找不到依据答 NO。只能答 YES 或 NO。\n\n"
                        f"问题: {question}\n回答: {final_answer}\n待验证数字: {'、'.join(_missing)}\n\n"
                        f"参考资料:\n{_ctx}", chat_key, chat_url)
                    if _v is None or not str(_v).strip():
                        pass  # B-3（2026-09-25 定案）：审计 LLM 失败/限流返回 None——
                        # 对齐旧链「审计挂了不拦答案」：不标注。旧逻辑 None → 非 YES → 误标 ⚠
                        # （用户两轮实测实锤：干净答案被挂「数字未找到依据」）
                    elif str(_v).strip().upper().startswith("YES"):
                        _steps_log.append("答案核验通过：数字均有资料出处")
                    else:
                        _steps_log.append("核验提醒：部分数字未找到资料出处，已在答案中标注")
                        final_answer += "\n\n⚠️ 注：以上回答中的部分数字在参考资料中未找到依据，请核实后再使用。"
                        for i in range(0, len("⚠️ 注：以上回答中的部分数字在参考资料中未找到依据，请核实后再使用。"), 80):
                            yield ("event: delta\ndata: " + _j.dumps(
                                {"text": "⚠️ 注：以上回答中的部分数字在参考资料中未找到依据，请核实后再使用。"[i:i+80]},
                                ensure_ascii=False) + "\n\n").encode()
            except Exception:
                pass  # 审计挂了不拦答案（旧链同款容错）
            # 五十修补丁（advisor 尾截两连修 2026-09-25）：
            # ① 弃用 event: round（前端 round 分支无条件渲染「第 N 轮请求」——尾截用
            #    round 会多一行假「第 6 轮请求」）→ 改发专用 event: steps，只渲染步骤行。
            # ② _sent_steps_n 在投票路（else 分支外）未定义 → NameError 被吞、审计步骤
            #    静默丢——get 兜底 0，投票路（计算题）也能推审计尾步骤。
            try:
                _tail_steps = [s for s in _steps_log[_sent_steps_n:]
                               if not s.startswith("模型请求调用") and not s.startswith("工具返回")]
                if _tail_steps:
                    yield ("event: steps\ndata: " + _j.dumps(
                        {"steps": _tail_steps}, ensure_ascii=False) + "\n\n").encode()
                    _sent_steps_n = len(_steps_log)
            except Exception:
                pass
        # ── D-5.7 忠实度校验：留在前端 SSE 分支（app.js verifyFaithfulness，四十一修）──
        # >200 字答后异步核对 + 警示条 + faithfulness_warn 经 /api/log 落服务端日志。
        # 服务端版已删（2026-09-25 advisor 实锤三连）：① event: faith 前端无消费端；
        # ② inline _llm_chat 挡 done 2-3s；③ 与前端重复判定。删链（D-6）不丢——
        # 该函数在 SSE 分支内非旧链段。12 类埋点 faithfulness_warn 仍可查（/api/log）。
        secs = round(_t.time() - t0, 1)
        try:
            _lg_chat.info("Agent执行器收尾", extra={
                "trace_id": trace_id, "rounds": MAX_ROUNDS, "kb_calls": kb_calls,
                "secs": secs, "answer_len": len(final_answer),
                "total_tokens": _tok_total, "llm_calls": _tok_calls})  # 4.3
        except Exception:
            pass
        yield ("event: done\ndata: " + _j.dumps(
            {"trace_id": trace_id, "kb_calls": kb_calls, "secs": secs,
             "total_tokens": _tok_total, "llm_calls": _tok_calls,
             "sources": _src_files[:15],
             "src_details": _src_details[:15]},  # 二十九修：块摘要（浮窗）
            ensure_ascii=False) + "\n\n").encode()

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Trace-Id": trace_id})


# ═══════════════════════════════════════════════════════════════
# 阶段三·会话管理（2026-09-23 开工·演进方案路线一冲刺「会话管理」）
# agent_sessions / agent_messages 两表 + userId 隔离 + 恢复/追加 API。
# 设计：同库（rag_index.db）追加表——SMB 落盘铁律不破；
#       userId 隔离（多用户边界——A 刷新不会恢复 B 的会话）；
#       刷新不丢会话（现状：前端内存变量刷新即丢——本段治它）。
# ═══════════════════════════════════════════════════════════════

def _agent_tables_init():
    """两表建表（幂等——重启安全）"""
    with _db_lock:
        _db.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id TEXT PRIMARY KEY,          -- 前端生成的 UUID
                user_id    TEXT NOT NULL DEFAULT 'local',  -- 用户标识（多用户隔离）
                title      TEXT,                     -- 首问截断（会话列表显示）
                created_at REAL, updated_at REAL
            )""")
        _db.execute("""
            CREATE TABLE IF NOT EXISTS agent_messages (
                msg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,             -- user/assistant/tool
                content    TEXT,
                meta       TEXT,                     -- JSON：{tool,secs,trace_id...}（assistant/tool 行）
                created_at REAL
            )""")
        _db.execute("CREATE INDEX IF NOT EXISTS idx_amsg_session ON agent_messages(session_id, msg_id)")
        _db.commit()

_agent_tables_init()

@app.get("/api/agent/sessions")
async def api_agent_sessions_list(user_id: str = "local"):
    """会话列表（按用户隔离——恢复下拉用）"""
    rows = _db.execute(
        "SELECT session_id, title, updated_at FROM agent_sessions "
        "WHERE user_id=? ORDER BY updated_at DESC LIMIT 50", (user_id,)).fetchall()
    return {"sessions": [{"session_id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]}

@app.get("/api/agent/sessions/{session_id}")
async def api_agent_session_load(session_id: str, user_id: str = "local"):
    """整会话恢复（消息全量——刷新后前端重放）"""
    s = _db.execute("SELECT session_id FROM agent_sessions WHERE session_id=? AND user_id=?",
                    (session_id, user_id)).fetchone()
    if not s:
        return {"error": "会话不存在或不属于该用户"}
    msgs = _db.execute(
        "SELECT role, content, meta, created_at FROM agent_messages "
        "WHERE session_id=? ORDER BY msg_id", (session_id,)).fetchall()
    import time as _t
    return {"session_id": session_id, "messages": [
        {"role": r[0], "content": r[1],
         **({"meta": json.loads(r[2])} if r[2] else {})} for r in msgs]}

@app.post("/api/agent/sessions/{session_id}/append")
async def api_agent_session_append(session_id: str, body: dict):
    """追加消息（user 问/assistant 答/tool 中间态都落）——body:
    {user_id, title?(首问建会话), messages: [{role, content, meta?}]}"""
    import time as _t
    user_id = body.get("user_id") or "local"
    msgs = body.get("messages") or []
    if not msgs:
        return {"error": "messages 必填"}
    now = _t.time()
    with _db_lock:
        # 会话不存在则建（首问自动建档）
        ex = _db.execute("SELECT session_id FROM agent_sessions WHERE session_id=?",
                         (session_id,)).fetchone()
        if not ex:
            title = (msgs[0].get("content") or "")[:40]
            _db.execute("INSERT INTO agent_sessions VALUES (?,?,?,?,?)",
                        (session_id, user_id, title, now, now))
        for m in msgs:
            _db.execute("INSERT INTO agent_messages (session_id, role, content, meta, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (session_id, m.get("role") or "user", m.get("content") or "",
                         json.dumps(m.get("meta"), ensure_ascii=False) if m.get("meta") else None, now))
        _db.execute("UPDATE agent_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
        _db.commit()
    return {"ok": True, "count": len(msgs)}

@app.delete("/api/agent/sessions/{session_id}")
async def api_agent_session_delete(session_id: str, user_id: str = "local"):
    """删会话（隔离校验——只能删自己的）"""
    with _db_lock:
        _db.execute("DELETE FROM agent_messages WHERE session_id=? AND session_id IN "
                    "(SELECT session_id FROM agent_sessions WHERE session_id=? AND user_id=?)",
                    (session_id, session_id, user_id))
        n = _db.execute("DELETE FROM agent_sessions WHERE session_id=? AND user_id=?",
                        (session_id, user_id)).rowcount
        _db.commit()
    return {"ok": True, "deleted": n}


if __name__ == "__main__":
    print("RAG 后端已启动: http://localhost:8001")
    print("前端页面（本机静态托管）: http://localhost:8001")
    # host 0.0.0.0 = 本机 + 局域网都能访问（同事打 http://<你的内网IP>:8001）。
    # 代价：同一 WiFi 下任何人都能调这个后端（含 API key）——局域网内测可接受
    uvicorn.run(app, host="0.0.0.0", port=8001)


