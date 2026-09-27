# context_manager.py —— 阶段三 4.2·上下文管理三件套（2026-09-23）
# 治长对话 token 膨胀（演进方案路线一冲刺「会话管理」第 2 件）：
#   ① 历史滑窗 + 摘要压缩：超窗历史压成一段摘要（复用 _llm_chat——不引新依赖）
#   ② 工具结果分级截断：按工具类型配置截断档（注册表 meta 可覆盖）
#   ③ 预检索块数动态下调：长会话里预检索从 15 块逐级降到 8（省 token 保头尾）
# 设计：纯函数模块——SSE 端点/前端旧路都能调；不依赖 server import（摘要回调注入）

# ---------- ② 工具结果分级截断 ----------
# 按工具名配置结果保留上限（字）。默认 4000；检索类工具大（资料要有余量），
# 确定性工具小（calc/list 结果短不用截）。
TOOL_RESULT_LIMITS = {
    "search_kb": 6000,      # 资料主路——与前端 6000 上限一致
    "sql_query": 3000,      # 精确行——够 5 行
    "calc": 800,            # 算式+结果
    "list_kb_files": 2000,  # 60 文件清单
    "read_chunk": 4000,     # 全文读取（截断就失去意义——但防单块超长）
}

def clip_tool_result(name, text):
    """工具结果按类型截断（超限保头+提示尾）。"""
    if not text:
        return text
    lim = TOOL_RESULT_LIMITS.get(name, 4000)
    if len(text) <= lim:
        return text
    return text[:lim] + f"\n…(已截断，原文 {len(text)} 字——需要全文用 read_chunk)"

# ---------- ① 历史滑窗 + 摘要压缩 ----------
def slide_history(history, keep_recent=12, max_summary_chars=600, summarize_fn=None):
    """历史滑窗：最近 keep_recent 条原样保留，更早的压成摘要。
    ★ 单位口径（2026-09-24 修正）：history 是平铺 [{role, content}]，每轮问答
      = 2 条（user+assistant）。keep_recent 按【条】计——默认 12 条 = 6 轮
      （旧默认 6 条=3 轮是单位错位，比旧链 slice(-10) 还少，已修）。
    summarize_fn: 可选注入的摘要函数（server._llm_chat 包装——签名 (text)->str）。
                  None/失败时用本地截断摘要（零依赖兜底——首尾拼接）。
    返回 [{role:"system", content:"此前对话摘要…"}, ...最近 keep_recent 条原样]"""
    if len(history) <= keep_recent:
        return list(history)
    old, recent = history[:-keep_recent], history[-keep_recent:]
    old_text = "\n".join(f"{m['role']}: {str(m.get('content') or '')[:200]}" for m in old)
    if summarize_fn:
        try:
            summary = summarize_fn(old_text)
        except Exception:
            summary = None
    else:
        summary = None
    if not summary:
        # 本地兜底：首 2 条 + 尾 2 条拼接（保最早提问和最近上下文）
        head = "\n".join(f"{m['role']}: {str(m.get('content') or '')[:120]}" for m in old[:2])
        tail = "\n".join(f"{m['role']}: {str(m.get('content') or '')[:120]}" for m in old[-2:])
        summary = f"{head}\n…（中间省略 {max(0, len(old)-4)} 条）\n{tail}"
    summary = summary[:max_summary_chars]
    return [{"role": "system", "content": f"【此前对话摘要】\n{summary}"}] + list(recent)

# ---------- ③ 预检索块数动态下调 ----------
def dynamic_presearch_k(rounds, base_k=15):
    """长会话逐级下调预检索块数（演进方案 B2 原文）：
    0-4 轮 15 块 → 5-9 轮 12 块 → 10-14 轮 10 块 → 15+ 轮 8 块
    ★ 单位口径（2026-09-24 修正）：参数是【轮】数——调用方传 len(history)//2
      （每轮 2 条）。旧签名收条数是单位错位。
    （多跳题靠工具补查兜底——预检索只是首轮参考）"""
    if rounds <= 4:
        return base_k
    if rounds <= 9:
        return min(base_k, 12)
    if rounds <= 14:
        return min(base_k, 10)
    return min(base_k, 8)

# ---------- 组合入口（SSE 端点一次调全） ----------
def prepare_context(history, tool_name=None, tool_result=None, base_k=15):
    """三件套组合：给 SSE 端点的单入口。
    - history 滑窗（默认 12 条=6 轮 verbatim，更早压摘要）
    - 工具结果截断（clip_tool_result）
    - 预检索块数（按轮数——条数//2）
    返回 (shaped_history, presearch_k)"""
    shaped = slide_history(history)
    k = dynamic_presearch_k(len(history) // 2, base_k)
    return shaped, k
