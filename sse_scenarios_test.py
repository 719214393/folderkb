# sse_scenarios_test.py —— AGENT_SSE_ON 切换前的四场景回归（2026-09-23）
# 前端走 SSE（AGENT_SSE_ON=true 模拟）的四个场景挨个验——全过才切开关
import urllib.request, json, time, sys

BASE = 'http://127.0.0.1:8001'
# 密钥从环境变量读，不写进代码；未设置时报错提示
import os
KEYS = {
    'chat_key': os.environ.get('RAG_CHAT_KEY', ''),
    'chat_url': os.environ.get('RAG_CHAT_URL', 'https://api.siliconflow.cn/v1'),
    'vec_key': os.environ.get('RAG_VEC_KEY', ''),
    'vec_url': os.environ.get('RAG_VEC_URL', 'https://api.siliconflow.cn/v1'),
    'model': os.environ.get('RAG_MODEL', ''),
}
if not KEYS['chat_key'] or not KEYS['vec_key']:
    sys.exit('请先设置环境变量 RAG_CHAT_KEY / RAG_VEC_KEY（BYOK——密钥不入仓库）')

def sse(q, scan_id=None, timeout=240):
    if scan_id is None:
        scan_id = int(os.environ.get('RAG_SCAN_ID', '0')) or None  # 扫自己的库后设 RAG_SCAN_ID
    body = json.dumps({'question': q, 'scan_id': scan_id, **KEYS}).encode()
    req = urllib.request.Request(BASE + '/api/agent/chat', data=body,
        headers={'Content-Type': 'application/json'})
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=timeout)
    buf = ''
    answer = ''
    tools = []
    while True:
        chunk = resp.read(512)
        if not chunk: break
        buf += chunk.decode('utf-8', errors='replace')
        while '\n\n' in buf:
            block, buf = buf.split('\n\n', 1)
            ev = ''
            for line in block.split('\n'):
                if line.startswith('event: '): ev = line[7:]
                elif line.startswith('data: '):
                    try: d = json.loads(line[6:])
                    except: continue
                    if ev == 'delta': answer += d.get('text', '')
                    elif ev == 'tool': tools.append(d.get('name'))
    return answer, tools, time.time() - t0

scenarios = [
    ('普通聊天', '你好，今天天气怎么样？', None),          # 无需检索——应正常闲聊
    ('RAG 问答', '科目编码1001的科目名称是什么？', '库存现金'),  # 单跳——已验证
    ('多跳补查', '科目1001和1002的科目名称分别是什么？', None),  # 两跳——工具拆查
    ('降级作答', '公司去年的火星殖民地预算是多少？', None),      # 库里没有——应说没有
]
results = []
for name, q, expect in scenarios:
    try:
        ans, tools, secs = sse(q)
        ok = (expect in ans) if expect else ('执行器异常' not in ans)
        results.append((name, ok, secs, tools, ans[:60]))
        print(f"{'✓' if ok else '✗'} [{name}] {secs:.0f}s 工具{tools[:3]}")
        print(f"   {ans[:80]}")
    except Exception as e:
        results.append((name, False, 0, [], str(e)[:50]))
        print(f"✗ [{name}] {type(e).__name__}")
print()
n_ok = sum(1 for r in results if r[1])
print(f"四场景: {n_ok}/4 —— {'✓ 可切 AGENT_SSE_ON=true' if n_ok == 4 else '✗ 有失败——别切'}")
