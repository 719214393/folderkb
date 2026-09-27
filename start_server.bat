@echo off
rem RAG 后端启动器（2026-09-16）：先清 __pycache__ 再启动——
rem SMB 网络盘时间戳 2 秒粒度坑：.pyc 与 .py mtime 撞车时 Python 误判缓存有效，
rem 后端吃到旧字节码（当日实录：SQL 路由三验全失效）。清缓存是唯一可靠解。
cd /d "%~dp0"
if exist __pycache__ rmdir /s /q __pycache__
py -X utf8 server.py
