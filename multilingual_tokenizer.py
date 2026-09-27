# multilingual_tokenizer.py —— 通用性改造①②（2026-09-23 开工）
# ① 停用词多语言化：中文虚词表 + 英文停用词 + 库内高频自动降权（换库自适应）
# ② 分词器语言分流：中文 jieba / 拉丁空格+短语 / 日韩 bigram——按查询字符构成自动路由
# 铁律：不准写死语料相关规则（换库必须自动适配）——本模块零语料专属词，
#       库内高频降权的数据源是当前库的统计（换库自动重算）

import re

# ---------- ① 停用词：最小功能词集 + 库内统计学习（2026-09-23 重构） ----------
# 设计变更（用户批评"写死"成立）：写死表只保留"语言学通用功能词"冷启动兜底
# ——疑问代词/冠词/礼貌语在任何语言、任何库都无检索意义（这是语言事实，
# 不是语料知识）。语料相关的泛词（原 48 词里的"业务/信息/数据/记录"等）
# 全部移出写死表，交给库内高频统计自动学习（换库自动重算）。
STOP_ZH_MIN = {"什么", "哪些", "怎么", "如何", "多少", "是否", "可以",
               "请问", "还是", "以及", "这个", "那个", "一下", "一个"}
STOP_EN_MIN = {"what", "which", "how", "does", "is", "are", "the", "a", "an",
               "of", "in", "on", "for", "to", "and", "or", "please",
               "tell", "show", "list", "give", "me", "this", "that"}
# （日韩最小集：黏着语功能词靠 bigram 统计自然降权——无需写死）

STOP_ALL = STOP_ZH_MIN | STOP_EN_MIN



# ---------- 库内高频自动降权（换库自适应的核心） ----------
# 数据源：当前库 FTS 统计——出现率超阈值的 token 自动加入停用池
# （"出现率"= 含该词的块数 / 总块数——超过 15% 的词是"库内泛词"）
_corpus_stop = None  # 惰性缓存（首次调用算，换库后由外部调 reset_corpus_stats()）

def reset_corpus_stats():
    """换库/重扫后调用——清缓存，下个查询重算降权映射"""
    global _domain_weights
    _domain_weights = None

def _scan_domain_weights(db_path):
    """库内高频【降权】（2026-09-23 advisory 重构：降权≠硬删——统计层返回权重映射，
    调用方压 LIKE/BM25 分，词绝不从查询里删除。载荷词如"科目/余额"高频是正常的
    ——它们只是排序信号弱，不是该消失的词）。
    返回 {token: weight}，weight∈(0,1]，覆盖率越高权重越低：
      覆盖率 <15% → 不在映射里（权重 1.0）
      15-40% → 0.5
      >40%   → 0.2
    """
    import sqlite3
    weights = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if total < 50:
            return weights  # 库太小不统计（噪声大）
        threshold = total * 0.15
        # 路 A：FTS vocab（精确）
        try:
            rows = con.execute(
                "SELECT term, doc FROM chunks_fts_v WHERE doc > ?", (threshold,)
            ).fetchall()
            for term, doc in rows:
                ratio = doc / total if total else 0
                if not term.replace('.', '').replace(',', '').isdigit():
                    # 2026-09-23 修（advisory 数字矛盾）：平权 0.5 压不住
                    # freq4 倍差——改覆盖率反比（IDF 思想）：
                    # 覆盖率翻倍权重减半。15%→0.5，30%→0.25，60%→0.125
                    weights[term] = max(0.05, 0.5 * (0.15 / max(ratio, 0.15)))
        except sqlite3.OperationalError:
            # 路 B：LIKE 抽样（无 vocab 的库——自适应兜底）
            import random as _rnd
            _rnd.seed(42)  # 稳定抽样（同库同结果）
            sample = con.execute(
                "SELECT text FROM chunks ORDER BY RANDOM() LIMIT 200"
            ).fetchall()
            from collections import Counter
            tok_doc = Counter()  # 词 → 出现块数（去重计块）
            for (txt,) in sample:
                seen_toks = set(re.findall(r'[\u4e00-\u9fff]{2,4}|[A-Za-z]{2,}', txt[:500]))
                for t in seen_toks:
                    tok_doc[t] += 1
            for t, c in tok_doc.items():
                ratio = c / len(sample) if sample else 0
                if ratio > 0.15 and not t.replace('.', '').isdigit():
                    # 同上：覆盖率反比权重（advisory 修）
                    weights[t] = max(0.05, 0.5 * (0.15 / max(ratio, 0.15)))
        con.close()
    except Exception:
        pass
    return weights

def get_stopwords():
    """停用池 = 语言功能词（最小写死集——语言事实，非语料知识）。
    注意：不含库内统计词（那些走 get_domain_weights 降权，不删词）"""
    return STOP_ALL

_domain_weights = None  # 缓存（换库调 reset_corpus_stats 清）

def get_domain_weights(db_path):
    """库内高频降权映射（自适应——换库重算）。调用方用它乘 BM25/LIKE 分，
    词不删除（advisory 2026-09-23：载荷词高频是正常现象，降权不硬删）。
    2026-09-23 夜修：首次调用不阻塞——SMB 冷读统计分钟级会卡死首个查询。
    改两段式：立即返回空映射（全 1.0 权重=无降权），后台线程算完缓存生效。"""
    global _domain_weights
    if _domain_weights is None:
        import threading
        def _bg():
            global _domain_weights
            try:
                _domain_weights = _scan_domain_weights(db_path)
            except Exception:
                _domain_weights = {}
        threading.Thread(target=_bg, daemon=True).start()
        return {}  # 首查空映射——不阻塞
    return _domain_weights

# ---------- ② 语言感知分词路由 ----------
def _script_ratio(q):
    """字符构成：中文/日韩/拉丁占比——路由依据"""
    n = max(len(q), 1)
    zh = len(re.findall(r'[\u4e00-\u9fff]', q))
    ja_ko = len(re.findall(r'[\u3040-\u30ff\uac00-\ud7af]', q))
    latin = len(re.findall(r'[A-Za-z]', q))
    return zh / n, ja_ko / n, latin / n

def tokenize(q, db_path=None):
    """语言感知分词（替代 db_keyword_search 里的写死 jieba 三路）：
    - 中文占比高 → jieba 全量三路（原行为——中文库零回归）
    - 日韩占比高 → 字符 bigram（日韩黏着语，词边界无空格）
    - 拉丁占比高 → 空格分词 + 连字符短语保留
    - 混合 → 中文段走 jieba + 拉丁段走短语（两种并集）
    返回 words 列表（已滤停用词）
    """
    stop = get_stopwords()
    zh_r, jk_r, la_r = _script_ratio(q)
    words, seen = [], set()
    def _add(w):
        # 2026-09-23 修：拉丁词 casefold 后比对（What→what 命中功能词表）
        _w = w.casefold() if re.match(r'^[A-Za-z]+$', w) else w
        if len(w.strip()) >= 2 and _w not in stop and w not in seen:
            if not re.match(r"^[\s，。？！、,.;:?!()（）]+$", w):
                seen.add(w)
                words.append(w)
    # 拉丁短语路（所有语言都跑——英文编号/代码名必须保）
    for m in re.findall(r'[A-Za-z][A-Za-z0-9/.\-\[\]]{3,}', q):
        if any(c.isupper() for c in m) or '/' in m or '[' in m:
            _add(m)
    if zh_r >= 0.15:  # 含中文 → jieba 路（原行为）
        import jieba
        for w in jieba.cut(q):
            _add(w)
        for w in jieba.cut_for_search(q):
            _add(w)
    if jk_r >= 0.15:  # 含日韩 → bigram 路
        for seg in re.findall(r'[\u3040-\u30ff\uac00-\ud7af]+', q):
            for i in range(len(seg) - 1):
                _add(seg[i:i+2])
    if zh_r < 0.15 and jk_r < 0.15 and la_r >= 0.3:  # 纯拉丁 → 空格分词
        for w in re.split(r'\s+', q):
            _add(w)
    # 标点切分兜底（所有语言）
    for w in re.split(r"[\s，。？！、,.;:?!()（）]+", q):
        _add(w)
    # 中英混合连刀词清洗（原 优化1c 行为）
    words = [w for w in words
             if not (re.search(r'[A-Za-z]', w) and re.search(r'[\u4e00-\u9fff]', w))]
    return words
