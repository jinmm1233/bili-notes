# BiliNotes 0.2.8

> 紧急修复版。0.2.7 的 Windows 安装包有一个会让**所有视频任务在起始阶段崩溃**的问题，请务必升级。

## 修复

### 点「生成笔记」立刻崩溃（卡在 probe 2%）

**现象**：任何视频、任何网络、任何 API Key，任务一创建就在「解析视频信息」阶段失败，日志里是 `AttributeError: module 'backports.zstd' has no attribute 'ZstdError'`。

**根因**：安装包里的 `backports.zstd` 只打进了 C 扩展 `_zstd.cp313-win_amd64.pyd`，漏掉了 `__init__.py`。Python 会把这种目录当成"命名空间包"——`import` 能过，但里面没有任何 Python 层定义。`urllib3` 因此认为 zstd 可用，真正取值时才抛 `AttributeError`，连带 `yt_dlp` 导入失败，任务在下载开始前就终止。

**处理**：在 `build.spec` / `build-mac.spec` 中彻底排除 `backports` / `backports.zstd`，让 `urllib3` 走正常的 `ImportError` 降级分支（zstd 只是可选压缩，不影响下载与笔记生成）。

## 打包验收加固（防同类问题再次流出）

- 冻结环境探针从 6 项扩到 14 项，新增：`yt_dlp`、`urllib3`、`app.services.downloader`、`app.services.pipeline`、`docx/lxml/latex2mathml/mathml2omml`、导出服务、`qrcode/PIL/zhconv/pypinyin`。
  这些模块都是**延迟导入**，旧验收只测重型原生库，所以 0.2.7 的 34 项全绿也没拦住。
- 新增关键数据文件断言：`docx/templates/default.docx`（缺了 `Document()` 直接报错）、`zhconv/zhcdict.json`（缺了繁简静默失效）、`certifi/cacert.pem`（缺了 HTTPS 全挂）。
- 新增回归断言：产物中**不得存在** `_internal/backports`。

## 验证

- 打包产物验收 **45 项全绿**（含新增 `yt_dlp` / `urllib3,yt_dlp` 探针、数据文件断言、`backports` 缺失断言）
- 安装包验收 **21 项通过**（全新目录 + 全新用户数据，装完即用，卸载保留笔记）
- 笔记导出实测：Word（4.9 MB，695 段落 / 2 表格 / 49 张图 / 6 个可编辑公式）、PDF（6.5 MB，58 页，8 个字体文件已嵌入）
- 真实业务链路：安装后创建视频任务，2 秒内从 `probe 2%` 进入 `text 8.6%`，无报错

## 校验

```
文件    BiliNotes-setup-0.2.8.exe
大小    224,650,263 字节
SHA256  4eaad3e301f3542f154b4f5e0e6e78341f069b0d0e629da080e2d38ad75d0df7
```

## 升级说明

- 覆盖安装即可，笔记、配置、API Key 都不受影响。
- 若之前为绕过问题手动删过 `D:\BiliNotes\_internal\backports`，本版安装包已不再包含该目录，无需再处理。
