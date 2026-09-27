# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：RAG 知识库助手桌面版
# 用法：py -m PyInstaller rag_desktop.spec
# 产物：dist/RAG助手/ 目录版（launcher.exe + server.py + 前端 + 内嵌 Python 运行时）
import os

block_cipher = None

# launcher 同进程 import server（函数内动态 import），显式声明让分析器抓全；
# server.py 不再作为 data 打包——它被编译进 PYZ（import 方式）
hidden = [
    'server',
    'fastapi', 'fastapi.staticfiles', 'fastapi.responses', 'fastapi.middleware.cors',
    'uvicorn', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
    'numpy', 'jieba',
    'sqlite3', 'urllib.request', 'urllib.error', 'concurrent.futures',
    'string', 'json', 're', 'time', 'threading',
    # 2026-09-26 开源打包修复（advisor 三连）：
    'PIL', 'PIL.Image',                      # PDF 图片/截图表格 OCR 多尺度拍（server.py L2240 实用）
    'pdfplumber', 'pypdfium2', 'pypdf',      # PDF 解析栈
    'openpyxl', 'docx', 'pptx', 'lxml',      # Office 解析栈
    'rapidocr', 'onnxruntime',             # OCR（截图/图片表格——.onnx 模型走 datas）
    'pydantic', 'httpx',                     # fastapi 依赖链补全
    'contextmanager' if False else 'context_manager',   # server 顶层 import
    'agent_tools', 'sandbox', 'multilingual_tokenizer',
]

from PyInstaller.utils.hooks import collect_data_files
_extra_datas = []
# jieba 词典（分词引擎语言模型）+ rapidocr 内置 .onnx 模型——运行时加载的资源文件
for _pkg in ('jieba', 'rapidocr'):
    try:
        _extra_datas += collect_data_files(_pkg)
    except Exception:
        pass

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # server.py 编译进 PYZ（import 方式），但静态托管要按文件读 index.html/app.js
        # —— 它们必须以真实文件存在（server.py 的 FileResponse 读磁盘路径）
        ('index.html', '.'),
        ('app.js', '.'),
    ] + _extra_datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib'],  # PIL 移出排除（server.py OCR 实用）
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RAG助手',
    debug=False,
    strip=False,
    upx=False,
    console=False,  # 桌面程序不出黑色控制台
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='RAG助手',
)
