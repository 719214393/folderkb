# sandbox.py —— 空间隔离沙箱（2026-09-21 硬闸版铁律）
# 用法：py -X utf8 sandbox.py <任意命令或脚本路径>
# 机制：接管 open()/os.* 的路径校验——越界路径直接抛异常拒绝执行
# 铁律：唯一可访问目录 = 项目根（RAG_SANDBOX_ROOT 环境变量可覆盖，零写死）
#
# ⚠️ 诚实声明（边界如实）：
# 本沙箱是【Python 进程内】防御——拦 open/os/shutil/sqlite3.connect/
# pathlib/subprocess 六类。它不是操作系统级沙箱：
# ① 只在"py sandbox.py ..."方式启动时生效——直接 py -c/heredoc 不装载
# ② 会话工具（read/bash/grep 等宿主工具）不走本补丁
# ③ C 扩展/ ctypes 直调系统 API 不经过 Python 层
# 真正的红线保证 = 本沙箱（进程内）+ 我自身纪律（进程外）双层。
# 沙箱是保险丝，不是唯一的门。

import os, sys, builtins

import os as _os
ALLOWED_ROOT = _os.path.dirname(_os.path.abspath(__file__))  # 动态取根（2026-09-21 换IP事故教训：不硬编码）
_real_open = builtins.open

def _check(path):
    p = str(path)
    # 解析为绝对路径（相对路径基于 SMB 项目目录）
    ap = os.path.abspath(p)
    # UNC 路径归一化（正反斜杠）
    norm = ap.replace("/", "\\").lower()
    root = ALLOWED_ROOT.replace("/", "\\").lower()
    if not norm.startswith(root + "\\") and norm != root:
        raise PermissionError(
            f"[沙箱拦截] 越界访问被拒绝: {p}\n"
            f"唯一可访问范围: {ALLOWED_ROOT}")

def sandboxed_open(file, mode="r", *args, **kwargs):
    _check(file)  # 读写都拦（读外部文件同样违规）——B12：删 or True 恒真条件
    return _real_open(file, mode, *args, **kwargs)

builtins.open = sandboxed_open

# os 层拦截
for fn in ("remove", "unlink", "mkdir", "makedirs", "rmdir", "rename",
           "replace", "stat", "listdir", "scandir", "walk", "getsize",
           "copyfile", "move", "rmtree"):
    if hasattr(os, fn):
        _real = getattr(os, fn)
        def make_guard(real, name):
            def guarded(path, *args, **kwargs):
                _check(path)
                if name in ("rename", "replace") and len(args) >= 1:
                    _check(args[0])
                if name in ("copyfile", "move") and len(args) >= 1:
                    _check(args[0])
                return real(path, *args, **kwargs)
            return guarded
        setattr(os, fn, make_guard(_real, fn))

# shutil 层拦截
try:
    import shutil
    for fn in ("copy", "copy2", "copyfile", "move", "rmtree"):
        if hasattr(shutil, fn):
            _real = getattr(shutil, fn)
            def make_sguard(real, name):
                def guarded(src, *args, **kwargs):
                    _check(src)
                    if args: _check(args[0])
                    return real(src, *args, **kwargs)
                return guarded
            setattr(shutil, fn, make_sguard(_real, fn))
except ImportError:
    pass

# sqlite3.connect 拦截（红线最大向量：eval 直连 C 盘库）
try:
    import sqlite3 as _sq
    import server as _srv
    _real_connect = _sq.connect
    def guarded_connect(database, *args, **kwargs):
        db = str(database)
        # 评测器只读例外（2026-09-21 用户拍板）
        # 2026-09-25 「不准写死路径」：放行判定=真源前缀（零写死）
        if db.lower().startswith((str(_srv.DB_PATH.parent).lower(),)):
            # 只读例外（用户拍板）：immutable=1 —— 连 -shm 都不碰的真零写
            # （mode=ro 在 WAL 库上仍会写 -shm 触碰红线；immutable=1 完全不写）
            # 代价：读不到未 checkpoint 的 WAL —— 评测前须验证块数新鲜度
            uri = "file:" + db.replace("\\", "/") + "?mode=ro&immutable=1"
            return _real_connect(uri, *args, uri=True, **kwargs)
        _check(database)
        return _real_connect(database, *args, **kwargs)
    _sq.connect = guarded_connect
except ImportError:
    pass

# pathlib 拦截（monkey-patch WindowsPath/PosixPath 的读写方法——子类化会炸 _str）
try:
    import pathlib as _pl
    for _cls in (_pl.WindowsPath, _pl.PosixPath):
        for _m in ("read_text", "write_text", "read_bytes", "write_bytes", "open"):
            if hasattr(_cls, _m):
                _real_m = getattr(_cls, _m)
                def make_pguard(real, name):
                    def guarded(self, *args, **kwargs):
                        _check(str(self))
                        return real(self, *args, **kwargs)
                    return guarded
                setattr(_cls, _m, make_pguard(_real_m, _m))
except Exception:
    pass

# subprocess 拦截（防 Popen 跑外部命令绕过）
try:
    import subprocess as _sp
    _real_run = _sp.run
    _real_popen = _sp.Popen
    def guarded_run(cmd, *args, **kwargs):
        if isinstance(cmd, str) and any(k in cmd for k in ("C:", "C:\\", "/Users/", "/home/")):
            raise PermissionError(f"[沙箱拦截] subprocess 命令含越界路径: {cmd[:80]}")
        return _real_run(cmd, *args, **kwargs)
    def guarded_popen(cmd, *args, **kwargs):
        if isinstance(cmd, str) and any(k in cmd for k in ("C:", "C:\\", "/Users/", "/home/")):
            raise PermissionError(f"[沙箱拦截] subprocess 命令含越界路径: {cmd[:80]}")
        return _real_popen(cmd, *args, **kwargs)
    _sp.run = guarded_run
    _sp.Popen = guarded_popen
except Exception:
    pass

# 装载完毕——执行用户命令
if __name__ == "__main__":
    if len(sys.argv) > 1:
        # 支持: sandbox.py script.py [args...] 或 sandbox.py -c "code"
        if sys.argv[1] == "-c":
            exec(sys.argv[2])
        else:
            script = sys.argv[1]
            _check(script)
            sys.argv = sys.argv[1:]
            exec(_real_open(script, encoding="utf-8").read(), {"__name__": "__main__", "__file__": script})
    else:
        print("沙箱已装载。用法: py -X utf8 sandbox.py <脚本> 或 -c <代码>")
        print(f"唯一可访问: {ALLOWED_ROOT}")
