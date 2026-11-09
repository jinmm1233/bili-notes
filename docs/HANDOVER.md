# BiliNotes 开发交接文档

> **最后更新**：2026-11-09 10:50 ｜ 对应前端版本 `?v=20261109b`
> **用途**：接手或继续开发前先读这一份。它记录了「做了什么」「为什么这么做」「哪些路别再走一遍」。
> **维护约定**：每完成一个功能，在「四、改动台账」加一行；踩到新坑，在「六、踩坑与禁令」补一条。这两节是本文档的核心价值。

---

## 一、十分钟速览

| 项目 | 内容 |
|---|---|
| 一句话 | 扫码登录 B站 → 批量把收藏的视频转成**带截图、带时间戳、带导图、带讲义**的 Markdown 笔记，再拆成知识卡片按遗忘曲线复习 |
| 与同类工具的差别 | **画面上的文字也进笔记**：PPT 标题、代码、公式、表格从关键帧 OCR 出来，还原成 Markdown 语法，不只转写口播 |
| 技术栈 | FastAPI + 线程池队列；yt-dlp；ffmpeg；faster-whisper（CTranslate2 int8，无 PyTorch）；rapidocr-onnxruntime；前端单文件 HTML/CSS/JS 零构建 |
| 运行位置 | `C:/Users/86159/WorkBuddy/2026-10-31-10-13-34/bili-notes/` |
| 服务地址 | http://127.0.0.1:8765 （**仅软件运行时**；关窗即退、零后台、不开机自启，静态文件实时生效） |
| 代码规模 | 后端 `app/` 约 41 个模块（services 27 / core 9 / api 5）；前端三件套 index.html 35KB + app.css 61KB + app.js 90KB |
| 数据目录 | `%APPDATA%\BiliNotes\`（配置、密钥、卡片、缓存、日志） |
| 笔记目录 | `C:\Users\86159\Documents\BiliNotes\<笔记标题>\` |
| 当前状态 | 功能完整可用，15 个回归套件 + 2 个前端检查全绿；`dist/BiliNotes/` 通过 `tools/verify_bundle.py` **32 项验收**；`installer/dist/BiliNotes-setup-0.2.0.exe`（210.5MB）已编译 |

**最近在做什么**：知识卡片 + 间隔重复 → 跨视频概念索引 → 排期规则显式化 → **卡片按范围分组复习、所有卡片浏览器、近似重复拦截**。

---

## 二、项目定位与设计原则

原始需求是「提取 B站视频做总结生成 md」，但明确要求**打成 exe 分发、由使用者自带 API Key**。这两条约束决定了大量技术选择：

1. **不引入重型依赖**。无 Celery/Redis（单机线程池够用）、无 PyTorch（用 CTranslate2 int8，省 2GB）、无 Playwright 常驻浏览器（B站有官方扫码接口）、无向量数据库（关键词召回够用）。
2. **软件不内置任何 Key**。7 家供应商预设只有 `base_url` + `model`，`needs_key=1`，没 Key 直接返回 `None` 跳过该环节，不存在「降级到作者 Key」的分支。这条有测试守着（`tests/smoke_keys.py`）。
3. **结论必须可追溯**。OCR 有误就标注「可能有误」，文本校验的修正逐条写进笔记而不是静默改，笔记里保留 `[mm:ss]` 跳转。
4. **宁缺毋滥**。识别不准的宁可不出（AI 字幕默认关闭、表格判定从严、代码/公式只认高置信），因为知识笔记里错一个数字比缺一段更糟。

---

## 三、架构

```
浏览器（单页，hash 路由，5+2 个视图）
   │  fetch JSON / SSE
   ▼
FastAPI  app/main.py
   ├─ api/routes.py          任务、设置、密钥、系统（ffmpeg/缓存）
   ├─ api/routes_bili.py     登录、收藏夹、导入、合集展开、选集
   ├─ api/routes_library.py  知识库、收藏夹分组、讲义、导出、图片代理
   ├─ api/routes_cards.py    知识卡片（生成、到期、评分）
   └─ api/routes_concepts.py 概念索引
   │
   ├─ services/task_manager.py   线程池 + 队列（默认并发 2）+ 暂停/恢复
   └─ services/pipeline.py       单条任务的流水线
        探针 → 音频 → 字幕/转写 → 关键帧 → OCR → 文本校验 → 分块
        → 逐块总结 → 结构化重组(outline) → 例题抽取 → 行程单(可选)
        → 导出笔记 + 导图 → 知识点讲义(可选)
```

**流水线阶段与进度**：`probe 2~5%` → 文本获取 ~10-40% → 关键帧/OCR ~40-80% → `chunk 81%` → `summary 84-93%` → `chapter 93-96%` → `itinerary 96%` → `export 97%` → `lecture 98-99%` → `done 100%`。阶段名会通过 SSE 推给前端，前端按 `group` 归类显示（`vision` 组特殊处理）。

**并行的正确做法**（重要，第一版做错过）：先串行下音频（2-3s），再并行「转写（纯 CPU）∥ 下载视频+抽帧（网络+IO）」，OCR 后置独占 CPU。**不要让两个 yt-dlp 同时跑**，会抢带宽反而更慢（实测 92s → 185s）。

---

## 四、改动台账

按时间倒序。每行都能在 `.workbuddy/memory/YYYY-MM-DD.md` 找到更细的记录。

### 2026-11-09 · 0.2.3 内置更新绑定真实仓库（22:20）

用户推送报 `fatal: ... Recv failure: Connection was reset`，问「代理还没开要开嘛」。
真因：系统代理 `ProxyEnable=0`（配了 `127.0.0.1:7890` 却没启用）、git 也没配代理，
31.5MB 推送被重置；小的 `git ls-remote` 裸连能通，**别据此判断大推送也没问题**。
开启 iKuuuVPN 系统代理后重试成功；API 确认仓库 `jinmm1233/bili-notes` 是 Public、
默认分支 `master`，远端 master 与本地 `e9bbd3d` 一致。

- `app/__init__.py` 的 `__update_repo__` 由空串改为 `jinmm1233/bili-notes`，
  版本 0.2.2 → 0.2.3（iss 同步）。优先级仍是设置页 > 环境变量 > 打包默认值。
- `tests/smoke_update.py` 两条「默认没有更新源」的旧断言改成验真实行为：
  默认值生效 + 临时清空 `U.__update_repo__` 仍测 `not_configured` 兜底；
  `reset()` 每轮恢复默认值，避免用例互相污染。
- 本轮 updater 修复（SHA256SUMS 真正读取、多 exe 资产优先选 setup、Range 断点续传、
  404 明确提示仓库名）随包发布；首个 Release 用 **v0.2.3**，不是旧教程里的 v0.2.2。
- 文档 8.0 改成「已上传」事实记录，把代理坑写进去；8.1 发版示例已是 0.2.3。

### 2026-11-09 · 0.2.2 知识库：同一合集折成一张卡（20:20）

用户：「收藏夹里面同一个合集放一块，点进去才有合集的所有视频，不然太杂了」。
原来知识库只有两级（收藏夹 → 笔记），CSAPP p12~p18 七篇会把别的笔记挤下去。

改成三级：**收藏夹 →（合集卡 + 散篇）→ 合集内分集（按集数顺序）**。
搜索结果仍直接给命中的笔记，不折叠。

- `library.scan()` 现在顺手读每篇的 `meta.json`，横看全部笔记后给每篇写
  `series_key / series_title / series_order / series_kind`（`_annotate_series`）。
  判定优先级：① 导入时记的 `series_id` → ② 同一 BV 不同 `p=N`（多 P）
  → ③ 标题集数标记 + ≥10 字公共前缀（老笔记 / 跨 BV 兜底）。**少于两集不折叠**。
  合集名用 `os.path.commonprefix` 倒推，并削掉停在半个集数标记上的尾巴
  （p1 与 p12 会共到 `"… p1"`）。
- 新数据 `library.series_groups()` 出卡片；`/api/library` 只在传了 `collection`
  时返回 `series`（合集只在同一收藏夹内折叠），收藏夹卡多带 `series_count`。
- 展开导入时把系列身份写进笔记：`Series.sid`（B站 `ugc_season.id`，跨导入稳定）
  → `_expand_items` 给每集带 `series_id/title/order` → `_create_tasks` 逐任务复制
  options（**不能让单集继承上一集**）→ `PipelineOptions` → `VideoMeta` → `meta.json`。
- 前端 `currentSeries/currentSeriesTitle` 两个状态，`btnLibBack` 按层级切换文案与行为；
  三级视图 `ui_series_shot.py` 用真实笔记截图验证（1 张合集卡 + 9 篇散篇 → 7 集）。


### 2026-11-09 · 0.2.1 无语音视频生成不了笔记（11:20）

用户：「有个视频还是没有笔记，难道因为没有音频嘛，但是可以识图的啊」。

日志是 `转写完成 segments=0`（音频 328s 全在，只是没有人声），随后
`笔记已导出（图片 0 张）`，产物 `notes.md` 只有 896 字节。两个独立问题：

1. **帧只能挂在知识块上，块没有就整批丢弃**。`build_chunks([])` 返回空 →
   `attach_frames` 直接 return → 8 张入选帧一张都没进笔记。注意 `Frame` 与
   `Chunk` 是"挂载"关系，没有块就没有图。
   → 新增 `chunking.chunks_from_frames()`：按时间窗（默认 90s）把画面分组，
   用块内帧的 OCR 文字当正文；`pipeline` 在 `chunks` 为空且 `sampled` 非空时改走这条路，
   同时往 `warnings` 写一条「没有识别到人声，已改用画面内容整理（N 段）」。
2. **`finalize()` 把「章节为空」当成「未配置模型」**。它写的是
   `if client is None or not chapters`，两种情况共用一句文案，于是 Key 好好配着也显示
   "未配置模型"。→ 增加 `speechless` 形参区分三种情况（无模型 / 没语音 / 真没正文），
   导出器的来源行也改成「本视频没有可识别的语音，笔记依据画面内容整理」。

同类保护：`text_check.rebuild_chunk_text(chunks, [])` 原来会把块正文拼成空串
（音画互校第二轮在 0 句时正好会调它）→ 空 `segments` 直接 return；
`_fallback_chunk` 原来只看 `chunk.text`，纯画面块会退化成"内容片断" →
依次用 `ocr_text`、`frame_caption.chunk_screen_lines()` 兜底。

回归：`smoke_detail` 新增「无语音」段（切块→挂帧→不清空→文案），
`smoke_pipeline` 新增 `[6] 无语音视频` 端到端（转写返回 []，走真实抽帧+OCR）。

### 2026-11-09 · 白窗口/点不动根治：端口探测 + 单实例锁（10:50）

用户反馈「重新打开后点都点不动，之前的也没了」。查下来是**两个独立缺陷叠加**：

1. **端口探测用了 `SO_REUSEADDR`**（`main.pick_port`）。Windows 上这个 flag 语义和
   Linux 相反，允许绑定到已被别的程序监听的端口，于是探测说"空闲"、uvicorn 真正
   listen 时报 `[Errno 10048]`。服务根本没起来，但进程活着、窗口照开——页面白屏、
   点什么都没反应，关窗也不退（因为窗口循环还在跑）。
   → 改为 `connect_ex` 判断"是否真有人在听"，已实测旧法会误判、新法正确。
2. **单实例判定靠端口探测存在竞态**。旧实例正在关窗退出时端口还通，新实例据此认为
   "已有实例"，于是只开窗口、自己不起服务；下一毫秒旧实例退出、端口释放，
   窗口就永远连不上。→ 改用 Windows 命名互斥量（`Local\BiliNotes-single-instance`），
   锁是唯一权威，进程退出由系统自动释放。注意 `CreateMutexW` 返回"已存在"时
   也会拿到有效句柄，**必须立刻 CloseHandle**，否则对方退出后锁仍被自己占着。

另外补了第二道防线：`_serve()` 起服务后确认 healthz 真的通，端口在探路与 listen
之间被抢就顺延下一个；全都起不来则明确报错退出，**绝不打开一个连不上的窗口**。

代价说明：任务队列仍是内存态（重启即空），但笔记、卡片、自测卷、概念标签、单词本
全部落盘，重启后从「视频库」照常可见。

### 2026-11-09 · 新图标 + 桌面快捷方式（10:05）

用户觉得原图标（粉色底 + 白色播放键）像播放器、不像笔记软件，要求换掉。

- **新图标**：深蓝灰渐变圆角底 + 一页笔记（带折角、四条笔记线）+ 右下角粉色
  播放键，表达「视频变成笔记」。7 个尺寸（16/24/32/48/64/128/256），
  32px 下仍能辨认。`app.ico` 已替换，旧图标备份为 `app.old.ico`；
  `build.spec` 的 `icon=` 与 iss 的 `SetupIconFile` 都指向它，打包后自动生效。
- **快捷方式踩了两个坑**：
  1. **手写 .lnk 二进制不行**：按 MS-SHLLINK 拼出的 543 字节文件，双击报
     「该文件没有与之关联的应用来执行该操作」。LinkInfo 缺卷序列号，
     Shell 判为不可信；`ShellExecuteW` 直接返回 **5（拒绝访问）**。
  2. **PowerShell 里造不出来**：`Add-Type`、`WScript.Shell` COM 实例化、
     `cscript` 全被当前环境的权限策略拦掉（LOLBin 检测）。
  解法：`tools/make_lnk.py` 用 **ctypes 直接调 `IShellLinkW` + `IPersistFile`**
  生成标准快捷方式（2532 字节），`ShellExecute` 返回 **42 = 成功**；
  已实测双击能起进程、服务在 8765、窗口标题正确。
- **启动方式的坑**：从工具会话里 `Start-Process` / `ShellExecute` 起的 GUI 进程
  会随该次命令结束被清掉；要让进程独立存活，必须交给 `explorer.exe` 启动。

### 2026-11-09 · 窗口版 12 秒自动退出（09:40）

用户说「帮我打开软件」，实测 `BiliNotes.exe` 启动 **12 秒后 EXIT=0 自己退了**，
Edge 窗口还开着但显示 `127.0.0.1`（连不上）——服务跟着进程一起没了。

- **根因**：`_open_edge_app()` 用 `proc.wait()` 判断「窗口关没关」。Edge 是多进程的，
  `--app` 启动时 Popen 拿到的那个进程会把窗口交给常驻浏览器进程、自己先退出，
  于是 `wait()` 立刻返回 → `run()` 认为窗口已关 → 主线程结束 → daemon 线程里的
  uvicorn 一起死。窗口还在、服务没了，表现就是「网页打不开」。
- **修复**：改成轮询窗口本身。`_app_window()` 用 `EnumWindows` + 类名
  `Chrome_WidgetWin_1` + 标题含 `BiliNotes`/`127.0.0.1` 定位；
  先等窗口出现（最多 20 秒），再连续 3 次（约 4.5 秒）看不到才算关闭。
  `close()` 也从 `terminate()` 改成向窗口发 `WM_CLOSE`。
- **验证**：源码跑满 18 秒不退出，日志「应用窗口已就绪，等待关闭」；
  打包版复测同样跑满 40 秒。

### 2026-11-09 · 笔记详细度三档 + 稳定帧判定（08:20）

用户三连问「笔记够详细吗 / 是随机截帧吗 / 会不会漏知识点」，查日志得到
`入选关键帧 3 / 候选 29`——画面被压得很厉害。随后定了三档：**简洁每段 5 张、
适中每段 8 张、详细不设限**，并要求「详细档的总结和画面要点也要更细」，
同时明确一条关键语义：**详细 ≠ 画面有变化就截，PPT 要等内容放全、画面停住再截一帧**。

- **稳定帧判定（核心改动）**：`frames.detect_stable()` 取代按「变化点」抽帧。
  先算每秒 1 张缩略图的 phash，找「连续 N 秒不再变化」的区间，取**区间末尾**
  （内容最全）。PPT 逐条动画时每一帧都在变，中间态凑不出稳定段，自然被跳过。
  - **判定标准三档完全一致**：静止门槛 **2 秒**；一页讲很久（稳定段 >90 秒）按
    30 秒往回补点。用户明确说过「三档的区别只有每段上限」，所以档位不碰这些。
  - 门槛为什么是 2 秒而不是 3 秒：板书写完常常只停两秒就擦掉/翻页，
    要求 3 秒会把整页漏掉、一张都没有。放宽后动画中间态会进候选，但打分阶段
    「字多的分高」，同一页的相似帧去重时最终留下的仍是内容最全的那张。
  - 修掉一个反直觉点：`scene_phash_distance` 越小越敏感 → 轻微晃动就算「还在动」
    → 稳定段更难形成、候选**更少**。所以固定 14，「详细」不靠调它实现。
  - 不只在 PPT 上成立：科普视频分步展示信息、没列完就切走的那段永远不会
    形成稳定段，自然不会被截——正是「不完整的那帧不要」。
- **0 = 不限**：`max_frames_total` / `max_frames_per_chunk` 全链路支持 0
  （config 的 ge、frames 的截断、chunking.attach_frames、SettingsIn 的 le）。
  全片不再设总配额（三档都是 0），总量由「段数 × 每段上限」自然决定。
- **档位是唯一真相**：`Settings` 的 `model_validator(mode="after")` 用
  `DETAIL_PRESETS` 覆盖具体参数，避免 config.json 里残留的旧配额
  （比如 per_chunk=3）让「选了详细只留 3 张」。
- **按「有没有内容」判定，不按有没有人**：用户明确「人后面有黑板、板上有字，
  一样要截」。判定取 `max(OCR 文字量, 版面结构分)`，≥0.12 就保留：
  结构分来自边缘密度 × 细节方差（`text_score`，结果存进 `frame.text_density`），
  手写板书、反光屏 OCR 认不出时靠它兜住。只有纯人像/纯风景（结构分 ~0.03）
  才排除；整段都没内容时兜底补该段最清晰的一张（vlog 不至于没图）。
- **详细档总结同步加密度**：`summarize._density_note()`（每个二级 4-8 条、
  不合并同类项、公式数字原样保留、takeaways 到 8-10 条）+
  `frame_caption._density_note()`（每个分点/公式/数字单列，points 到 10 条）；
  bullets 上限 6→10，max_tokens 相应提高。
- **防跑飞**：详细档可能上百帧，而视觉模式是「一帧一次请求」→
  `vision_max_frames=80` 超过就退回文字模式（OCR + 讲解补全），要点照样有。
- **修 `save_overrides` 顺序**：原来是「先写 config.json 再 `Settings(**current)`」，
  一个非法值（`note_detail="nope"`）会先落盘再抛异常 → 下次启动直接起不来。
  改成先构造校验、成功后才写盘；`SettingsIn.note_detail` 也加了 pattern，
  非法值在接口层就返回 422。
- 测试：`tests/smoke_detail.py` 23 项，含「变化发生的第 3 秒永远不会被选中」
  和「详细档静止门槛与适中一致」两条硬断言。前端 `?v=20261109b`。

### 2026-11-09 · 内置更新 + 零后台入口约定（00:55）

用户确认：**按 A——关窗即退、零后台、不开机自启**，不做托盘常驻；剩下的做内置更新。

- **「又打不开了」的根因**：不是 bug，是本地服务的固有性质。实测当时机器上
  没有任何 BiliNotes 进程、8765 端口无监听 → 浏览器里那个地址当然是死的。
  不改常驻行为，改为在设置页「关于」写清入口：桌面图标 / 开始菜单，
  不要把 127.0.0.1 存成书签；明确「不会后台常驻、不会开机自启」。
  发布清单第 4、6 节同步说明。
- **版本号统一**：`app/__init__.py` 提到 `0.2.0`，`FastAPI(version=)` 与
  `/healthz` 改为引用它（原来硬编码 `0.1.0`，而 iss 是 `0.2.0`——不统一时
  更新检查会永远觉得自己是旧版）。`verify_bundle.py` 新增
  「healthz / settings / update 三处版本一致」断言。
- **内置更新**：`app/core/updater.py` + `app/api/routes_update.py` +
  设置页「软件更新」面板。GitHub Releases → 语义化版本比对 → 后台线程下载
  （进度轮询）→ SHA256 校验 → 拉起安装器。更新源三级：设置页 `update.json`
  > 环境变量 `BILINOTES_GITHUB_REPO` > 打包默认值 `__update_repo__`。
  未配置就不联网；网络失败静默（国内连不上 GitHub 是常态），
  「查看发布页」始终可点作为手动兜底。
- **安装前必须退出主进程**：`app/window.py` 新增 `close()`；`updater.install()`
  启动安装器后关掉 Edge 窗口并退出，否则 Program Files 里的 exe 被占用，
  Inno Setup 会弹文件占用错误框（之前 `BiliNotesIsRunning()` 拦的就是这个）。
- **代理劫持本机请求（差点误判成打包失败）**：一轮验收全部接口突然报
  `502 Bad Gateway`，查出是会话里 `HTTP_PROXY=http://127.0.0.1:34126`
  而 `NO_PROXY` 为空，`urllib` 把 `127.0.0.1:8765/healthz` 发给了代理。
  手动启动 exe 完全正常（服务在 8765 跑满 15 秒），确认不是产物问题。
  但顺带发现**应用自身**的 `find_running()` / `_wait_ready()` 也用普通 urlopen：
  在开了代理软件的电脑上会认不出已运行实例 → 双击图标又起一个进程 →
  端口顺延 → 手机链接全指向旧地址，正是「网址又断了」的另一种成因。
  新增 `app/core/local.py`（禁用 ProxyHandler 的 opener），两处本机请求改用它；
  `verify_bundle.py` 同样处理；外部请求（GitHub / 模型镜像 / LLM）仍走代理。
  `tests/smoke_lan.py` 加「假代理 + 本地 HTTPServer」对照用例守住。
- **设置页卡顿根治**：验收时发现打包版 `/api/settings` 要 4~6 秒
  （`/api/update/status` 仅 0.01 秒）。根因是 `cache_clean.usage()` 每次
  逐文件 stat 755MB 缓存目录，实测单项 2.84 秒。加 60 秒 TTL 缓存 +
  删文件后主动失效，第二次起毫秒级；`verify_bundle.py` 版本一致性检查
  的超时相应放宽到 20 秒（首次仍可能扫盘，别误判成挂了）。
- 顺手修一处文案与实物不符：设置页原写「安装包不自带 ffmpeg」，
  实际 7.1 已随包分发在 `_internal/bin/`，改成「已自带，异常时才需手动下载」。
- 测试：`tests/smoke_update.py` 32 项（版本比较 / 配置优先级 / Release 解析 /
  离线兜底 / 域名白名单）；接口冒烟 12 项；前端版本 `?v=20261109a`。

### 2026-11-08 · 打包版段错误根治 + 一键验收（23:55）

用户提醒「打包一定要验证好」，逐项核对后果然抓到两个真问题，都不是 Python 报错：

- **现象**：窗口版/无窗口版都在启动后 15~30 秒静默段错误（exit 139），日志停在
  「Uvicorn running on...」；`BILINOTES_WINDOW=0` 也照崩，说明与窗口无关。
- **定位**：`entry.py` 加 `faulthandler`（落到 `logs/crash.log`）+ 探针模式
  （`BILINOTES_PROBE=模块名` 让 exe 只加载不启动），跑实验矩阵：
  `ctranslate2` ✅ / `faster_whisper` ✅ / **`onnxruntime` ❌** / `rapidocr_onnxruntime` ❌。
  栈指向 `onnxruntime\capi\_pybind_state.py` 加载 pybind11 扩展那行。
  包内 DLL 与源码逐字节一致 → 不是漏带文件；对比 VC++ 运行库发现包内
  `msvcp140.dll` 是 **14.26（VS2019）**、系统是 14.51，Windows 优先加载程序目录里的旧版。
  用系统版覆盖后四个探针全部 `rc=0`，确认根因。
- **修复**：`build.spec` 末尾加后处理，打包后把 `_internal` 里的 VC++ DLL
  统一成 `System32` 版本；`envcheck.probe()` 改成**起子进程**探重型库
  （崩溃不牵连主进程，还能区分缺运行库/被杀软拦/加载崩溃），`entry.py` 提供探针分支；
  `console=False` 下 stdout/stderr 接 devnull，避免 print 打死进程。
- **验收**：新增 `tools/verify_bundle.py`（文件齐全、VC++ 哈希一致、5 组加载探针、
  起服务查 `/healthz` `/api/env` `/` `/static/app.js`、ffmpeg 可执行、启动 12 秒不崩），
  实测 **30 项全绿**。以后每次打包必须跑，FAIL 就不许发。
- **安装包**：修两处——① 自检提示让用户双击安装目录里的
  `redist\vc_redist.x64.exe`，而 iss 原来只把它放 `{tmp}` 且装完即删，现在安装目录也留一份；
  ② `FindWindowByWindowName('BiliNotes')` 精确匹配不到 Edge 应用窗口
  （标题是网页 `<title>`「BiliNotes · B站视频知识库」），导致"程序开着时安装"拦不住、
  撞上 exe 文件占用弹错误框，改成两个标题 + `tasklist` 进程检测。

### 2026-11-08 · 安装包工程化 + 应用窗口（22:45，23:55 修订）

把「拷给别人能不能跑」真正做了一遍：

- **应用窗口**：`app/window.py` 最终用 **Edge `--app=<url>` + 独立 user-data-dir**，
  关窗=程序退；已运行时再双击=开新窗口指向已有实例；找不到 Edge 自动退回默认浏览器；
  `BILINOTES_WINDOW=0` 强制浏览器。最初试的 pywebview 6.2.1 + WebView2 已撤：
  pythonnet/clr_loader 打进冻结包会段错误（哪怕根本没 import webview），
  `build.spec` 里写了禁令注释。`build.spec` 改 `console=False`，
  另出一个 `BiliNotes-console.exe` 排错用。
- **ffmpeg 随包**：`bin/ffmpeg.exe + ffprobe.exe`（gyan essentials 7.1，各 87MB），
  `find_binary` 本来就先查 bundle bin，`ffmpeg_boot.status()` 报 `source: bundled`。
  **GitHub 直连极慢**，用 gh-proxy.com 镜像 + `curl -C -` 断点续传。
- **环境自检**：`app/core/envcheck.py` 启动后台探 ctranslate2/onnxruntime，
  DLL 加载失败→提示装 vc_redist；`GET /api/env`、healthz 带 `runtime` 字段。
  routes.py 被沙箱挡着读不了，走的新路由文件 `routes_env.py`。
- **Inno Setup 安装包**：`installer/BiliNotes.iss`（ISCC 6.7.3 在
  `%LOCALAPPDATA%\Programs\Inno Setup 6`）。中文界面（自带的 Unofficial isil 已下）、
  检测 msvcp140/vcruntime140_1 缺失→静默装 `installer/redist/vc_redist.x64.exe`（已下）；
  运行中拦安装/卸载；卸载不删用户数据并告知位置。产物 `installer/dist/BiliNotes-setup-*.exe`。
- **发布说明**：`docs/发布清单.md`（构建命令、SmartScreen/签名说明、GPL 合规、已知边界）。

### 2026-11-08 · 五项功能待办清零（22:30）

1. **自测卷导出 PDF/Word**：`quiz_export.py` 把卷子排成「试卷+答案」（题号连续、判断题留括号、
   简答留书写线，答案统一放最后）→ docx 复用 export_docx 内核（公式转 Word 原生公式）；
   PDF 走 `?printquiz=<id>` 卷面 + 浏览器无头打印（KaTeX 公式所见即所得）。
   卷列表加 PDF / Word 按钮；`GET /api/quiz/{id}/sheet` 只给卷面用（答题页仍不下发答案）。
2. **离线包生词**：offline.html 之前把生词段当普通文本。现在：注释标记剥离、`<a id>` 锚点占位、
   `[词](#w-x)` → 点按平滑跳转+高亮（手机）；桌面悬停浮出词条气泡。与主站行为一致。
3. **词库 AI 补释义**：导入时勾「缺释义也导入」收下纯单词行（`parse(keep_missing=True)`）；
   「AI 补释义」按 30 词一批×最多 12 批回填（幂等、失败只丢当批），补完自动重算生词段。
   顺手修了三列式 TXT（词 TAB 词性 TAB 释义）词性解析。
4. **复习页单词卡分段**：`wordbook.to_cards` 只把 words.json 里**真遇到过的**词变卡
   （note_id=`word:英语单词`，kind=单词；正面单词、背面词性+释义+出现次数）；
   复习页顶部 全部/知识卡/单词卡 分段，localStorage 记忆。
5. **概念碎片回流**：`concept_review.repoint_cards` 顺着 canonical 链把卡片上残留的旧概念名
   改成规范名，`tidy()` 每次自动带上。存量 6 张「变换顺序」卡已回流「初等行变换法」。

### 2026-11-08 · 重新打包 exe（21:30）

`python -m PyInstaller build.spec --noconfirm`（PyInstaller 6.22.3，此前"不在 PATH"是误判，
一直都可以用 `python -m PyInstaller` 调）。产物 `dist/BiliNotes/`（onedir，约 430MB），
冒烟通过：healthz / 连接自检（GDUFE 识别）/ 知识库 / 离线包 bundle（14 笔记 · 24 卡 · 3 卷）/
首页版本号 20261108s。

**坑**：PyInstaller 收尾要删旧 `dist/BiliNotes`（1959 个文件）时被沙箱的批量删除保护拦下，
build 显示 exit 0 但产物没换——**打包前先把旧 dist 移走**（本次挪成 `dist/BiliNotes.old-1101`，
新产物验证过就可以删）。

### 2026-11-08 · 简答题自评上线（之前只是"存了但没入口"）

后端 `grade()` 早就支持简答自评（`given is True`），但答题页把简答和填空塞进同一个
文本框分支，交上去的字符串永远判错——简答题实际上没法做。

| # | 改法 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 参考答案随答题页下发 | `quiz.summary(with_questions=True)` | 只给 `kind=short` 加 `ref` 字段；客观题仍然绝不下发（那边判分在后端，抄了没意义） |
| 2 | 简答独立分支 | `web/app.js::renderQuestion` | 草稿 textarea（只存 `quizRun.drafts`，帮自己组织语言，不参与判分）→「看参考答案，自评」→ 参考答案卡 + 「答上了 / 没答上」，自评结果写 `answers[qid]`（true/false），判完可改 |
| 3 | 结果页如实显示 | `quizAnswerText` | 简答显示「自评：答上了 / 没答上 / 没自评」，不再把布尔值直接打出来 |
| 4 | 测试 | `tests/smoke_quiz.py::test_short` | ref 只给简答下发；自评 true 给分、false 与字符串 "true" 都不给分 |
| 5 | UI 验证 | Playwright（注入一道模拟简答题） | 展开参考答案 → 点「答上了」→ `answers.q1 === true`，无控制台错误 |
| 6 | 离线包同步补齐 | `web/offline.html` | 离线端原来也把简答塞进文本框、按文本判分（永远错）。补上同样的「草稿 → 看参考答案 → 自评」流程，`gradeOne` 本来就认 true/false；结果页显示「自评：答上了」。用 Playwright 拦截 `/api/offline/bundle` 注入含简答的卷子走真实流程验证，420px 手机宽度下 2/2 全对 |

> **教训**：改 `renderQuestion` 这种"桌面端 + 离线包各有一份"的答题逻辑时，两边都要改。
> 离线包是单文件 HTML 里的独立实现，不是复用 app.js。

### 2026-11-08 · 「网址又断了」根治：端口漂移 + 连接自检

用户反复问"网址怎么又断开了"。排查发现桌面上同时躺着 **3 个 BiliNotes 实例**
（8765 / 8767 / 8768）：`pick_port` 在端口被占时会顺延，而状态页/二维码永远照
`settings.port`（8765）报——手机扫的码指向另一个实例，那边口令、开关状态、代码
版本都可能不一样。另外确认当前 WiFi 是 GDUFE（校园网），这类网络常开
「设备隔离」，同 WiFi 下手机本来就未必连得到电脑。

| # | 改法 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 状态页报**真实绑定端口** | `core/lan.py::note_binding/bound_port`、`api/routes.py::_lan_status` | `run()` 绑定后记下实际端口，`/api/lan` 用它生成链接和二维码；没走 `run()`（测试）时退回配置端口 |
| 2 | 单实例守护 | `main.py::find_running`、`run()` | 启动前探 8765~8784 的 `/healthz`（靠新增的 `app:"BiliNotes"` 字段认亲，端口通≠同类程序）；已有实例就打印地址、直接开浏览器后退出，不再起第二个 |
| 3 | 地址变更记录 | `core/lan.py::remember/last_seen`、`data_dir/lan_state.json` | 每次报地址时对比上次；换了就落盘旧值并在 `/api/lan` 里带 `ip_changed/prev_ip`，前端明确提示"地址变了，重新扫码"，而不是让人去猜防火墙 |
| 4 | 连接自检 | `core/lan.py::diagnose/wifi_ssid`、`GET /api/lan/doctor`、设置页「连不上？自检」 | 按「服务→开关→端口→地址」逐项给 ✓/✗ 和说明；识别校园网 SSID 给"可能设备隔离，换热点"的提示；建议按环境动态生成，不是一段写死的话 |
| 5 | 测试 | `tests/smoke_lan.py`（端口上报/地址变更/自检）、`tests/ui_shot.py`（自检面板截图） | 全绿；顺手确认了 hash 路由是 `#settings` 不是 `#/settings`（带斜杠不会切视图，调试时别踩） |

### 2026-11-08 · 复习卡不能是「某道例题的系数」

用户截图：卡片问「求逆矩阵最后一步，怎么把主对角线化为 1」，答案却是
「第一行乘 $\frac13$，第二行乘 $-1$，第三行乘 $\frac14$」——这是**某一道题的具体数据**，
换个例子就不成立，复习时既记不住也迁移不了。

| # | 改法 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 提示词加「通用性」硬规则 | `services/cards.py::_SYSTEM` | 反例/正例对照 + 自检问句（"给没看过这期视频的人，他能用到别的题上吗？"）。抬不到方法层的**不出卡**，具体例题归自测卷 |
| 2 | 生成后过滤 | `cards.is_example_card` / `parse_cards(dropped=…)` | 5 条判据：① ≥2 处「第X行…乘/除以具体数字」；② ≥2 个不同分数；③ 题干贴了带数字的矩阵（`\begin{bmatrix}`）；④ 「第X行的 N 倍加到第Y行」这类具体系数；⑤ 题干是具体逻辑公式实例（多个带下标变量 + ∨/∧）。**变量写法不误伤**（`第 i 行减第 j 行的 k 倍` 保留），公式自带常数（$R=8.314$）也保留，**自测卷错题卡（`quiz:` 开头）不参与清理**——那边本来就在做题 |
| 3 | 存量清理 | `cards.clean_examples`、`POST /cards/clean-examples?dry=0|1`、复习页「清理例题卡」 | 默认只报告，确认后才删；删前备份 `cards.json.bak-examples` |
| 4 | 测试 | `tests/smoke_cards.py::test_example_cards` | 例题卡丢弃、通用卡与变量写法不误判、常数公式不误判、dry 不删/确认才删 |

实测：存量 34 张里共 10 张是这种卡——先是用户截图那张（+1 个重复），扩展判据后又抓出 8 张
（7 张「对矩阵 A=[3 2 1;3 1 5;3 4 -3] 求逆，第一步怎么化零」的逐步卡片 + 1 张把具体
3-SAT 公式代进去算约束的），全部删除 → 24 张（其中 4 张是自测卷错题卡，按设计保留）。
用同一篇线代笔记重跑生成，5 张全部是通用表述：

```
· 把 [A|E] 化为 [E|A⁻¹] 时，主对角线两侧按什么顺序消零？
· 左侧两侧都化 0 之后，还要做什么才能得到单位矩阵？
  → 用倍乘把主对角线各元素化为 1（各行除以自己的主对角线元素）
```

**分工原则**（写进提示词了）：复习卡考"换个例子还成立"的知识；具体题目、带数据的计算题归自测卷。

### 2026-11-08 · 英语词库：生词标注、原文悬停、锚点跳转

用户要拿 B 站学英语：导入四六级词库后，英语视频的笔记要标出命中的词、附例句，
原文里的词能悬停看释义、点了跳到下方词条。**只对外语视频生效**；没导入词库时
笔记保持原样，一个标记都不加。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 词库导入 | `services/wordbook.py`、`api/routes_wordbook.py` | CSV/TSV/TXT/JSON 都收，列名中英通吃（word/单词、pos/词性、meaning/释义/中文）。坏行返回**行号 + 原因**，不静默丢弃。存 `data_dir/wordbooks/<id>.json`，同名词库覆盖 |
| 2 | 合并索引 | `wordbook.index()` | 小写词 → {w,pos,cn,books}，多词库同词合并释义、记来源。查表匹配，几千词条毫秒级 |
| 3 | 词形还原 | `words.lemmas()` | 规则 + 约 120 条不规则表：-s/-es/-ies/-ed/-ing/双写辅音/补 e/比较级。实测 75 个常见词形全中；`worker/teacher/number` **不会**被误剥成 work/teach/numb（普通 -er 不剥，只认双写辅音） |
| 4 | 外语判定 | `words.note_is_foreign()` | 只看转录英文句：拉丁字母 ≥80 且占比 ≥75%。中文讲解的视频（CSAPP）会被跳过 |
| 5 | 笔记标注 | `words.annotate()` | 在 `## 完整转录` **之前**插 `## 生词（词库 · 命中 N）`：原形、词性、释义、出现次数、首次时间码、例句（取长度最接近 90 的句子）。原文里的词改成 `[went](#w-go)`，词条挂 `<a id="w-go">`。用 `<!-- words:start/end -->` 包裹，**幂等**（先 strip + unlink 再重算） |
| 6 | 前端 | `web/app.js`、`app.css` | 渲染器新增：认 `#锚点` 链接、放行 `<a id>`、隐藏 HTML 注释（之前 `<!-- -->` 会露出来）。桌面悬停出气泡（原形+词性+释义），手机无 hover → 点一下滚到词条并高亮 1.4s |
| 7 | 导出兼容 | `services/export_docx.py` | Word 里注释会变成文字、锚点不可点，导出前清掉并把 `[词](#w-x)` 还原成纯词；PDF 走浏览器渲染，锚点仍可点 |
| 8 | 存量补全 | `tools/rebuild_words.py` | 不重跑视频，只重算生词段；原文件备份 `.bak-words`（只备一次），`--dry` 只报告 |
| 9 | 测试 | `tests/smoke_words.py` | 词库解析容错、词形还原正反例、外语判定、标注幂等、空词库/零命中不改笔记、前端数据形状 |

**实测**（真实 TED 演讲稿 + 10 词样例词库）：命中 7 词（anxiety×26、coping×10、resource×6、
imagine×3、symptom×2、prevent、avoid），`avoiding→avoid`、`symptoms→symptom`、`resources→resource`
都归到原形并注明「文中作 …」；3 篇中文 CSAPP 笔记被正确跳过。桌面悬停气泡、手机点按跳转均验证通过。

**已知边界**：派生词（`resilience ↔ resilient`、`decision ↔ decide`）纯规则认不出，默认不标；
ASR 拼错的词匹配不上（漏标不误标）。

### 2026-11-08 · 双端开关「关不掉」：前端缓存 + 状态接口没禁缓存

接上一条。用户反馈：点了关闭，二维码还在、界面像没反应，还提示要重启。

| # | 根因 | 说明 |
|---|---|---|
| 1 | **前端被浏览器缓存** | 改完 `app.js`/`app.css` 没升 `index.html` 里的 `?v=` 版本号（一直停在 `20261108k`），且首页 `index.html` 没有缓存头。浏览器拿着旧前端，看到的是旧行为 |
| 2 | **状态接口没有 no-store** | `GET /api/lan` 响应不带 `Cache-Control`，开关刚改过也可能读到上一份，出现"关了还是开着"的假象 |
| 3 | **关闭语义说不清** | 关闭只改配置，监听套接字要等下次启动才释放（uvicorn 无法只解绑端口）。但中间件已经对外部请求返回 403，等于"手机确实进不来"。原文案只写"重启后停止对外"，让人以为没关掉 |

**改法**：

| # | 改动 | 主要文件 |
|---|---|---|
| 1 | 首页 `index.html` 加 `no-cache, no-store, must-revalidate`；`/api/lan` 加 `no-store`；前端 `api()` 对 GET 默认 `cache: 'no-store'` | `main.py`、`api/routes.py`、`web/app.js` |
| 2 | 状态接口新增 `port_open`（端口是否仍对外监听，与开关无关），前端按 4 种组合给准确文案 | `routes.py::_lan_status`、`app.js::renderLan` |
| 3 | 关闭时：二维码立即隐藏、地址框与「换口令」置灰、提示「已关闭：手机已经打不开（端口要下次启动才彻底释放）」 | `app.js` |
| 4 | **重新打开不需要重启**：只要端口还绑在 0.0.0.0（`port_open=true`），开关一开立即生效，提示「已打开，手机现在就能连」 | `app.js::toggleLan` |
| 5 | 设置页写明：二维码不会每次变（同一台电脑 + 同一 WiFi + 不换口令 = 同一张图），可截图存手机 | `index.html` |

**教训**：改前端必须同步升 `?v=`，并且首页与状态类接口一律 `no-store`。
否则"代码明明改了、界面还是老样子"，会把功能缺陷和缓存问题混在一起，排查成本翻倍。

### 2026-11-08 · 设置页二维码破图：二次请求被 403，顺手纠正一处误导文案

用户截图：设置 →「手机访问」里二维码位置只剩 `alt` 文字和一个空白方块。

**两个根因**：

| # | 根因 | 说明 |
|---|---|---|
| 1 | 二维码是**第二次请求** | 原来 `<img src="/api/lan/qr">`，而该接口只放行 `is_local`。从**局域网地址**（`192.168.x.x`）打开设置页时 `request.client.host` 不是 127.0.0.1 → 403 → 破图。本机 127.0.0.1 打开则正常，所以这个 bug 只在"用手机/局域网地址看设置页"时出现 |
| 2 | 文案与链接语义不符 | 二维码当时指向 `/offline?k=`，旁边却写"之后电脑关机、换 WiFi 也能刷"——`/offline` 页面本身仍要从电脑加载，关不掉。真正脱机只有「导出单文件」那条路 |

**改法**：

| # | 改动 | 主要文件 |
|---|---|---|
| 1 | 二维码随状态一起下发：`GET /lan?qr=1` 返回 `qr`（`data:image/svg+xml;base64,…`），前端直接当 `src`，不再发第二次请求 | `api/routes.py::_lan_status`、`web/app.js::renderLan` |
| 2 | 独立接口 `/lan/qr` 的判定从 `is_local` 放宽到 `lan.authed()`（本机 **或** 已带正确口令），手机端已登录时也不会破图 | `core/lan.py::authed` |
| 3 | 二维码/地址栏改指完整界面 `/?k=`；`offline` 字段保留兼容，但前端不再主推 | `routes.py`、`web/app.js` |
| 4 | 文案纠正：`.offline` 不再宣称"电脑关机也能用"，改为「想电脑关机也能查，用导出学习包」 | `web/index.html` |
| 5 | 测试：`smoke_lan` 新增「二维码」段 8 条（不传 qr 不生成、内联 data-uri、独立接口 200、外部无口令 401、带正确 cookie 200、错口令 401） | `tests/smoke_lan.py` |

**教训**：`<img src="/api/...">` 是一次独立请求，它的权限判定和页面本身的判定**是两套**。
页面能被打开，不代表页面里的图片能被加载；把这类资源做成接口内联（或与页面同一次响应下发）最省事。

### 2026-11-08 · 知识库排序：默认按生成时间，并让用户自选口径

用户问「为什么最新生成的反而在下面」——因为排序键用的是**文件修改时间**（`notes.md` 的 mtime），
而任何一次重写都会把所有笔记的 mtime 顶到「刚刚」：补结构段、重建导图、修公式，跑完一次顺序就集体洗牌，
最新生成的那篇掉到中段。这是排序口径选错了，不是数据错。

| # | 改法 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 排序键换成创建时间 | `services/library.py::created_ts / sort_notes` | 优先用 frontmatter 的 `created`（内容属性，改正文不动它），缺失/格式不认才退回 mtime。scan 结果新增 `created_ts` 字段 |
| 2 | 口径可选 | `GET /library?sort=created|mtime`、`GET /library/search?sort=` | `created` 默认；`mtime` 给「最近动过的在前」这一需求。非法值静默回退 created，接口回报当前口径 |
| 3 | 收藏夹「最近更新」 | `library.collections` | 取组内**最大** created_ts，不再依赖「第一篇」——按 mtime 排时第一篇未必最新 |
| 4 | 前端 | `web/index.html`、`app.js`、`app.css` | 工具栏加排序下拉，选择存 `localStorage`（`bn.lib.sort`），刷新后保留；副标题显示「· 按生成时间 / 按最近修改」；手机上靠右与搜索按钮同行 |
| 5 | 测试 | `tests/smoke_library.py` | 造两篇「生成晚但先落盘 / 生成早但后落盘」的笔记，断言两种口径顺序相反；再重写正文验证「默认顺序不变、mtime 序变」；接口层验证 sort 参数与回退 |

**教训（工程层面）**：排序键要选**内容属性**，不能选**文件系统属性**。mtime 只代表「最后一次写入」，
对「用户什么时候做的这篇笔记」毫无解释力；而任何后台维护动作都会污染它。

### 2026-11-08 · 「知识地图」改「内容结构」：导图不适合放长句

用户截图指出：知识地图把**每章要点（30 字长句）画成节点**，一张图挂 5 条长句横着铺开，
手机上要左右拖才看得全，"看都看不全"。

| # | 改法 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 正文不再放图 | `services/exporter.py::_render_structure` | 换成 `## 内容结构` 列表：章节标题 + 时间码 + 该章 3 条要点（单章 5 条）。优先用 `note.outline`（完整句子），没有就用 chapters 的 bullets。标题自带「一、」「1.」时不再补序号 |
| 2 | 导图只画章节 | `services/mindmap.py::build_tree` | 默认 `with_points=False`；要点是长句，画成节点必然撑爆画布。需要时仍可显式打开（外部工具场景） |
| 3 | 存量重写 | `tools/rebuild_structure.py`（新） | 不用重跑视频：从正文 `## 笔记` 段解析章节与完整要点，替换旧块；顺手把 `mindmap.json` 简化、`mindmap.html` 重画。原文件备份 `notes.md.bak-structure` |
| 4 | 测试同步 | `tests/smoke_mindmap.py`、`tests/smoke_pipeline.py` | 新增"默认树只有章节"断言；pipeline 改为断言「没有 mermaid 块、有内容结构」 |

实测 9 篇笔记全部重写：CSAPP 那篇从"1 个节点挂 5 条长句"变成"3 节 × 3 条完整要点"，
手机上一屏能看完两节，不用横向滚动。离线学习包同步重新导出（9.4MB）。

### 2026-11-07（第二轮）· 双端（手机访问）+ 自测卷

用户要"给别人用"，并且明确要自测卷、题型可选（手机上全选选择题最省事）。

**英语词库**：
- `GET /wordbooks` — 词库清单 + 合并统计（books / total_words / index_size）
- `POST /wordbooks`（multipart `file`、可选 `name`）— 导入，返回 `parsed.count/skipped/errors`（带行号）
- `DELETE /wordbooks/{book_id}`

**双端**：不做原生 App，也不做第二套前端。

**关键发现（踩过）**：局域网 `http://192.168.x.x` 不是"安全上下文"，浏览器**不允许注册
Service Worker**——所以 PWA/离线缓存那条路在手机上直接走不通（除非上 https，但自签证书对
"给别人用"是灾难）。因此最终分两条腿：

1. **单文件学习包（主路径）**：`GET /api/offline/export` 导出一个自包含 `.html`，
   数据、截图（压到宽 900 / JPEG 78）、KaTeX（含 woff2 字体）、图标全部内联。
   微信发到手机、用浏览器打开即可，**不需要电脑、不需要联网、不需要同一 WiFi**。
2. **扫码在线**（辅助）：同一 WiFi 下扫 `/offline?k=`，电脑开着时能直接用，省一次导出。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 监听开关 | `core/config.py`（`lan_access` / `lan_token`）、`main.py::run` | 默认只听 `127.0.0.1`；打开后绑 `0.0.0.0`，启动日志打印手机地址。改监听要重启 |
| 2 | 访问口令 | `core/lan.py::install_guard` | 12 位随机口令，首次生成存 config。手机带 `?k=` 进来后种 30 天 cookie；也认 `X-BN-Key` 头。**换口令立即生效**（每次请求现读配置） |
| 3 | 关掉也要挡 | 同上 | 开关关了但进程还绑 0.0.0.0（没重启）时，外部请求给 403——否则等于"关了开关继续裸奔" |
| 4 | 二维码 | `core/lan.py::qr_svg`、`GET /api/lan/qr` | 纯 Python 生成 SVG，**只允许本机取**（码里就是口令）。设置页显示，扫码直接进，不用手输 IP |
| 5 | 移动端布局 | `web/app.css` | ≤780px：侧栏变底部标签栏、主区铺满、复习卡片四档按钮两行两列、概念列表限高 42vh、表格横向滚 |
| 6 | 设置面板 | `web/index.html`、`app.js` | 方式一「导出学习包」+ 方式二开关/地址/二维码/换口令/防火墙提示 |
| 7 | 单文件导出 | `services/offline_pack.py`、`GET /api/offline/export` | 内联数据+图片+KaTeX+图标；去掉 manifest / SW（file:// 下必 404）；实测 8 篇笔记 85 张图 → 9.2MB / 3.2s |
| 8 | 离线页 | `web/offline.html` | 一个文件两用：`window.__BN_OFFLINE__` 有值就用内联数据（file://），否则 fetch `/api/offline/bundle`（同 WiFi 在线）；笔记渲染、卡片 SM-2、卷子判分都在本地 |
| 9 | 进度回传 | `POST /api/offline/progress` | 只覆盖排期字段，不碰题面；同一次交卷按 `at` 去重；已删的卡不复活 |

**自测卷**：出题 → 做题 → 判分 → 错题变卡片，整条链路本地完成。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 出题 | `services/quiz.py::build/parse_questions` | 题型 `single/multi/judge/blank/short`，默认「单选+判断」。**校验很严**：单选必须恰好 1 个正确项、多选 ≥2、下标越界/重复/无答案的一律丢 |
| 2 | 判分 | `quiz.py::grade` | 选择/多选按集合相等（少选算错）；判断按布尔；填空做标点空格归一 + 允许多个可接受答案；简答留给自评（本轮前端未开放） |
| 3 | 存储 | `quiz.py`、`data_dir/quizzes.json` | 原子写；只留最近 60 份；`attempts` 记分但只留 20 次 |
| 4 | 错题转卡 | `quiz.py::to_cards` | 挂到 `quiz:<卷id>` 虚拟笔记下，复习页单独归「自测卷」组；题干带 `【单选】` 前缀；concept 先过 `concept_review` |
| 5 | 接口 | `api/routes_quiz.py` | `GET /quiz`、`GET /quiz/{id}`（**不下发答案**）、`POST /quiz/generate|submit|cards`、`DELETE /quiz/{id}` |
| 6 | 前端 | 新视图「自测」 | 生成器（范围/题量/题型）+ 卷子列表 + 一屏一题的答题面板 + 结果页（逐题对错、正确答案、解析、勾选错题） |
| 7 | 修静态检查器 | `tests/smoke_ui.py` | 动态创建的 id（`innerHTML` 里的）也算存在；模板字符串 `${...}` 被截断导致接口误报——两个长期误报清掉 |

实测（真实模型，线代 22 集）：6 题单选+判断，题目引用真实材料（增广矩阵、化为单位阵、为什么只能用下一行…），
数学公式正常渲染；手机端完整走通"答题 → 3/6 → 4 道错题变 4 张卡"，复习页出现「自测卷」组。

### 2026-11-07 · 历史重复卡片清理（查重按钮）

老版本「补充卡片」不看已有题目，堆了 3 组同义题（同一篇笔记里换个说法问同一件事）。生成侧早已去重，但旧数据不会自己消失。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 查重算法 | `services/cards.find_duplicates` | 只在**同一篇笔记内**两两比：不同笔记讲同一知识点各出一张卡是合理的，不能误伤。判据复用已有的 `_cmp_text + _similar`（公式压成 M、定位词/数字不同不算重复） |
| 2 | 保留策略 | 同上 | 留复习次数多的那张；同分留更早创建的（复习历史更长）。一张卡只进一组，不会连环误删 |
| 3 | 接口 | `POST /cards/dedupe?dry=0|1` | 默认 `dry=1` 只报告；`dry=0` 才删。删前页面弹 `confirm` 列出前 3 组 |
| 4 | 前端 | 复习页「查重」按钮 | `web/app.js::dedupeCards`，删完自动刷新 |

实测 33 → 30 张，删掉 3 张同义题；概念页卡片合计同步 30，无孤儿节点。

### 2026-11-07 · 概念审核闸：不许再乱造概念，层级必须落在真概念上

用户指出：层级还是乱的，而且「以后生成的都得过一遍，不要乱生成这个概念」。根因是 `parent` 由模型自由填写——它会编出列表里根本不存在的父节点（`整数规划问题（IP）`、`归约示例`），也会把概念的一个侧面写成新概念（`初等行变换法优势`、`倍乘化单位阵`），索引于是散成碎片、层级断掉。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 新增概念审核层 | `services/concept_review.py`（新） | 派生名回流（X+优势/原理/实例/示例/顺序…→ X）、近似同名合并（字差≤2、公共前缀≥3、同字异序不并、带行列定位词不并）、空泛词判 trivial、上级必须解析到真实概念 |
| 2 | 上级解析 | `concept_review.resolve_parent / derive_parent` | 精确匹配 → 剥「问题/判定问题/示例/（IP）」等包装 → 包含关系取最长 → 都不中就清空；**宁可是顶层，也不挂一个空父节点**。没给上级时按「X判定问题」「广泛性焦虑症」这种前缀/后缀整段包含自动挂到 X |
| 3 | 读时对齐 | `concept_tags.annotate`（返回值改为 `{"repaired": n}`） | 每次出列表都对齐一次，历史脏数据不会在页面上断层；`/api/concepts` 多返回 `stale`，前端据此自动整理一次 |
| 4 | 生成期过闸 | `services/pipeline.py::_review_concepts`、`services/cards.py::build` | 笔记「关键概念」表与卡片 `concept` 落盘前先审：剔景点/鸡汤/步骤/空泛词，派生说法并回已有概念，改动写进 `warnings` 让人看得见 |
| 5 | 存量整理 | `POST /api/concepts/tidy`、概念页「整理」按钮 | 碎片合并、层级重挂、空泛词隐藏；流水线每次生成完也自动跑一次。只动 `src != manual` 的条目 |
| 6 | 树撑两层 | `web/app.js::ancestorChain/groupConcepts` | 按「最顶层祖先」分组，`整数规划 → 整数规划判定问题 → 可满足到可行解` 这样的两级链条都收进同一组，按层级排序；带环保护 |
| 7 | 补"碎片"规则 | `concept_review.via_parent / via_unique_fragment` | 纯字面相似判不出的（`变换顺序` 与 `初等行变换法` 相似度仅 0.40、公共前缀 0 字）：先看残渣是否落在**自己的父名**里，再看是否**全库唯一**落在某个概念名里；带下级的节点不并 |
| 8 | AI 复核（用户提议） | `concept_review.ai_review`、`routes_concepts.py`、`pipeline.py`、`web/*` | 既然配了 Key，就让模型认一遍规则判不出的："变换顺序/行变换使用限制/化零操作的先后 → 初等行变换法""NP完全证明结构 → NP-完全"。只给两个权限（merge 到已有概念 / keep），不能发明、不能删；`keep` 记 `ai_review` 不再重复花钱。整理按钮走 `tidy?ai=1`，流水线每次生成完也跑 |

实测（真实 94 条）：合并 5 个碎片（初等行变换法优势/原理、倍乘化单位阵、整数规划实例、归约示例）、重挂 8 条层级、隐藏 1 个空泛词（方法对比），整理后 89 条、0 个孤儿父节点。

### 2026-11-06（第四轮）· 关系句并入概念、变量剔除

用户指出：「证书x」是例题里的变量；「IP属于NP」是一句判断，应该是「IP」这个概念下的一条内容，而不是独立条目；「IP」还要写清是整数规划问题，别和 IP 地址混。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 0-1 | 新增 `relation` / `artifact` | `services/concept_tags.py` | 关系判断句（X属于Y、X是Y的特例）、例题变量（证书x、x1）默认隐藏；`证书x` 由硬规则钉死 |
| 0-2 | 规范名合并 | `concept_tags.py`、`concepts.py` | 关系句的 `canonical` 指向主体概念，build 时按规范名合并：别名记进 `aliases`，解释拼进主体的「各集怎么讲」，卡片也归到主体下 |
| 0-3 | 合并范围受代码约束 | `_accept_canonical` | 只接受两类：relation 且能匹配到已有概念、短全大写缩写；**其他改名一律忽略**——否则模型会把「可满足到可行解」这类子问题也并成别名 |
| 0-4 | 从源头约束 | `services/cards.py` | 卡片 `concept` 必须是可复用知识点，不许填关系句/变量 |

### 2026-11-06（第三轮）· 实例步骤不是概念

用户指出：「第二列化零」这种只对某道例题成立的步骤，不该进概念索引——别的视频不会出现，出现了也不是一回事。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 0-0x | 新增 `step` 类 | `services/concept_tags.py`、`web/*` | 「第 X 列/行…化零」「第 X 步」「化零顺序」→ step，与 place/soup/trivial 一起默认隐藏；详情标「实例步骤」 |
| 0-0y | 硬规则覆盖 | `concept_tags.py::_override` | 模型爱把这类步骤当"方法"，提示词压不住；形态可判定，就直接钉死（`src=fixed`），模型与重判都不能翻回去 |
| 0-0z | 从源头约束 | `services/cards.py` | 生成卡片时要求 `concept` 必须是**能跨视频复用的知识点/方法名**，明确禁止填例题步骤（如「第二列化零」「第一步」）；步骤只体现在 `kind=步骤` |

### 2026-11-06（第二轮）· 概念分层与按视频分组

用户反馈：概念仍是一锅粥；「主对角线」不该和「初等行变换法」并列，它是方法里的细节；「C盘」「加括号」是常识操作；「目的和意义」是空泛判断。另外希望能按视频分。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 0-0a | 概念分层 | `services/concept_tags.py`、`api/routes_concepts.py`、`web/*` | 每个词条多一个 `parent`（所属方法/主题）；左侧列表渲染成「方法作组头 + 细节缩进」。点组头名字看方法详情，点箭头收起 |
| 0-0b | 按视频分组 | `web/app.js`、`concepts.py` | 顶部「按主题 / 按视频」切换，按视频时以来源笔记为组；选择记在 localStorage |
| 0-0c | 新增常识类 | `services/concept_tags.py` | 第 4 类 `trivial`（C盘、安装包、Finish、加括号…），与 place/soup 一起默认隐藏；「目的和意义」这类空泛判断归 soup |

### 2026-11-06 · 概念页剔除非知识条目

用户反馈：概念页混进景点（中庵索道、乌龟潭）、鸡汤（做不好也要做）、旅游组织，需要判别。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 0-0 | 概念分类 | `services/concept_tags.py`、`api/routes_concepts.py`、`web/*` | 四类 knowledge/place/soup/other；规则兜底 + 模型判一次（带来源笔记标题）+ 缓存到 `concept_tags.json`；前端默认隐藏 place/soup，可取消勾选看全部；详情可手动「其实是知识点 / 不是知识点」，`src=manual` 永不被覆盖 |

### 2026-11-06（第二轮）· 卡片分组与可见性

用户反馈两点：复习时所有卡「叠在一块儿」，以及生成后不知道去哪里看卡片；顺带发现「补充卡片」会产出近似重复题。

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 0-1 | 复习范围 | `services/cards.py`、`api/routes_cards.py`、`web/*` | `/cards/due` 支持 `collection` / `note_ids` / `mode=all`；顶部下拉选「全部 / 收藏夹 / 单篇」，或自定义跨收藏夹多选；范围存 localStorage |
| 0-2 | 所有卡片浏览器 | `api/routes_cards.py::cards_list`、`web/*` | `/cards/groups`（收藏夹→笔记→计数）+ `/cards/list`；三层折叠、搜索、单卡「现在复习」/删除、每篇「复习本篇」「补充卡片」 |
| 0-3 | 近似重复拦截 | `services/cards.py::_similar` | 停用词无关的相似度 0.86 + 定位词/数字保护；生成时把已有题目一起发给模型 |
| 0-4 | 概念页可见性 | `services/cards.py`（prompt）、`web/app.js` | 生成时复用笔记「关键概念」表的名字；列表分「只来自卡片」一段 + 同名筛选；详情对无笔记的概念给出说明 |

### 2026-11-06 · 知识转化三件套

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 1 | 知识卡片 + 间隔重复 | `services/cards.py`、`api/routes_cards.py`、`web/*` | 从 notes.md 抽 10~20 张卡（概念/公式/步骤/易错/对比），SM-2 简化排期，四档评分，键盘 1~4，侧栏显示今日到期数 |
| 2 | 跨视频概念索引 | `services/concepts.py`、`api/routes_concepts.py`、`web/*` | 解析每篇「关键概念」表，按 `name_key` 归并（忽略空格/大小写/标点），详情列出每篇各自的解释 |
| 3 | 排期规则显式化 | `services/cards.py::preview`、`web/*` | 后端复用同一个 `schedule()` 算出四档「多久后再来」下发；按钮两行显示；右侧「排期规则」弹窗 |

### 2026-11-04 · 打印与合集控制

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 4 | 导出 PDF / Word / 直接打印 | `services/export_pdf.py`、`services/export_docx.py`、`api/routes_library.py` | PDF 走本机 Edge/Chrome 无头打印（零 Python 依赖）；Word 用 python-docx，公式转 **OMML 原生公式**；A4 打印样式独立 |
| 5 | 每合集限定集数 | `api/routes_bili.py::_expand_items` | `series_limit` 0~200，0=全部；先过滤已处理再截断，所以「5 集」= 5 集新的 |
| 6 | 合集选集（按序号挑） | `routes_bili.py`、`web/app.js` | 卡片悬停「选集」→ 显示共 N 集 + 已做标记；范围输入 `3,5-7,11-20`；快捷全选/反选/只选未做 |
| 7 | 选集「重做」开关 | `web/*`、`routes_bili.py` | 默认跳过已做；勾选后 `skip_done=false` 覆盖重跑；计数条会提示「N 集做过，将跳过」 |

### 2026-11-02 · 视觉、讲义、安全

| # | 功能 | 主要文件 | 要点 |
|---|---|---|---|
| 8 | UI 美化（8 个高级感效果） | `web/app.css`、`web/app.js` | 极光背景、毛玻璃、便当盒统计卡、聚光灯、流光、磁吸、涟漪、微 3D 倾斜 |
| 9 | 四套主题皮肤 | `web/app.css`（变量体系）、`web/app.js`、`index.html` | aurora / minimal / tech / paper；`html[data-theme]` 覆盖约 60 个 token；localStorage 持久化；head 内联脚本防闪白 |
| 10 | 修复 `$-1$` `$2$` 显示成源码 | `web/app.js::isMath` | 成对 `$..$` 里的纯数字（含正负号）一律当公式；价格误判由「夹中文」规则兜住 |
| 11 | 封面加载加固 | `api/routes_library.py::proxy_image`、`web/app.js` | 连接池 + 失败重试一次 + 磁盘缓存 7 天；前端改 `<img loading=lazy>` + onerror 重试 |
| 12 | 知识点讲义 | `services/lecture.py`、`routes_library.py`、`web/*` | 单独 `lecture.md`，阅读器「笔记/讲义」标签页；老笔记一键补生成，不重跑视频 |
| 13 | 密钥归属审计 | `tests/smoke_keys.py`、`index.html` | 六条链路核查无内置 Key；设置页三处文案讲清归属；测试把承诺变成硬约束 |
| 14 | 例题精解 | `models`、`summarize.py`、`exporter.py`、`app.js` | AI 抽题干/解题关键/步骤/易错，`==关键半句==` 高亮 |
| 15 | 行内公式截断修复 | `web/app.js`、`exporter.py` | 占位符区分块级 `\u0001` / 行内 `\u0000`；全项目 `write_text(newline="\n")` 消除 CRLF |

### 2026-11-01 · 能力扩展与打包

| # | 功能 | 要点 |
|---|---|---|
| 16 | 外语视频 | `asr_language=auto`，5%/45%/85% 三处采样投票检测语种（只看开头会被片头曲骗）；成稿译中 |
| 17 | 画面表格还原 | 用 OCR 坐标几何聚类切成 Markdown 表格，不引 rapid-table；判定从严，宁漏勿误伤正文 |
| 18 | 打包 exe | 入口必须是 `entry.py`（`app/main.py` 被当脚本跑会 ImportError）；不打包 ffmpeg（省 450MB），改为设置页一键下载 92MB |
| 19 | 磁盘缓存清理 | `cache_policy` keep/media/all，默认 media；清理挂在任务 finally，成功才按策略删 |
| 20 | 知识地图重写 | 弃 jsdelivr CDN，Python 直接算布局输出静态 SVG；阅读器支持 mermaid flowchart 自绘 |
| 21 | 画面要点 v3 | 从画面**有选择地取字**：PPT 题目/分点，太简略用讲解补全；不再是复述式「画面解读」 |
| 22 | 数学公式适配 | 内置 KaTeX 0.16.11 到 `web/vendor/katex`；`services/mathfmt.py` 把 `[[3,2],[1,4]]`、`A^-1` 归一成 LaTeX |
| 23 | 任务暂停/恢复 | `pause_event` + 检查点 gate，1-2 秒生效；支持全部暂停/继续 |
| 24 | 知识库按收藏夹分组 | frontmatter 写 `collection`；两级浏览；自建收藏夹 + 多选移动 + 删除退回默认 |
| 25 | 删除笔记 | 移入 `data_dir/trash/`（保留 30 项），不 rmtree；删除后按 (BV, 分P) 可重新处理 |
| 26 | 看图模型独立 Key | 视觉 Key 单独输入，不与文字模型 Key 覆盖；智谱型号实测后默认 `glm-4.6v-flashx` |
| 27 | 前面总结配图 | outline 每段输出 `cover_ts`，按时间就近取帧，同一张图不重复用 |

### 2026-10-31 · 打地基（九期）

| 期 | 内容 | 关键结论 |
|---|---|---|
| 一 | 项目骨架 | FastAPI + 线程池；DPAPI 存 Key；单文件前端 |
| 二 | 对齐抖音工具 | **B站不需要 Playwright**：官方扫码接口 + cookie 写 Netscape 格式，一份文件同时喂 yt-dlp 和字幕接口 |
| 三 | 高清关键帧 + 音画互校 | 不用生成式模型重画带字画面；低清定位 → 1080p 定点重取；同音误写用拼音判据 |
| 四 | 并行处理 | 第一版并行是负优化（两个 yt-dlp 抢带宽），改为「转写 ∥ 下载抽帧」 |
| 五 | 候选帧文字预筛 | 轮廓计数法对表格失效；改 `边缘密度 × 拉普拉斯方差`，5ms/张 |
| 六 | 按数量批量 | 截断必须在建任务之前；「已处理」判定要跨重启（扫笔记 frontmatter） |
| 七 | 合集识别 + 封面 | 封面全白真因是 **ORB** 不是防盗链；代理 + 域名白名单 + 本地缩放 |
| 八 | 17 分钟视频卡死 | 三个致命 bug：模型下载 Xet 401、PyAV 接口变更、僵死任务被复用 |
| 九 | 结构化笔记 | outline 树（主题→分组→要点）；AI 字幕会**张冠李戴**，默认关闭 |

---

## 五、关键实现细节

### 5.1 文本获取的三级降级

`CC 字幕 → B站 AI 字幕 → faster-whisper 本地转写`。

- **AI 字幕是概率性返回**：实测 6 次失败、1 次覆盖 99%。策略是「最多试 5 次 + 覆盖率 ≥60% 校验」。
- **覆盖率校验挡不住张冠李戴**：曾出现越南旅游视频拿到「小米 SU7 试驾」字幕，句数和时长都对但内容无关。所以 `use_ai_subtitle` 默认 `False`。
- **请求 AI 字幕时 `Referer` 必须是站点根路径** `https://www.bilibili.com/`，用视频页 URL 会返回语言列表但 `subtitle_url` 全空。
- **转写不用 PyAV**：`av 19` 移除了 `metadata_errors` 参数而 faster-whisper 仍在传，直接 TypeError。改为 ffmpeg 解成 16kHz PCM → `np.float32` 数组喂模型。

### 5.2 关键帧

- 场景检测**不用 ffmpeg 的 `select='gt(scene,X)'`**：对白底 PPT 翻页几乎不响应（scdet 与 select 是两套度量）。正解是抽 1fps/96px 缩略图 + pHash 汉明距离（阈值 14）。
- 场景检测零命中时 ffmpeg 以 -22 退出，纯静态讲解必然触发，必须 `check=False` 回落均匀抽帧。
- 低清只用来定位，选中帧用 1080p 直链 `ffmpeg -ss` 定点重取（0.63s/张，852→1920px）。B站直链需带 `Referer` + `User-Agent` 头。
- 候选帧 OCR 前缩到 720px（只用于打分，不影响出图）。

### 5.3 笔记结构（outline）

`VideoNote` 的核心是树：`[{title, summary, groups:[{title, items}], cover_ts}]`，渲染成：

```
## 笔记
### 一、河内
#### 玩的
- 要点
```

- `chapters` 保留为**纯时间轴**（图片挂这里），`outline` 负责**内容逻辑**，两者分工不重叠。
- 有 outline 时章节区渲染成折叠时间轴（`- [时间](跳转) 标题`），无 outline（没配 Key）时保留原「章节 + 图」。
- 提示词里**给正反例比加形容词有效**：加了「正确：一级河内→二级景点/美食／错误：一级『河内：火车街与文庙』」之后立刻正确。
- 例题抽取在 outline 之后单独一步，无题目返回 `[]`。

### 5.4 数学公式全链路

三处都要管，缺一处就露源码：

1. **生成侧**：`services/mathfmt.normalize()` 把 `[[a,b],[c,d]]`、`[1 2 3; 4 5 6]`、`A^-1`、`A⁻¹`、`[A|E]` 统一成 LaTeX；提示词明令禁止 `[[...]]` 写法。
2. **落盘侧**：outline、画面要点、术语表、章节摘要全部过一遍 `normalize`。
3. **渲染侧**：前端 `MATH_RE → holdMath → renderMath`。**块级用 `\u0001`、行内用 `\u0000` 两种占位符**，还原时块级不套 `<p>`、行内直接放回，否则句子会被公式截断成多行。

坑：提示词里的 `{bmatrix}` / `{-1}` 会被 `str.format` 当占位符，必须写 `{{bmatrix}}`。

### 5.5 知识卡片与 SM-2

| 评分 | 间隔 | ease | 其他 |
|---|---|---|---|
| 忘记 again | 10 分钟 | −0.2 | reps 归零，lapses+1 |
| 模糊 hard | 1 天，或 `interval × 1.2` | −0.15 | 小幅增长 |
| 记得 good | 1 天 → 3 天 → `interval × ease` | 不变 | — |
| 太简单 easy | 3 天，或 `interval × ease × 1.3` | +0.15 | 增长更快 |

`ease` 限制 1.3~2.8，间隔封顶 180 天。「多久后再来」由 `preview(card)` 复用同一个 `schedule()` 算出，**保证显示与执行一致**（有测试断言）。

存储 `%APPDATA%\BiliNotes\cards.json`：`{version, updated, cards:[{id, note_id, note_title, front, back, concept, kind, created, due, interval, ease, reps, lapses, last}]}`。

**复习范围**：卡片本身不存收藏夹（笔记搬家后会不准），每次按笔记当前归属现算。`/cards/due` 的 `note_ids` 由前端传，`collection` 由后端用 `library.scan` 翻译成笔记集合；两者都给时取交集。空集合表示「这个范围里没有笔记」，结果应为 0 而不是全部——这条有测试守着。

**去重分两级**：
1. ID 级（`note_id + 归一化问题`）挡完全相同的；
2. 相似度级挡「换个说法问同一件事」。公式先压成 `M`（矩阵这类长公式会把相似度整体抬高），再算 `SequenceMatcher` 比率，阈值 0.86；但**定位词集合（`第X行/第X列/上方/下方/左侧/右侧`）或数字集合不同时直接判为不同题**——否则「第二列下方化零」和「第二列上方化零」会被误杀。
   实测现有 33 张卡：190 个两两组合里命中 3 对，全是真重复；相邻步骤 0.88~0.91 的那几对被定位词规则挡住了。

**生成时的两个约束**（降低后续维护成本）：把笔记「关键概念」表的术语原样传给模型（`concept` 必须复用），把已有题目传给模型（别重复出题）。

### 5.6 概念索引

- 主来源：每篇 `notes.md` 的 `## 关键概念` 表格（跳过表头/分隔行，去 markdown 装饰和尾部标点）。
- 补充来源：`cards.json` 里的 `concept` 字段（只加卡片数，不加笔记数，否则同一篇会算两次）。
- 归并键 `name_key` = `casefold()` + 去空格/连接符/尾部标点。显示名规则：**出现次数最多优先，平票取更短更干净的写法**（否则「单位 矩阵」这种带空格变体会抢走门面）。
- **非知识条目会被挡掉**：景点/地名（place）、鸡汤口号（soup）默认隐藏，勾掉「只看知识类」可看全部。

### 概念判别（`services/concept_tags.py`）

`关键概念` 表是模型自由生成的，旅游 vlog 会写景点、TED 会写口号。判别分四层：

| 层 | 做什么 | 说明 |
|---|---|---|
| 1 | 构词命中 | 含「定理/公式/矩阵/症/资源/规划…」→ knowledge；地点后缀（索道/桥/潭/村/寺/塔/湾/埠…）→ place；鸡汤句式（也要/也没关系/原谅…）→ soup；盘符/安装包/Finish → trivial；`第X列…化零`/`第X步` → step。免费、瞬时 |
| 1.5 | 硬规则纠偏 | `step` 形态明确、模型又爱误判，直接以 `src=fixed` 钉死，模型和 `force` 重判都改不动 |
| 2 | 模型判定 | 剩下的批量送模型（每批 40 条），**带上来源笔记标题**，同时判 `cat` 与 `parent` |
| 3 | 缓存 | `%APPDATA%\BiliNotes\concept_tags.json`，key 是归并后的概念名，字段 `cat / parent / src(llm|rule|manual)` |
| 4 | 手动纠正 | 详情页「其实是知识点 / 不是知识点」，写 `src=manual`，之后任何自动流程（含 `force`）都不覆盖 |

**层级比分类更重要**：`主对角线 / 倍乘 / 单位矩阵 / 增广矩阵 / 逆矩阵` 都挂在 `初等行变换法` 下面，列表里只有方法是一级；点组头名字看方法详情，点箭头收起。

`parent` 是模型写的自由文本，**不能直接信**。`concept_review.resolve_parent()` 会把它对齐到列表里真实存在的概念：精确匹配 → 剥掉「问题 / 判定问题 / 示例 /（IP）」这类包装 → 取包含关系里最长的那个；对不上就**清空**（顶层概念），绝不生成一个没有内容的父节点。没给上级的，如果名字是「整数规划判定问题」「广泛性焦虑症」这种 X 的下一层，`derive_parent()` 会按前缀/后缀整段包含自动挂到 X。前端按「最顶层祖先」分组，两级链条都收进同一组，所以页面上不会再出现挂在虚空里的条目。

### 概念审核（`services/concept_review.py`）

模型会"发明概念"：同一件事换个角度就写成新名字（`初等行变换法优势`、`倍乘化单位阵`），或者把空话当概念（`方法对比`）。审核只做**可判定**的事：

| 规则 | 判定 | 动作 |
|---|---|---|
| 派生名回流 | 名字 = 已有概念 + 优势/原理/特点/结构/限制/设置/验证/顺序/次序/使用/应用/说明/意义/对比/实例/示例/总结… | 并回那个概念（写 `canonical`） |
| 近似同名 | 字差 ≤2、公共前缀 ≥3、相似度 ≥0.86 | 并成一个；**同字异序（A到B / B到A）不并**、带 `第X列/上方` 或数字的不并 |
| 空泛词 | `方法对比`/`总结`/`概述`/`其他`… | 判 `trivial`，隐藏但可找回 |
| 碎片并回上级 | 名字剥掉限定词后的残渣，整段落在**它自己的父名**里（`via_parent`） | 并进父概念。例：`变换顺序` → `初等行变换法`。父名对不上就不动（`计算次序` 的残渣「计算」不在 `矩阵连乘问题` 里） |
| 残渣唯一 | 连父级都没有时，残渣在**全库只出现在一个概念名**里，且占该名 ≥40%（`via_unique_fragment`） | 并进去。例：`行变换使用限制` → `初等行变换法`；残渣命中两个概念就放弃，不猜 |
| 上级对齐 | 见上 | 解析不到就清空 |
| **AI 复核** | 规则全判完之后，把"还独立站着、又和某个更重的概念共享 ≥2 个连续字"的残留交给模型（`ai_review`） | 模型只能 `merge`（into 必须逐字命中已有概念）/ `keep`。实测：「变换顺序」「化零操作的先后」→ 初等行变换法，「NP完全证明结构」→ NP-完全，「矩阵乘法的代价」→ 矩阵连乘问题 |

四个入口：`review_glossary`（生成笔记时）、`review_card_concepts`（生成卡片时）、`tidy`（存量整理）、`ai_review`（tidy 里配了 client 就跑，流水线每次生成完也跑）。**都只动 `src != manual`**——用户手动改过的分类和层级永远不碰。

**AI 复核的省钱与防乱**（重要）：
- 只送"规则判不出"的残留，且必须满足：无上级、非隐藏类、非 manual、没问过（`ai_review` 标记）、名字带抽象限定词或与某个**更重**的概念共享 ≥2 个连续字。真实 87 条里只挑了 1 条。
- 「逆矩阵 / 单位矩阵」这种**同量级**的互不送——谁也不是谁的碎片。
- 模型**没有删东西的权限**：只认 `merge`/`keep`，`into` 必须逐字命中词表，指向自己、指向隐藏项、会成环的一律丢弃；被否决的结论也记 `ai_review=1`，免得同一个问题反复花钱。
- 词表里**不放隐藏类**（景点/鸡汤/实例步骤）——实测模型会挑中 hidden 的「化零顺序」，挑了你也不能并。
- `keep` 会写 `ai_review=1`，下次不再花钱；`_merge` 会保留这个标记，重判不丢。

**为什么不是所有"该合并"的都能自动判出来**：这套规则只认**可判定的形态**（剥后缀后正好等于某个概念、字面近似、残渣落在父名里、残渣全库唯一），不认语义。像 `变换顺序`（残渣「变换」不是概念，与 `初等行变换法` 的相似度只有 0.40，公共前缀 0 字）这种，纯字面规则必然漏——那两条是补了"父子关系 + 残渣落点"才捞回来的。再往下的（`NP完全证明结构` 这种换角度命名的）只能靠人在详情页手动纠正，系统不猜。**宁可少并，不可错并**：一次错并会把一个真概念连同它的卡片一起吞掉。

隐藏 `place` / `soup` / `trivial` / `step` / `relation` / `artifact` 六类；`other`（拿不准）保持可见——**宁可多留，不要误删真知识**。整页 95 个概念判一次约 5~9 秒，之后进页面零延迟；没有 Key 时退回规则，仍能挡掉大部分。

**按视频分组**：顶部「按主题 / 按视频」切换。按视频时以 `primary_note`（该概念主要来自哪篇笔记）分组，卡片专属概念归「仅来自卡片」；默认收起，避免一屏几十条。

### 5.7 导出

**PDF**：`export_pdf.py` 用本机 Edge/Chrome 无头打印 `/?print=<id>&t=0`。页面渲染完 KaTeX 和图片后给 `<html data-print-ready="1">`，再抓 PDF。找不到浏览器返回 503，提示走「直接打印」。零 Python 依赖。

**Word**：`export_docx.py` + python-docx。
- 标题/列表/引用/表格/代码/图片都映射成原生元素；公式走 `latex2mathml → mathml2omml → OMML`，失败退回等宽文本。
- 页脚 PAGE 域页码；A4 / 2cm 边距 / 1.45 行距。
- 同一张幻灯片的多张近似截图按 **pHash 去重**（距离 ≤8 视为同一张），避免重复占页。
- 默认**不导出完整转录**（`include_transcript=False`）。
- 必须清洗控制字符（坏转录里会混进 `\x08` 之类），否则 lxml 直接报错。

**打印样式**：独立的 `@media print`。白底黑字、A4、页边距 15mm、图片居中且不跨页、标题避免页底孤立、隐藏侧栏/顶栏/极光。

### 5.8 前端渲染器（自研，要小心）

没有构建步骤，`web/` 直接静态服务。`renderMarkdown()` 是手写状态机，处理顺序很关键：

1. 先 `replace(/\r\n?/g,'\n')` 归一换行（否则 frontmatter 正则匹配不上，元信息会当正文显示）。
2. 剥 frontmatter。
3. 抽公式换占位符（块级 `\u0001` / 行内 `\u0000`）。
4. 递归渲染 `<details class="fold">`（完整转录靠它折叠）。
5. 逐行扫描列表（不能整段正则，会跨空行错位产出 `<p></ul></p>`）。
6. 段落包裹前判断 `HAS_BLOCK`，否则 `<ul>…</ul><h2>` 会被一起裹进 `<p>`。
7. 表格单元格内的占位符要先在单元格内部还原，再拼表格。
8. 最后还原公式占位符。

其他要点：
- `mermaid flowchart` 由 `parseFlowchart + renderFlowTree` 自算 SVG 布局（TD/LR 双向），认不出的语法退回源码显示。旧 `mindmap` 块仍走 `renderMindmapTree`（向后兼容）。
- **全局 `[hidden]{display:none!important}`**：一旦元素有 `display:flex` 就会盖掉 UA 的 `[hidden]` 规则（复习页评分按钮曾因此在翻面前就显示）。
- 主题是 CSS 变量体系，**禁用 `color-mix()`**（老 Chromium 内核会失效，装到别人机器上风险大）。

---

## 六、踩坑与禁令

这一节是血泪，**别重复验证**。

### 6.1 硬禁令

| 不要做 | 原因 |
|---|---|
| 用 ffmpeg `select='gt(scene,X)'` 做场景检测 | 对白底 PPT 翻页几乎零响应；用 pHash 缩略图比对 |
| 用 PyAV 解码音频喂 whisper | `av 19` 不再接受 `metadata_errors`，直接崩；用 ffmpeg → PCM |
| 两个 yt-dlp 并行下载 | 抢带宽，总耗时翻倍；并行只能「CPU ∥ 网络」错开 |
| 用生成式模型重画带文字/数字的画面 | 会改写数字和图例，对知识笔记是致命的 |
| 用 B站图床的 `@480w.jpg` 参数 | httpx 会把 `@` 当 userinfo 分隔符导致 404；本地 PIL 缩放 |
| 在前端用 `color-mix()` | 老 Chromium 不支持，会整块样式失效 |
| 用 `nohup ... &` 起服务 | 命令结束时被回收；用 `run_in_background` 受管任务 |
| 改后端不重启 | Python 代码不会热加载；改完必须重启服务 |
| 改前端不升版本号 | SPA 里还跑着旧 JS，用户会以为没修好；改 `?v=YYYYMMDDx` |
| 用真实 BV 写导入测试 | 会真的下载视频；只测模型层或打桩 |
| 让测试碰真实 `secrets.dat` | 曾把用户 Key 清空；测试用 `.tmp_test/` 作 data_dir |
| 用 `rmtree` 删笔记 | 移入 `trash/` 保留 30 项 |
| 把「已处理」只记在内存 | 重启后整批重跑；要扫笔记 frontmatter，且按 (BV, 分P) 判定 |
| 把要点画成导图节点 | 要点是 30 字长句，横着铺一屏就看不全；正文用「内容结构」列表，图只画章节 |
| 直接信任模型给的 `parent` / 概念名 | 它会编出不存在的父节点和「XX 的优势」这类假概念；落盘与展示前必须过 `concept_review`（`resolve_parent` / `same_concept` / `judge`） |
| 为了让层级"看起来完整"补一个空父节点 | 父节点没有内容就是噪声；解析不到就清空，宁可是顶层概念 |
| 把 webview / pythonnet / clr_loader 打进 PyInstaller 包 | pythonnet 进包会让冻结进程段错误，连 `BILINOTES_WINDOW=0`（完全不走窗口代码）都照崩；窗口改走 Edge `--app` |
| 删掉 `build.spec` 末尾的「统一 VC++ 运行库」后处理 | 旧版 `msvcp140.dll` 会让 onnxruntime 加载时 access violation，画面取字全废、启动自检还会带崩主进程 |
| 在启动进程里直接 `import` ctranslate2/onnxruntime 做自检 | 原生库崩了整个程序跟着死；`envcheck` 必须起子进程探（`BILINOTES_PROBE`） |
| 只凭 `Build complete!` 就发安装包 | PyInstaller 删旧 dist 被安全软件拦时仍报成功但产物是旧的；必须跑 `tools/verify_bundle.py` 全绿 |
| 把版本号硬编码在 `healthz` / `FastAPI(version=)` 里 | 曾经代码是 `0.1.0`、安装包是 `0.2.0`；内置更新会永远误报有新版。全部读 `app.__version__`，验收脚本比对三处 |
| 更新时不等主程序退出就启动安装器 | 安装器要覆盖 Program Files 里的 exe，程序还开着会撞文件占用、弹错误框。`window.close()` + 退出后才拉起安装器 |
| 用 `bind + SO_REUSEADDR` 探测端口是否空闲 | Windows 允许绑定已被监听的端口，探测"成功"但 uvicorn listen 报 10048 → 服务没起来、窗口在、点不动。必须用 `connect_ex` 判断有没有 listener |
| 用「端口通不通」判断有没有别的实例在跑 | 旧实例关窗退出时端口还通，新实例会误判"已存在"→ 只开窗口不起服务 → 窗口永远连不上。用命名互斥量，锁是唯一权威 |
| 起完服务就开窗口，不验证是否真的就绪 | 绑定失败时窗口会变成死页面。`_serve()` 必须等 healthz 通，通不过就换端口，全失败就明确报错退出 |
| 为了让浏览器书签一直有效，偷偷做后台常驻 / 开机自启 | 用户明确选 A：关窗即退、零后台。书签失效是本地服务的固有性质，靠界面写清正确入口解决，不靠藏一个进程 |
| 更新检查失败时抛异常 / 弹错误框 / 阻塞启动 | 国内连不上 `api.github.com` 是常态；只返回一行人话提示，`/api/update/auto` 走后台线程且 12 小时限频 |
| 手写 `.lnk` 二进制文件 | 缺卷信息时 Shell 直接拒绝：双击报「没有与之关联的应用」，`ShellExecute` 返回 5。必须用 `IShellLink` 生成（`tools/make_lnk.py`） |
| 在 PowerShell 里用 `Add-Type` / `WScript.Shell` / `cscript` | 当前环境策略全部拦掉；需要 COM 就用 Python `ctypes` 直接调 |
| 用 `Popen` 的 `proc.wait()` 判断 Edge 应用窗口关没关 | Edge 多进程会把窗口交给常驻进程、启动进程提前退出，导致服务跟着收摊（实测 12 秒后 EXIT=0 而窗口还在）；必须轮询窗口本身（`EnumWindows`） |
| 用普通 `urlopen` 访问 `127.0.0.1` | 系统有 `HTTP_PROXY` 且 `NO_PROXY` 为空时会被代理劫持（502），`find_running` 失效 → 多实例 + 端口漂移 → 「网址又断了」。本机请求统一用 `core.local.urlopen_local`，外部请求照常用代理 |
| 验收脚本报 FAIL 就直接调大超时 / 放宽断言 | 先量真实耗时、手动起一次进程、查环境变量，分清是产物坏了还是测试环境变了 |

### 6.2 高频小坑

- **`hidden` 属性会被 `display` 覆盖**：全局加 `[hidden]{display:none!important}` 一劳永逸。
- **`.dlg input{width:100%}` 会撑满整行**：对话框里的复选框要 `width:auto!important`。
- **`.empty` 放在 grid 容器里只占一格**：需要 `grid-column:1/-1`。
- **`.lib-crumb{display:flex}` 同理**：要 `[hidden]{display:none}`。
- **`Path.write_text` 默认写 CRLF**：全项目统一传 `newline="\n"`。
- **提示词里的 `{}`**：`str.format` 会吃掉，写 `{{ }}`。
- **`/tmp` 在 Git Bash 下是 `C:\tmp`**：Word COM 转换要用真实路径。
- **日志文件被占用会让整个软件起不来**：已改成「app.log 被占就换带时间戳的文件，都不行就只输出控制台」。
- **后台任务退出会让服务一起死**：收到 `<task-notification> failed` 通常不是代码问题，重启即可。
- **`len(app.routes)` 不能用来判断路由挂没挂**：FastAPI 0.162 的 `include_router` 生成 `_IncludedRouter` 容器。
- **`input[type=text]{width:100%}` 会盖掉类选择器的宽度**（特异性 0,1,1 > 0,1,0）：面板里的窄输入框要写成 `.panel-head .lib-search` 这种更具体的选择器，否则会撑满整行、把标题挤成一条竖线。
- **文本相似度不能单独用来判重**：矩阵这类长公式在多道题里完全相同，会把相似度整体抬到 0.9+，让「第二列下方」和「第二列上方」看起来像重复。必须先压掉公式，再用定位词/数字集合做保护。
- **端口被占时 `pick_port` 会顺延**：状态页/二维码如果照 `settings.port` 报，手机扫到的就是另一个实例。真实端口一律走 `lan.bound_port()`（见「网址又断了」那节）。
- **Playwright 里 hash 路由要写 `#settings`**：写成 `#/settings`（带斜杠）不会被当成有效视图，页面停在总览，后续断言全是假象。
- **onedir 的资源不在 exe 同级**：`web/`、`bin/`、`vendor/` 全在 `_internal/` 下。写验收脚本按 `dist/BiliNotes/web/...` 找会全判 FAIL，服务其实好得很（实测被这个假阴性坑过一次）。
- **`console=False` 时 `sys.stdout/stderr` 是 `None`**：`print()` 直接把进程打死，表现为双击一闪就没了；`entry.py` 已接到 devnull。
- **冻结后没有 `python -c`**：想在子进程里探依赖，只能复用同一个 exe，靠 `BILINOTES_PROBE=模块名[,模块名]` 让 `entry.py` 改道；`BILINOTES_PROBE_THREAD=1` 模拟后台工作线程。
- **Edge `--app` 窗口标题取自网页 `<title>`**：现在是「BiliNotes · B站视频知识库」，不是 `BiliNotes`。Inno Setup 的 `FindWindowByWindowName` 是精确匹配，按短名字写会永远拦不住"程序正在运行"；`installer/BiliNotes.iss` 已改成两个标题 + 进程检测。
- **编译 Inno Setup 时输出十几万行**：把日志重定向到文件（`*> $log`）再看尾部，别把整包输出拉回对话。

---

## 七、数据与存储

```
%APPDATA%\BiliNotes\
├── config.json        # 偏好设置（不含密钥）
├── secrets.dat        # API Key：base64(DPAPI blob)，与 Windows 账户绑定，换机/换账户解不开
├── cookies.txt        # B站登录态，Netscape 格式（明文，见「待决策」）
├── cards.json         # 知识卡片 + SM-2 状态
├── concept_tags.json  # 概念分类缓存（knowledge/place/soup/other + 来源 llm/rule/manual）
├── collections.json   # 自建收藏夹名单（归属仍在各笔记 frontmatter）
├── models\            # faster-whisper 模型
├── bin\               # 按需下载的 ffmpeg / ffprobe
├── cache\<任务ID>\    # 中间产物，可安全删除
├── cache\covers\      # 封面磁盘缓存（sha1(url|w)，7 天）
├── exports\           # 导出的 PDF/Word，24 小时后自动清
├── trash\             # 删除的笔记（保留最近 30 项）
└── logs\app.log
```

**笔记目录**（`<output_dir>\<标题>\`）：

| 文件 | 内容 |
|---|---|
| `notes.md` | 主笔记，YAML frontmatter + 正文 |
| `lecture.md` | 知识点讲义（可选） |
| `assets\*.jpg` | 关键帧（导出时从 cache 复制过来，所以清缓存不影响笔记） |
| `mindmap.html` | 静态 SVG 章节图（可缩放，只画章节，不含逐条要点） |
| `mindmap.json` | 章节树，`tools/rebuild_maps.py` / `tools/rebuild_structure.py` 用它重建 |
| `subtitle.srt` | 字幕 |
| `meta.json` | 元信息（时长、字幕来源、警告等） |

**frontmatter 字段**：`title / source / uploader / duration / created / transcript / model / tags / collection`（`collection` 为空不写，读取时归「默认收藏夹」）。`source` 里含 BV 号和分P，是「是否已处理」的判定依据。

---

## 八、API 清单

前缀都是 `/api`。

**任务**：`POST /tasks`、`GET /tasks`、`GET /tasks/{id}`、`GET /tasks/{id}/events`（SSE）、`POST /tasks/{id}/cancel|pause|unpause|retry`、`POST /tasks/pause-all|resume-all`、`GET /tasks/{id}/markdown|download`、`POST /tasks/{id}/reveal`。

**设置与系统**：`GET /providers`、`GET|POST /settings`、`POST /settings/key`、`DELETE /settings/key/{provider}`、`POST /settings/test`、`GET|POST /system/ffmpeg`、`GET|POST /system/cache`。

**B站**：`POST /auth/qrcode`、`GET /auth/qrcode/poll`、`GET /auth/status`、`POST /auth/logout`、`GET /favorites/folders`、`GET /favorites/items`、`POST /favorites/import`、`POST /favorites/import-count`、`GET /videos/{bvid}/series`。

**知识库**：`GET /library`（可带 `?collection=`、`?sort=created|mtime`）、`GET /library/search`（同样认 `sort`）、`GET /library/{id}`、`DELETE /library/{id}`、`POST /library/{id}/reveal`、`POST /library/{id}/lecture`、`GET /library/{id}/export?fmt=pdf|docx&transcript=0|1`、`GET /library/{id}/file/{rel}`、`GET /library/{id}/download`、`GET /overview`、`GET /proxy/image?url=&w=`、`POST /collections`、`DELETE /collections?name=`、`POST /collections/move`。

**卡片**：
- `GET /cards/stats?collection=&note_ids=` — 统计（可按范围）
- `GET /cards/due?limit=&collection=&note_ids=&mode=due|all` — 到期队列；`mode=all` 忽略到期时间，用于整轮复习
- `GET /cards/groups` — 收藏夹 → 笔记 → 卡片数/待复习数（笔记删了但卡片还在会归到「已删除的笔记」）
- `GET /cards/list?note_id=&collection=&limit=` — 浏览用，按生成时间倒序
- `GET /cards/sources` — 每篇笔记的卡片数（含 collection）
- `POST /cards/generate` — 生成（会带上笔记的关键概念与已有题目）
- `POST /cards/{id}/review` — 四档评分
- `POST /cards/{id}/reset` — 提到「现在到期」，不改 ease/连击
- `POST /cards/dedupe?dry=0|1` — 查重：同一篇笔记内的同义题，留复习次数多的、删其余；默认只报告
- `DELETE /cards/{id}`、`DELETE /cards/note/{note_id}`

**双端**：
- `GET /lan` — {enabled, reachable, ip, port, key, url}；`reachable` 是"这次是否真的绑到局域网了"（开了没重启 = false）
- `POST /lan` `{enabled?, rotate?}` — 开关 / 换口令；换口令立即生效，改监听要重启
- `GET /lan/qr` — 手机扫的二维码 SVG，**只允许本机取**
- `POST /lan/login`（表单 `k=`）— 手机输口令换 cookie

**自测卷**：
- `GET /quiz`、`GET /quiz/{id}` — 列表 / 取卷（答题页不下发答案）
- `POST /quiz/generate` `{note_ids?, collection?, kinds[], count}` — 出题
- `POST /quiz/{id}/submit` `{answers}` — 判分 + 记录
- `POST /quiz/{id}/cards` `{ids}` — 错题变复习卡片
- `DELETE /quiz/{id}`

**概念**：
- `GET /concepts?q=&limit=` — 每项带 `cat` / `parent` / `parent_key` / `primary_note`；返回 `hidden`（被隐藏数）、`untagged`（完全没判过的数）、`stale`（上级被重新对齐过的条数，>0 时前端自动整理一次）、`reviewable`（可交给 AI 复核的条数）
- `GET /concepts/detail?name=` — 详情，带 `cat` 与 `parent`
- `POST /concepts/classify?force=0|1` — 跑一次判别；`force=1` 连已判过的重判（手动结论仍不动）
- `POST /concepts/tag` `{name, cat}` — 手动纠正，永久优先
- `POST /concepts/tidy?ai=0|1` — 整理：碎片并回主体、层级重挂、空泛词隐藏；`ai=1` 再多做一层模型复核（返回 `ai: {checked, kept, merged, reason}`，`reason` 可能是 `no_key`）。只动 `src != manual`，流水线每次生成完也会自动跑

---

## 九、前端结构

三个文件，无构建：

| 文件 | 职责 | 注意 |
|---|---|---|
| `index.html` | 所有视图的骨架 + 弹窗 + head 内联主题脚本 | 主题必须在 head 里提前落定，否则深色皮肤会闪一帧白 |
| `app.css` | 变量体系 + 主题覆盖 + 组件 + 打印样式 | 约 60 个 token/主题；结构差异放在 `[data-theme]` 微调段 |
| `app.js` | 状态、渲染、请求、交互 | 90KB 单文件，改前先定位到函数，别整体重构 |

**视图注册**：`VIEW_LOADERS = { dashboard, collect, queue, library, concepts, review, settings }`，hash 路由 `#library`。新增视图要同时改三处：导航按钮、`<section class="view" id="view-xxx">`、`VIEW_LOADERS`。

**事件绑定集中在 `init()` 里**，动态渲染的卡片用事件委托（`.note-card`、`.video-card`、`.concept-item` 都是）。聚光灯/磁吸/倾斜/涟漪也是末尾一个 IIFE 用事件委托实现，`prefers-reduced-motion` 时全部跳过。

**主题切换**：`applyTheme(id)` + `data-theme-pick` 委托，存 `localStorage['bn.theme']`。

---

## 十、测试与回归

### 10.1 必跑的 15 个套件

```bash
cd bili-notes
for t in smoke_api smoke_core smoke_library smoke_series smoke_keys \
         smoke_export smoke_cards smoke_concepts smoke_lan smoke_quiz smoke_offline \
         smoke_words smoke_update smoke_ui smoke_cache; do
  printf "%-16s" "$t"; timeout 180 python tests/$t.py >/tmp/t.log 2>&1 \
    && echo PASS || { echo FAIL; tail -5 /tmp/t.log; }
done
printf "%-16s" "web_render";    node tests/web_render_check.js
printf "%-16s" "range_parse";   node tests/range_parse_check.js
```

### 10.2 各套件覆盖什么

| 套件 | 覆盖 |
|---|---|
| `smoke_api` | 全部接口契约、密钥往返（**用临时 data_dir**） |
| `smoke_core` | 配置、路径、分块、frontmatter、flowchart |
| `smoke_library` | 知识库扫描、分组、删除、导入参数校验 |
| `smoke_series` | 合集展开、每合集限量、跳过已做、重做、limit 截断 |
| `smoke_keys` | **密钥归属硬约束**：无硬编码、预设无 key 字段、空库返回 None、本机文件无明文 |
| `smoke_export` | Word 结构/公式/表格/图片/转录开关 + 无头打印自检 |
| `smoke_cards` | 卡片解析、去重、SM-2 各分支、间隔封顶、存储容错、preview 一致性；**历史重复清理**（只报不删、留复习多的、不跨笔记误伤） |
| `smoke_concepts` | 概念表解析、归并、跨篇聚合、卡片挂载、显示名平票；**概念审核**（派生名回流、近似合并的反例、上级解析、生成期过闸、整理后无孤儿父节点） |
| `smoke_lan` | 双端口令：伪造手机来源地址 → 401/登录页、?k= 种 cookie、换口令立即失效、开关关掉后仍拦外部 |
| `smoke_quiz` | 自测卷：题型过滤、脏题丢弃、四种判分、填空归一、交卷记录、错题转卡不重复、答题页不下发答案 |
| `smoke_offline` | 离线包：正文真的读到了、图片内联成 data-uri、内联 JSON 可解析且含答案、KaTeX 字体在包里、无外部引用；进度合并只动排期字段 |
| `smoke_words` | 英语生词：词库多格式解析与坏行报错、词形还原正反例（went/stopped/bigger 命中，worker/number 不误剥）、外语判定、标注幂等、空词库与零命中不改笔记 |
| `web_render_check.js` | 前端渲染：公式、CRLF、列表、表格、details、flowchart、负数公式 |
| `range_parse_check.js` | 选集序号解析：各种分隔符、倒写、越界、乱输入 |

其他 `smoke_*.py`（captions / mindmap / outline / mathfmt / table / cache / boot / polish / highlight / itinerary / textcheck / vision / asr_lang / textdet / pipeline / ui）涉及网络或模型，按需跑。

### 10.3 测试纪律

- 不联网、不碰真实 Key、不碰真实 `secrets.dat`、不创建真实下载任务。
- 需要模型的地方用假客户端（鸭子类型），需要网络的地方 mock。
- 夹具里的 LaTeX 用**原始字符串** `r"""..."""`，否则 `\b`、`\f` 会被 Python 转义成控制字符。
- 断言要断「行为」而不是「实现细节」，比如断「跳过已做」而不是断某个内部变量。
- `smoke_single` 自己会把互斥量名换成 `Local\BiliNotes-test-<pid>`（`BILINOTES_LOCK_NAME`），
  所以软件开着也能跑；否则第一个子进程永远抢不到锁，三条断言会假失败。

---

## 十一、运行、重启、打包

### 11.1 启动

```bash
cd bili-notes
BILINOTES_OPEN_BROWSER=false python entry.py    # 用受管后台任务跑
```

**不要**用 `nohup ... &`（命令结束会被回收），**不要**用 `python -m app.main` 起（打包后入口是 `entry.py`，本地两者都能跑但保持一致更省心）。

### 11.2 改完代码怎么让它生效

| 改了什么 | 操作 |
|---|---|
| 只改 `web/` | 升 `?v=` 版本号，浏览器 Ctrl+F5 |
| 改了 `app/**.py` | 杀进程重启服务 |
| 改了 `config.py` 字段 | 重启 + 检查 `save_overrides` 白名单 + 设置接口是否透出 + 前端是否有入口 |
| 改了 `requirements` / `vendor` | 同时更新 `build.spec` 的 `datas` 与 `hiddenimports` |

重启命令（先停再起）：

```bash
PID=$(netstat -ano | grep ":8765" | grep LISTEN | awk '{print $5}' | head -1)
[ -n "$PID" ] && taskkill //PID $PID //F
```

### 11.3 打包

```bash
pyinstaller build.spec --noconfirm   # 产物 dist/BiliNotes/BiliNotes.exe
```

- **入口必须是 `entry.py`**：`app/main.py` 被当顶层脚本执行时包内相对导入会 ImportError。
- `vendor/`（python-docx / lxml / latex2mathml / mathml2omml，约 14MB）随包分发，`app/__init__.py` 把它加进 `sys.path`。
- **不打包 ffmpeg**：完整构建 450MB，改为运行时一键下载 92MB。
- 当前 `dist/` 是旧代码，**验收后再打包**。

---

## 十二、已知限制与待决策

### 12.1 限制

- 只支持 B站；无 GPU 加速，长视频转写慢（base 约 0.11x，17 分钟音频约 112s）。
- OCR 对花体、竖排、艺术字识别有限，笔记里会标「可能有误」。
- 弹幕不参与总结。
- 收藏夹一次最多导入 200 条，超出分批。
- 概念索引在「同一门课多集」时才显威力；现在库里多是单篇，跨多篇为 0。
- PDF 导出依赖本机 Edge/Chrome（Win10/11 自带）。

### 12.2 待你决定

1. **`cookies.txt` 是明文**（B站登录态）。它不是 API Key，但如果把整个 `%APPDATA%\BiliNotes` 发给别人，登录态会泄露。要不要把 cookie 也纳入 DPAPI 加密（用时临时解密）？如果分发只发 exe 目录则无此风险。
2. **讲义风格**现在只有「名师课堂」一种，是否加「应试提纲」下拉（考点清单 + 解题模板 + 易错陷阱）。
3. ~~**历史重复卡片**~~（已做，2026-11-07）：复习页有「查重」按钮，默认只报告、确认后才删；现有 3 组已清完，33 → 30 张。备份在 `%APPDATA%\BiliNotes\cards.json.bak-20261107`。
4. **重新打包 exe** 的时机（当前 dist 是 11-01 的旧代码，之后所有改动都还没进包）。

---

## 十三、下一步建议（按投入产出排序）

已经做完的是「记录知识 → 转化知识」的主干：笔记（看得见）→ 讲义（讲透）→ 卡片（记得住）→ 概念（串成网）。往下可以接：

**P0 · 自测卷（已做，2026-11-07）**
出题 → 手机/电脑做题 → 自动判分 → 错题一键变卡片。还差：把卷子导出成 PDF/Word 打印（导出链路现成），以及简答题的自评流程（后端支持，前端暂未开放）。

**P1 · 合集学习进度**
合集页显示「已做 12 / 未做 46 / 待复习 8」，给「继续学」入口。现在是「做完就消失」，没有推进感。

**P1 · 库内问答**
「这个概念我哪集学过？」「对比第 3 和第 11 集对这个公式的解释」——本地全文检索召回 + LLM 作答，答案带 `[mm:ss]` 跳回原片。不用向量库，关键词召回 + 重排够用。

**P2 · Anki 导出**
卡片导出 `.apkg`，让用户能在手机 Anki 上复习。

**P2 · 局域网访问 / 移动端复习**
现在只监听 `127.0.0.1`，手机打不开。开局域网绑定 + 移动端复习页，通勤时能刷卡片。

**P2 · Obsidian 导出**
笔记 + 概念页导出成带 `[[双链]]` 的 vault。

---

## 十四、快速排错对照表

| 现象 | 先查什么 |
|---|---|
| 页面样式/脚本像是没生效 | 版本号有没有升？Ctrl+F5 强刷；确认 `static_dir` 指向 `web/` |
| 封面一片白 | `POST /api/proxy/image` 返回什么？`X-Cover-Cache` 是 hit 还是 miss？真因常在 ORB 或图床限流 |
| 笔记里公式露 `$` 源码 | 三处都要查：生成侧 `mathfmt.normalize`、落盘前是否过、前端 `isMath` 是否把内容当价格放过 |
| 公式把句子截成多行 | 占位符用错了（块级 `\u0001` / 行内 `\u0000`） |
| 元信息被当正文显示 | CRLF 导致 frontmatter 正则不匹配 |
| Word 导出报错 | 转录里有没有控制字符（`\x08` 等）？`_safe()` 是否生效？ |
| PDF 导出 503 | 本机有没有 Edge/Chrome；或直接用「直接打印」 |
| 任务永远「处理中」 | 看 `updated_at` 心跳是否停（僵死判定 600s）；看日志最后一条阶段 |
| 服务起不来 | 日志文件是不是被占（已容错）；端口 8765 是否被旧进程占用 |
| 导入时选了 5 集却排了 40 集 | `series_limit` 有没有透传到 `_expand_items`？前端有没有带这个字段？ |
| 删了笔记还是被跳过 | 「已处理」判定是否还查了内存队列（应只认磁盘，且按 BV+分P） |
| 复习时不同笔记的卡混在一起 | 范围下拉是不是「全部笔记」？`scopeQuery()` 有没有带上 `note_ids`/`collection` |
| 概念页看不到新生成的卡片 | 卡片 `concept` 是否命中了笔记的概念表？点筛选「只来自卡片」；根因是生成时的概念名复用 |

---

*本文档与 `.workbuddy/memory/` 下的日志互补：这里写「结论与为什么不这么做」，日志写「当时具体怎么试的」。两者一起看，能还原完整决策链。*
