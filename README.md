# AI 对话助手 + RAG 知识库问答（源码开放 · 非商业许可）

> 本地部署的 RAG 知识库问答系统：扫描本地文件夹，分块、向量化，混合检索后把命中的资料块拼进提示词，模型开卷作答。
>
> **BYOK（Bring Your Own Key）**：代码里不内置任何 key，聊天 / 向量 / 重排三路都填你自己的 URL 与 key，密钥不经过第三方。

FastAPI + SQLite 混合检索（bge-m3 向量 + BM25 + RRF + Cross-Encoder 重排），叠加服务端 Agent 工具循环（Function Calling 五工具）与滑窗摘要上下文管理；产品侧含多会话 AI 聊天（SSE 流式 / 图片 / 文档附件），以及断路器、令牌桶限流、trace_id 全链路日志等韧性设计。

**实测成绩**（1546 题全量评测，库内表格占 60.6%）：检索召回 **90.36%** ｜ 端到端准确率 **94.37%**。
优化全过程见 [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md)。

## 快速开始（源码运行）

> 本文命令按 Windows 写。macOS / Linux 把 `py` 换成 `python3`、`python` 换成 `python3`，
> venv 内的解释器 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

```bash
# 1. 克隆
git clone https://github.com/719214393/folderkb.git && cd folderkb

# 2. 安装依赖（Python 版本分平台，见下「环境要求」对照表；内置 SQLite ≥ 3.34）
#    首选 uv——自动下载钉死的 Python 3.12（读项目根 .python-version），任何机器零准备：
uv venv && uv pip install -r requirements.txt
#    本机已装 3.12–3.14 也可直接：python -m pip install -r requirements.txt
#    （macOS / Linux 用 python3 -m pip；Intel Mac 仅 3.12，见下方警告）
#    ⚠ Intel Mac：本机若是 3.13/3.14，pip 会因 onnxruntime 无轮子直接装不上
#    （上游已停发 Intel-mac 轮子，无源码包可编译，换镜像救不了）——走上面的 uv 路线；
#    Windows / Apple Silicon（macOS 14+）不受限

# 3. 启动后端（终端 1，监听 8001）
#    系统 Python：Windows 用 py server.py ｜ macOS/Linux 用 python3 server.py
#    uv/venv 环境内：Windows 用 .venv\Scripts\python.exe server.py ｜ macOS/Linux 用 .venv/bin/python server.py
py server.py

# 4. 启动前端静态服务（终端 2，监听 8080）
#    macOS / Linux：python3 -m http.server 8080
py -m http.server 8080
```

浏览器打开 `http://localhost:8080` → 右上角「API 设置」填入你自己的 key（聊天模型 + 向量服务）→ 左侧扫描本地文件夹建库 → 开始问答。

## 桌面版打包（仅 Windows，可选）

```bash
pip install pyinstaller
# macOS / Linux：python3 -m pip install pyinstaller
py -m PyInstaller rag_desktop.spec
# 产物：dist/RAG助手/ 目录版（双击 RAG助手.exe 即用，WebView2 窗口）
```

- Win11 自带 WebView2；Win10 老机器若无，装 [Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
- 桌面版数据库落 `%APPDATA%\RAGAssistant\`，日志同目录

## 技术栈总览

| 层 | 技术 | 用在哪 |
|---|---|---|
| **后端** | Python 3.12–3.14（Intel Mac 仅 3.12）+ FastAPI + uvicorn + SQLite（FTS5 trigram） | 全部服务端化——检索/Agent 循环/会话持久化都在服务端 |
| **检索** | 混合检索：bge-m3 向量语义路 + BM25 关键词路 → **RRF 融合**（0.6:0.4）→ **Cross-Encoder 重排**（bge-reranker-v2-m3，格式感知） | api_search 单入口，Top100 池 → Top15 |
| **查询增强** | Multi-Query（3 变体）+ 同义词/反馈双模改写（Top1 分数分流）+ 追问补全（FOLLOWUP/NEW 三阶判定） | 检索召回主路，口语题/近义词盲区专项 |
| **Agent 层** | 服务端工具循环（Function Calling）：search_kb / sql_query / calc / list_kb_files / read_chunk 五只读工具，≤5 轮自主决策 + 墙钟 6 分钟 | SSE 流式端点 /api/agent/chat——多跳题模型自己补查 |
| **表格专项** | SQL 路由（结构信号 + LLM 兜底 → tables 元数据库直查）+ 表头翻译回填（LLM 预翻词典）+ 行组切块 | 复杂表格 87.6% 召回的支柱（业界 RAG 重灾区） |
| **防幻觉** | 双层：检索锚定提示词 + 拒答硬闸（"资料里没有就明说"）；Self-Consistency 投票（3 采样 0/0.7/0.7 多数决，计算题专项） | 诚实优先于硬答 |
| **上下文工程** | 滑窗摘要（最近 12 条原文 + 更早 600 字摘要——20 轮不丢语境）+ 工具结果分级截断（6000/3000/800）+ 预检索块数按轮数动态下调 | token 成本与语境保真的平衡 |
| **韧性** | 断路器（连续 5 败跳闸 10 分钟）+ 重试退避（429/503/超时）+ 令牌桶限流（自适应降速）+ rerank 降级为原始排序 | 三个故障域一套三件套（Hystrix/resilience4j 标准） |
| **可观测** | JSON 结构化日志 + trace_id 全链路穿透（contextvars）+ 明细档（一次提问一个 JSON：MQ 变体/双路/融合/重排全程） | 排障与评测归因的基础设施 |
| **文档解析** | pdfplumber/pypdfium2 + openpyxl/docx/pptx + RapidOCR（扫描件 OCR + 图片表格多尺度识别） | 10 种格式全接入 |
| **前端** | 原生 JS 零构建 + SSE 流式渲染；pywebview（WebView2）桌面壳 + PyInstaller 打包 | 三形态：源码/局域网/桌面 EXE |
| **评测** | 1546 题全量双口径（检索召回 + 端到端）+ LLM-as-judge 三取二 + grep 原文终审三层判定 | 检索口 90.36% / 端到端 94.37% 的出处 |

表格重灾库（60.6% 文件是 xlsx/xls/csv）上做到检索召回 90.36%、端到端 94.37%——不是纯文档库的虚高成绩。

## 项目结构

```
├── index.html                  页面（三栏布局：会话列表 / 聊天流 / 知识库面板）
├── app.js                      前端逻辑（聊天、流式渲染、知识库交互）
├── server.py                   后端服务（扫描/分块/向量化/检索/SSE 端点，FastAPI + SQLite）
├── agent_tools.py              服务端 Agent 执行器（@tool 注册表 + 五个只读工具）
├── context_manager.py          上下文管理三件套（滑窗摘要 / 工具结果分级截断 / 预检索块数动态下调）
├── multilingual_tokenizer.py   分词器语言分流（中文 jieba / 拉丁空格+短语 / 日韩 bigram）
├── sandbox.py                  空间隔离沙箱（接管 open()/os.* 路径校验，越界即抛异常）
├── launcher.py                 桌面版启动器（同进程：uvicorn 子线程 + 主线程 WebView2）
├── rag_desktop.spec            PyInstaller 打包配置（桌面版，仅 Windows）
├── start_server.bat            Windows 一键启动脚本
├── tools/                      运维件：com.rag.backend.plist（macOS launchd 看门狗）
├── requirements.txt            后端依赖（onnxruntime / pypdfium2 按平台标记双行钉版）
├── .python-version             钉 Python 3.12（uv 读它自动下载解释器）
└── README.md                   本文件
```


## 环境要求

- 内置 SQLite ≥ 3.34（FTS5 trigram 分词需要；Python 3.12 自带 3.49，没问题）

**Python 版本按平台**（拿到源码装不上依赖，先查这张表）：

| 平台 | 可用 Python | 说明 |
|---|---|---|
| Windows | 3.12 – 3.14 | 钉版依赖全有轮子，直接装 |
| Apple Silicon（macOS 14+） | 3.12 – 3.14 | 同上；onnxruntime 1.30.0 的 arm 轮子最低要求 macOS 14 |
| Intel Mac | **仅 3.12** | 本清单钉 onnxruntime 1.19.2（universal2 轮子止步 cp312，兼容 macOS 11+）；上游 1.23.2 是最后一个带 Intel 轮子的版本（且要 macOS 13），1.24 起彻底停发，无源码包可编译——3.13/3.14 换镜像也救不了。requirements.txt 已按平台标记自动钉 1.19.2 / pypdfium2 5.11.0，3.12 下零感知 |
| M 系列 + macOS 12/13 | ❌ 全功能暂未适配 | onnxruntime 1.30.0 的 arm 轮子最低要 macOS 14（pypdfium2 5.13.0 要 13），换 Python 版本救不了。**逃生口：改用降级清单 req314.txt**（剔 OCR 链 4 包，本地 OCR/表格识别没了、扫描 PDF 走云端视觉 API，纯文本文档不受影响） |

拿不准机器该用哪个？统一走 uv（见下）——自动落 Python 3.12；Windows 与 macOS 14+ 全功能零感知，老系统（Intel Mac 3.13/3.14、M 系列 + macOS 12/13）装不动全量清单时退降级清单 req314.txt。
- 没装 Python 或版本不符？装 [uv](https://docs.astral.sh/uv/) 后在项目根执行
  `uv venv && uv pip install -r requirements.txt`——uv 按 `.python-version` 自动下载
  Python 3.12，无需手动装解释器；server.py 启动时有版本守卫，区间外中文报错
- 任何现代浏览器（Chrome/Edge 均可）

## 安装依赖

后端依赖全部冻结在 `requirements.txt`（22 个包，含 rapidocr/rapid-table 的 OCR 栈；onnxruntime / pypdfium2 按平台双行钉版，macOS 12 Intel 自动落 1.19.2 / 5.11.0）：

```
python -m pip install -r requirements.txt
# macOS / Linux：python3 -m pip install -r requirements.txt
```

- jieba：中文分词（关键词检索路）
- numpy：向量余弦矩阵化计算（检索提速 60+ 倍，且支持多用户并发）

前端零依赖零构建：marked / DOMPurify 走 CDN，index.html 直接双击开不了
（要用静态服务，见下），app.js 原生 JS。

## 启动（两个服务，两个终端）

> macOS / Linux 把下文的 `py` 换成 `python3`。

**终端 1（后端）**：目录树扫描、SQLite 存储、embedding/检索 API，监听 8001

```
py server.py
```

**终端 2（前端静态服务）**：页面托管，监听 8080

```
py -m http.server 8080
```

然后浏览器打开 **http://localhost:8080**

> 为什么不能直接双击 index.html：file:// 协议下浏览器禁止 fetch 跨源请求，
> 前端调不到后端接口，必须经 HTTP 服务。

## API Key（BYOK：自带钥匙，三组按需填）

页面右上角「⚙ 设置」配置，存在浏览器 localStorage，发送请求时才带上。
一共涉及三个模型，各自可独立指向不同服务商（也可全用同一个账号）：

| # | 用途 | 地址 | Key | 模型名 | 默认 |
|---|---|---|---|---|---|
| ① | 聊天 / 拉模型列表 | 可改 | 必填 | 界面里选 | 硅基流动 |
| ② | 嵌入（RAG 检索） | 可改 | 仅开 RAG 时需要 | **可改** | `BAAI/bge-m3` |
| ③ | 重排（RAG 精排） | 可改 | 可不填 | **可改** | `BAAI/bge-reranker-v2-m3` |

三点说明：

- 默认全部指向硅基流动（https://siliconflow.cn，注册即送免费额度），两个检索模型
  都免费。也可以用任何 OpenAI 兼容服务。
- **嵌入和重排是两套独立凭证**。同一个账号就把 ③ 的「地址 / Key 同嵌入模型」
  勾上，只填模型名即可；不是同一个账号（比如嵌入用硅基、重排用 Cohere / Jina /
  Voyage）就把钩去掉，三样一起填。
- **模型名都能改**。换服务商必须改——重排各家叫法不同（Cohere 是
  `rerank-multilingual-v3.0`、Jina 是 `jina-reranker-v2-base-multilingual`），
  留默认值会 404。改完点对应分组的「测试连接」验证。

③ 不配任何东西时会自动沿用 ② 的地址和 Key（只换模型名），所以只用一个账号的
场景不用管它。代码里不内置任何 key，你自己的账号自己管。

## 使用流程（RAG 知识库问答）

1. 页面右上「选择文件夹」→ 选一个放 .txt/.md 文档的目录（不支持子目录里的
   node_modules/.git 等，会自动跳过）
2. 扫描完成自动分块（500 字/块，表格感知切分）
3. 开「RAG」开关 → 直接提问。首次会自动向量化（bge-m3 拍语义指纹，免费但
   分钟级），之后提问走混合检索：关键词路（FTS5 全文索引）+ 语义路（向量
   余弦）+ RRF 融合 + 重排（bge-reranker-v2-m3），命中的资料块拼进提示词，
   模型开卷作答并标注出处
4. 也可手动点「看切块」「向量化」「检索框回车」逐步观察各阶段产物

## 局域网访问（选配）

server.py 监听 0.0.0.0，同一 WiFi 下其他设备可用你的 IP 访问。查自己的 IP：

```
ipconfig   # 看 WLAN 适配器的 IPv4，比如 192.168.x.x
```

- 页面：http://192.168.x.x:8080（换成上一步查到的 IP；前端自动用 hostname 定位后端，无需改代码）
- 首次访问 Windows 防火墙可能弹窗，放行"专用网络"即可

注意：局域网内任何人都能调你的后端（含向量化 API key），仅限内网测试用。

## 数据存放

- `rag_index.db`：SQLite，首次运行自动创建（扫描历史/文件清单/块/向量/全文索引都在里面）
- 想清空重来：停掉 server.py 删掉该文件即可

## 韧性保护

三个故障域 × 三件套（超时/重试/断路器）全配齐：

| 故障域 | 超时 | 重试 | 断路器 | 熔断后行为 |
|---|---|---|---|---|
| chat（云端 deepseek） | 90s | 2 次退避 2s/4s | 连续 5 败跳闸 30 分钟 | LLM 功能静默降级（calc/MQ/路由退单查询） |
| embed（硅基 bge-m3） | 120s | 4 次退避 3/6/9s | 连续 5 败跳闸 10 分钟 | 明确报错"向量服务暂不可用，请稍后再试" |
| rerank（硅基 bge-reranker） | 30s | 2 次退避 3s/6s | 连续 5 败跳闸 10 分钟 | 降级为原始排序 + 前端显示"排序服务降级中"（哨兵异常每请求信号——并发零串扰） |

设计要点：断路器按（服务, key）隔离——多用户 BYOK 互不串扰；成功即复位；
熔断期内不再重试（用户秒得降级结果，不陪等超时）。chat 域另有 1.5s 节流
（同 key 调用强制间隔，防评测密度打爆限流）。

## 已知边界

- 文档格式支持 .txt / .md / .pdf / .docx / .doc / .xlsx / .xls / .csv /
  .pptx / .ppt（全格式接入，含扫描件 OCR 与表格还原）
- 检索质量：检索口 **90.36%**（D6 复测 1397/1546；另有一版 D5 基线冻结轮 89.97% = 1391/1546，两版口径见 [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md) 的「D-6 删链前双账终局」）；端到端 **94.37%**（r2 全量轮定稿 1459/1546，见 [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md) 的「端到端评测」）。端到端同库成绩序列：77.2 → 79.6 → 81.3 → 85.8（流水线基线）→ 90.23（Agentic 轮）→ 94.37（r2 全量轮）
- 云端 API 偶发 429 限流，重试即可
- deepseek-v4-flash 的思考输出（<think> 标签）前端已自动分拣进思考面板

## 长对话上下文管理

**问题**：长对话把全部历史塞给模型——撑爆上下文窗口、token 成本随轮数暴涨；此前前端还偷砍只发最近 10 条消息（6 轮以前直接丢，早期语境全没）。

**实现方式**（业界上下文工程标准三件套）：
- **滑窗+摘要**（LangChain `ConversationSummaryBufferMemory` 同款）：最近 6 轮（12 条消息）原文保留，更早的压成 300 字摘要放开头——早期语境经摘要传递，不丢关键实体/数字/结论；摘要失败自动退本地首尾拼接兜底
- **工具结果分级截断**：检索 6000 字/查表 3000/计算 800 上限——工具结果不无限膨胀
- **预检索块数递减**：对话越长开局自动带资料越少（15→12→10→8 块）——多跳题靠模型自主补查兜底

**实测**（20 轮真实对话）：第 1 轮埋假实体（蓝鲸项目/负责人张三/验收12月30日），中间 18 轮闲聊把第 1 轮推出原文窗口，第 20 轮追问——模型答出「张三」「12月30日」，语境经摘要成功传递；token 记账同步生效。

**顺手修复两 bug**：①检索缓存命中分支被无条件检索覆盖（缓存从未生效，重复问题没有秒回）②答题超 6 分钟墙钟强制停止时返回空白（现统一为超时也基于已有资料降级作答）。



## 更多文档

| 文档 | 内容 |
|---|---|
| [docs/检索流程图.md](docs/检索流程图.md) | Mermaid 图：一问到底主链 / 入库解析全链路 / 四条路径 / 块数速记卡 / 工具循环两种位置 |
| [docs/检索全链路说明.md](docs/检索全链路说明.md) | 逐段拆解：入库链路 / 查询路由 / SQL 路 / 混合检索 / 重排 / 改写 / 防幻觉 / 韧性 |
| [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md) | 优化全过程 61.5% → 90.36%：一轮到五轮 + 冲刺①② + 端到端评测建设 |

## 许可证

本项目采用 **PolyForm Noncommercial License 1.0.0**（全文见 [LICENSE](LICENSE)）：源码开放，可自由使用、修改、分发，**但仅限非商业目的**。

| 授权范围内 | 授权范围外 |
|---|---|
| 下载、阅读、本地部署自己使用 | 打包成产品出售，或提供收费服务 |
| 学习、研究、实验、业余爱好项目 | 在公司业务系统中商用（需另行取得授权） |
| 修改源码做二次开发 | 移除许可证文件或署名声明 |
| 分享给他人（附带 LICENSE 原文） | 以本项目为基础做商业化运营 |

教育、科研、公益、政府机构的使用同样在授权范围内。如需商业授权，可开 [Issue](https://github.com/719214393/folderkb/issues) 联系。

> 说明：带「禁止商用」条款的许可证不属于 OSI 认定的开源许可证，业界称 source-available。本项目面向个人本地使用场景，故采用此许可。
