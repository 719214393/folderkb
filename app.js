// ============================================================
// 大模型聊天前端：多会话 / 流式渲染 / 图片文档上传 / RAG 知识库问答
// BYOK 模式：用户自己的 API Key（设置弹窗配置，存 localStorage）
// 聊天 Key 必填；嵌入 Key 仅开 RAG 时必填；重排默认沿用嵌入配置（可单独配）
// 官方文档: https://docs.siliconflow.cn/
// ============================================================

// ---------- BYOK 配置（用户自己的 key，存 localStorage，发送时才带上） ----------
const DEFAULT_CHAT_URL = "https://api.siliconflow.cn/v1";
const DEFAULT_VEC_URL = "https://api.siliconflow.cn/v1";
const DEFAULT_EMBED_MODEL = "BAAI/bge-m3";
const DEFAULT_RERANK_URL = "https://api.siliconflow.cn/v1";
const DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3";
const SETTINGS_KEY = "rag_chat_settings";

// 读取配置：{chatUrl, chatKey, vecUrl, vecKey, sameKey, embedModel,
//           rerankUrl, rerankKey, rerankModel, sameRerankKey}。没配过返回空骨架
function getSettings() {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    return {
      chatUrl: s.chatUrl || DEFAULT_CHAT_URL,
      chatKey: s.chatKey || "",
      vecUrl: s.vecUrl || DEFAULT_VEC_URL,
      vecKey: s.vecKey || "",
      sameKey: !!s.sameKey,
      embedModel: s.embedModel || DEFAULT_EMBED_MODEL,
      rerankUrl: s.rerankUrl || DEFAULT_RERANK_URL,
      rerankKey: s.rerankKey || "",
      rerankModel: s.rerankModel || DEFAULT_RERANK_MODEL,
      // 老配置（升级前只有两把 Key）没有这个字段——默认 true，
      // 让重排沿用嵌入配置，行为与升级前完全一致
      sameRerankKey: s.sameRerankKey !== false,
      // ⑩2 两档开关（2026-09-22）：检索模式——agentic=模型自决（默认，⑩ 已上线行为）
      // pipeline=纯流水线（只自动注入预检索资料，不下发 search_kb 工具）
      ragMode: s.ragMode === "pipeline" ? "pipeline" : "agentic",
      // D-6.0a 对话引擎开关（2026-09-25）：sse=服务端新链（默认）| legacy=旧前端链（对照档）
      engine: s.engine === "legacy" ? "legacy" : "sse",
    };
  } catch {
    return {
      chatUrl: DEFAULT_CHAT_URL, chatKey: "", vecUrl: DEFAULT_VEC_URL, vecKey: "",
      sameKey: false, embedModel: DEFAULT_EMBED_MODEL,
      rerankUrl: DEFAULT_RERANK_URL, rerankKey: "", rerankModel: DEFAULT_RERANK_MODEL,
      sameRerankKey: true, ragMode: "agentic", engine: "sse",
    };
  }
}

function saveSettings(s) { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); }

// 取"生效配置"（三个模型各解各的）：
//   · 嵌入的地址/Key 在勾选 sameKey 时跟随聊天侧
//   · 重排勾了 sameRerankKey（或自己那栏没填）时跟随嵌入侧——升级前的老配置
//     只有一把向量 Key，这样零改动就能继续重排
function effectiveSearchSettings() {
  const s = getSettings();
  const vecUrl = s.sameKey ? s.chatUrl : s.vecUrl;
  const vecKey = s.sameKey ? s.chatKey : s.vecKey;
  const _rrSame = s.sameRerankKey || !s.rerankUrl || !s.rerankKey;
  return {
    vecUrl: vecUrl,
    vecKey: vecKey,
    embedModel: s.embedModel || DEFAULT_EMBED_MODEL,
    rerankUrl: _rrSame ? vecUrl : s.rerankUrl,
    rerankKey: _rrSame ? vecKey : s.rerankKey,
    rerankModel: s.rerankModel || DEFAULT_RERANK_MODEL,
  };
}

// API_URL / MODELS_URL 改为函数：聊天地址随配置走（旧代码里的常量引用全部换调函数）
function chatApiUrl() { return getSettings().chatUrl.replace(/\/+$/, "") + "/chat/completions"; }
function chatModelsUrl() { return getSettings().chatUrl.replace(/\/+$/, "") + "/models"; }
function chatApiKey() { return getSettings().chatKey; }

// 兼容旧引用（模型列表拉取等处直接用了常量名）——指向当前配置的 getter
const API_URL = { toString: () => chatApiUrl() };
const MODELS_URL = { toString: () => chatModelsUrl() };

// 坑1 修复：改写引擎的模型名自适应。写死 "deepseek-v4-flash" 在硅基流动直连时
// 400（全名 deepseek-ai/DeepSeek-V4-Flash）；公司代理不认全名只认短名——
// 按 URL 分派：siliconflow 用全名，其他（公司代理等）用短名。
// 选 flash 档：改写是低成本高频任务，用快模型，不占主聊天模型额度
function rewriteModel() {
  const url = (getSettings().chatUrl || "").toLowerCase();
  if (url.includes("siliconflow")) return "deepseek-ai/DeepSeek-V4-Flash";
  return "deepseek-v4-flash";
}

// 向后端发起需要向量服务的请求时统一带 BYOK 头（三个模型各带各的）
let feTraceId = "";  // ⑫5：本次提问的 trace_id（callChatWithTools 内赋值，vecHeaders 读取带 X-Request-Id）
function vecHeaders() {
  // ⑨ 五轮（2026-09-17 用户定谳）：search 请求同时捎带聊天配置（X-Chat-Key/X-Chat-Url）
  // ——后端 SQL 意图/路由兜底用公司代理（设置①），向量/排序用硅基流动（设置②）。
  // 与 scan/chunk 的 X-Chat-Key 管道同款（⑦2 架构），key 用完即弃不落盘
  // ⑫5（2026-09-18）：X-Request-Id 带 feTraceId——后端中间件复用它，一次提问
  // 全链（api/search/sql/chat）同一个 id，一条 grep 拉全程（全链路留痕承重墙）
  // 三模型改造：嵌入模型名（X-Embed-Model）与重排三件套（X-Rerank-*）独立下发——
  // 后端拿到就能各走各的服务商，缺省时它自己逐级回退
  const v = effectiveSearchSettings();
  const h = { "Content-Type": "application/json" };
  if (v.vecKey) h["X-Vec-Key"] = v.vecKey;
  if (v.vecUrl) h["X-Vec-Url"] = v.vecUrl;
  if (v.embedModel) h["X-Embed-Model"] = v.embedModel;
  if (v.rerankKey) h["X-Rerank-Key"] = v.rerankKey;
  if (v.rerankUrl) h["X-Rerank-Url"] = v.rerankUrl;
  if (v.rerankModel) h["X-Rerank-Model"] = v.rerankModel;
  const chatK = chatApiKey(), chatU = getSettings().chatUrl;
  if (chatK) h["X-Chat-Key"] = chatK;
  if (chatU) h["X-Chat-Url"] = chatU;
  if (typeof feTraceId !== "undefined") h["X-Request-Id"] = feTraceId;
  return h;
}

// 获取页面元素（新三栏布局）
const userInput = document.getElementById("userInput");
const sendBtn = document.getElementById("sendBtn");
const modelSelect = document.getElementById("modelSelect"); // combobox 的输入框
const modelList = document.getElementById("modelList");
const chatFlow = document.getElementById("chatFlow");
const chatInner = document.getElementById("chatInner");
const convList = document.getElementById("convList");
const topbar = document.getElementById("topbar");
const attachRow = document.getElementById("attachRow");
// 停止生成：当前请求的 AbortController。非 null = 正在流式输出，
// 点"停止"调它的 abort() 掐断 fetch（reader.read() 会立刻抛 AbortError）
let chatAbort = null;

  
const tools=[
  {
    type:"function",
    "function":{
      "name":"search_kb",
      // ⑩1 Agentic 检索工具（2026-09-22）：模型自主决定查什么/查几次。
      // 描述四要素（治理③同款）：多跳问题拆多查（先查A再查B合并答）；
      // 结果不够换关键词再查（最多3轮）；简单问题/寒暄不查。
      "description":"检索公司知识库（文档/表格/财务数据）。功能：按关键词搜知识库，返回最相关的文档片段。何时用：需要查具体数据、文档内容、表格数值、编号/名称对应关系时。多跳问题（问两件事）拆成多次检索分别查。检索结果不够时换关键词重查（最多3次）。何时禁用：闲聊/常识问题/无需查资料时。参数：query 必填，检索关键词（用文档里可能出现的原文表述，不要口语化）。",
      "parameters":{
        "type":"object",
        "properties":{
          "query":{"type":"string","description":"检索关键词——用文档原文表述（含表格名/编号/列名等文档里实际出现的词，而不是口语说法）"}
        },
        "required":["query"]
      }
    }
  },
  {
    type:"function",
    "function":{
      "name":"getTemperature",
      // 工具调用治理③（2026-09-14）：描述四要素（功能/何时用/何时禁用/
      // 参数）——Anthropic 官方建议写"何时不该用"，只写"何时用"模型对
      // 边界的认知就靠猜；负例列举具体场景（查数据/文档问答）比抽象
      // 规则有效，模型对具体例子的泛化更好
      "description":"查询指定经纬度的当前气温。功能：返回某坐标点的实时温度。何时用：用户明确询问天气/温度/气温时。何时禁用：问题与天气无关时（查资料数据、文档问答、闲聊）；用户未提供位置且无法从上下文推断时。参数：latitude/longitude 必填，十进制度数。",
      "parameters":{
        "type":"object",
        "properties":{
          "latitude":{"type":"number"},
          "longitude":{"type":"number"}
        },
        "required":["latitude","longitude"]
      }
    }
  }
]


async function getTemperature(lat, lon) {
  // 工具调用治理④（2026-09-14）：执行前校验。旧签名默认值 (lat=39.9,
  // lon=116.4) 写死北京——模型无参调用会"成功"返回北京天气，误调变
  // 静默错误答案（用户看到 23.8°C 还以为答对了）。现在：无参/非数字/
  // 出界一律拒绝，拒绝原因作为"观察结果"回给模型（ReAct 的正确用法：
  // 拒绝也是一次合法观察），模型看到会自己转文字回答，对话不中断
  if (typeof lat !== "number" || typeof lon !== "number" ||
      Number.isNaN(lat) || Number.isNaN(lon) ||
      lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    return "工具调用被拒绝：参数无效（缺少经纬度或超出值域）。本工具仅用于天气查询，与当前问题无关时请直接基于已有信息回答。";
  }
  const url = new URL('https://api.open-meteo.com/v1/forecast');
  url.searchParams.append('latitude', lat);
  url.searchParams.append('longitude', lon);
  url.searchParams.append('current', 'temperature_2m');

  try {
      const response = await fetch(url);
      if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
      }
      const data = await response.json();
      const temperature = data.current?.temperature_2m;
      console.log(`当前温度: ${temperature}°C`);
      return temperature;
  } catch (error) {
      console.error('获取天气数据失败:', error);
      return null;
  }
}


// ============================================================
// 知识库：后端目录浏览器选文件夹 → 后端扫描。
// 浏览器安全模型禁止网页拿绝对路径，后端直接跑在系统上能列盘符拿路径
// （和 OpenCode 等本地程序同思路），所以选择器 = 后端 /api/browse
// 提供目录数据 + 前端模态框渲染。
// ============================================================
// 后端地址：谁打开页面，后端地址就用"谁看到的主机名"——本机开是 localhost，
// 局域网同事开就是 http://<主机内网IP>:8080（否则他页面里的 localhost 指向他自己电脑）
const BACKEND_URL = "http://" + window.location.hostname + ":8001";

// ---------- 知识库共享状态 ----------
// knowledgeFiles：当前知识库的文件清单 [{path, size}]（rag.js 删除后归属这里）
let knowledgeFiles = [];

const pickFolderBtn = document.getElementById("pickFolderBtn");
const kbStatus = document.getElementById("kbStatus");
const kbPathInput = document.getElementById("kbPathInput");
const browseFolderBtn = document.getElementById("browseFolderBtn");
const folderModal = document.getElementById("folderModal");
const folderModalClose = document.getElementById("folderModalClose");
const folderCrumbs = document.getElementById("folderCrumbs");
const folderList = document.getElementById("folderList");
const folderCurrentLabel = document.getElementById("folderCurrentLabel");
const folderConfirmBtn = document.getElementById("folderConfirmBtn");
// 切块预览：模态框元素
const viewChunksBtn = document.getElementById("viewChunksBtn");
const chunkModal = document.getElementById("chunkModal");
const chunkModalClose = document.getElementById("chunkModalClose");
const chunkModalTitle = document.getElementById("chunkModalTitle");
const chunkList = document.getElementById("chunkList");
const embedBtn = document.getElementById("embedBtn");
const embedStatusBar = document.getElementById("embedStatusBar");
const searchInput = document.getElementById("searchInput");
const ragToggle = document.getElementById("ragToggle");
// RAG 开关持久化（2026-09-23 用户实测痛点：刷新丢勾选状态）
try { if (localStorage.getItem("rag_toggle_state") === "1") ragToggle.checked = true; } catch {}
ragToggle.addEventListener("change", () => {
  try { localStorage.setItem("rag_toggle_state", ragToggle.checked ? "1" : "0"); } catch {}
});
// currentScanId：当前知识库是哪次扫描——查块/向量化/检索的唯一钥匙
let currentScanId = null;

// ---------- 目录浏览器模态框 ----------
// browsePath：模态框当前所在目录（"" = 盘符列表层）
// confirmTarget：当前"选定"的目标目录（选中态），确认时扫它
let browsePath = "";

browseFolderBtn.addEventListener("click", () => {
  folderModal.classList.add("open");
  // 路径框里有值就从那进（上次扫描的根），没有就从盘符层开始
  const initial = (kbPathInput.value || "").trim();
  loadBrowse(initial && /[/\\]/.test(initial) ? initial : "");
});

// 关闭：× 按钮 / 点遮罩空白处
folderModalClose.addEventListener("click", () => folderModal.classList.remove("open"));
folderModal.addEventListener("click", (e) => {
  if (e.target === folderModal) folderModal.classList.remove("open");
});

// 拉取目录数据并渲染：path 为空 → 盘符列表；否则该目录的子目录列表
async function loadBrowse(path) {
  browsePath = path;
  folderList.innerHTML = "<div class='dir-item' style='color:#9ca3af'>加载中...</div>";
  try {
    const url = path ? BACKEND_URL + "/api/browse?path=" + encodeURIComponent(path) : BACKEND_URL + "/api/browse";
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) {
      renderBrowseError(data.error);
      return;
    }
    renderBrowse(data);
  } catch (err) {
    renderBrowseError("连不上后端: " + err.message);
  }
}

// 取父目录（跨平台）：POSIX 保留开头 /，Windows 保留盘符
// /Users/a/b → /Users/a；/Users → /；/ → ""（回根层）；E:/a/b → E:/a；E:/ → ""（回盘符层）
function parentDir(p) {
  const cur = String(p || "").replace(/[\\/]+$/, "");
  if (!cur) return "";
  if (cur.startsWith("/")) {
    if (cur === "/") return "";
    const idx = cur.lastIndexOf("/");
    return cur.slice(0, idx) || "/";
  }
  const parts = cur.split(/[\\/]/).filter(Boolean);
  parts.pop();
  return parts.length ? parts.join("/").replace(/^([A-Za-z]):?$/, "$1:/") : "";
}

// 报错时把面包屑/底部路径一起清空，避免屏幕上残留上一轮的旧内容
// 同时留一个"返回顶层"的出口，否则报错后只能关弹窗重开
function renderBrowseError(msg) {
  folderCrumbs.innerHTML = "";
  const back = document.createElement("span");
  back.textContent = "← 返回顶层";
  back.addEventListener("click", () => loadBrowse(""));
  folderCrumbs.appendChild(back);
  folderList.innerHTML = "";
  const box = document.createElement("div");
  box.className = "dir-item";
  box.style.color = "#dc2626";
  box.textContent = msg;
  folderList.appendChild(box);
  folderCurrentLabel.textContent = "（未选择）";
  folderConfirmBtn.dataset.path = "";
}

// 顶层是盘符列表（Windows）还是真实根目录（POSIX）——按后端给的根层 entries 判断
let posixRoots = null;

// 渲染目录列表 + 面包屑
function renderBrowse(data) {
  if (!data.current && data.entries.length) posixRoots = String(data.entries[0].path).startsWith("/");
  const driveLayer = posixRoots === false; // Windows：根层是"此电脑"的盘符列表
  // 面包屑："←返回上级" + 当前路径（顶层只显示"顶层"/"此电脑"）
  folderCrumbs.innerHTML = "";
  if (data.current) {
    const back = document.createElement("span");
    back.textContent = "← 返回上级";
    back.style.marginRight = "12px";
    // 返回上级：取当前路径的父目录，跨平台
    back.addEventListener("click", () => loadBrowse(parentDir(data.current)));
    folderCrumbs.appendChild(back);
    const label = document.createElement("span");
    label.textContent = data.current;
    label.style.color = "#6b7280";
    folderCrumbs.appendChild(label);
  } else {
    const label = document.createElement("span");
    label.textContent = driveLayer ? "此电脑（选择盘符）" : "顶层（选择位置）";
    label.style.color = "#6b7280";
    folderCrumbs.appendChild(label);
  }

  // 目录列表
  folderList.innerHTML = "";
  if (data.entries.length === 0) {
    const empty = document.createElement("div");
    empty.className = "dir-item";
    empty.style.color = "#9ca3af";
    empty.textContent = "（空目录）";
    folderList.appendChild(empty);
  }
  for (const entry of data.entries) {
    const item = document.createElement("div");
    item.className = "dir-item" + (entry.hidden ? " hidden-dir" : "");
    item.innerHTML = "<span class='icon'>📁</span><span></span>";
    item.children[1].textContent = entry.name; // textContent 赋值防 XSS
    item.addEventListener("click", () => loadBrowse(entry.path));
    folderList.appendChild(item);
  }

  // 进到哪就默认选哪
  folderCurrentLabel.textContent = data.current || (driveLayer ? "（未选择盘符）" : "（未选择位置）");
  folderConfirmBtn.dataset.path = data.current;
}

// "选定此文件夹"：确认 → 关模态框 → 用后端持有的绝对路径直接扫描
folderConfirmBtn.addEventListener("click", () => {
  const target = folderConfirmBtn.dataset.path;
  folderModal.classList.remove("open");
  if (!target) return; // 还在盘符层没进任何目录
  kbPathInput.value = target;
  scanKnowledgeBase({ path: target });
});

// 共享扫描函数：body 为 {path}（路径模式）或 {name}（选文件夹模式）
async function scanKnowledgeBase(body) {
  try {
    kbStatus.classList.remove("ready");
    kbStatus.textContent = "后端扫描中...";

    // POST /api/scan：后端递归扫描，重活后端干，前端管交互
    const resp = await fetch(BACKEND_URL + "/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Chat-Key": chatApiKey(), "X-Chat-Url": getSettings().chatUrl },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    if (data.error) {
      kbStatus.textContent = "扫描失败: " + data.error;
      return;
    }

    knowledgeFiles = data.files.map((f) => ({ path: f.path, size: f.size }));
    kbPathInput.value = data.root; // 真实根路径回填路径框
    currentScanId = data.scan_id;  // "看切块"按钮靠它拉块
    // 扫描秒回但分块在后台跑（大语料几十秒）——像"向量化"一样轮询显示进度，
    // 不然用户以为卡死。显示格式对齐向量化：切块中 X%
    const pollChunk = setInterval(async () => {
      try {
        const s = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/chunk/status");
        const st = await s.json();
        if (st.status === "running") {
          const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
          kbStatus.textContent = `切块中 ${pct}%（${st.done}/${st.total}）${st.current_fmt ? "· 正在处理 " + st.current_fmt.toUpperCase() + " 文件" : ""}...`;
        } else if (st.status === "done") {
          clearInterval(pollChunk);
          kbStatus.textContent = `已索引「${data.root}」: ${data.count} 个文本文件（${formatDuration(data.elapsed_ms)}，${data.total_size}，切块 ${st.chunk_count} 块）`;
          kbStatus.classList.add("ready");
        } else if (st.status === "error") {
          clearInterval(pollChunk);
          kbStatus.textContent = "切块失败: " + (st.error || "未知");
        }
      } catch { /* 轮询失败下轮再试 */ }
    }, 1000);
    console.log("知识库文件清单:", knowledgeFiles.map((f) => f.path));
  } catch (err) {
    // 多半是后端没启动（Connection refused）
    kbStatus.textContent = "连不上后端: " + err.message + "（确认 server.py 已启动）";
  }
}

// ============================================================
// 持久化恢复：页面加载时从后端库拉上次的知识库（只查 SQLite，不重新扫盘）
// ============================================================
async function restoreKnowledgeBase() {
  try {
    const resp = await fetch(BACKEND_URL + "/api/kb");
    const data = await resp.json();
    const scans = data.scans || [];
    if (scans.length === 0) return; // 没扫过：保持"未选择知识库"

    // 最新一条（后端按 id 倒序）
    const latest = scans[0];
    kbStatus.textContent = `已从库恢复「${latest.root}」: ${latest.count} 个文本文件（上次扫描 ${formatDuration(latest.elapsed_ms)}）`;
    kbStatus.classList.add("ready");
    kbPathInput.value = latest.root;
    currentScanId = latest.id;

    // 文件清单单独拉（列表接口不带文件，省流量）
    const filesResp = await fetch(BACKEND_URL + "/api/kb/" + latest.id + "/files");
    const filesData = await filesResp.json();
    knowledgeFiles = filesData.files.map((f) => ({ path: f.path, size: f.size }));
    console.log(`知识库已恢复（scan #${latest.id}）:`, knowledgeFiles.length, "个文件");
  } catch (err) {
    console.log("无历史知识库可恢复:", err.message); // 后端没起也不阻塞页面
  }
}
restoreKnowledgeBase();

// ============================================================
// 切块预览：点"看切块" → 拉后端 /api/kb/{scan_id}/chunks → 弹窗渲染。
// 亲眼看到块边界切在哪，是判断分块质量、调 CHUNK_SIZE 参数的依据
// ============================================================
chunkModalClose.addEventListener("click", () => chunkModal.classList.remove("open"));
chunkModal.addEventListener("click", (e) => {
  if (e.target === chunkModal) chunkModal.classList.remove("open");
});

viewChunksBtn.addEventListener("click", viewChunks);

async function viewChunks(failedFiles) {
  if (currentScanId == null) {
    showError("还没有扫描记录——先选文件夹扫描，或确认 server.py 已启动");
    return;
  }
  try {
    const resp = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/chunks?limit=1000");
    const data = await resp.json();
    renderChunks(data, failedFiles);
    // 顺手刷一下向量状态条（向量化没开始就隐藏，开始了就显示进度）
    fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/embed/status")
      .then((r) => r.json())
      .then(updateEmbedBar)
      .catch(() => {});
    chunkModal.classList.add("open");
  } catch (err) {
    showError("拉取切块失败: " + err.message + "（确认 server.py 已启动）");
  }
}

// 把块清单画进模态框：每块一张卡片（文件名/序号/字数 + 正文预览）。
// 正文用 textContent 写——块内容是硬盘上的任意文本，防 XSS
function renderChunks(data, failedFiles) {
  const total = data.total || 0;
  chunkModalTitle.textContent = "切块预览（#" + currentScanId + " · 共 " + total + " 块）";
  chunkList.innerHTML = "";
  if (!data.chunks || data.chunks.length === 0) {
    // 空态：还没分过块——给"开始分块"按钮就地干活
    const empty = document.createElement("div");
    empty.className = "chunk-empty";
    const msg = document.createElement("div");
    msg.className = "chunk-progress";
    msg.textContent = "这个扫描还没分块——切好块才能做向量和检索";
    const startBtn = document.createElement("button");
    startBtn.className = "chunk-start-btn";
    startBtn.textContent = "开始分块";
    startBtn.addEventListener("click", startChunking);
    empty.appendChild(msg);
    empty.appendChild(startBtn);
    chunkList.appendChild(empty);
    return;
  }
  // ⑦7 格式分布条：知识库由什么格式组成一眼可见（PDF×12 · DOCX×3 ...）
  // ——从文件清单统计（knowledgeFiles 是当前库的权威清单）
  const fmtCount = {};
  for (const f of knowledgeFiles) {
    const ext = (f.path.includes(".") ? f.path.split(".").pop() : "").toLowerCase();
    fmtCount[ext] = (fmtCount[ext] || 0) + 1;
  }
  const fmtBar = document.createElement("div");
  fmtBar.className = "fmt-bar";
  fmtBar.textContent = "格式构成: " + Object.entries(fmtCount)
    .sort((a, b) => b[1] - a[1])
    .map(([ext, n]) => (ext ? ext.toUpperCase() : "无扩展名") + "×" + n)
    .join(" · ");
  chunkList.appendChild(fmtBar);
  // ⑦7 失败明因区：哪些文件没进知识库+为什么（坑2 病根：静默跳过无感知）。
  // failedFiles 由分块轮询 done 时从账本带出（手动打开弹窗没轮询过程=没有
  // 该参数，不展示——历史失败可在 rag_parse.log 按文件名查）
  if (failedFiles && failedFiles.length > 0) {
    const failBar = document.createElement("div");
    failBar.className = "fail-bar";
    const title = document.createElement("div");
    title.textContent = "⚠ " + failedFiles.length + " 个文件解析失败（未进知识库）:";
    title.style.fontWeight = "600";
    title.style.marginBottom = "4px";
    failBar.appendChild(title);
    for (const ff of failedFiles.slice(0, 20)) {
      const line = document.createElement("div");
      line.textContent = "· " + ff.file + " — " + ff.reason;
      failBar.appendChild(line);
    }
    if (failedFiles.length > 20) {
      const more = document.createElement("div");
      more.textContent = "… 共 " + failedFiles.length + " 个（完整清单见 rag_parse.log）";
      failBar.appendChild(more);
    }
    chunkList.appendChild(failBar);
  }
  for (const c of data.chunks) {
    const item = document.createElement("div");
    item.className = "chunk-item";
    const head = document.createElement("div");
    head.className = "chunk-item-head";
    // ⑦7 格式徽章：后端 chunks 接口新带 ext 字段（大写），徽章样式走
    // index.html 的 .fmt-badge（无扩展名/纯文本不显示徽章，减噪）
    if (c.ext && c.ext !== "TXT" && c.ext !== "MD") {
      const badge = document.createElement("span");
      badge.className = "fmt-badge";
      badge.textContent = c.ext;
      head.appendChild(badge);
    }
    const file = document.createElement("span");
    file.className = "chunk-item-file";
    file.textContent = c.file;
    const meta = document.createElement("span");
    meta.className = "chunk-item-meta";
    meta.textContent = "#" + c.seq + " · " + c.len + " 字";
    head.appendChild(file);
    head.appendChild(meta);
    // preview 是后端截的前 200 字，len 超了补省略号提示被截
    const text = document.createElement("div");
    text.className = "chunk-item-text";
    text.textContent = c.preview + (c.len > 200 ? " …" : "");
    item.appendChild(head);
    item.appendChild(text);
    chunkList.appendChild(item);
  }
}

// 空态里"开始分块"按钮：POST 启动后台分块（秒回）→ 每秒轮询进度 → 干完自动刷新
async function startChunking() {
  const btn = document.querySelector(".chunk-start-btn");
  if (btn) { btn.disabled = true; btn.textContent = "分块中…"; }
  try {
    // 接口秒回（后台线程慢慢干），所以接下来要轮询问进度
    const resp = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/chunk", { method: "POST", headers: { "X-Chat-Key": chatApiKey(), "X-Chat-Url": getSettings().chatUrl } });
    const started = await resp.json();
    if (started.error) {
      showError(started.error);
      if (btn) { btn.disabled = false; btn.textContent = "开始分块"; }
      return;
    }
    const poll = setInterval(async () => {
      try {
        const sResp = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/chunk/status");
        const s = await sResp.json();
        if (s.status === "running") {
          const msg = document.querySelector(".chunk-progress");
          if (msg) msg.textContent = "分块中… " + (s.done ?? 0) + " / " + (s.total ?? "?") + " 个文件" + (s.current_fmt ? "（正在处理 " + s.current_fmt.toUpperCase() + "）" : "");
        } else if (s.status === "done") {
          viewChunks(s.failed_files); // 停表重拉块清单——弹窗原地变成卡片列表
          // ⑦7 failed_files：后端账本带出的失败清单（[{file, reason}]）
        } else if (s.status === "error") {
          clearInterval(poll);
          showError("分块失败: " + (s.error || "未知错误"));
        }
      } catch { /* 单次轮询失败（后端瞬时忙）忽略，下一秒再问 */ }
    }, 1000);
  } catch (err) {
    showError("启动分块失败: " + err.message);
    if (btn) { btn.disabled = false; btn.textContent = "开始分块"; }
  }
}

// ============================================================
// 向量化：给每块算 embedding 指纹存库——检索靠指纹比距离，没指纹没法比
// ============================================================
embedBtn.addEventListener("click", startEmbedding);

async function startEmbedding() {
  if (currentScanId == null) {
    showError("还没有扫描记录——先扫描或恢复知识库");
    return;
  }
  embedBtn.disabled = true;
  embedBtn.textContent = "向量化中…";
  try {
    const resp = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/embed", { method: "POST", headers: vecHeaders() });
    const started = await resp.json();
    if (started.error) {
      showError(started.error);
      resetEmbedBtn();
      return;
    }
    // 每 2 秒轮询（打 API 慢，问太快没意义）
    const poll = setInterval(async () => {
      try {
        const sResp = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/embed/status");
        const s = await sResp.json();
        const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
        embedBtn.textContent = s.status === "running" ? "向量化中 " + pct + "%" : "向量化中…";
        // 切块弹窗开着时同步进度条（一块屏幕看全）
        if (chunkModal.classList.contains("open")) updateEmbedBar(s);
        if (s.status === "done" || s.status === "partial_done") {
          // partial_done（2026-09-14 前端卡死事故）：后端三终态 done/error/
          // partial_done，轮询原只认前两个——超时重试改造后 partial_done 成
          // 常态，按钮永久卡「向量化中」无法再点。partial_done 也算到站：
          // 提示缺块数，按钮恢复，用户可再点续传补齐
          clearInterval(poll);
          if (s.status === "partial_done") {
            embedBtn.textContent = "缺" + (s.total - s.done) + "块·再点补齐";
            setTimeout(resetEmbedBtn, 4000);
          } else {
            embedBtn.textContent = "已向量化 ✓";
            setTimeout(resetEmbedBtn, 3000); // 3 秒后恢复原样
          }
        } else if (s.status === "error") {
          clearInterval(poll);
          showError("向量化失败: " + (s.error || "未知错误"));
          resetEmbedBtn();
        }
      } catch { /* 单次轮询失败忽略，下轮再问 */ }
    }, 2000);
  } catch (err) {
    showError("启动向量化失败: " + err.message);
    resetEmbedBtn();
  }
}

function resetEmbedBtn() {
  embedBtn.disabled = false;
  embedBtn.textContent = "向量化";
}


// ============================================================
// 检索前自动补课：新用户没点过"向量化"直接提问——这里在提问入口处
// 自动发现缺指纹就启动向量化、轮询到就绪再放行检索。用户全程只看到
// "首次使用：正在生成指纹…"提示，不需要知道"切块""向量化"这些概念
// ============================================================
async function ensureEmbedded() {
  const resp = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/embed/status");
  const st = await resp.json();
  if (st.status === "done" || st.status === "partial_done") return true;  // 已齐放行
  // ⑬ 入库即用（2026-09-20 开工·advisory 定案）：分块完成即放行——
  // 不等向量！关键词路（FTS 分块完即有索引）先行检索，
  // 语义路等向量后台补齐自动生效（后端 keyword-only 降级+前端提示已接）
  // 2026-09-14 的"等它完成"改为"放行+提示"——用户 0 等待
  if (st.status === "running") {
    addProcessStep("tool-result", "📦 向量生成中（" + (st.done||0) + "/" + (st.total||"?") + "）——本次用关键词模式检索，语义检索稍后自动生效");
    return true;  // ⑬ 放行（不等）
  }
  if (st.status === "not_chunked") {
    // 刚扫完几秒内分块还在自动跑：提示稍等，不硬等（本次按普通聊天回答）
    addProcessStep("tool-result", "知识库正在准备（分块中），几秒后再试");
    return false;
  }
  // not_started：⑬ 入库即用（2026-09-20）——自动触发向量化（后台补齐）
  // 但【不等】：关键词模式先答，语义路向量好了自动生效
  if (st.status !== "running") {
    addProcessStep("request", "首次使用：后台自动生成语义指纹（大库约 2 分钟）——本次先用关键词模式检索...");
    const started = await fetch(BACKEND_URL + "/api/kb/" + currentScanId + "/embed", { method: "POST", headers: vecHeaders() });
    const sd = await started.json();
    if (sd.error) {
      addProcessStep("tool-result", "自动向量化失败: " + sd.error);
      return false;  // 失败不硬扛：按普通聊天回答，用户还能手动点按钮重试
    }
    return true;  // ⑬ 放行（触发后不等——关键词先行）
  }
  // partial_done 当作"可用"放行——大部分指纹已在，检索质量损失极小，
  // 不为最后 1% 卡用户 2 分钟（重跑会断点续传）
  // （⑬ 2026-09-20：原 2 秒轮询等待循环已删——所有状态分块完成即放行，不等向量）
}

// 切块弹窗顶部的向量状态条：已指纹 N/M 块 · 百分比
function updateEmbedBar(s) {
  if (!s.total) {
    embedStatusBar.style.display = "none";
    return;
  }
  const pct = Math.round((s.done / s.total) * 100);
  embedStatusBar.style.display = "flex";
  embedStatusBar.innerHTML = "";
  const label = document.createElement("span");
  label.textContent = "语义指纹 " + s.done + " / " + s.total + " 块" +
    (s.status === "done" ? " ✓" : s.status === "running" ? "" : "（未开始）");
  const pctEl = document.createElement("span");
  pctEl.className = "pct";
  pctEl.textContent = pct + "%";
  embedStatusBar.appendChild(label);
  embedStatusBar.appendChild(pctEl);
}

// ============================================================
// 检索：问题 → 后端比指纹距离 → 最相关的 K 块（带文件名+分数）。
// RAG 的"R"：聊天前先找出最像的几块，⑤ 再拼进提示词
// ============================================================
// 回车触发检索（IME 输入法回车选字不算）
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) searchKnowledge();
});

async function searchKnowledge() {
  const q = searchInput.value.trim();
  if (!q) return; // 空问题不打 API（免费配额也是钱）
  if (currentScanId == null) {
    showError("还没有扫描记录——先扫描或恢复知识库");
    return;
  }
  chunkModalTitle.textContent = "检索中…（问题拍指纹 + 比对 1 万+ 指纹）";
  try {
    // encodeURIComponent：中文/空格转 URL 安全编码
    const resp = await fetch(
      BACKEND_URL + "/api/kb/" + currentScanId + "/search?q=" + encodeURIComponent(q) + "&k=15&pool=" + (getSettings().searchTier || "100"),
      { headers: vecHeaders() }
    );
    const data = await resp.json();
    if (data.error) {
      showError(data.error);
      return;
    }
    renderHits(data, q);
  } catch (err) {
    showError("检索失败: " + err.message + "（确认 server.py 已启动）");
  }
}

// 渲染检索结果：命中块复用切块卡片样式 + 绿色分数角标（相似度 0~1，越大越像）
function renderHits(data, q) {
  chunkModalTitle.textContent = "「" + q + "」→ " + data.hits.length + " 块命中（从 " + data.total_chunks + " 块中挑出）";
  chunkList.innerHTML = "";
  if (data.hits.length === 0) {
    const empty = document.createElement("div");
    empty.className = "chunk-empty";
    empty.textContent = "没有命中（知识库还没向量化？）";
    chunkList.appendChild(empty);
    return;
  }
  for (const h of data.hits) {
    const item = document.createElement("div");
    item.className = "chunk-item";
    const head = document.createElement("div");
    head.className = "chunk-item-head";
    const file = document.createElement("span");
    file.className = "chunk-item-file";
    file.textContent = h.file;
    const meta = document.createElement("span");
    meta.className = "chunk-item-meta";
    meta.textContent = "#" + h.seq + " · " + h.text.length + " 字";
    const score = document.createElement("span");
    score.className = "hit-score";
    score.textContent = h.score.toFixed(4);
    head.appendChild(file);
    head.appendChild(meta);
    head.appendChild(score);
    // 命中块给全文（检索结果就是要看内容，不再截 200 字）
    const text = document.createElement("div");
    text.className = "chunk-item-text";
    text.textContent = h.text;
    item.appendChild(head);
    item.appendChild(text);
    chunkList.appendChild(item);
  }
}

// 毫秒 → 人类可读：<1s 显示毫秒；否则一位小数秒（1234 → "1.2s"）
function formatDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return ms + "ms";
  return (ms / 1000).toFixed(1) + "s";
}

// 字节 → 人类可读：B/KB/MB/GB 自适应（4567890 → "4.4MB"）
function formatSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return bytes + "B";
  const units = ["KB", "MB", "GB"];
  let v = bytes;
  let i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return v.toFixed(1) + units[i];
}

// 点击"扫描"按钮 → 路径框里的内容当完整路径用
pickFolderBtn.addEventListener("click", () => {
  const path = (kbPathInput.value || "").trim();
  if (!path) {
    kbStatus.textContent = "请先点「选择文件夹」选目录，或在路径框直接输入绝对路径";
    kbPathInput.focus();
    return;
  }
  scanKnowledgeBase({ path });
});
// 按钮两态：空闲=发送；生成中=停止（点它掐断当前请求，见 chatAbort）
sendBtn.addEventListener("click", () => {
  // D-6.0a 双语义（advisor 实锤：旧链升为开关首选路径后停止按钮必须分档）：
  // SSE 档 → 停当前会话的流（注册表 abort）；旧链档 → 停全局 chatAbort（旧链
  // 不写注册表，靠 chatAbort 生命周期）。发送语义两档共用。
  const _reg = curConvId ? _STREAMING[curConvId] : null;
  if (_reg && _reg.abort) {          // SSE 档：当前会话有活流
    _reg.abort.abort();
    return;
  }
  if (getSettings().engine === "legacy" && chatAbort) {  // 旧链档：chatAbort 在跑
    chatAbort.abort();
    return;
  }
  callChat();
});
userInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    // D-6.0a 双语义生成锁：SSE 档=当前会话活流；旧链档=chatAbort 在跑
    if (curConvId && _STREAMING[curConvId]) return;
    if (getSettings().engine === "legacy" && chatAbort) return;
    callChat();
  }
});
// ============================================================
// 粘贴图片：Ctrl+V 粘贴截图 -> 压缩 -> 预览 -> 随消息发给多模态模型
// ============================================================
// 待发送的图片列表（base64 data URL 形式）
let pendingImages = [];

const imgPreview = document.getElementById("attachRow");

// paste 事件：逐个看剪贴板 items 里有没有图片
userInput.addEventListener("paste", (e) => {
  for (const item of e.clipboardData.items) {
    if (item.type.startsWith("image/")) {
      const blob = item.getAsFile();
      if (blob) {
        // 阻止默认行为（图片会被当成文字粘进输入框变乱码）
        e.preventDefault();
        compressImage(blob).then((dataUrl) => {
          pendingImages.push(dataUrl);
          renderImgPreview();
        });
      }
    }
  }
});

// 压缩图片：canvas 缩到最长边 1568px（多模态模型"够清晰又不大"的尺寸）转 jpeg。
// 全屏截图动辄几 MB，直接发可能超请求体上限
function compressImage(blob) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const maxSide = 1568;
        // 最小边护栏：多模态模型有最小像素要求（实测 1x1 被 400 拒收），太小的图等比放大到 280
        const minSide = 280;
        let w = img.width, h = img.height;
        if (Math.max(w, h) > maxSide) {
          if (w >= h) { h = Math.round(h * maxSide / w); w = maxSide; }
          else { w = Math.round(w * maxSide / h); h = maxSide; }
        } else if (Math.min(w, h) < minSide) {
          if (w <= h) { h = Math.round(h * minSide / w); w = minSide; }
          else { w = Math.round(w * minSide / h); h = minSide; }
        }
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL("image/jpeg", 0.85)); // 质量 85%：清晰度和体积的平衡点
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(blob);
  });
}

// ============================================================
// 上传文档：选 .txt/.md 文件 -> 读全文 -> 随消息发给模型总结
// （纯文本文件浏览器自己能读；pdf/docx 需要解析库，后续再加）
// ============================================================
// 待发送的文档（一次一份）：{ name: 文件名, content: 全文 }
let pendingDoc = null;

const docFileInput = document.getElementById("docFileInput");
const uploadDocBtn = document.getElementById("uploadDocBtn");
// 文档大小护栏：1MB（公司代理对请求体有限制；1MB ≈ 35 万字，够总结了）
const MAX_DOC_SIZE = 1024 * 1024;

uploadDocBtn.addEventListener("click", () => docFileInput.click());

docFileInput.addEventListener("change", () => {
  const file = docFileInput.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = () => {
    let content = reader.result;
    let truncated = false;
    if (content.length > MAX_DOC_SIZE) {
      content = content.slice(0, MAX_DOC_SIZE);
      truncated = true;
    }
    pendingDoc = { name: file.name, content, truncated };
    userInput.value = "请帮我总结这份文档，提炼要点"; // 预填指令（用户可改）
    renderImgPreview(); // 文档卡片和图片共用预览区
  };
  reader.onerror = () => showError("读取文档失败: " + (reader.error?.message || "未知错误"));
  reader.readAsText(file, "utf-8");
});

// 删掉待发文档（预览区的 ×）
function clearPendingDoc() {
  pendingDoc = null;
  if (userInput.value === "请帮我总结这份文档，提炼要点") userInput.value = "";
  renderImgPreview();
}

// 渲染预览区：图片缩略图 + 文档卡片共用一个区
function renderImgPreview() {
  imgPreview.innerHTML = "";
  const hasDoc = !!pendingDoc;
  imgPreview.style.display = (pendingImages.length || hasDoc) ? "flex" : "none";

  if (hasDoc) {
    const box = document.createElement("div");
    box.className = "doc-box";
    const label = document.createElement("span");
    label.textContent = "📄 " + pendingDoc.name + (pendingDoc.truncated ? "（过大已截断）" : "");
    const del = document.createElement("div");
    del.className = "img-del";
    del.textContent = "×";
    del.addEventListener("click", clearPendingDoc);
    box.appendChild(label);
    box.appendChild(del);
    imgPreview.appendChild(box);
  }

  pendingImages.forEach((dataUrl, i) => {
    const box = document.createElement("div");
    box.className = "img-box";
    const thumb = document.createElement("img");
    thumb.src = dataUrl;
    const del = document.createElement("div");
    del.className = "img-del";
    del.textContent = "×";
    del.addEventListener("click", () => {
      pendingImages.splice(i, 1);
      renderImgPreview();
    });
    box.appendChild(thumb);
    box.appendChild(del);
    imgPreview.appendChild(box);
  });
}




// ══════════ 阶段五 6.3·会话导出（2026-09-23）══════════
// 当前会话全量导出 Markdown：问答对+引用来源+trace_id（证据链给领导看）
document.getElementById("exportBtn").addEventListener("click", () => {
  const msgs = document.querySelectorAll("#chatFlow .msg, #chatFlow .message");
  if (!msgs.length) { alert("当前无会话可导出"); return; }
  let md = `# 知识库问答记录\n\n导出时间: ${new Date().toLocaleString()}\n\n---\n\n`;
  document.querySelectorAll("#chatFlow *").forEach(el => {
    // user 气泡与 ai 气泡按类名识别（与渲染层同款类名）
    if (el.classList && (el.classList.contains("user-msg") || el.classList.contains("msg-user"))) {
      if (el.textContent.trim()) md += `## 🙋 提问\n\n${el.textContent.trim()}\n\n`;
    } else if (el.classList && (el.classList.contains("ai-msg") || el.classList.contains("msg-ai"))) {
      if (el.textContent.trim()) md += `## 💡 回答\n\n${el.textContent.trim()}\n\n---\n\n`;
    }
  });
  md += `\n> 由 RAG 知识库问答系统导出 · 检索/回答均带知识库引用溯源\n`;
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `问答记录_${new Date().toISOString().slice(0,10)}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
});

// ══════════ 阶段二 3.4·服务端 Agent SSE 客户端（2026-09-23）══════════
// 消费 POST /api/agent/chat 的 SSE 四事件流（round/tool/delta/done）。
// 切换闸：AGENT_SSE_ON（默认 false——curl 验收过后切 true 走服务端循环；
// false 时走原前端 callChatWithTools——对照期双路并存可回退）。
const AGENT_SSE_ON = true;  // 已退役（D-6.0a）：分派改读 getSettings().engine（设置弹窗开关）——本常量仅存档，D-6.1 删链时一并清理

async function agentChatSSE(question, history, onDelta, onTool, onDone, onSteps) {
  /* 调服务端 Agent 执行器。回调式消费事件流。
     返回 {answer, traceId, kbCalls, secs}；异常上抛（调用方回退旧路）。 */
  const s = getSettings();
  const resp = await fetch(BACKEND_URL + "/api/agent/chat", {
    method: "POST",
    signal: chatAbort ? chatAbort.signal : undefined,  // 二十四修：停止按钮可掐 SSE
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      scan_id: currentScanId,
      // C 阶段收尾（2026-09-24）：不再 slice(-10) 预砍——服务端 slide_history
      // 统一滑窗+摘要（>6 轮压摘要），20 轮长对话语境经摘要进模型。旧 slice(-10)
      // 是旧链时代遗留护栏，与「历史不截断」铁律冲突，正是本步根治对象。
      // 单条 content 上限保留 4000（防单条巨块打爆请求体——非历史截断）。
      mode: ragToggle.checked && currentScanId != null ? "auto" : "chat",  // D-1：RAG 关/无库→chat（服务端单轮直答）；带库→auto（服务端自路由）
      // D-2 附件归一化（2026-09-24）：pendingImages/pendingDoc → attachments
      // 数组（服务端 D-2.1 消费——图片多模态 content 数组/文档全文注入）。
      attachments: [
        ...pendingImages.map(u => ({ type: "image", data: u })),
        ...(pendingDoc ? [{ type: "doc", name: pendingDoc.name, content: pendingDoc.content }] : []),
      ],
      chat_key: chatApiKey(), chat_url: chatApiUrl().replace(/\/chat\/completions$/, ""),
      vec_key: effectiveSearchSettings().vecKey, vec_url: effectiveSearchSettings().vecUrl,
      embed_model: effectiveSearchSettings().embedModel,
      rerank_key: effectiveSearchSettings().rerankKey,
      rerank_url: effectiveSearchSettings().rerankUrl,
      rerank_model: effectiveSearchSettings().rerankModel,
      model: selectedModel,
    }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const reader = resp.body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "", answer = "", meta = {};
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop(); // 半行回存
    let ev = "";
    for (const l of lines) {
      if (l.startsWith("event: ")) ev = l.slice(7).trim();
      else if (l.startsWith("data: ")) {
        try {
          const d = JSON.parse(l.slice(6));
          if (ev === "delta" && d.text) {
            // 三十八修：返回的 r.answer 只留正文——reasoning 若混入，收尾 showResult(r.answer) 会把思考覆盖进正文
            if (d.type !== "reasoning") answer += d.text;
            onDelta && onDelta(d.text, answer, d.type);
          }
          else if (ev === "round") {
            // 三十修 + C 方案：round 事件 steps——先走 onSteps 回调进注册表（数据层），
            // 回调内决定是否上屏（切走时 addProcessStep 是 no-op 静默丢——advisor 终核实锤）
            if (d.steps && d.steps.forEach) {
              if (onSteps) d.steps.forEach(s => onSteps("request", s));
              else d.steps.forEach(s => addProcessStep("request", s));
            }
            if (onSteps) onSteps("request", `第 ${d.round} 轮请求...`);
            else addProcessStep("request", `第 ${d.round} 轮请求...`);
          }
          else if (ev === "steps") {
            // 五十修补丁 + C 方案：审计尾步骤——同走 onSteps
            if (d.steps && d.steps.forEach) {
              if (onSteps) d.steps.forEach(s => onSteps("tool-result", s));
              else d.steps.forEach(s => addProcessStep("tool-result", s));
            }
          }
          else if (ev === "tool") { onTool && onTool(d); }
          else if (ev === "done") { meta = d; }
        } catch {}
        ev = "";
      }
    }
  }
  onDone && onDone(meta);
  return { answer, traceId: meta.trace_id, kbCalls: meta.kb_calls, secs: meta.secs };
}

async function callChat() {
  const question = userInput.value.trim();

  // 输入校验：有文字 或 有图片 或 有文档 都能发
  if (!question && pendingImages.length === 0 && !pendingDoc) {
    showError("请先输入问题，或粘贴图片/上传文档");
    userInput.focus();
    return;
  }

  if (!chatApiKey()) {
    showError("请先点右上角「⚙ 设置」填入聊天 API Key");
    openSettings();
    return;
  }

  // D-3.2 分派更新（2026-09-24）：短追问拦截删——服务端三阶判定链接管
  const _sseEligible = getSettings().engine === "sse";  // D-6.0a：设置弹窗双链开关（默认 sse；legacy=对照期旧链）
  // classifyFollowup/rewriteFollowup 本体保留（旧链仍用，D-6 删链时统一删）。
  if (_sseEligible) {
    // D-4.2 分派更新（2026-09-24）：计算投票前置判定删——服务端投票路接管
    // （数字预筛+LLM 判定+素材冻结 3 采样多数决+短路定稿，全在 SSE 端点内）。
    // needsVote 函数本体保留（旧链仍用，D-6 删链时统一删）。
    {
      // 二十一修（advisory 三条）：await 前先占位——防双击双查 + 渲染链完整
      // 二十二修：SSE 路径也要 new AbortController——旧链在后面 new，
      // SSE return 后跳过 → chatAbort 为 null → 停止按钮点时 onstop 炸（用户弹框实锤）
      chatAbort = new AbortController();
      chatAbort.onstop = null;
      const sseConv = ensureConv(question);  // 二十五修：会话列表建案（advisory——conv 只在旧链声明，SSE 分支是未声明标识符）
      // D-2：附件标签与旧链一致（L1160-1162）——无附件时第二参为空串
      const _attInfo = (pendingImages.length ? `[${pendingImages.length} 张图]` : "") + (pendingDoc ? ` [文档: ${pendingDoc.name}]` : "");
      appendUserMsg(question, _attInfo.trim());
      userInput.value = "";
      curAIMsg = appendAIMsg(selectedModel);  // 占位气泡（"思考中"由 CSS 显示）
          try { curAIMsg.body.innerHTML = '<p>正在思考中...</p>'; } catch (e) {}
      let _sseAnswer = "";       // D-5.9：提升到 try 外（catch 停止分支要读半截答案——块内声明 catch 不可见）
      let _sseReasoning = "";
      let sseConvRef = null;     // D-5.9：会话引用提升（catch 里落库用）
      feTraceId = "fe_" + Date.now().toString(36);
      setLoading(true);  // 发送按钮禁用（等待期不可再点）
      try {
        sseConvRef = sseConv;
        // C 方案：注册流式目标——每会话独立，切走不丢、切回继续、别的会话提问不串台
        _STREAMING[sseConv.id] = {
          conv: sseConv,
          abort: chatAbort,
          steps: [],           // 过程步骤累积器（advisor 实锤：原从 DOM 摘取，切走即空）
          reasoning: "",       // 思考累积器
          answer: "",          // 答案累积器
          feTraceId: feTraceId,
          attachments: { images: pendingImages.slice(), doc: pendingDoc },  // 快照（收尾记账用）
          msg: { role: "assistant", content: "", streaming: true, meta: { reasoning: "", steps: [] } },
          histUser: (pendingDoc ? (question || "请总结这份文档") + pendingDoc.content : question),  // C 方案：user 消息记账（advisor 实锤：只推 assistant 切回只见答案不见问题）
        };
        // C 方案：user 消息先进会话数据（流式未结束切回也显示完整一问一答占位）
        sseConv.messages.push({ role: "user", content: _STREAMING[sseConv.id].histUser });
        sseConv.messages.push(_STREAMING[sseConv.id].msg);  // 流式态消息进会话数据
        setLoading(true);  // C 方案修复（用户实锤时序）：注册表已写入——此刻按钮才变「停止」（L1061 那次调用时注册表还没写，新 setLoading 逻辑查空=误显示发送）
        const hist = chatHistory;  // 传全量——截断/摘要交服务端 slide_history（2026-09-24，slice(-10) 私设护栏拆除）
        const r = await agentChatSSE(question, hist,
          (delta, full, type) => {
            // 二十八修 + C 方案：思考/答案流式——先写会话数据（真源），DOM 只是投影
            const reg = _STREAMING[sseConv.id];
            if (reg) {
              if (type === "reasoning") {
                reg.reasoning += delta; reg.msg.meta.reasoning = reg.reasoning;
              } else {
                reg.answer += delta; reg.msg.content = reg.answer;
              }
            }
            if (type === "reasoning") {
              _sseReasoning += delta;
              if (streamMsgDomUpdate(sseConv.id)) try { showReasoningUpdate(_sseReasoning, true); } catch (e) {}  // 三十七修：showReasoning 是旧链局部别名——callChat 不可见（ReferenceError 被吞）
            } else {
              _sseAnswer += delta;
              if (streamMsgDomUpdate(sseConv.id)) try { showResult(_sseAnswer, false, true); } catch (e) {}  // isStreaming=true 流式模式
            }
          },
          (toolInfo) => {
            const _txt = `🔧 ${toolInfo.name} (${toolInfo.secs || "?"}s)`;
            const reg = _STREAMING[sseConv.id];
            if (reg) reg.steps.push({ type: "tool-result", text: _txt });  // C 方案：步骤进数据（advisor 实锤：原从 DOM 摘取切走即空）
            if (streamMsgDomUpdate(sseConv.id)) addProcessStep("tool-result", _txt);
          },
          (meta) => {
            const _fin = `SSE 完成：预检索 ${meta.sources ? meta.sources.length : 0} 块 · 工具补查 ${meta.kb_calls || 0} 次 · ${meta.secs || "?"}s · ${meta.total_tokens ?? "?"} tokens`;
            const reg = _STREAMING[sseConv.id];
            if (reg) reg.steps.push({ type: "final", text: _fin });  // C 方案：收尾行也进数据
            if (streamMsgDomUpdate(sseConv.id)) addProcessStep("final", _fin);
            // 三十二修：组装 __ragHits——旧链 renderCitations/buildSourcesList/
            // 点击浮窗整条渲染链直接复用（advisory：只设 __ragSources 不够，
            // 2663/2675/2694 行读的是 __ragHits）
            try { if (meta.src_details && meta.src_details.length) {
              window.__ragHits = meta.src_details.map(d => ({ file: d.file, text: d.text || "", score: (typeof d.score === "number" ? d.score : 0), seq: d.seq }));
              if (reg) reg.msg.meta.hits = window.__ragHits.slice();  // C 方案：来源快照进数据
            } } catch (e) {}
            try { if (meta.sources && meta.sources.length) {
              window.__ragSources = meta.sources;
              if (reg) reg.msg.meta.sources = meta.sources.slice();
            } } catch (e) {}
          },
          // C 方案终核补口（advisor blocker）：round/steps 事件经 onSteps 进注册表——
          // 预检索/判定/SQL 命中/第2路同义词/命中15块/答案核验全进 reg.steps，
          // 切走不丢（原直写 DOM：curAIMsg=null 时 no-op 静默丢+收尾 meta.steps 缺整段）
          (stype, stext) => {
            const reg = _STREAMING[sseConv.id];
            if (reg) reg.steps.push({ type: stype, text: stext });
            if (streamMsgDomUpdate(sseConv.id)) addProcessStep(stype, stext);
          });
        if (r && r.answer && r.answer.trim()) {
          const reg = _STREAMING[sseConv.id];
          // C 方案收尾：定稿写会话数据（真源），DOM 投影按当前会话判断
          const _sseMeta = {
            trace_id: r.traceId,
            kb_calls: r.kbCalls,
            reasoning: _sseReasoning,
            steps: reg ? reg.steps : [],  // C 方案：步骤从累积器读（advisor 实锤：原从 DOM 摘取切走即空）
            sources: (window.__ragHits || []).map((h) => ({ file: h.file, text: h.text || "", seq: h.seq })),
          };
          let _histUserContent = question;
          if (pendingDoc) _histUserContent = (question || "请总结这份文档") + pendingDoc.content;
          // C 方案：流式态消息原地转定稿（不 push 新条目——流式时已占位）
          if (reg) {
            reg.msg.streaming = false;
            reg.msg.content = r.answer;
            reg.msg.meta = _sseMeta;
          }
          // C 方案（advisor 实锤串会话污染）：chatHistory 是全局、切走后被重指到
          // 新会话的拷贝——原会话收尾无条件 push 会把这一问一答灌进「当前正看的
          // 会话」的多轮上下文。守卫：只有原会话还激活时才写。
          if (curConvId === sseConv.id) {
            chatHistory.push({ role: "user", content: _histUserContent });
            chatHistory.push({ role: "assistant", content: r.answer });
          }
          // DOM 投影（只在原会话还挂着屏时做）
          if (streamMsgDomUpdate(sseConv.id)) {
            try { showResult(r.answer, false, false); } catch (e) {}
            try { showReasoningUpdate(_sseReasoning, false); } catch (e) {}
            try {
              if (window.__ragHits && window.__ragHits.length && curAIMsg) {
                const _srcList = buildSourcesList();
                if (_srcList) curAIMsg.body.insertAdjacentHTML("beforeend", DOMPurify.sanitize(renderCitations(_srcList)));
              }
            } catch (e) {}
            try { appendTraceBtn(r.traceId); } catch (e) {}
          }
          // 四十一修：忠实度校验（前端异步版——切走了照跑，__ragHits 是全局的够用）
          try { if (r.answer && r.answer.length > 200) verifyFaithfulness(r.answer).catch(() => {}); } catch (e) {}
          if (reg) delete _STREAMING[sseConv.id];  // 注销流式目标
          renderConvList();  // 徽标刷新（生成中→完成）
          const sid = (sseConv && sseConv.id) ? sseConv.id : ("s_" + Date.now().toString(36));
          fetch(BACKEND_URL + "/api/agent/sessions/" + sid + "/append", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ user_id: "local", messages: [
              { role: "user", content: _histUserContent },
              { role: "assistant", content: r.answer, meta: _sseMeta }] })
          }).catch(() => {});
          // 切走场景：原会话不是当前会话——chatHistory/pending 不动（那是当前会话的）
          if (curConvId === sseConv.id) {
            pendingImages = [];
            pendingDoc = null;
            renderImgPreview();
            curAIMsg = null;
            chatAbort = null;
            setLoading(false);
          }
          return; // SSE 路径完成
        }
        // answer 为空——清理占位走旧链
        curAIMsg = null;
        chatAbort = null;
        setLoading(false);
      } catch (e) {
        if (e && e.name === "AbortError") {
          // D-5.9 停止语义（2026-09-25 · 用户拍板：半截定稿进历史）：
          // SSE 中止时拿已收 delta 定稿——不回退旧链（回退=重跑整题，停止失效）。
          const reg = _STREAMING[sseConvRef ? sseConvRef.id : ""];
          const _partial = (_sseAnswer || (reg ? reg.answer : "") || "").trim();  // C 方案：注册表累积器兜底（advisor 实锤：闭包变量在回调链外不可靠）
          if (_partial) {
            if (streamMsgDomUpdate(sseConvRef.id)) {
              try { showResult(_partial, false, false); } catch (e2) {}
              try { showReasoningUpdate(_sseReasoning || "", false); } catch (e2) {}
            }
            let _histUser = question;
            if (reg && reg.attachments.doc) _histUser = (question || "请总结这份文档") + reg.attachments.doc.content; // C 方案：附件从注册表快照读（切走后 pending 已被清）
            else if (pendingDoc) _histUser = (question || "请总结这份文档") + pendingDoc.content; // D-2 记账对齐
          if (curConvId === (sseConvRef && sseConvRef.id)) {  // C 方案守卫：同收尾段（advisor 串会话污染）
            chatHistory.push({ role: "user", content: _histUser });
            chatHistory.push({ role: "assistant", content: _partial });
          }
            // C 方案：流式态消息原地转半截定稿
            if (reg) {
              reg.msg.streaming = false;
              reg.msg.content = _partial;
              reg.msg.meta = { stopped: true, partial: true, reasoning: _sseReasoning || "", steps: reg.steps };
              delete _STREAMING[sseConvRef.id];
            } else {
              try { if (sseConvRef && sseConvRef.messages) {
                sseConvRef.messages.push({ role: "assistant", content: _partial,
                  meta: { stopped: true, partial: true } });
              } } catch (e2) {}
            }
            const _sid = (sseConvRef && sseConvRef.id) ? sseConvRef.id : ("s_" + Date.now().toString(36));
            fetch(BACKEND_URL + "/api/agent/sessions/" + _sid + "/append", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ user_id: "local", messages: [
                { role: "user", content: _histUser },
                { role: "assistant", content: _partial, meta: { stopped: true, partial: true } }] })
            }).catch(() => {});  // 半截答案落库（刷新不丢）
          } else {
            // 一个字没出就停：气泡定格提示，不进历史（旧链 L1941-1946 同款）
            if (streamMsgDomUpdate(sseConvRef ? sseConvRef.id : "")) {
              try { showResult("（已停止生成）", false); } catch (e2) {}
            }
            if (reg) delete _STREAMING[sseConvRef.id];
            // C 方案：注册时已 push 的 user 消息 + streaming 占位——一个字没出不留在会话数据
            try { if (sseConvRef && sseConvRef.messages) {
              const _c = sseConvRef.messages;
              if (_c.length && _c[_c.length-1] === reg.msg) _c.pop();
              if (_c.length && _c[_c.length-1] && _c[_c.length-1].role === "user" && _c[_c.length-1].content === (reg ? reg.histUser : "")) _c.pop();
            } } catch (e2) {}
          }
          renderConvList();
          if (curConvId === (sseConvRef && sseConvRef.id)) {
            pendingImages = [];
            pendingDoc = null;
            renderImgPreview();
            curAIMsg = null;
            chatAbort = null;
            setLoading(false);
          }
          return;  // 停止=终态，绝不落旧链重跑
        }
        addProcessStep("tool-result", "SSE 执行器失败——回退前端循环: " + e.message);
        // C 方案收口（advisor 实锤①）：回退旧链前清注册表 + 撤销已 push 的
        // user/streaming 占位（否则旧链自己再 push 一问一答=双 user + 孤儿
        // streaming:assistant 常驻，徽标常亮泄漏）
        try {
          const _rg = sseConv ? _STREAMING[sseConv.id] : null;
          if (_rg && sseConv && sseConv.messages) {
            const _c = sseConv.messages;
            if (_c.length && _c[_c.length-1] === _rg.msg) _c.pop();
            if (_c.length && _c[_c.length-1] && _c[_c.length-1].role === "user" && _c[_c.length-1].content === _rg.histUser) _c.pop();
          }
          if (sseConv) delete _STREAMING[sseConv.id];
          renderConvList();
        } catch (e2) {}
        curAIMsg = null;
        chatAbort = null;
        setLoading(false);
        // 落到下面的旧路径（对照期双保险）
      }
    }
  }

  // 模型必须定选过

  // 模型必须定选过（输入框里的自由文字不算，得从列表里选一个）
  if (!selectedModel) {
    showError("请先从下拉框选择一个模型");
    modelSelect.focus();
    return;
  }

  // ---------- 画用户气泡 + 空 AI 气泡 ----------
  const conv = ensureConv(question || "图片/文档对话"); // 第一条消息创建会话并立标题
  document.getElementById("topbarTitle").textContent = conv.title;
  const attachInfo =
    (pendingImages.length ? `[${pendingImages.length} 张图]` : "") +
    (pendingDoc ? ` [文档: ${pendingDoc.name}]` : "");
  appendUserMsg(question, attachInfo.trim());
  userInput.value = "";
  curAIMsg = appendAIMsg(selectedModel); // AI 占位气泡，流式期间持续更新
  curAIMsg.body.textContent = "正在思考中...";

  setLoading(true); // 防重复提交（按钮变形为"停止"）——C 方案：旧链无注册表，setLoading 内部对 curConvId 查 _STREAMING 为空时会退回本参数（isLoading 兜底）
  // 整轮共用一个 AbortController：RAG 检索等待、流式输出全程都能停。
  // 注意别在 callChatWithTools 里再 new——那里看不到这个生命周期
  chatAbort = new AbortController();
  chatAbort.onstop = null; // 停止时的收尾回调（callChatWithTools 里注册：
                           // 半截回答定稿+记账；没到流式阶段就停则无操作）

  // 抄一份待发（发送过程中用户可能再操作，别互相干扰）
  const imagesToSend = pendingImages.slice();
  const docToSend = pendingDoc;

  try {
    // 走工具调用循环：模型要调工具时本地执行并把结果回传
    const answer = await callChatWithTools(question, imagesToSend, docToSend);
    pendingImages = [];
    pendingDoc = null;
    renderImgPreview();
  } catch (err) {
    if (err.name === "AbortError") {
      // 手动停止。到过流式阶段 = 走注册的收尾回调（半截定稿+记账）；
      // 停在 RAG/向量化等待阶段 = 回调还是 null，气泡定稿为提示文字
      if (typeof chatAbort?.onstop === "function") chatAbort.onstop();
      else showResult("（已停止生成）", false);
      // 图片/文档已随这条消息发出去了，清掉待发区
      pendingImages = [];
      pendingDoc = null;
      renderImgPreview();
    } else {
      showError(err.message);
    }
  } finally {
    chatAbort = null;  // 无论正常结束/停止/出错，请求生命周期完结
    setLoading(false);
    curAIMsg = null;
  }
}

// ============================================================
// 多会话管理 + 对话流渲染（豆包式聊天界面）
// ============================================================
// conversations：所有会话；curConvId：当前激活会话；
// curAIMsg：正在流式输出的 AI 消息 DOM 引用；chatHistory：当前会话历史
let conversations = [];
let curConvId = null;
let curAIMsg = null;
let chatHistory = [];

// ═══ C 方案·流式注册表（2026-09-25 用户拍板：切会话不断流+生成中徽标）═══
// 问题：流式渲染靠全局 curAIMsg/chatAbort/闭包 _sseAnswer 三件套——切会话清屏
// 即断（气泡 DOM 被炸，字写进孤儿节点）；切到别的会话再提问还会串台
// （advisor 实锤：全局单例被新 callChat 覆盖，会话 1 的字打进会话 2 的气泡）。
// 修法：流式状态挂到会话对象上（数据驱动渲染）——SSE 的四条渲染入口
// （delta/工具步骤/思考/收尾）全部改写会话消息对象；DOM 只是数据投影，
// 切走切回=换投影，数据流（SSE 连接）不断。_STREAMING 记录每个会话的
// 进行中流（每会话同时最多一条——同会话二问由 setLoading 挡）。
// pendingAttachmentsSnapshot：切换后 pendingImages/pendingDoc 被清（别的会话
// 发问会清），快照保住原会话的附件状态供收尾记账。
const _STREAMING = {};  // convId -> {msg, abort, conv, steps[], attachments:{images,doc}, feTraceId}

function streamReg(convId) { return _STREAMING[convId] || null; }

// 流式态消息上屏/更新（只在该会话是当前会话时操作 DOM——切走了就只写数据）
function streamMsgDomUpdate(convId) {
  const reg = _STREAMING[convId];
  if (!reg || curConvId !== convId || !curAIMsg) return false;  // 不在屏上=只写数据
  return true;
}

// 开新会话：等第一条消息来了才立标题
function newConversation() {
  curConvId = null;
  chatHistory = [];
  chatInner.innerHTML = `
    <div class="empty-hint">
      <h2>有什么可以帮你？</h2>
      输入问题开始对话 · Ctrl+V 粘贴图片 · 上传文档总结
    </div>`;
  renderConvList();
  setLoading(false);  // C 方案修复（用户实锤：新对话按钮残留停止态）：curConvId=null → 注册表查空 → 按参数 false → 发送态
}
function ensureConv(firstQuestion) {
  if (curConvId !== null) return conversations.find((c) => c.id === curConvId);
  // 四十二修：本地会话 id 与服务端 session_id 同一套（"s_"+时间戳），
  // 否则 ensureConv 生成 Date.now() 数字 id，服务端存的是 "s_..." 字符串，两者对不上，
  // 持久化拿不到正确 sid → 只能依赖全局 sessionStorage → 所有对话挤进同一会话
  const conv = {
    id: "s_" + Date.now().toString(36),
    title: (firstQuestion || "新对话").slice(0, 20),
    messages: [],
  };
  conversations.unshift(conv);
  curConvId = conv.id;
  renderConvList();
  chatInner.innerHTML = ""; // 清掉欢迎页
  return conv;
}

function renderConvList() {
  convList.innerHTML = "";
  if (conversations.length === 0) {
    convList.innerHTML = '<div class="empty-hint" style="padding:20px 8px;font-size:12px;">暂无会话</div>';
    return;
  }
  for (const conv of conversations) {
    const item = document.createElement("div");
    item.className = "conv-item" + (conv.id === curConvId ? " active" : "");
    item.textContent = conv.title;
    // C 方案：生成中徽标——该会话有流式注册=还在答题（蓝点+文字，答完消失）
    if (_STREAMING[conv.id]) {
      const badge = document.createElement("span");
      badge.textContent = " ● 生成中";
      badge.style.cssText = "color:#2563eb;font-size:10px;font-weight:600;margin-left:4px;";
      item.appendChild(badge);
    }
    item.addEventListener("click", () => switchConversation(conv.id));
    convList.appendChild(item);
  }
}

// 切换会话：重放历史消息 + 恢复 chatHistory

// ═══ 三十四修：刷新恢复会话（阶段三 4.4——SSE 增值：旧链也没有）═══
// 页面加载时从服务端拉会话列表 → 渲染左栏 → 点击恢复消息
async function restoreSessionsFromServer() {
  try {
    const resp = await fetch(BACKEND_URL + "/api/agent/sessions?user_id=local");
    const data = await resp.json();
    const sessions = data.sessions || [];
    for (const s of sessions) {
      // 转换为 conversations 数组格式（与旧链 newConversation 同构）
      const conv = {
        id: s.session_id,
        title: s.title || "对话",
        messages: [],
        createdAt: s.updated_at || Date.now(),
      };
      conversations.push(conv);
    }
    renderConvList();
    // 恢复最近的会话消息（点击时才拉——首次只拉列表）
  } catch (e) {
    // 服务端不可用——静默（旧链行为：空列表）
  }
}

// C 方案：会话消息统一重放（切会话/切回共用）——流式态消息渲染半截 + 挂流式目标继续收字
function renderConvMessages(conv) {
  chatInner.innerHTML = "";
  for (const m of conv.messages) {
    if (m.role === "user") {
      appendUserMsg(m.content, "");
    } else if (m.role === "assistant") {
      if (m.streaming) {
        // 流式态：渲染已收到的半截（正文/思考/步骤），并把 curAIMsg 指到这个气泡
        // ——SSE 回调的 streamMsgDomUpdate 从此放行，新到的字实时上屏
        curAIMsg = appendAIMsg(selectedModel);
        try { if (m.meta && m.meta.reasoning) showReasoningUpdate(m.meta.reasoning, true); } catch (e) {}
        try { (m.meta && m.meta.steps || []).forEach((s) => addProcessStep(s.type || "tool-result", s.text)); } catch (e) {}
        if (m.content) { try { showResult(m.content, false, true); } catch (e) {} }
        else { try { curAIMsg.body.innerHTML = '<p>正在思考中...</p>'; } catch (e) {} }  // C 方案修复：还没出字=思考占位（用户实锤：切回空气泡）
      } else {
        renderAssistantTurn(m);
      }
    }
  }
}


function switchConversation(id) {
  const conv = conversations.find((c) => c.id === id);
  if (!conv || id === curConvId) return;
  curConvId = id;
  // 三十四修：SSE 会话消息可能还没拉（服务端恢复）——空则先拉
  if (!conv.messages.length) {
    loadSessionMessages(id).then(() => {
      chatHistory = conv.messages.slice();
      renderConvList();
      renderConvMessages(conv);  // C 方案：统一重放（含流式态）
      setLoading(!!_STREAMING[id]);  // C 方案修复：异步分支同款按钮刷新
    });
    return;
  }
  chatHistory = conv.messages.slice();
  renderConvMessages(conv);  // C 方案：统一重放（含流式态）——消除两分支分别清屏的竞态（advisor）
  setLoading(!!_STREAMING[id]);  // C 方案修复：按钮跟随新会话的流状态（有流=停止可停它；无流=发送）
  if (!_STREAMING[id]) chatAbort = null;  // C 方案根治（advisor）：新会话无活流时清全局残留——旧链回退的 abort 不跨会话
  renderConvList();
  document.getElementById("topbarTitle").textContent = conv.title;
  chatFlow.scrollTop = chatFlow.scrollHeight;
}

// 会话点击恢复：拉该会话的消息（三十四修——服务端持久化配套）
async function loadSessionMessages(convId) {
  try {
    const resp = await fetch(BACKEND_URL + "/api/agent/sessions/" + convId + "?user_id=local");
    const data = await resp.json();
    if (data.messages) {
      const conv = conversations.find((c) => c.id === convId);
      if (conv) {
        conv.messages = data.messages.map((m) => ({ role: m.role, content: m.content, meta: m.meta }));
        // 渲染到聊天区——四十二修：完整重放（思考/调用/来源/trace），C 方案：走统一重放
        chatInner.innerHTML = "";
        renderConvMessages(conv);
        curAIMsg = null;
      }
    }
  } catch (e) {}
}
// ---------- 对话流消息渲染 ----------

// 追加一条用户消息（右对齐气泡）。attachInfo 是附加说明
function appendUserMsg(text, attachInfo) {
  // 二十七修：首条消息清欢迎语（SSE 分派实测——empty-hint 残留）
  try { const eh = document.querySelector(".empty-hint"); if (eh) eh.remove(); } catch (e) {}
  const wrap = document.createElement("div");
  wrap.className = "msg-user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text || "（图片/文档）";
  if (attachInfo) {
    const tag = document.createElement("span");
    tag.className = "attach-tag";
    tag.textContent = attachInfo;
    bubble.appendChild(tag);
  }
  wrap.appendChild(bubble);
  chatInner.appendChild(wrap);
  chatFlow.scrollTop = chatFlow.scrollHeight;
}

// 追加一条 AI 消息（左对齐 + 思考/调用两个折叠块），返回消息句柄
function appendAIMsg(modelName) {
  const wrap = document.createElement("div");
  wrap.className = "msg-ai";
  const head = document.createElement("div");
  head.className = "ai-head";
  head.textContent = "🤖 " + (modelName || selectedModel || "AI");
  const body = document.createElement("div");
  body.className = "ai-body";
  const thinkBlock = makeFoldBlock("💡 思考过程");
  const procBlock = makeFoldBlock("⚙️ 调用过程");
  wrap.appendChild(head);
  wrap.appendChild(body);
  wrap.appendChild(thinkBlock);
  wrap.appendChild(procBlock);
  chatInner.appendChild(wrap);
  chatFlow.scrollTop = chatFlow.scrollHeight;
  return { wrap, body, thinkBody: thinkBlock.querySelector(".fold-body"), procBody: procBlock.querySelector(".fold-body"), thinkBlock, procBlock };
}

// 造一个折叠块：可点头部展开/收起
function makeFoldBlock(title) {
  const block = document.createElement("div");
  block.className = "fold-block";
  const header = document.createElement("div");
  header.className = "fold-header";
  const arrow = document.createElement("span");
  arrow.className = "arrow";
  arrow.textContent = "▶";
  header.appendChild(arrow);
  header.appendChild(document.createTextNode(" " + title));
  const body = document.createElement("div");
  body.className = "fold-body";
  header.addEventListener("click", () => block.classList.toggle("open"));
  block.appendChild(header);
  block.appendChild(body);
  return block;
}

// 四十二修：给 AI 气泡尾追加「可复制 trace_id」按钮（在线 SSE 收尾 + 会话恢复共用）
function appendTraceBtn(traceId) {
  if (!traceId || !curAIMsg) return;
  const btn = document.createElement("button");
  btn.textContent = "📋 " + traceId;
  btn.title = "点击复制本次提问的 trace_id（报障时发给开发者查日志）";
  btn.style.cssText = "margin-top:4px;padding:2px 8px;font-size:11px;background:transparent;color:#6b7280;border:1px solid #d1d5db;border-radius:6px;cursor:pointer;display:block";
  btn.onclick = () => {
    try { navigator.clipboard.writeText(traceId || ""); btn.textContent = "✓ 已复制"; setTimeout(() => btn.textContent = "📋 " + traceId, 1500); } catch {}
  };
  // 四十五修：挂到 wrap（整条 AI 气泡）末尾 = 「调用过程」折叠块下面，
  // 与旧链 finalizeTurn 的手工按钮位置统一（原挂 body 末尾，在正文/参考来源下面）
  (curAIMsg.wrap || curAIMsg.body).appendChild(btn);
}

// 四十二修：完整重放一条 AI 消息（思考/调用过程/正文/来源清单/trace），
// 与在线渲染完全一致——会话恢复（loadSessionMessages / switchConversation）共用
function renderAssistantTurn(aiMsg) {
  curAIMsg = appendAIMsg(selectedModel);
  const meta = aiMsg && aiMsg.meta ? aiMsg.meta : {};
  // 思考过程
  if (meta.reasoning) {
    try { showReasoningUpdate(meta.reasoning, false); } catch (e) {}
  }
  // 调用过程步骤
  if (Array.isArray(meta.steps)) {
    meta.steps.forEach((s) => { try { addProcessStep(s.type || "tool-result", s.text); } catch (e) {} });
  }
  // 来源（renderCitations/buildSourcesList/点击浮窗 都读 window.__ragHits）
  if (Array.isArray(meta.sources) && meta.sources.length) {
    window.__ragHits = meta.sources;
  }
  // 正文（[N] 徽标 + markdown）
  try { showResult(aiMsg.content || "", false, false); } catch (e) {}
  // 参考来源清单
  if (Array.isArray(meta.sources) && meta.sources.length) {
    try {
      const _srcList = buildSourcesList();
      if (_srcList) curAIMsg.body.insertAdjacentHTML("beforeend", DOMPurify.sanitize(renderCitations(_srcList)));
    } catch (e) {}
  }
  // trace 复制按钮
  if (meta.trace_id) {
    try { appendTraceBtn(meta.trace_id); } catch (e) {}
  }
  curAIMsg = null;
}

const newChatBtn = document.getElementById("newChatBtn");
newChatBtn.addEventListener("click", newConversation);

async function callChatWithTools(question, images = [], doc = null) {
  const rawQuestion = question; // 原始问题存一份（历史记账用）
  // 有文档时：把全文拼进问题。<<< >>> 围栏明确文档边界，防止文档内容被当成指令
  if (doc) {
    question = (question || "请总结这份文档")
      // + "\n\n文档内容如下：\n<<<\n" + doc.content + "\n>>>\n";
      + doc.content;
  }

  // 有图片时 content 是数组格式（OpenAI 多模态标准）；没图片时是字符串
  // （绝大多数模型只认字符串，数组格式反而报错）
  const userContent = images.length
    ? [
        { type: "text", text: question || "请分析这张图片" },
        ...images.map((url) => ({ type: "image_url", image_url: { url } })),
      ]
    : question;

  const userMessage = { role: "user", content: userContent };

  // ★ 多轮记账：这一问一答存进历史，下一轮模型能看到上文 ★
  // KEEP_DOC_IN_HISTORY = true：带文档的这轮把文档全文也存进历史——
  // 后续"扩写/深挖"都能看到原文；代价是每轮重发全文（请求体变大）
  const KEEP_DOC_IN_HISTORY = true;
  let userHistContent = rawQuestion || "（图片/文档）";
  if (KEEP_DOC_IN_HISTORY && doc) {
    userHistContent = question; // 存当轮真正发出的那条（含文档全文），扩写才有原文可依
  }
  // 收尾记账（闭包：能拿到这轮的 user 内容）：正常完成和"手动停止保留
  // 半截回答"共用。存历史 + 存会话对象 + 渲染定稿
  function finalizeTurn(finalAnswer) {
    const turnUser = { role: "user", content: userHistContent };
    const turnAI = { role: "assistant", content: finalAnswer };
    // 四十四修：收集 meta（思考/调用步骤/来源/trace）——与 SSE 分支 1118 行对齐。
    // 旧链此前只写内存、从不调 /append 持久化 → 刷新后 restoreSessionsFromServer
    // 拉不到旧链消息（用户实锤：短问题走旧链扩写 → 刷新后"1002"整轮消失）。
    // 注意：chatHistory 保持纯净（meta 只进会话对象 + 服务端，不混进下轮 LLM 请求体）
    let _steps = [];
    try {
      if (curAIMsg && curAIMsg.procBody) {
        curAIMsg.procBody.querySelectorAll(".step").forEach((el) => {
          _steps.push({ type: el.className.replace("step", "").trim(), text: el.textContent });
        });
      }
    } catch (e) {}
    const _meta = {
      trace_id: feTraceId,
      reasoning: reasoning || "",
      steps: _steps,
      sources: (window.__ragHits || []).map((h) => ({ file: h.file, text: h.text || "", seq: h.seq })),
    };
    chatHistory.push(turnUser, turnAI);
    // 同步进当前会话对象（左侧列表切换/重放靠它）——这里带 meta，重放才完整
    const conv = conversations.find((c) => c.id === curConvId);
    if (conv) conv.messages.push(turnUser, { role: "assistant", content: finalAnswer, meta: _meta });
    // 会话持久化：追加到服务端（与 SSE 分支同款）——旧链补齐这一步，
    // 否则刷新后这条对话在服务端不存在、列表/重放都拿不到
    try {
      const _sid = (conv && conv.id) || curConvId || ("s_" + Date.now().toString(36));
      fetch(BACKEND_URL + "/api/agent/sessions/" + _sid + "/append", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: "local", messages: [
          { role: "user", content: userHistContent },
          { role: "assistant", content: finalAnswer, meta: _meta }] })
      }).catch(() => {});
    } catch (e) {}
    // 【防幻觉第三层·出处可验】回答里的 [N] 由提示词强制生成，
    // 渲染在此——用户可对照检索块原文核对，幻觉内容通常给不出可信出处
    // ⑨.5 引用溯源（2026-09-20）：回答尾部自动附参考来源清单（RAGFlow 式）
    const _srcList = (typeof buildSourcesList === "function") ? buildSourcesList() : "";
    showResult(finalAnswer + (_srcList ? "\n\n" + _srcList : ""), false);
    // ⑫8 C2 复制 trace_id 按钮（用户报障带 ID——答案旁一键复制）
    // 四十五修：复用 appendTraceBtn，统一挂到「调用过程」折叠块下面（与 SSE/恢复一致）
    try { appendTraceBtn(feTraceId); } catch (e) {}

    // ② 忠实度校验（Faithfulness·轻量版 2026-09-20）：RAG 长回答答完后
    // 异步问一次 LLM"每句都能在资料里找到依据吗"——不拦截（避免误杀），
    // 低置信时在消息尾补"⚠ 忠实度校验：部分内容未在资料中找到依据"提示
    if (ragPath && ragSystem && finalAnswer && finalAnswer.length > 200) {
      verifyFaithfulness(finalAnswer).catch(() => {});
    }
  }

  // ---------- ⑥1 多轮追问改写 ----------
  // 检索器是无记忆的："销售模块呢？"里没有"负责人"语义（实测这种追问答案块
  // 排 687 名；改写成完整问题后升到第 3 名）。改写要多打 1 次聊天 API，
  // 所以只在 ① RAG 开着 ② 有历史 ③ 问题 ≤30 字 三条都满足时触发
  async function rewriteFollowup(q) {
    // 只取最近一轮问答当上下文——拿太多历史反而稀释重点
    const recent = chatHistory.slice(-2);
    const ctx = recent.map((m) => (m.role === "user" ? "用户: " : "助手: ") + m.content).join("\n").slice(0, 1500);
    // temperature 0：改写要"忠实补全"不要"发挥"
    const resp = await fetch(chatApiUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
      body: JSON.stringify({
        model: rewriteModel(),
        messages: [
          { role: "system", content: "把用户的追问改写成独立、完整的问题（把上文语境补进去）。只输出改写后的问题本身，不要任何解释。" },
          { role: "user", content: "对话上文：\n" + ctx + "\n\n用户追问：" + q + "\n\n改写后的完整问题：" },
        ],
        temperature: 0,
      }),
    });
    const data = await resp.json();
    // 剥掉模型可能带的引号/换行；失败/为空退回原问题（宁可不增强，不能中断）
    return (data.choices?.[0]?.message?.content || "").trim().replace(/^["'\u201c]|["'\u201d]$/g, "").slice(0, 200) || q;
  }

  // ---------- 4-2B 刀2：投票路由判定（业界 Self-Consistency 线上版）----------
  // 计算类/多跳类问题 → 3 次采样对答案值投票（更准，多花 2 倍时间）
  // 普通问题 → 单次直答（不增加延迟）。判定复用 LLM 分类底子（零词表）
  async function needsVote(q) {
    try {
      const resp = await fetch(chatApiUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
        body: JSON.stringify({
          model: rewriteModel(),
          messages: [
            { role: "system", content: "判断这个问题是否涉及金额/数量的计算（加减、汇总、求和、剩余、差额、多步数值推理）。只回答 YES 或 NO。不要任何解释。" },
            { role: "user", content: q },
          ],
          temperature: 0,
        }),
      });
      const data = await resp.json();
      return (data.choices?.[0]?.message?.content || "").trim().toUpperCase().startsWith("YES");
    } catch { return false; } // 判定挂了走单次（保守不拖累）
  }

  // ---------- ⑥1b 追问判定：本问题是接上文，还是换了话题 ----------
  // 背景（2026-09-14 用户实测）：上一轮问优惠券、这轮问销售部，旧逻辑无差别
  // 把优惠券语境揉进检索词，检索池被污染。但"小李呢"这种真追问又必须带上文
  // （否则 AI 不知道问的是小李的什么）。所以先判定类型再决定要不要改写：
  //   NEW      = 问题离开上文仍然语义完整（"销售部的工作效率如何"）→ 纯净检索
  //   FOLLOWUP = 问题依赖上文才有意义（"小李呢"/"那市场部呢"）→ 照旧补全改写
  // 判据交给模型（温度 0 输出一个词），比前端写规则（疑问词/实体词启发式）
  // 对中文口语的覆盖面广得多；判定错了最坏情况 = 一次普通检索的质量。
  // 兜底方向定死：NEW 检索低分时不回退带语境改写（会把换话题的污染重新引回
  // 来——词面差导致的低分该由 synonymize 术语扩展补，不是上文语境），宁缺
  // 不污染
  async function classifyFollowup(q) {
    const recent = chatHistory.slice(-2);
    const ctx = recent.map((m) => (m.role === "user" ? "用户: " : "助手: ") + m.content).join("\n").slice(0, 1500);
    try {
      const resp = await fetch(chatApiUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
        body: JSON.stringify({
          model: rewriteModel(),
          messages: [
            { role: "system", content: "判断用户的最新问题是否依赖对话上文才能理解。只输出一个词：FOLLOWUP（问题离开上文不完整或有省略指代，如\"小李呢\"\"那市场部呢\"）或 NEW（问题本身语义完整、是换了话题的新问题，如\"销售部的工作效率如何\"）。不要任何解释。" },
            { role: "user", content: "对话上文：\n" + ctx + "\n\n用户最新问题：" + q },
          ],
          temperature: 0,
        }),
      });
      const data = await resp.json();
      const v = (data.choices?.[0]?.message?.content || "").trim().toUpperCase();
      return v.includes("FOLLOWUP") ? "FOLLOWUP" : "NEW";
    } catch {
      return "FOLLOWUP";  // 判定调用失败：宁可带上文（旧默认行为），不损失追问能力
    }
  }
  // ---------- ⑥2 同义改写：换语料里可能用的词再检索一次 ----------
  // embedding 对近义词有盲区：问"购买模块"打 0.52 分，换"采购模块"打 0.58 分
  // ——词面差直接决定答案块进不进 Top-K。两路（原词+同义词）双保险
  async function synonymize(q, feedbackHits = null) {
    // ⑨ 三轮步2（2026-09-15）：检索反馈改写——原版 LLM 凭空改写不知道库里
    // 用什么词（"银行流水"改不出库里的"对账单"）。feedbackHits = 第一轮检索
    // 的 Top 命中块：命中差时（Top1 分数 < 0.4）把命中块的文件名/标题喂给
    // LLM 参考改写；命中好时传 null 走原版凭空改写（省上下文）。零词典维护
    // ——语料换=自动适配，从检索反馈学语料用词
    let sysPrompt = "把问题改写成知识库检索用的最佳查询：1) 口语词换专业词（购买→采购）；2) 补上语料可能用的术语、实体名、领域词（如工作包编号、表名、字段名、\"移交\"\"负责人\"等文档常用词）。只输出改写后的查询，不要解释。";
    if (feedbackHits && feedbackHits.length) {
      const refs = feedbackHits.slice(0, 5).map(h =>
        `文件名: ${h.file}｜内容开头: ${String(h.text).slice(0, 80).replace(/\n/g, " ")}`).join("\n");
      sysPrompt = `知识库检索命中不佳，需要改写查询重新检索。以下是知识库里实际存在的文档（含文件名和内容开头）——请参考这些文档的用词和表述，把用户的问题重新表述成最匹配这些文档的检索查询。只输出改写后的查询，不要解释。\n\n参考文档：\n${refs}`;
    }
    const resp = await fetch(chatApiUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
      body: JSON.stringify({
        model: rewriteModel(),
        messages: [
          { role: "system", content: sysPrompt },
          { role: "user", content: q },
        ],
        temperature: 0,
      }),
    });
    const data = await resp.json();
    const v = (data.choices?.[0]?.message?.content || "").trim().replace(/^["'\u201c]|["'\u201d]$/g, "").slice(0, 200);
    return (v && v !== q) ? v : null;  // 没变化返回 null（省得白检索一次）
  }
  // ---------- RAG 注入：开关打开时先检索，命中块拼进 system ----------
  let ragSystem = null;  // null = 没开开关/没命中
  let ragPath = false;   // 工具调用治理①（2026-09-14）：本轮是否真的走了 RAG 检索
  let hits = [];         // 检索结果（函数级——第四层防幻觉审计在 for 循环层要读它，
                         // 块级 let 会 ReferenceError；ragPath 同款作用域坑，L1224 注释实录）
  feTraceId = "fe_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); // ⑫5：本次提问的 trace_id（模块级——vecHeaders 要读它带 X-Request-Id）
  function reportLog(event, q, detail) { // ⑫5：前端两步上报（改写/审计触发——后端无痕必须上报）
    try { fetch(BACKEND_URL + "/api/log", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, trace_id: feTraceId, q: String(q).slice(0, 60), detail: String(detail).slice(0, 200) }) }).catch(() => {}); } catch {}
  }
  // 链路（含零命中）。块内 const ready 活不过 if 块，函数后面读它会
  // ReferenceError——函数级标志带出去，供请求体决定要不要下发 tools
  if (ragToggle.checked && currentScanId != null) {
    // BYOK 分层：开 RAG 但向量 key 未填 -> 引导去设置（不硬报错）
    if (!effectiveSearchSettings().vecKey) {
      addProcessStep("tool-result", "RAG 需要向量模型 API Key——点「⚙ 设置」补填后即可检索（本次按普通聊天回答）");
    } else {
    // 检索前自动补课：缺指纹就自动启动向量化并等到就绪，然后放行
    const ready = await ensureEmbedded();
    if (!ready) {
      addProcessStep("tool-result", "知识库未就绪，本次按普通聊天回答");
    }
    if (ready) {
    ragPath = true;  // 治理①：走到检索链路（后面命中与否都算 RAG 轮——零命中
    // 也是检索过了，资料环节已给出"资料里没有"的结论，同样不需要工具）
    try {
      // ⑥1b：短追问且有历史时，先判类型再决定是否补语境——
      // FOLLOWUP（"小李呢"）→ rewriteFollowup 补全；NEW（换话题的完整问题）
      // → 只走原词+同义词检索，上文语境不进检索词（防跨话题污染）
      let query = question;
      const isShortFollowupCandidate = question.length <= 30 && chatHistory.length >= 2;
      if (isShortFollowupCandidate) {
        const kind = await classifyFollowup(question);
        reportLog("追问判定", question.slice(0, 40), "判定=" + kind);  // ⑫8 A10 判定结果留痕
        if (kind === "FOLLOWUP") {
          addProcessStep("request", "补全追问语境...");
          query = await rewriteFollowup(question);
          reportLog("检索词补全", question.slice(0, 40), "补全后=" + String(query).slice(0, 100));  // ⑫8 A11
          addProcessStep("tool-result", "检索问题: " + query.slice(0, 60));
        } else {
          addProcessStep("tool-result", "新话题，纯净检索: " + question.slice(0, 40));
        }
      }
      // 多路召回：原词 + 反馈改写两路，命中按分数排序合并去重
      addProcessStep("request", "RAG 检索知识库（第 1 路：原词）...");
      const rResp = await fetch(
        BACKEND_URL + "/api/kb/" + currentScanId + "/search?q=" + encodeURIComponent(query) + "&k=15&pool=" + (getSettings().searchTier || "100"),
        { headers: vecHeaders() }
      );
      const rData = await rResp.json();
      hits = rData.hits || [];
      // ⑥ C（2026-09-22）：SQL 命中走独立字段——拼进资料池头部
      // （sql_hits 的 seq 是 "sql:sheet" 字符串，不混 hits 防 [N] 溯源错乱）
      if (rData.sql_hits && rData.sql_hits.length) {
        hits = rData.sql_hits.concat(hits);
        addProcessStep("tool-result", "SQL 直查命中 " + rData.sql_hits.length + " 行（已并入资料）");
      }
      // 刀1 计算注入：检索响应带 calc 字段（计算题代码核算）——存供 ragSystem 拼装
      var rCalc = rData.calc || null;
      // rerank 降级提示（2026-09-19 前端闭环）：排序服务降级中——用户可见
      if (rData.rerank_degraded) {
        addProcessStep("tool-result", "⚠ 排序服务降级中——本次为粗排结果，质量可能下降（约10分钟自动恢复）");
      }
      // ⑬ 入库即用（2026-09-20）：向量未生成时关键词模式先行——用户可见
      if (rData.mode === "keyword-only" && rData.vector_pending) {
        addProcessStep("tool-result", "📦 向量生成中（0/" + rData.vector_total + "）——当前为关键词检索模式，语义检索将在向量化完成后自动生效");
      }
      try {
        const top1Score = hits.length ? hits[0].score : 0;
        const feedback = top1Score < 0.4 ? hits : null;  // rerank 分数域
        reportLog("rewrite_trigger", query, `Top1=${top1Score.toFixed(3)} ${feedback ? "<0.4 触发反馈改写" : "≥0.4 走同义词改写"}`); // ⑫5 上报
        const variant = await synonymize(query, feedback);
        if (variant) {
          addProcessStep("request", "RAG 检索（第 2 路：" + (feedback ? "反馈改写" : "同义词") + "）: " + variant.slice(0, 40));
          const vResp = await fetch(
            BACKEND_URL + "/api/kb/" + currentScanId + "/search?q=" + encodeURIComponent(variant) + "&k=15&pool=" + (getSettings().searchTier || "100"),
            { headers: vecHeaders() }
          );
          const vData = await vResp.json();
          // 同一块（file+seq）只留分数高的那路
          const seen = new Map();
          for (const h of [...hits, ...(vData.hits || [])]) {
            const key = h.file + "#" + h.seq;
            if (!seen.has(key) || h.score > seen.get(key).score) seen.set(key, h);
          }
          hits = [...seen.values()].sort((a, b) => b.score - a.score).slice(0, 15);
          // ⑫8 A7 两路合并留痕（排障可见两路怎么合的）
          reportLog("两路合并", query.slice(0, 40), "第2路=" + variant.slice(0, 30) + " | 合并后" + hits.length + "块: " + hits.slice(0, 5).map(h => (h.file || "").split(/[\\/]/).pop().slice(0, 20) + "#" + h.seq).join(", "));
        }
      } catch { /* 同义词这路挂了不影响主路 */ }
      // 刀1 计算注入（4-2B 欠账还清 2026-09-19）：检索响应带 calc 字段（计算题
      // 代码核算结果）→ 注入 ragSystem——模型引用现成结果不再心算
      const _calc = typeof rCalc !== "undefined" ? rCalc : null;
      if (hits.length > 0) {
        // 拼资料：每块带编号+出处（和后端 build_rag_context 同款格式）
        const parts = hits.map(
          (h, i) => `【资料${i + 1}】出处: ${h.file} 第${h.seq}块\n${h.text}`
        );
        // 【防幻觉第一层·检索锚定】提示词把答案空间锁进检索块：只许依据资料
        // 回答，编造被显式禁止——模型开口的素材来源被锚定
        // ⑩ Agentic 版（2026-09-22）：预检索资料仍拼入（模型可直接用），
        // 但系统提示改为自主指引——模型可调 search_kb 工具补查/深挖/拆多跳。
        // 防幻觉第一层不变：答案空间仍锚定在"预检索资料 + 工具检索结果"。
        const _ragHead = "以下是从项目知识库预检索的参考资料（可能与问题相关也可能不完整）：\n\n" + parts.join("\n\n");
        // ⑩2 两档开关（2026-09-22）：设置弹窗选检索模式——
        // · agentic（默认）：资料注入 + search_kb 工具下发，模型自决补查/拆多跳（⑩ 上线行为）
        // · pipeline（流水线）：只注入预检索资料，不下发工具——纯检索增强问答（对照档）
        if (getSettings().ragMode === "pipeline") {
          ragSystem = _ragHead +
            "\n\n请只依据以上资料回答。资料已含答案 → 直接引用（用 [1][3] 标记编号）；" +
            "资料里没有 → 明确说\"资料里没有\"，不要编造。";
        } else {
          ragSystem = _ragHead +
            "\n\n你有 search_kb 工具可自主检索知识库。使用指引：\n" +
            "· 预检索资料已含答案 → 直接引用回答（用 [1][3] 标记编号）\n" +
            "· 问了两件事（多跳）且资料只覆盖一件 → 对另一件调 search_kb 补查\n" +
            "· 资料与问题不相关 → 换用文档原文表述的关键词调 search_kb 重查\n" +
            "· 检索结果不够 → 最多再换词查 2 次\n" +
            "· 资料和检索都没有 → 明确说\"资料里没有\"，不要编造。";
        }
        if (_calc) {
          ragSystem += "\n\n" + _calc;
          addProcessStep("tool-result", "计算题：已注入代码核算结果");
        }
        addProcessStep("tool-result", `RAG 命中 ${hits.length} 块（已注入提示词）`);
        window.__ragHits = hits.slice(0, 15);  // ⑨.5 引用溯源：本轮命中块（渲染 [N] 用）
        window.__ragSources = parts.map((p, i) => p.split("\n")[0].replace(/^【资料(\d+)】出处: /, (m, n) => n));  // 编号→出处行
      } else {
        // 【防幻觉第二层·拒答闸】检索零命中 → 不退普通聊天（那是幻觉最高危
        // 路径：模型凭训练记忆自由发挥）。注入拒答指令：明确说"资料里没有"
        // 并可按模型自身知识补充但要标注非资料来源——素材边界画死。
        reportLog("reject_gate", String(query).slice(0, 60), "检索零命中→拒答闸触发 | 原问题=" + String(question).slice(0, 60));  // ⑫8 A13 拒答快照（原问题+检索词都留）
        ragSystem = "知识库检索未命中任何相关内容。请明确回答\"知识库资料里没有相关内容\"。" +
          "如你的通用知识能部分解答，可补充说明并标注\"（以下为模型通用知识，非知识库资料）\"。" +
          "禁止不标注来源就给出具体数字、编号、日期等事实性内容。";
        addProcessStep("tool-result", "RAG 未命中相关内容");
      }
    } catch (err) {
      addProcessStep("tool-result", "RAG 检索失败（按普通聊天回答）: " + err.message);
    }
    }  // if (ready) —— 补课成功才走检索；失败按普通聊天（ragSystem 保持 null）
    }  // else（向量 key 已填）—— key 未填时上面的引导分支已处理
  }

  // ---------- 4-2B 刀2：RAG 投票执行（needsVote=YES 时 3 采样多数决）----------
  // 与评测器 e2e_vote 同逻辑：第1遍 temp=0（基线）+第2、3遍 0.7（多样性），
  // 对抽出的答案值投票，多数票的完整回答作为本轮定稿。
  let voteWinnerText = null; // 非 null = 投票已出结果（主循环直接展示，不再流式）
  const _needsVote = ragPath && ragSystem && await needsVote(question);  // ⑫8 A12 判定留痕
  reportLog("算术判定", question.slice(0, 40), "判定=" + (_needsVote ? "计算题→投票" : "普通题→单答"));
  if (_needsVote) {
    addProcessStep("tool-result", "计算类问题——启用 3 次采样投票（更稳，多等几秒）...");
    const sample = async (temp) => {
      const resp = await fetch(chatApiUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
        body: JSON.stringify({ model: selectedModel,
          messages: [{ role: "system", content: ragSystem + "\n\n" + baseSystem }, ...chatHistory, userMessage],
          temperature: temp }),
      });
      const data = await resp.json();
      return (data.choices?.[0]?.message?.content || "").trim();
    };
    try {
      const extractVal = (t) => {
        const nums = [...new Set((t.match(/-?\d[\d,]*(?:\.\d+)?/g) || []).map(s => s.replace(/,/g, "")))].sort();
        return nums.length ? "N:" + nums.slice(0, 6).join(",") : t.slice(0, 40);
      };
      const a1 = await sample(0);
      const votes = [extractVal(a1)];
      const answers = [a1];
      for (const t of [0.7, 0.7]) {
        try { const a = await sample(t); answers.push(a); votes.push(extractVal(a)); } catch {}
      }
      // 多数决：≥2 票相同的值胜出；3 票各异取第 1 遍
      const cnt = {};
      votes.forEach(v => cnt[v] = (cnt[v] || 0) + 1);
      const top = Object.entries(cnt).sort((x, y) => y[1] - x[1])[0];
      let winner = a1;
      if (top[1] >= 2) {
        const wi = votes.indexOf(top[0]);
        if (wi >= 0) winner = answers[wi];
      }
      voteWinnerText = winner;
      addProcessStep("tool-result", `投票完成：${votes.length} 次采样，胜出答案已确定`);
      // ⑫8 A8 投票留痕（计算题答案对错直接查这里）
      reportLog("答题投票", question.slice(0, 40), "采样值=" + votes.join(" | ").slice(0, 120) + " → 胜出=" + String(winner).slice(0, 80));
    } catch (e) {
      addProcessStep("tool-result", "投票采样失败，回退单次直答: " + e.message);
    }
  }

  // 历史组装：system = RAG 资料（如有）+ 基础提示，再拼历史和新消息。
  // 工具调用治理②（2026-09-14）：旧文案"运行您可用的操作之一"是无条件
  // 强制——纯问答时模型被循环格式逼着硬调工具箱里唯一的天气工具（现金券
  // 事故实录）。改条件触发："如需外部信息才用"，并给显式出口（资料已有/
  // 工具无关时禁止调用直接回答）+ few-shot 正反例（负例就用当天翻车场景，
  // 教训固化成训练信号）。保留 思考/答案 标签结构（showReasoning 靠它渲染）
  const baseSystem =
    "你可以在思考、行动、观察、回答的循环中工作：用'思考'描述你对问题的分析；" +
    "如需外部信息才用'操作'运行工具——判断标准：答案在已有资料/知识里找不到，且某工具的功能正好覆盖它。" +
    "资料已给答案、或问题与所有工具无关时，禁止调用工具，直接回答。" +
    "'观察'是工具返回的结果；'答案'是分析后的结论。用中文回答，包括思考过程也用中文！\n" +
    "示例1：用户问'北京今天多少度' → 调用天气工具（问题正好需要气温数据）。\n" +
    "示例2：用户问资料里某个表的具体数值（如'XX 表里某项合计多少'）→ 资料已有就直接回答，禁止调工具。\n" +
    "示例3：资料里已有数据，用户问其中的内容 → 引用资料回答（资料已有，无需工具）。";
  const messages = [
    { role: "system", content: ragSystem ? ragSystem + "\n\n" + baseSystem : baseSystem },
    ...chatHistory,
    userMessage,
  ];
  let reasoning = ""; // 思考文字，工具多轮时接着累积
  const showReasoning = (text, withCursor) => showReasoningUpdate(text, withCursor);

  // 4-2B 刀2 短路：投票已出结果——跳过流式循环直接定稿
  if (voteWinnerText) {
    finalizeTurn(voteWinnerText);
    return;
  }

  // 最多循环 3 轮，防止模型无限调工具
  for (let round = 0; round < 5; round++) {
    addProcessStep("request", `第 ${round + 1} 轮请求...`);
    const _t0Gen = Date.now(); // ⑫8 主答案生成计时（上报用）
    // ⑫8 修正（日志里要看到问题原文）——传用户问题不是注入资料
    const _userQ = [...messages].reverse().find(m => m.role === "user");
    reportLog("LLM生成开始", _userQ ? String(_userQ.content).slice(0, 60) : "普通聊天", selectedModel);
    const response = await fetch(chatApiUrl(), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${chatApiKey()}`,
      },
      body: JSON.stringify({
        model: selectedModel,
        messages,
        // ⑩2 Agentic 模式（2026-09-22）：RAG 轮也下发 tools——
        // 含 search_kb（模型自主检索）。治理① 的教训是"天气工具误调"，
        // 不是"工具机制本身有问题"——现在 RAG 场景的工具是检索本身，
        // 模型调它=干活不是误调。普通聊天保留天气演示。
        // ⑩2 两档：pipeline 档 RAG 轮不下发任何工具（纯资料注入——对照档）；
        // agentic 档下发 search_kb（⑩ 上线行为）；普通聊天始终全量工具（天气演示）
        ...(ragPath
            ? (getSettings().ragMode === "pipeline"
                ? {}  // 流水线档：无 tools——模型只依据注入资料回答
                : { tools: [tools.find(t => t.function?.name === "search_kb")].filter(Boolean) })
            : { tools: tools.filter(t => t.function?.name !== "search_kb") }),  // 十七修：未勾 RAG 不发检索工具（search_kb 混在"全部"里——天气演示本意）
        // 温度分档：RAG 命中(有资料)时 0.3 忠实转述防幻觉；普通聊天 0.7 保持自然（原 2 极端高温导致 Hy4 乱码，已实测）
        temperature: ragSystem ? 0.3 : 0.7,
        stream:true,
      }),
      signal: chatAbort.signal, // 停止按钮掐的就是这个信号（AbortController 在 callChat 里创建）
    });

    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`HTTP ${response.status}: ${errText}`);
    }

    // ---------- 流式读取核心 ----------
    // response.body.getReader() 反复 read()，每次拿到一小块字节（Uint8Array）；
    // TextDecoder 解码成字符串（一个汉字 UTF-8 占 3 字节，stream:true 让被劈开
    // 的汉字攒在解码器内部等下个 chunk，不出乱码）
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");

    let answer = "";          // 累积最终回答
    let finishReason = null;  // stop=说完了，tool_calls=要调工具
    const toolCalls = [];     // 按分片拼装的工具调用，流结束才是完整对象
    // buffer：网络分块可能把一行 data: 劈成两半，半截 JSON 解析不了，
    // 先攒 buffer 凑成完整行再处理
    let buffer = "";
    // 流式增量渲染缓存：已完整的段落解析一次就缓存固化，只有正在长的
    // 尾块每次重新解析——豆包同款"增量渲染"的核心
    let streamStableRendered = "";
    let streamStableHtml = "";
    // 停止收尾注册：闭包住本轮的 answer/reasoning。点"停止"→ abort() →
    // read() 抛 AbortError 冲出循环 → callChat catch 调这个回调定稿。
    // 每轮循环重新注册（上一轮的对象已废弃）
    chatAbort.onstop = () => {
      // 去掉流式光标；已输出的半截按 markdown 定稿渲染
      if (reasoning) showReasoning(reasoning, false);
      const partial = answer.replace(/<think>[\s\S]*?<\/think>/, "").trim();
      if (partial) {
        addProcessStep("final", "已手动停止，保留已生成的部分");
        finalizeTurn(partial);
      } else {
        // 一个字没出就停：不留空气泡，也不进历史（历史里只有 user 的话
        // 下一轮消息序列就断了）
        addProcessStep("final", "已手动停止");
        showResult("（已停止生成）", false);
      }
    };
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE：每条消息一行（data: {...} / data: [DONE]）。
      // pop() 取出的最后一段后面没有换行符，是"半行"，放回 buffer 等拼齐
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;

        const payload = trimmed.slice(5).trim();
        if (payload === "[DONE]") continue; // 流结束标记，不是 JSON

        let chunk;
        try {
          chunk = JSON.parse(payload);
        } catch {
          continue; // 容错：非法 JSON 行跳过，别让页面崩
        }

        // delta = 这个分片新带来的一小段内容
        const delta = chunk.choices?.[0]?.delta;
        if (chunk.choices?.[0]?.finish_reason) {
          finishReason = chunk.choices[0].finish_reason;
        }

        // 思考分片：先思考、再正式回答（也可能没有思考段）
        if (delta?.reasoning_content) {
          reasoning += delta.reasoning_content;
          showReasoning(reasoning, true);
        }

        // 情况 1：普通文字分片 —— 分拣 <think> + 增量渲染上屏
        if (delta?.content) {
          answer += delta.content;
          // 分拣：<think> 段进思考面板，其余进正文
          const openIdx = answer.indexOf("<think>");
          let displayText = answer;
          if (openIdx !== -1) {
            const closeIdx = answer.indexOf("</think>");
            if (closeIdx === -1) {
              // 只有开头标签：思考还在流——全部进思考面板，正文暂不显示
              showReasoning(answer.slice(openIdx + 7), true);
              continue;
            } else {
              // 闭合标签到了：思考进面板，正文 = 标签之后的部分
              showReasoning(answer.slice(openIdx + 7, closeIdx), false);
              displayText = answer.slice(closeIdx + 8);
            }
          }
          // 按空行切块：[:-1] 是已完整的块（解析一次就缓存），[最后] 是在长的尾块
          const blocks = displayText.split("\n\n");
          const stablePart = blocks.slice(0, -1).join("\n\n");
          const tailPart = blocks[blocks.length - 1] || "";
          if (stablePart !== streamStableRendered) {
            streamStableHtml = DOMPurify.sanitize(marked.parse(stablePart));
            streamStableHtml = renderCitations(streamStableHtml);  // ⑨.5 引用溯源
            streamStableRendered = stablePart;
          }
          const tailHtml = tailPart ? DOMPurify.sanitize(marked.parse(tailPart)) : "";
          // 固化HTML + 尾块HTML + 闪烁光标
          curAIMsg.body.innerHTML = streamStableHtml + tailHtml + '<span class="cursor-blink">▌</span>';
          chatFlow.scrollTop = chatFlow.scrollHeight;
        }

        // 情况 2：工具调用分片 —— 服务器把一次调用拆成多个分片陆续发，
        // 等流结束才能拼出完整对象
        if (delta?.tool_calls?.length) {
          for (const tc of delta.tool_calls) {
            const slot = toolCalls[tc.index] ||= { id: "", name: "", arguments: "" };
            slot.id += tc.id ?? "";
            slot.name += tc.function?.name ?? "";
            slot.arguments += tc.function?.arguments ?? "";
          }
        }
      }
    }
    // 流读完收尾：① decoder.decode() flush 解码器内部扣着的最后几个字节
    // ② buffer 里的"半行"流结束了说明是完整的最后一行，补解析一次
    buffer += decoder.decode();
    if (buffer.trim().startsWith("data:")) {
      const payload = buffer.trim().slice(5).trim();
      if (payload && payload !== "[DONE]") {
        try {
          const chunk = JSON.parse(payload);
          const delta = chunk.choices?.[0]?.delta;
          if (chunk.choices?.[0]?.finish_reason) {
            finishReason = chunk.choices[0].finish_reason;
          }
          if (delta?.content) {
            answer += delta.content;
            // flush 块同样走分块渲染
            const blocks = answer.split("\n\n");
            const stablePart = blocks.slice(0, -1).join("\n\n");
            const tailPart = blocks[blocks.length - 1] || "";
            if (stablePart !== streamStableRendered) {
              streamStableHtml = DOMPurify.sanitize(marked.parse(stablePart));
              streamStableHtml = renderCitations(streamStableHtml);  // ⑨.5 引用溯源
              streamStableRendered = stablePart;
            }
            const tailHtml = tailPart ? DOMPurify.sanitize(marked.parse(tailPart)) : "";
            curAIMsg.body.innerHTML = streamStableHtml + tailHtml + '<span class="cursor-blink">▌</span>';
          }
          if (delta?.reasoning_content) {
            reasoning += delta.reasoning_content;
            showReasoning(reasoning, true);
          }
          if (delta?.tool_calls?.length) {
            for (const tc of delta.tool_calls) {
              const slot = toolCalls[tc.index] ||= { id: "", name: "", arguments: "" };
              slot.id += tc.id ?? "";
              slot.name += tc.function?.name ?? "";
              slot.arguments += tc.function?.arguments ?? "";
            }
          }
        } catch {}
      }
    }
    // 去掉思考面板的假光标（下一步无论调工具还是给答案都适用）
    if (reasoning) showReasoning(reasoning, false);


    // 情况 1：模型想调用工具——必须把带 tool_calls 的 assistant 消息原样追加回历史
    if (finishReason === "tool_calls" || toolCalls.length) {
      messages.push({
        role: "assistant",
        tool_calls: toolCalls.map((tc) => ({
          id: tc.id,
          type: "function",
          function: { name: tc.name, arguments: tc.arguments },
        })),
        content: answer || null,
      });

      // 逐个执行模型要求的工具调用；工具名不存在时返回错误说明让模型自行处理
      for (const toolCall of toolCalls) {
        const fnName = toolCall.name;
        const fnArgs = JSON.parse(toolCall.arguments);
        addProcessStep("tool", `模型请求调用: ${fnName}(${toolCall.arguments})`);
        let result;
        if (fnName === "getTemperature") {
          result = await getTemperature(fnArgs.latitude, fnArgs.longitude);
        } else if (fnName === "search_kb") {
          // ⑩3 Agentic 检索执行器：调后端 /search，结果格式化为资料文本
          // 返回给模型（模型看完决定够不够/要不要换词再查）
          try {
            const sq = (fnArgs.query || "").slice(0, 200);
            addProcessStep("tool-result", "Agentic 检索: " + sq.slice(0, 40));
            const sResp = await fetch(
              BACKEND_URL + "/api/kb/" + currentScanId + "/search?q=" + encodeURIComponent(sq) + "&k=8&pool=" + (getSettings().searchTier || "100"),
              { headers: vecHeaders() }
            );
            const sData = await sResp.json();
            const sHits = sData.hits || [];
            const sqlH = sData.sql_hits || [];
            if (!sHits.length && !sqlH.length) {
              result = "检索零命中——尝试换用文档原文表述的关键词（如表格列名/文件名/编号）";
            } else {
              let out = sHits.slice(0, 8).map((h, i2) => {
                // ⑩ 修3（2026-09-22）：含查询词的行前置——治"目标行被 400 字截断埋掉"
                const sqTokens = sq.split(/[\s，。、,]+/).filter(w => w.length >= 2);
                const full = h.text || "";
                const hitLines = full.split("\n").filter(line =>
                  sqTokens.some(t => line.includes(t)) && line.trim().startsWith("|")
                ).slice(0, 3);
                const head = hitLines.length ? "⚡ 命中行:\n" + hitLines.join("\n") + "\n---\n" : "";
                return `【资料${i2 + 1}】出处: ${(h.file || "").split(/[\\/]/).pop()} 第${h.seq}块\n${head}${full.slice(0, 400)}`;
              }).join("\n\n");
              if (sqlH.length) {
                out += "\n\n" + sqlH.map(h => `【SQL直查】${(h.text || "").slice(0, 300)}`).join("\n");
              }
              result = out.slice(0, 6000);
            }
          } catch (e) {
            result = "检索失败: " + e.message + "——可重试或换关键词";
          }
        } else {
          result = `未知工具: ${fnName}`;
        }
  // 2026-09-23 修（用户报：字面 \n 不换行全挤一行）：JSON.stringify 把换行
  // 转义成 "\n" 两字面字符——截断后还原转义，调用过程恢复可读换行
  {
    let _disp = JSON.stringify(result);
    if (_disp && _disp.length > 500) _disp = _disp.slice(0, 500) + "…(截断)";
    _disp = _disp.replace(/\\n/g, "\n");
    addProcessStep("tool-result", `工具返回: ${_disp}`);
  }
        messages.push({
          role: "tool",
          tool_call_id: toolCall.id,
          content: JSON.stringify(result),
        });
      }

      continue; // 带着工具结果发起下一轮请求
    }

    // ⑫8 主答案生成完成上报（日志里要看到最终答案）
    // 修：q=问题原文（原传 answer 被思考头污染）；detail=答案正文前 200 字
    const _answerClean = String(answer).replace(/<think>[\s\S]*?<\/think>/g, "").trim();
    reportLog("LLM生成完成", _userQ ? String(_userQ.content).slice(0, 60) : "", selectedModel + " " + (Date.now() - _t0Gen) + "ms | 答案: " + _answerClean.slice(0, 1000));
    // ⑫8 B2 明细档答案段补写（两段式第二段——合并进同一份 detail JSON）
    try { fetch(BACKEND_URL + "/api/detail/append", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trace_id: feTraceId, model: selectedModel, latency_ms: Date.now() - _t0Gen, text: _answerClean }) }).catch(() => {}); } catch {}
    // 情况 2：模型给出最终文字回答
    if (!answer) {
      throw new Error("流式响应中没有收到任何内容"); // 整轮流读完一个字没有 = 异常
    }
    // 兜底剥掉 <think>（万一闭合标签在最后一个分片才到）
    let finalAnswer = answer.replace(/<think>[\s\S]*?<\/think>/, "").trim();
    // ---- 第四层防幻觉审计（2026-09-18 ）：线上生成后验 ----
    // 触发条件（低成本，不是每题都审）：RAG 路且模型答案里出现了
    if (ragPath && hits && hits.length > 0) {
      reportLog("audit_trigger", question, `答案${finalAnswer.length}字，块${hits.length}个——进入数字溯源审计`); // ⑫5 上报
      try {
        finalAnswer = await auditGrounding(finalAnswer, hits, question);
      } catch { /* 审计挂了不拦答案——降级为不标注 */ }
    }
    addProcessStep("final", "模型给出最终回答");
    finalizeTurn(finalAnswer); // 记账 + 渲染（和"手动停止"共用一套收尾）
    return finalAnswer;
  }
  // ⑩ 修1（2026-09-22 用户拍板）：5 轮到限【降级作答】不报错——
  // 强制拿已有资料让模型给部分答案（业界 Agentic 标准降级策略：
  // 部分答案好过报错）
  addProcessStep("tool-result", "⚠ 已达 5 轮上限——强制基于已有资料作部分回答");
  try {
    const degradedResp = await fetch(chatApiUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
      body: JSON.stringify({
        model: selectedModel,
        messages: [...messages, { role: "user", content: "已达检索轮次上限。请基于以上已有资料给出最终回答：已确认的部分直接答，未找到的部分明确说明'资料中未找到'。不要再调用工具。" }],
        temperature: 0.3,
      }),
      signal: chatAbort.signal,
    });
    const dd = await degradedResp.json();
    const degradedAnswer = (dd.choices?.[0]?.message?.content || "").trim();
    if (degradedAnswer) {
      reportLog("Agentic降级作答", question.slice(0, 40), "5轮到限，强制部分回答");
      finalizeTurn(degradedAnswer);
      return degradedAnswer;
    }
  } catch { /* 降级也挂了才走下面的真报错 */ }
  throw new Error("工具调用超过 5 轮且降级作答失败");
}

// ---- 第四层防幻觉审计实现 ----
// 信号：答案中的数字/金额（幻觉高危物）在检索块文本里字面找不到
// 动作：LLM 审计支撑性；不可支撑 → 答案尾部标注"未经资料证实"（不删答案，
// 用户自行判断——企业场景宁可标注也不静默改写，审计过程透明）
async function auditGrounding(answer, hits, question) {
  // 1) 抽答案里的数字/金额（3 位以上数字，含小数）——幻觉最常编的就是数
  const nums = (answer.match(/\d[\d,]*\.?\d*/g) || [])
    .map(s => s.replace(/[,，]/g, ""))
    .filter(s => s.replace(/\./g, "").length >= 3);
  if (nums.length === 0) return answer; // 无数字答案不审（文本幻觉率低且有出处标注兜底）
  // 2) 字面检查：数字在块文本里找得到吗
  const corpus = hits.map(h => h.text).join("\n");
  const missing = nums.filter(n => !corpus.includes(n) && !corpus.includes(n.replace(/\./g, "")));
  if (missing.length === 0) return answer; // 全部有出处——不审
  // 3) LLM 审计：这些数字是否可由检索块支撑（可能换算/加总/等价写法）
  addProcessStep("tool-result", `答案核验：${missing.length} 个数字正在核对资料出处...`);
  const ctx = hits.slice(0, 8).map((h, i) => `【资料${i+1}】${h.text.slice(0, 400)}`).join("\n");
  const resp = await fetch(chatApiUrl(), {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
    body: JSON.stringify({
      model: selectedModel,
      temperature: 0, max_tokens: 10,
      messages: [
        { role: "system", content: "你是答案审计员。回答里的数字若能从参考资料的原文、换算或加总得出答 YES，完全找不到依据答 NO。只能答 YES 或 NO。" },
        { role: "user", content: `问题: ${question}\n回答: ${answer}\n待验证数字: ${missing.join("、")}\n\n参考资料:\n${ctx}` }
      ]
    })
  });
  const data = await resp.json();
  const verdict = (data.choices?.[0]?.message?.content || "").trim().toUpperCase();
  if (verdict.startsWith("YES")) {
    addProcessStep("tool-result", "答案核验通过：数字均有资料出处");
    return answer;
  }
  addProcessStep("tool-result", "核验提醒：部分数字未找到资料出处，已在答案中标注");
  return answer + "\n\n⚠️ 注：以上回答中的部分数字在参考资料中未找到依据，请核实后再使用。";
}


function showResult(text, isError, isStreaming = false) {
  if (!curAIMsg) return;
  if (isError) {
    curAIMsg.body.textContent = text; // 错误保持纯文本（不是 markdown，不渲染更醒目）
    curAIMsg.body.classList.add("error");
    return;
  }
  curAIMsg.body.classList.remove("error");
  if (isStreaming) {
    curAIMsg.body.textContent = text; // 流式中纯文本直出（快、不闪）
  } else {
    // 最终渲染走 markdown 管线：marked 解析 + DOMPurify 消毒（防 XSS）
    let html = DOMPurify.sanitize(marked.parse(text));
    html = renderCitations(html);  // ⑨.5 终稿也要 [N] 可点击（advisory 实锤：只在流式帧渲染的话收尾即失效）
    curAIMsg.body.innerHTML = html;
  }
  chatFlow.scrollTop = chatFlow.scrollHeight; // 跟随滚动
}

// 调用过程：往折叠块里加一条步骤
function addProcessStep(type, text) {
  if (!curAIMsg) return;
  const step = document.createElement("div");
  step.className = "step " + type;
  step.textContent = text;
  curAIMsg.procBody.appendChild(step);
  curAIMsg.procBody.scrollTop = curAIMsg.procBody.scrollHeight;
  // 有了步骤自动展开折叠块（想看再手动收起）
  if (curAIMsg.procBody.children.length === 1) curAIMsg.procBlock.classList.add("open");
}

// 旧接口兼容（callChat 里可能还有调用）：空实现
function resetProcessPanel() {}

// 思考过程：更新折叠块文字（流式期间反复调用）
function showReasoningUpdate(text, withCursor) {
  if (!curAIMsg) return;
  curAIMsg.thinkBody.textContent = text + (withCursor ? "▌" : "");
  curAIMsg.thinkBody.scrollTop = curAIMsg.thinkBody.scrollHeight;
  // 思考一开始就展开，正文出来后自动收起
  if (withCursor) curAIMsg.thinkBlock.classList.add("open");
  else if (text) curAIMsg.thinkBlock.classList.remove("open");
}

function showError(message) {
  if (curAIMsg) {
    showResult("出错了: " + message, true); // 错误写进气泡（红字）
    return;
  }
  // 没有正在输出的气泡（如校验失败）：alert 更直接
  alert(message);
}
function setLoading(isLoading) {
  // C 方案修复（用户实锤按钮卡"停止"）：全局单按钮但每会话独立流——
  // 状态改为跟随「当前会话是否有流在跑」，切会话时也重算（switchConversation 调）。
  // 旧链路径兼容：旧链不走注册表——本会话在注册表无痕时退回 isLoading 参数（旧链 true/false 语义不变）。
  let _active;
  if (curConvId && _STREAMING[curConvId]) _active = true;           // 当前会话有活流
  else if (curConvId && curConvId in _STREAMING) _active = false;   // 本会话注册过又清了=流刚结束
  else _active = !!isLoading;                                        // 旧链/未注册路径：按调用方参数
  sendBtn.classList.toggle("stop", _active);
  sendBtn.textContent = _active ? "⏹ 停止" : "发送";
}
// 输入框 + 绝对定位列表，纯手写下拉。不用原生 <select>：它不能输入搜索，
// 65 个模型没法找。交互：点开全量列表 → 输入过滤 → ↑↓/Enter/Esc/点击选中
// ============================================================
let allModels = [];       // 全量模型名缓存（过滤只用内存，不重新请求）
let selectedModel = null; // 定选的模型 id（输入框文字可随便改，发 API 用这个值）
let activeIndex = -1;     // 键盘 ↑↓ 高亮的列表项下标

// 依据当前输入算出该显示的模型列表（多词 AND 模糊匹配）
function getMatchedModels() {
  // 输入恰好等于已定选的模型名时不过滤——否则 chooseModel 把模型名写进
  // 输入框后，列表会被拿模型名当搜索词过滤得只剩它自己
  if (modelSelect.value === selectedModel) return allModels;

  const keywords = modelSelect.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (keywords.length === 0) return allModels;
  // 每个关键字都得命中（顺序不限）：输 "flash deepseek" 也能匹配 DeepSeek-V4-Flash
  return allModels.filter((id) =>
    keywords.every((kw) => id.toLowerCase().includes(kw))
  );
}

function renderModelList() {
  const matched = getMatchedModels();
  modelList.innerHTML = "";
  if (matched.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "无匹配模型";
    modelList.appendChild(li);
    return;
  }
  matched.forEach((id, i) => {
    const li = document.createElement("li");
    li.textContent = id;
    if (id === selectedModel) li.textContent = "✓ " + id; // 定选项加 ✓ 标记
    li.addEventListener("mousedown", (e) => {
      // mousedown 而非 click：click 会先失焦触发 blur 关列表，点击落空
      e.preventDefault();
      // stopPropagation：chooseModel 重画列表会把 li 移出 DOM，冒泡到 document
      // 时已无父节点，会被误判成"点了外部"而收起列表
      e.stopPropagation();
      chooseModel(id);
    });
    modelList.appendChild(li);
  });
  updateActiveItem();
}

// 高亮同步：activeIndex 对应的 li 加 .active 并滚进可视区
function updateActiveItem() {
  const items = modelList.querySelectorAll("li:not(.empty)");
  items.forEach((li, i) => li.classList.toggle("active", i === activeIndex));
  if (activeIndex >= 0 && items[activeIndex]) {
    items[activeIndex].scrollIntoView({ block: "nearest" });
  }
}

// 定选某个模型。不收起列表——选中后仍显示全部模型，方便直接再换一个
function chooseModel(id) {
  selectedModel = id;
  // 换模型 = 开新对话（不同模型的上下文不通用，历史续过去会串味）
  if (chatHistory.length > 0) {
    newConversation();
    addProcessStep("user", `已切换模型: ${id}（历史已清空）`);
  }
  modelSelect.value = id;
  renderModelList(); // 重画更新 ✓ 位置，列表保持展开
}

function openModelList() {
  renderModelList();
  modelList.classList.add("open");
}

function closeModelList() {
  modelList.classList.remove("open");
  activeIndex = -1;
}

// 输入即过滤；已定选但用户改了字，视为未定选
modelSelect.addEventListener("input", () => {
  selectedModel = null;
  openModelList();
});
modelSelect.addEventListener("focus", openModelList);
// 失焦收起（mousedown 里 preventDefault 保证了点列表项不会先触发这里）
modelSelect.addEventListener("blur", closeModelList);

// 键盘导航：↑↓ 移高亮、Enter 定选、Esc 关闭
modelSelect.addEventListener("keydown", (e) => {
  if (!modelList.classList.contains("open")) return;
  const matched = getMatchedModels();
  if (e.key === "ArrowDown") {
    e.preventDefault(); // 阻止光标跳到输入框末尾
    activeIndex = (activeIndex + 1) % matched.length;
    updateActiveItem();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    activeIndex = (activeIndex - 1 + matched.length) % matched.length;
    activeIndex = Math.max(0, activeIndex); // -1 时回到第一项，简单化处理
    updateActiveItem();
  } else if (e.key === "Enter") {
    e.preventDefault(); // 别让 Enter 冒泡去触发表单/发送逻辑
    if (matched[activeIndex]) chooseModel(matched[activeIndex]);
  } else if (e.key === "Escape") {
    closeModelList();
  }
});

// 点下拉区域外部收起（标准下拉行为）
// 用 closest 而不是 contains：chooseModel 重画列表会把点中的 <li> 从 DOM 移除，
// 脱离文档的节点 contains 判不出来，closest 沿祖先链查找依然能匹配
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest(".combobox")) closeModelList();
});

// ============================================================
// 模型列表：从代理拉取可用的对话模型
// ============================================================
async function loadModels() {
  modelSelect.value = "";
  modelSelect.placeholder = "正在拉取模型列表...";
  selectedModel = null;

  if (!chatApiKey()) {
    modelSelect.placeholder = "请先点「⚙ 设置」填入 API Key";
    return;
  }

  try {
    const resp = await fetch(chatModelsUrl(), {
      headers: { Authorization: `Bearer ${chatApiKey()}` },
    });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
    }
    const data = await resp.json();

    allModels = data.data.map((m) => m.id);
    chooseModel(allModels[0]); // 默认定选第一个模型，发送按钮随时可用
    modelSelect.placeholder = "点击选择，或输入关键字筛选";
  } catch (err) {
    allModels = [];
    modelSelect.placeholder = "拉取失败: " + err.message;
  }
}

loadModels();

// ============================================================
// 设置弹窗（BYOK）：聊天 key 必填；嵌入 key 仅 RAG 用；重排可独立配置。
// sameKey 联动：勾选后嵌入输入区折叠，嵌入侧自动跟随聊天侧配置；
// sameRerankKey 联动：勾选后重排输入区折叠，重排侧自动跟随嵌入侧配置
// ============================================================
const settingsModal = document.getElementById("settingsModal");
const settingsModalClose = document.getElementById("settingsModalClose");
const settingsBtn = document.getElementById("settingsBtn");
const chatUrlInput = document.getElementById("chatUrlInput");
const chatKeyInput = document.getElementById("chatKeyInput");
const vecUrlInput = document.getElementById("vecUrlInput");
const vecKeyInput = document.getElementById("vecKeyInput");
const embedModelInput = document.getElementById("embedModelInput");
const sameKeyCheck = document.getElementById("sameKeyCheck");
const vectorFields = document.getElementById("vectorFields");
const chatTestBtn = document.getElementById("chatTestBtn");
const chatTestResult = document.getElementById("chatTestResult");
const vecTestBtn = document.getElementById("vecTestBtn");
const vecTestResult = document.getElementById("vecTestResult");
const rerankUrlInput = document.getElementById("rerankUrlInput");
const rerankKeyInput = document.getElementById("rerankKeyInput");
const rerankModelInput = document.getElementById("rerankModelInput");
const sameRerankKeyCheck = document.getElementById("sameRerankKeyCheck");
const rerankFields = document.getElementById("rerankFields");
const rerankTestBtn = document.getElementById("rerankTestBtn");
const rerankTestResult = document.getElementById("rerankTestResult");
const settingsSaveBtn = document.getElementById("settingsSaveBtn");
const settingsCancelBtn = document.getElementById("settingsCancelBtn");

function openSettings() {
  const s = getSettings();
  chatUrlInput.value = s.chatUrl;
  chatKeyInput.value = s.chatKey;
  vecUrlInput.value = s.vecUrl;
  vecKeyInput.value = s.vecKey;
  embedModelInput.value = s.embedModel;
  sameKeyCheck.checked = s.sameKey;
  vectorFields.style.display = s.sameKey ? "none" : "block";
  rerankUrlInput.value = s.rerankUrl;
  rerankKeyInput.value = s.rerankKey;
  rerankModelInput.value = s.rerankModel;
  sameRerankKeyCheck.checked = s.sameRerankKey;
  rerankFields.style.display = s.sameRerankKey ? "none" : "block";
  // ⑩2 检索模式回填
  const _mode = s.ragMode === "pipeline" ? "pipeline" : "agentic";
  document.getElementById("ragModeAgentic").checked = _mode === "agentic";
  document.getElementById("ragModePipeline").checked = _mode === "pipeline";
  // 通用性改造③：检索档位回填
  const _tier = ["60", "100", "150"].includes(s.searchTier) ? s.searchTier : "100";
  document.getElementById("tierFast").checked = _tier === "60";
  document.getElementById("tierBalanced").checked = _tier === "100";
  document.getElementById("tierDeep").checked = _tier === "150";
  // D-6.0a：对话引擎回填（sse=新链默认 / legacy=旧链对照）
  document.getElementById("engineSse").checked = s.engine !== "legacy";
  document.getElementById("engineLegacy").checked = s.engine === "legacy";
  chatTestResult.textContent = "";
  vecTestResult.textContent = "";
  rerankTestResult.textContent = "";
  settingsModal.classList.add("open");
}

function closeSettings() { settingsModal.classList.remove("open"); }

settingsBtn.addEventListener("click", openSettings);
settingsModalClose.addEventListener("click", closeSettings);
settingsCancelBtn.addEventListener("click", closeSettings);
settingsModal.addEventListener("click", (e) => {
  if (e.target === settingsModal) closeSettings();
});

// 联动勾选：勾上折叠嵌入区（保存时 vecUrl/vecKey 自动跟聊天侧）
sameKeyCheck.addEventListener("change", () => {
  vectorFields.style.display = sameKeyCheck.checked ? "none" : "block";
});

// 联动勾选：勾上折叠重排区（保存时 rerankUrl/rerankKey 自动跟嵌入侧）
sameRerankKeyCheck.addEventListener("change", () => {
  rerankFields.style.display = sameRerankKeyCheck.checked ? "none" : "block";
});

// 测试连接：拉一下模型列表，通了就是 key/URL 都对
async function testConnection(url, key, resultEl, label) {
  resultEl.className = "settings-test-result";
  resultEl.textContent = "测试中...";
  try {
    const resp = await fetch(url.replace(/\/+$/, "") + "/models", {
      headers: { Authorization: `Bearer ${key}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const n = (data.data || []).length;
    resultEl.classList.add("ok");
    resultEl.textContent = `✓ 连接成功（${n} 个模型可用）`;
  } catch (err) {
    resultEl.classList.add("err");
    resultEl.textContent = `✗ ${label}失败: ${err.message}（检查地址和 Key）`;
  }
}

chatTestBtn.addEventListener("click", () =>
  testConnection(chatUrlInput.value.trim(), chatKeyInput.value.trim(), chatTestResult, "聊天"));
vecTestBtn.addEventListener("click", () =>
  testConnection(vecUrlInput.value.trim(), vecKeyInput.value.trim(), vecTestResult, "嵌入"));
rerankTestBtn.addEventListener("click", () =>
  testConnection(rerankUrlInput.value.trim(), rerankKeyInput.value.trim(), rerankTestResult, "重排"));

// 保存：校验聊天 key 必填；sameKey 时向量侧跟随
settingsSaveBtn.addEventListener("click", () => {
  const chatUrl = chatUrlInput.value.trim() || DEFAULT_CHAT_URL;
  const chatKey = chatKeyInput.value.trim();
  if (!chatKey) {
    chatTestResult.className = "settings-test-result err";
    chatTestResult.textContent = "✗ 聊天 Key 必填（没有就去硅基流动注册，免费）";
    chatKeyInput.focus();
    return;
  }
  const sameKey = sameKeyCheck.checked;
  const vecUrl = sameKey ? chatUrl : (vecUrlInput.value.trim() || DEFAULT_VEC_URL);
  const vecKey = sameKey ? chatKey : vecKeyInput.value.trim();
  const embedModel = embedModelInput.value.trim() || DEFAULT_EMBED_MODEL;
  // 重排：勾了"同嵌入"就跟随嵌入侧；没勾但漏填某一项也回退（不留半残配置）
  const sameRerankKey = sameRerankKeyCheck.checked;
  const rerankUrl = sameRerankKey ? vecUrl : (rerankUrlInput.value.trim() || DEFAULT_RERANK_URL);
  const rerankKey = sameRerankKey ? vecKey : (rerankKeyInput.value.trim() || vecKey);
  const rerankModel = rerankModelInput.value.trim() || DEFAULT_RERANK_MODEL;
  const _ragMode = document.getElementById("ragModePipeline").checked ? "pipeline" : "agentic";
  const _tier = document.querySelector('input[name="searchTierRadio"]:checked');
  // D-6.0a：对话引擎存档（sse/legacy）
  const _engine = document.getElementById("engineLegacy").checked ? "legacy" : "sse";
  saveSettings({ chatUrl, chatKey, vecUrl, vecKey, sameKey, embedModel,
                 rerankUrl, rerankKey, rerankModel, sameRerankKey,
                 ragMode: _ragMode,
                 searchTier: _tier ? _tier.value : "100", engine: _engine });
  closeSettings();
  updateSettingsBtnState();
  loadModels(); // 地址/key 可能换了，模型列表重拉
});

// 设置按钮状态：已填 key 显示绿色（一眼看出配置过）
function updateSettingsBtnState() {
  settingsBtn.classList.toggle("has-key", !!chatApiKey());
  settingsBtn.textContent = chatApiKey() ? "⚙ 已配置" : "⚙ 设置";
}
updateSettingsBtnState();


// ═══ ⑫7d 服务健康面板（2026-09-20 监控三层·前端层）═══
function renderHealthPanel(stats) {
  const el = document.getElementById("healthPanel");
  if (!el) return;
  const up = stats.uptime_s ? Math.floor(stats.uptime_s / 60) : 0;
  const eps = stats.endpoints || {};
  const evs = stats.events || {};
  const alerts = stats.alerts || [];
  const hasRed = alerts.some(a => a.level === "red");
  const hasYellow = alerts.some(a => a.level === "yellow");
  const color = hasRed ? "#e74c3c" : hasYellow ? "#f39c12" : "#27ae60";
  const stateText = hasRed ? "🔴 有故障" : hasYellow ? "🟡 有降级" : "🟢 全正常";
  let html = `<div style="font-weight:600;color:${color};margin-bottom:6px;">${stateText} · 运行 ${up} 分钟</div>`;
  // 六指标卡片
  const cards = [];
  for (const [ep, d] of Object.entries(eps)) {
    cards.push(`<div style="display:inline-block;margin:2px 8px 2px 0;padding:4px 10px;background:#f5f6f7;border-radius:6px;">
      <b>${ep}</b>：${d.count} 次 · p50 ${d.p50_ms}ms · 失败 ${d.error_rate}%</div>`);
  }
  cards.push(`<div style="display:inline-block;margin:2px 8px 2px 0;padding:4px 10px;background:#f5f6f7;border-radius:6px;">
    <b>rerank 降级</b>：${evs.rerank_degraded || 0} 次</div>`);
  cards.push(`<div style="display:inline-block;margin:2px 8px 2px 0;padding:4px 10px;background:#f5f6f7;border-radius:6px;">
    <b>断路器跳闸</b>：${evs.breaker_trip || 0} 次</div>`);
  if (stats.kb && stats.kb.vectors) {
    cards.push(`<div style="display:inline-block;margin:2px 8px 2px 0;padding:4px 10px;background:#f5f6f7;border-radius:6px;">
      <b>知识库</b>：${stats.kb.vectors} 向量 · 扫描 ${stats.kb.last_scan || "?"}</div>`);
  }
  html += cards.join("");
  // ⑫7d 最近 20 条 ERROR 速览
  if (stats.recent_errors && stats.recent_errors.length) {
    html += `<div style="margin-top:8px;font-weight:600;">最近 ERROR（${stats.recent_errors.length} 条）：</div>` +
      stats.recent_errors.map(e =>
        `<div style="color:#c0392b;font-size:11px;">[${e.ts}] ${e.module}: ${e.msg}</div>`).join("");
  }
  if (alerts.length) {
    html += `<div style="margin-top:8px;">${alerts.map(a =>
      `<div style="color:${a.level === "red" ? "#e74c3c" : "#f39c12"};">${a.level === "red" ? "🔴" : "🟡"} ${a.msg}</div>`).join("")}</div>`;
  }
  el.innerHTML = html;
}
// ═══ ⑫8 日志面板（2026-09-21 问错了能查全链路日志） ═══
function renderLogLine(obj) {
  const src = (obj._src || "?").toUpperCase();
  const ts = (obj.ts || "").slice(11);
  const level = obj.level || "-";
  const color = level === "ERROR" ? "#f87171" : (level === "WARNING" ? "#fbbf24" : "#94a3b8");
  const tid = obj.trace_id && obj.trace_id !== "-" ? " [" + obj.trace_id + "]" : "";
  const msg = obj.raw || obj.msg || "";
  let extra = "";
  for (const k of ["q", "detail", "exc", "hint", "file", "chunks", "status", "mode", "top5", "top1_preview", "top_files", "kw_count", "final_k", "lat_ms", "event", "method", "path", "duration_ms", "model", "latency_ms", "tokens", "chars", "variants", "hit_terms", "verdict", "sheet", "weights", "mq_count", "pool"]) {
    if (obj[k] !== undefined && obj[k] !== "" && obj[k] !== null) extra += " " + k + "=" + String(obj[k]).slice(0, 120);
  }
  const div = document.createElement("div");
  div.style.cssText = "border-bottom:1px solid #1e293b;padding:3px 0";
  const sT = document.createElement("span"); sT.style.color = "#60a5fa"; sT.textContent = ts + tid + " ";
  const sS = document.createElement("span"); sS.style.color = "#818cf8"; sS.textContent = src + " ";
  const sL = document.createElement("span"); sL.style.color = color; sL.textContent = level + " ";
  const sM = document.createElement("span"); sM.textContent = String(msg).slice(0, 160);
  const sE = document.createElement("span"); sE.style.color = "#64748b"; sE.textContent = extra;
  div.append(sT, sS, sL, sM, sE);
  makeLogLineClickable(div, obj);  // ⑫8 C1：点击行开详情弹窗
  return div;
}
async function fetchLogs(params) {
  const qs = new URLSearchParams(params).toString();
  const r = await fetch("/api/logs?" + qs);
  return r.json();
}
function displayLogs(data) {
  const list = document.getElementById("logList");
  const stats = document.getElementById("logStats");
  if (!list) return;
  if (data.error) { list.textContent = "查询失败: " + data.error; return; }
  const logs = data.logs || [];
  stats.textContent = logs.length + " 条" + (data.truncated ? "（已达上限，收窄条件）" : "") + " | 文件: " + (data.files_scanned || []).join(", ");
  list.innerHTML = "";
  if (!logs.length) { list.textContent = "无匹配日志"; return; }
  const frag = document.createDocumentFragment();
  logs.forEach(o => frag.appendChild(renderLogLine(o)));
  list.appendChild(frag);
  list.scrollTop = 0;
}
(function bindLogsModal() {
  const modal = document.getElementById("logsModal");
  const btnOpen = document.getElementById("logsBtn");
  const btnClose = document.getElementById("logsModalClose");
  if (!modal || !btnOpen) return;
  btnOpen.addEventListener("click", () => {
    modal.style.display = "flex";
    document.getElementById("logTailBtn").click();
  });
  if (btnClose) btnClose.addEventListener("click", () => modal.style.display = "none");
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.style.display = "none"; });
  document.getElementById("logSearchBtn").addEventListener("click", async () => {
    const params = {
      file: document.getElementById("logFile").value,
      trace_id: document.getElementById("logTrace").value.trim(),
      level: document.getElementById("logLevel").value,
      q: document.getElementById("logQ").value.trim(),
      limit: 300,
    };
    displayLogs(await fetchLogs(params));
  });
  document.getElementById("logTailBtn").addEventListener("click", async () => {
    displayLogs(await fetchLogs({ file: "all", limit: 50 }));
  });
  // ⑫8 C3 按问题搜明细档（点击结果开 C1 详情）
  document.getElementById("logDetailSearchBtn").addEventListener("click", async () => {
    const q = document.getElementById("logQ").value.trim();
    if (!q) return;
    const r = await fetch("/api/detail?q=" + encodeURIComponent(q));
    const data = await r.json();
    const list = document.getElementById("logList");
    const stats = document.getElementById("logStats");
    const ms = data.matches || [];
    stats.textContent = "明细档匹配 " + ms.length + " 条（点击查看完整明细）";
    list.innerHTML = "";
    ms.forEach(m => {
      const row = document.createElement("div");
      row.style.cssText = "cursor:pointer;border-bottom:1px solid #1e293b;padding:4px 0;color:#a78bfa";
      row.textContent = (m.ts || "") + "  " + (m.q || "").slice(0, 60) + "  [" + m.trace_id + "]";
      row.onclick = () => openDetailModal(m.trace_id);
      list.appendChild(row);
    });
    if (!ms.length) list.textContent = "明细档无匹配（可能早于明细档上线）";
  });
  document.getElementById("logTrace").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("logSearchBtn").click();
  });
})();

// ═══ ⑫8 C1 明细档详情弹窗（点击日志行看全量现场） ═══
async function openDetailModal(traceId) {
  const r = await fetch("/api/detail?trace_id=" + encodeURIComponent(traceId));
  const data = await r.json();
  const box = document.getElementById("detailBody");
  const title = document.getElementById("detailTitle");
  if (!box) return;
  if (data.error) { title.textContent = "明细档: " + traceId; box.textContent = data.error; }
  else {
    const d = data.detail || {};
    title.textContent = "明细档: " + traceId + (d.q ? " | " + d.q.slice(0, 40) : "");
    box.innerHTML = "";
    const sec = (name, arr, opts) => {
      if (!arr || !arr.length) return null;
      const h = document.createElement("h4");
      h.style.cssText = "color:#60a5fa;margin:10px 0 4px";
      h.textContent = name + "（" + arr.length + "）";
      box.appendChild(h);
      arr.forEach((item, i) => {
        const row = document.createElement("details");
        row.style.cssText = "margin:2px 0";
        const sum = document.createElement("summary");
        sum.style.cssText = "cursor:pointer;color:#94a3b8;font-size:12px";
        const fname = (item.file || "").split(/[\\/]/).pop();
        sum.textContent = (i + 1) + ". " + fname + "#" + (item.seq !== undefined ? item.seq : "") +
          (item.score !== undefined ? " (" + item.score + ")" : "");
        row.appendChild(sum);
        if (opts && opts.text && item.text) {
          const pre = document.createElement("pre");
          pre.style.cssText = "white-space:pre-wrap;color:#c9d1d9;font-size:11px;max-height:200px;overflow-y:auto;margin:4px 0 4px 16px";
          pre.textContent = item.text;
          row.appendChild(pre);
        }
        // ⑫8 C4：查入库按钮（这个文件当初怎么被切块/直采的）
        const ingBtn = document.createElement("button");
        ingBtn.textContent = "📦 查入库";
        ingBtn.style.cssText = "margin-left:16px;padding:1px 8px;font-size:11px;background:transparent;color:#7c3aed;border:1px solid #7c3aed;border-radius:6px;cursor:pointer";
        ingBtn.onclick = async () => {
          const r = await fetch("/api/ingest-log?file=" + encodeURIComponent(item.file || ""));
          const d = await r.json();
          const logs = d.logs || [];
          ingBtn.textContent = logs.length ? "📦 入库日志 " + logs.length + " 条" : "📦 无入库日志";
          logs.slice(0, 8).forEach(l => {
            const div2 = document.createElement("div");
            div2.style.cssText = "color:#64748b;font-size:11px;margin-left:24px";
            div2.textContent = (l.ts || "") + " [" + (l._src || "?") + "] " + (l.msg || l.raw || "").slice(0, 100);
            row.appendChild(div2);
          });
        };
        row.appendChild(ingBtn);
        box.appendChild(row);
      });
    };
    // 问题与变体
    const qh = document.createElement("h4"); qh.style.cssText = "color:#fbbf24;margin:6px 0";
    qh.textContent = "问题: " + (d.q || ""); box.appendChild(qh);
    if (d.queries && d.queries.length > 1) {
      const ql = document.createElement("div"); ql.style.cssText = "color:#818cf8;font-size:12px;margin-bottom:6px";
      ql.textContent = "MQ 变体: " + d.queries.join(" ｜ ");
      box.appendChild(ql);
    }
    sec("语义路 top", d.semantic_top);
    sec("关键词路 top", d.keyword_top);
    sec("RRF 融合池", d.rrf_pool);
    if (d.rerank_full && d.rerank_full.length) {
      const h = document.createElement("h4"); h.style.cssText = "color:#60a5fa;margin:10px 0 4px";
      h.textContent = "rerank 完整序（" + d.rerank_full.length + " 名）";
      box.appendChild(h);
      const pre = document.createElement("pre");
      pre.style.cssText = "color:#64748b;font-size:11px;max-height:150px;overflow-y:auto";
      pre.textContent = d.rerank_full.slice(0, 100).map((x, i) => (i + 1) + ". idx=" + x.idx + " " + x.score).join("\n");
      box.appendChild(pre);
    }
    sec("最终命中块（含全文）", d.final_hits, { text: true });
    if (d.answer) {
      const h = document.createElement("h4"); h.style.cssText = "color:#34d399;margin:10px 0 4px";
      h.textContent = "模型答案" + (d.answer.model ? "（" + d.answer.model + " " + (d.answer.latency_ms || "?") + "ms）" : "");
      box.appendChild(h);
      const pre = document.createElement("pre");
      pre.style.cssText = "white-space:pre-wrap;color:#d1fae5;font-size:12px;max-height:300px;overflow-y:auto";
      pre.textContent = d.answer.text || "";
      box.appendChild(pre);
    }
  }
  document.getElementById("detailModal").style.display = "flex";
}
(function bindDetailModal() {
  const modal = document.getElementById("detailModal");
  const btnClose = document.getElementById("detailModalClose");
  if (!modal || !btnClose) return;
  btnClose.addEventListener("click", () => modal.style.display = "none");
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.style.display = "none"; });
})();

// 日志行点击 → 有 trace_id 且非评测记录时开详情（C1 动线）
function makeLogLineClickable(div, obj) {
  const tid = obj.trace_id;
  if (!tid || tid === "-") return;  // fe_（页面）/ev_（评测）都开详情
  div.style.cursor = "pointer";
  div.title = "点击查看本次提问完整明细（池/块全文/答案）";
  div.addEventListener("click", () => openDetailModal(tid));
}

(function bindHealthModal() {
  const modal = document.getElementById("healthModal");
  const btnOpen = document.getElementById("healthBtn");
  const btnClose = document.getElementById("healthModalClose");
  const btnRefresh = document.getElementById("healthRefreshBtn");
  if (!modal || !btnOpen) return;
  btnOpen.addEventListener("click", () => { modal.style.display = "flex"; btnRefresh.click(); });
  if (btnClose) btnClose.addEventListener("click", () => modal.style.display = "none");
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.style.display = "none"; });
  btnRefresh.addEventListener("click", async () => {
    const el = document.getElementById("healthPanel");
    el.textContent = "读取中…";
    try {
      const r = await fetch(BACKEND_URL + "/api/stats");
      renderHealthPanel(await r.json());
    } catch (e) {
      el.innerHTML = '<span style="color:#e74c3c;">读取失败：服务未启动或网络断</span>';
    }
  });
})();

// ═══ ⑨.5 引用溯源（2026-09-20 开工）═══
function renderCitations(html) {
  // [N] → 可点击引用徽标 + 末尾来源清单（RAGFlow 式）
  const hits = window.__ragHits || [];
  if (!hits.length) return html;
  // [1] → <cite data-c="1">[1]</cite>（避开 markdown 链接 [text](url) 的方括号）
  html = html.replace(/\[(\d{1,2})\](?!\()/g, (m, n) => {
    const idx = parseInt(n, 10);
    if (idx < 1 || idx > hits.length) return m;  // 超范围原样
    return `<cite class="rag-cite" data-c="${idx}" title="${(hits[idx-1].file||'').replace(/"/g,'')} 第${hits[idx-1].seq}块" style="cursor:pointer;background:#e8f0fe;color:#1a73e8;border-radius:4px;padding:0 4px;font-size:0.85em;font-style:normal;">[${idx}]</cite>`;
  });
  return html;
}
function buildSourcesList() {
  // 末尾来源清单（回答完成后追加）
  const hits = window.__ragHits || [];
  if (!hits.length) return "";
  const seen = new Set();
  const rows = [];
  hits.forEach((h, i) => {
    const key = h.file + "#" + h.seq;
    if (!seen.has(key)) {
      seen.add(key);
      const _sc = (typeof h.score === "number" && h.score > 0) ? ` <span style="color:${h.score < 0.3 ? '#c0392b' : '#2e7d32'};font-size:10px;">(相似度 ${h.score.toFixed(2)}${h.score < 0.3 ? " · 低" : ""})</span>` : "";
      rows.push(`<div style="font-size:11px;color:#666;padding:1px 0;">[${i+1}] ${h.file} · 第${h.seq}块${_sc}</div>`);
    }
  });
  return rows.length ? `<div style="margin-top:10px;padding-top:6px;border-top:1px solid #eee;"><div style="font-weight:600;font-size:11px;color:#999;margin-bottom:2px;">参考来源</div>${rows.join("")}</div>` : "";
}
(function bindCitationClick() {
  // 事件委托：点 [N] 徽标 → 弹原文块浮窗
  document.addEventListener("click", (e) => {
    const cite = e.target.closest(".rag-cite");
    if (!cite) return;
    const idx = parseInt(cite.dataset.c, 10);
    const h = (window.__ragHits || [])[idx - 1];
    if (!h) return;
    // 浮窗（FastGPT 式）
    let ov = document.getElementById("ragCiteOverlay");
    if (ov) ov.remove();
    ov = document.createElement("div");
    ov.id = "ragCiteOverlay";
    ov.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:9999;display:flex;align-items:center;justify-content:center;";
    ov.innerHTML = `<div style="background:#fff;border-radius:10px;max-width:640px;width:90%;max-height:70vh;display:flex;flex-direction:column;box-shadow:0 8px 30px rgba(0,0,0,.18);">
      <div style="padding:10px 14px;border-bottom:1px solid #eee;display:flex;justify-content:space-between;align-items:center;">
        <b style="font-size:13px;">[${idx}] ${h.file} · 第${h.seq}块</b>
        <span style="cursor:pointer;color:#999;font-size:18px;" id="ragCiteClose">×</span>
      </div>
      <div style="padding:14px;overflow:auto;font-size:12.5px;line-height:1.8;white-space:pre-wrap;">${(h.text || "").replace(/</g, "&lt;")}</div>
    </div>`;
    ov.addEventListener("click", (ev) => { if (ev.target === ov || ev.target.id === "ragCiteClose") ov.remove(); });
    document.body.appendChild(ov);
  });
})();


// ═══ 忠实度校验（Faithfulness Check·轻量版 2026-09-20）═══
async function verifyFaithfulness(answer) {
  const hits = window.__ragHits || [];
  if (!hits.length) return;
  const ctx = hits.slice(0, 8).map((h, i) => `资料${i+1}: ${String(h.text).slice(0, 400)}`).join("\n\n");
  try {
    const r = await fetch(chatApiUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${chatApiKey()}` },
      body: JSON.stringify({
        model: rewriteModel(),
        messages: [
          { role: "system", content: "对照参考资料检查回答的忠实度。回答中每个事实性陈述（数字/名称/日期/结论）都能在资料里找到依据吗？只输出 JSON，字段 faithful 布尔值、unverified 字符串数组（列出无依据的句子）。观点性/总结性句子不算。" },
          { role: "user", content: `参考资料：\n${ctx}\n\n回答：\n${answer.slice(0, 1200)}` }
        ],
        temperature: 0,
        max_tokens: 300,
      }),
    });
    const d = await r.json();
    const txt = d.choices?.[0]?.message?.content || "";
    const m = txt.match(/\{[\s\S]*\}/);
    if (!m) return;
    const j = JSON.parse(m[0]);
    if (j.faithful === false && Array.isArray(j.unverified) && j.unverified.length) {
      // 找最后一条 AI 消息体，追加警示（不删原答案——校验只提示不裁决）
      const bodies = document.querySelectorAll(".msg-body, .ai-msg-body, [class*=body]");
      const last = bodies[bodies.length - 1];
      if (last && !last.querySelector(".faith-warn")) {
        const w = document.createElement("div");
        w.className = "faith-warn";
        w.style.cssText = "margin-top:8px;padding:6px 10px;background:#fff8e1;border-left:3px solid #f39c12;font-size:11.5px;color:#8a6d3b;";
        w.innerHTML = `⚠ 忠实度校验：以下内容未在检索资料中找到依据，请注意核实：<br>${j.unverified.slice(0, 3).map(s => `· ${String(s).slice(0, 60)}`).join("<br>")}`;
        last.appendChild(w);
      }
      fetch(BACKEND_URL + "/api/log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: "faithfulness_warn", q: "", detail: j.unverified.slice(0, 2).join("|").slice(0, 200) }) }).catch(() => {});
    }
  } catch (e) { /* 校验失败静默——不干扰主回答 */ }
}

// 页面加载即恢复历史会话（三十四修原调用行——C 方案编辑链中被吞，用户实锤刷新丢列表，2026-09-25 补回）
restoreSessionsFromServer();
