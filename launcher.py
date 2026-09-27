# ============================================================
# 桌面版启动器（同进程方案）：
#   import server 拿到 FastAPI app -> 子线程跑 uvicorn -> 主线程开 WebView2 窗口
#   关窗口 = 主进程退出 = 全部收干净（无线程残留、无子进程树）
# 打包：py -m PyInstaller rag_desktop.spec -> dist/RAG助手/RAG助手.exe
#
# 看门狗（2026-09-14 用户定标：零下载、双平台、打包进去用户无感知）：
#   同进程架构下后端崩 = exe 崩，进程内无"谁拉谁"——采用业界桌面工具标准
#   做法"崩溃遗言 + 启动自检"：崩溃瞬间把异常栈写 crash.log；下次启动
#   检测到上次异常退出就先展示"上次崩溃原因+已自动恢复"，用户双击即自愈。
#   （NSSM/子进程监督路线废弃：用户不接受任何下载，同进程打包最干净；
#    macOS 侧 launchd 模板在 tools/com.rag.backend.plist，⑪ 真机验证）
# ============================================================
import sys
import os
import time
import socket
import threading
import traceback

import webview  # pywebview：Windows 自动用系统自带 WebView2 内核（Edge）

BACKEND_PORT = 8001
# 崩溃日志：开发模式 = 脚本旁边；打包模式（frozen）= 用户数据目录
# （与 server.py 的 _DATA_DIR 同款约定，exe 更新/卸载重装不丢）
if getattr(sys, 'frozen', False):
    _DATA_DIR = os.path.join(os.environ.get("APPDATA", str(os.path.expanduser("~"))), "RAGAssistant")
    os.makedirs(_DATA_DIR, exist_ok=True)
    CRASH_LOG = os.path.join(_DATA_DIR, "crash.log")
else:
    CRASH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def start_backend():
    """子线程跑后端。已占用（别的实例在跑）就不重复起。
    后端线程挂了由 _run_uvicorn 内联的 try/except 记崩溃遗言（进程还活着，
    窗口还开着，用户感知 = 页面报错；重开即自愈——比静默死强）。"""
    if port_in_use(BACKEND_PORT):
        print(f"端口 {BACKEND_PORT} 已有服务，直接复用")
        return
    import server  # noqa: E402 —— 同进程 import：拿 app 对象（server.py 里的建库/建表随之执行）
    import uvicorn

    def _run_uvicorn():
        try:
            uvicorn.run(server.app, host="127.0.0.1", port=BACKEND_PORT, log_level="warning")
        except Exception:
            # 崩溃遗言：线程栈落盘（⑫5 哲学——不静默）。进程/窗口还活着，
            # 用户重开应用即自愈；栈在 crash.log 里等人查
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n===== 后端线程崩溃 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                f.write(traceback.format_exc())

    t = threading.Thread(target=_run_uvicorn, daemon=True)  # 主进程退，此线程跟着退
    t.start()
    # 等端口就绪（首次建库/FTS 对账可能要几十秒）
    t0 = time.time()
    while time.time() - t0 < 120:
        if port_in_use(BACKEND_PORT):
            return
        time.sleep(0.3)
    print("后端启动超时", file=sys.stderr)


def check_last_crash():
    """启动自检：上次崩溃遗言存在就打印摘要（开发模式控制台可见；
    崩溃本身已随重启自愈，这里只做"看得见"）。日志超 512KB 轮转防吃盘
    （rag_parse.log 无上限增长的教训——⑫6 同款防线提前落地）"""
    try:
        if os.path.exists(CRASH_LOG) and os.path.getsize(CRASH_LOG) > 0:
            print(f"[启动自检] 检测到上次崩溃记录（{CRASH_LOG}）——本次启动已自动恢复")
            if os.path.getsize(CRASH_LOG) > 512 * 1024:
                os.replace(CRASH_LOG, CRASH_LOG + ".old")  # 轮转：旧档改名留一份
    except OSError:
        pass  # 自检失败不阻塞启动


def main():
    check_last_crash()
    start_backend()
    # WebView2 原生窗口：无地址栏、任务栏独立图标
    webview.create_window(
        title='RAG 知识库助手',
        url=f'http://127.0.0.1:{BACKEND_PORT}',
        width=1280, height=800, min_size=(960, 600),
    )
    webview.start()  # 阻塞主线程直到窗口关闭
    sys.exit(0)      # 退出 = daemon 后端线程随之收干净


if __name__ == '__main__':
    main()
