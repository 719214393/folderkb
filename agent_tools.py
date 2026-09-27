# agent_tools.py —— 阶段二·服务端 Agent 执行器（2026-09-23 开工·顺序①）
# 演进方案路线一冲刺「服务端 Agent 执行器」的三个部件之一：
#   ① @tool 装饰器注册表（本文件）——工具一处注册处处用
#   ② 五个只读工具接入（本文件）——包装现有 server 函数，零新引擎
#   ③ SSE 执行端点（写进 server.py——agent_chat 端点段）
# 设计铁律：
#   - 工具只读（perm=ro）——写工具是冲刺 D（P2 按需，审批闸未建不开工）
#   - Schema 按 MCP Tool 规范（name/description 四要素/parameters JSON Schema）
#   - 结果上限可配（max_result_chars）——防上下文爆炸
#   - 轨迹落盘 _lg_chat（trace_id/工具名/参数/结果长度/耗时）
# 通用性铁律：工具描述零语料词（演进方案纪律）

import time, json

# ---------- 注册表 ----------
_REGISTRY = {}  # name -> {"schema": {...}, "handler": fn, "perm": "ro", "max_chars": int}

def tool(name, description, parameters, perm="ro", max_result_chars=4000):
    """@tool 装饰器——注册即生效（新工具只写一处）。
    description 四要素：功能/何时用/何时禁用/参数说明（演进方案 A1）"""
    def deco(fn):
        _REGISTRY[name] = {
            "schema": {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            },
            "handler": fn,
            "perm": perm,
            "max_chars": max_result_chars,
        }
        return fn
    return deco

def get_tools_json():
    """tools 数组自动生成（LLM 请求体直接用）"""
    return [v["schema"] for v in _REGISTRY.values()]

def execute_tool(name, args, trace_id="", scan_id=None, chat_key=None, chat_url=None,
                 vec_key=None, vec_url=None, rerank_key=None, rerank_url=None,
                 rerank_model=None, embed_model=None):
    """统一执行入口：调度 + 轨迹 + 结果截断 + scan 上下文注入。
    scan_id 由 agent_chat 端点传入（服务端无全局"当前扫描"——按请求绑定）。
    返回 (result_text, meta)；未知工具/异常都返回文本（协议要求每个 call 有响应）。"""
    t = _REGISTRY.get(name)
    if not t:
        return f"未知工具 {name}", {"ok": False, "secs": 0}
    if scan_id is not None:
        args = {**(args or {}), "_scan_id": scan_id}  # 工具按需读取（声明了的才收）
    if chat_key is not None:
        args = {**(args or {}), "_chat_key": chat_key, "_chat_url": chat_url,
                "_vec_key": vec_key, "_vec_url": vec_url,
                "_rerank_key": rerank_key, "_rerank_url": rerank_url,
                "_rerank_model": rerank_model, "_embed_model": embed_model}
        # 九修+十修：BYOK 双 key 注入（chat 检索路 + vec 语义/重排路——分开透传）
        # 三模型改造：重排/嵌入模型名也一起注入（工具只声明它实际要用的那几个，
        # 多余参数由下面的 TypeError 重试按签名自动剔除）
    t0 = time.time()
    try:
        result = t["handler"](**(args or {}))
        ok = True
    except TypeError:
        # 六十三修（2026-09-25 用户 DevTools 实锤破案）：九修注入 _chat_key 等参数后，
        # 凡是【只声明 _scan_id、没声明 BYOK 参数】的工具（list_kb_files/read_chunk）必抛
        # TypeError 走到这里；旧重试把 _scan_id 一起剔除 → 工具收到 None → 报
        # 「未指定知识库」——用户带库提问也被骗（预检索正常，工具层全瞎）。
        # 修法：重试只剔除【工具实际不收】的参数——_scan_id 声明了就保留。
        import inspect as _insp
        _params = set(_insp.signature(t["handler"]).parameters)
        keep = {}
        for k, v in (args or {}).items():
            if k in _params or not k.startswith("_"):
                keep[k] = v
        try:
            result = t["handler"](**keep)
            ok = True
        except Exception as e:
            result, ok = f"工具执行失败: {type(e).__name__}: {str(e)[:200]}", False
    except Exception as e:
        result, ok = f"工具执行失败: {type(e).__name__}: {str(e)[:200]}", False
    secs = round(time.time() - t0, 2)
    result = str(result)[: t["max_chars"]]
    # 轨迹落盘（演进方案 A5——_lg_chat 已有基建，这里走同名 logger）
    try:
        import server as _srv
        _srv._lg_chat.info("工具调用", extra={
            "trace_id": trace_id, "tool": name,
            "args": json.dumps(args, ensure_ascii=False)[:200],
            "result_len": len(result), "secs": secs, "ok": ok})
    except Exception:
        pass  # 轨迹失败不阻塞工具结果
    return result, {"ok": ok, "secs": secs}


# ---------- 五个只读工具（演进方案 A3——包装现有函数，零新引擎） ----------
# 依赖注入：server 模块的函数在 import 时绑定（延迟 import 防循环依赖）

@tool(
    name="search_kb",
    description=(
        "检索知识库（文档/表格/结构化数据）。功能：按关键词搜知识库，返回最相关的文档片段，"
        "含表格命中行前置。何时用：需要查具体数据、文档内容、表格数值、编号/名称对应关系时。"
        "多跳问题（问两件事）拆成多次检索分别查。检索结果不够时换关键词重查（最多再查 2 次）。"
        "何时禁用：闲聊/常识问题/无需查资料时。参数：query 必填，检索关键词（用文档里可能出现的原文表述，不要口语化）。"
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "检索关键词——用文档原文表述"}},
        "required": ["query"],
    },
    max_result_chars=6000,
)
def tool_search_kb(query: str, _scan_id=None, _chat_key=None, _chat_url=None,
                   _vec_key=None, _vec_url=None, _rerank_key=None, _rerank_url=None,
                   _rerank_model=None, _embed_model=None):
    """混合检索（api_search 同款链路：语义+关键词+RRF+重排+行前置+SQL 直查）"""
    # 6.5 注入消毒（2026-09-23）：query 截长 + 控制字符剥除
    # （防提示词注入经工具参数混入检索链——工具描述即消毒边界）
    query = "".join(ch for ch in (query or "")[:300] if ch.isprintable() or ch in "\n\t")
    import server as srv
    # D-3.0 契约：只查指定的库（_scan_id 唯一事实源——SSE 端点按请求注入）。
    # 双查兜底（换 _latest_scan 块数最多库 + 丢 key 重查——把 105 命中变 94 零命中）
    # 已删除。无 hits/异常 → 如实告知模型（同库不换、key 不丢）。
    if _scan_id is None:
        return _NO_SCAN_MSG
    if _scan_missing(_scan_id):
        return f"知识库 {_scan_id} 不存在——可能已被清理，请让用户重新选择"
    try:
        r = srv.api_search(_scan_id, query, k=15,
                           request=_fake_request(_chat_key, _chat_url, _vec_key, _vec_url,
                                                 _rerank_key, _rerank_url, _rerank_model, _embed_model))
    except Exception as _e:
        return f"检索异常: {type(_e).__name__}——请换关键词重试或基于已有资料回答"
    if not r or "hits" not in r:
        return f"检索异常（返回缺结果字段）: {str(r)[:100]}——请换关键词重试"
    hits = r.get("hits") or []
    sql_h = r.get("sql_hits") or []
    if not hits and not sql_h:
        return "检索零命中——尝试换用文档原文表述的关键词（如表格列名/文件名/编号）"
    import re as _re
    tokens = [w for w in _re.split(r"[\s，。、,]+", query[:200]) if len(w) >= 2]
    parts = []
    for i, h in enumerate(hits[:8]):
        full = h.get("text") or ""
        hit_lines = [l for l in full.split("\n")
                     if any(t in l for t in tokens) and l.strip().startswith("|")][:3]
        head = ("⚡ 命中行:\n" + "\n".join(hit_lines) + "\n---\n") if hit_lines else ""
        fname = (h.get("file") or "").replace("\\", "/").split("/")[-1]
        parts.append(f"【资料{i+1}】出处: {fname} 第{h.get('seq')}块\n{head}{full[:400]}")
    out = "\n\n".join(parts)
    if sql_h:
        out += "\n\n" + "\n".join(f"【SQL直查】{(h.get('text') or '')[:300]}" for h in sql_h)
    return out[:6000]


@tool(
    name="sql_query",
    description=(
        "查表取精确值（Text-to-SQL 路）。功能：把自然语言问题转成对知识库内结构化表格的精确查询，"
        "返回命中的数据行。何时用：问表格中的具体数值/金额/数量/编码/日期，或某行某列内容、"
        "编号与名称的对应关系——比语义检索更快更准。何时禁用：问原因/流程/怎么做/总结归纳类问题。"
    ),
    parameters={
        "type": "object",
        "properties": {"question": {"type": "string", "description": "要查的问题（自然语言，如'科目 1001 的期末余额'）"}},
        "required": ["question"],
    },
    max_result_chars=3000,
)
def tool_sql_query(question: str, _scan_id=None, _chat_key=None, _chat_url=None):
    """包 server.sql_route_search——LLM 生成 pandas 表达式本地执行返回精确行"""
    import server as srv
    # D-3.0 契约：只查指定的库
    if _scan_id is None:
        return _NO_SCAN_MSG
    if _scan_missing(_scan_id):
        return f"知识库 {_scan_id} 不存在——可能已被清理，请让用户重新选择"
    sh = srv.sql_route_search(_scan_id, question, vec_key=_chat_key, vec_url=_chat_url)
    # sql_route_search 的 vec_key 形参=历史遗留名，实际收 chat 配置（server 源码注释）
    if not sh:
        return "SQL 路未命中（该问题可能不适合查表，或表里没有该数据）——可改用 search_kb 语义检索"
    return "\n".join(h.get("text", "")[:300] for h in sh[:5]) or "SQL 查询无结果"


@tool(
    name="calc",
    description=(
        "计算器（代码核算）。功能：对资料中的数字做精确计算（求和/差额/均值/日期间隔等），"
        "返回核算过程与结果。何时用：答案需要数字运算时（金额合计、余额对比、差额计算）——"
        "比心算可靠。何时禁用：纯事实查询（不需要运算）时。"
    ),
    parameters={
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "要计算的表达式（自然语言描述即可，如'3 笔金额 100+200+300 求和'）"}},
        "required": ["expression"],
    },
    max_result_chars=1500,
)
def tool_calc(expression: str):
    """包 server.calc_tool 思路——LLM 结构化意图 + Python eval（白名单函数）"""
    # 安全闸：只允许数字/运算符/少量函数名（防注入——生产安全基线）
    import re as _re
    if not _re.fullmatch(r"[\d\s+\-*/().,%亿万千百十]+", expression or ""):
        # 非纯算式（自然语言）→ 包现成 server.calc_tool（advisory：中文金额/
        # 缺运算符场景正则版会静默算错——不自造引擎，走生产同款 LLM 结构化+核算）
        try:
            import server as srv
            r = srv.calc_tool(expression)
            if r:
                return str(r)[:1500]
        except Exception:
            pass
        # calc_tool 不可用（独立进程/失败）→ 本地保守提取（量词剔除+拼算式）
        _cleaned = _re.sub(r"\d+\s*[笔个次行张条份项道步]", "", expression)
        expr = "".join(_re.findall(r"[\d.+\-*/()]+", _cleaned))
        if not expr.strip("+-*/(). "):
            return "无法解析出可计算的表达式"
        expression = expr
    expr = expression.replace(",", "").replace("亿", "*100000000").replace("万", "*10000")
    expr = expr.replace("千", "*1000").replace("百", "*100").replace("十", "*10").replace("%", "/100")
    # B14 修（error.md）：** 幂运算可构造 9**9**9 长时间大整数运算挂住 worker，
    # 超长表达式同理——黑名单 + 长度上限双闸（预筛字符类含 *，拦不住 **）
    if "**" in expr or len(expr) > 200:
        return "表达式含幂运算或过长，拒绝计算"
    try:
        val = eval(expr, {"__builtins__": {}}, {})  # 白名单空环境——纯算式求值
        return f"{expression} = {val}"
    except Exception as e:
        return f"计算失败: {type(e).__name__}（表达式: {expression}）"


@tool(
    name="list_kb_files",
    description=(
        "列出知识库文件清单。功能：返回当前知识库里的文件列表（含每个文件的块数）。"
        "何时用：不确定库里有什么、想先看有什么文件再决定查法时。何时禁用：已明确知道要查什么时。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    max_result_chars=2500,
)
def tool_list_kb_files(_scan_id=None):
    """列出当前扫描的文件清单（块数）——帮模型先看库再定查法"""
    import sqlite3
    import server as srv
    con = sqlite3.connect(f"file:{srv.DB_PATH}?mode=ro", uri=True)
    # D-3.0 契约：只查指定的库
    if _scan_id is None:
        con.close()
        return _NO_SCAN_MSG
    if _scan_missing(_scan_id):
        con.close()
        return f"知识库 {_scan_id} 不存在——可能已被清理，请让用户重新选择"
    rows = con.execute(
        "SELECT file, COUNT(*) FROM chunks WHERE scan_id=? GROUP BY file ORDER BY file LIMIT 60",
        (_scan_id,)).fetchall()
    con.close()
    return "\n".join(f"{f}（{n} 块）" for f, n in rows) or "知识库为空"


class _FakeHeaders:
    def __init__(self, h): self._h = {str(k).lower(): v for k, v in (h or {}).items()}
    # 2026-09-23 十修（advisory blocker）：消费方读小写 "x-vec-key"/"x-chat-key"
    # ——get 大小写不敏感（键归一 lower），否则 key 全取 None 九修变 no-op
    def get(self, k, default=None): return self._h.get(str(k).lower(), default)

class _FakeRequest:
    """九修+十修+十四修：直调 api_search 的假 Request——BYOK 双 key 走 headers。
    十四修：空串→None（下游 `key or 常量` 回退才生效——空串 key 实锤 hits=0）
    三模型改造：再带重排(url/key/model) 与嵌入模型名（可空——下游逐级回退）"""
    def __init__(self, chat_key=None, chat_url=None, vec_key=None, vec_url=None,
                 rerank_key=None, rerank_url=None, rerank_model=None, embed_model=None):
        self.headers = _FakeHeaders({
            "x-chat-key": chat_key or None, "x-chat-url": chat_url or None,
            "x-vec-key": vec_key or chat_key or None,
            "x-vec-url": vec_url or chat_url or None,
            "x-rerank-key": rerank_key or None,
            "x-rerank-url": rerank_url or None,
            "x-rerank-model": rerank_model or None,
            "x-embed-model": embed_model or None,
        })

def _fake_request(chat_key, chat_url, vec_key=None, vec_url=None,
                  rerank_key=None, rerank_url=None, rerank_model=None, embed_model=None):
    return _FakeRequest(chat_key, chat_url, vec_key, vec_url,
                        rerank_key, rerank_url, rerank_model, embed_model)

# ── D-3.0 统一 scan_id 契约（2026-09-24 用户拍板·业界对齐）──
# 「用户指定了哪个库就在哪个库里查，兜底不换目标」：
# ① _scan_id 有则用之（唯一事实源——SSE 端点按请求绑定注入）
# ② 指定的 scan 不存在 → 「该知识库不存在」（404 语义，模型转告用户）
# ③ 没带 → 「未指定知识库」（400 语义）
# 动态猜库（原 _latest_scan 选块数最多——双查 bug 病灶）已删除，不留后门。
_NO_SCAN_MSG = "未指定知识库——请让用户先选择知识库（当前请求未携带库编号）"

def _scan_missing(scan_id):
    """404 语义：指定了 scan 但库里没有（供工具返回明因文本）"""
    import sqlite3
    import server as srv
    con = sqlite3.connect(f"file:{srv.DB_PATH}?mode=ro", uri=True)
    row = con.execute("SELECT id FROM scans WHERE id=?", (scan_id,)).fetchone()
    con.close()
    return row is None


@tool(
    name="read_chunk",
    description=(
        "读文件指定块的全文。功能：按文件名+块序号取该块完整内容（不截断）。"
        "何时用：检索结果里某块被截断（如表格只看到前几行）需要看全文时——"
        "用 search_kb 返回里的'出处: 文件名 第N块'定位。何时禁用：已拿到完整答案时。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "文件名（search_kb 结果里的出处文件名）"},
            "seq": {"type": "integer", "description": "块序号（出处里'第N块'的 N）"},
        },
        "required": ["file", "seq"],
    },
    max_result_chars=4000,
)
def tool_read_chunk(file: str, seq: int, _scan_id=None):
    """治 400 字截断——按 (file, seq) 取全文（演进方案 A3 原文目标）"""
    import sqlite3
    import server as srv
    con = sqlite3.connect(f"file:{srv.DB_PATH}?mode=ro", uri=True)
    # D-3.0 契约：只查指定的库
    if _scan_id is None:
        con.close()
        return _NO_SCAN_MSG
    if _scan_missing(_scan_id):
        con.close()
        return f"知识库 {_scan_id} 不存在——可能已被清理，请让用户重新选择"
    rows = con.execute(
        "SELECT text FROM chunks WHERE scan_id=? AND seq=? AND file LIKE ? ORDER BY id DESC LIMIT 1",
        (_scan_id, seq, f"%{file}%")).fetchall()
    con.close()
    if not rows:
        return f"未找到 块{seq}（文件 {file}）——核对 search_kb 返回里的出处"
    return rows[0][0][:4000]
