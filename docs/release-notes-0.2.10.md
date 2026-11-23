# BiliNotes 0.2.10

> 两件事：DeepSeek 现在能直接当看图模型用；顺手修掉它已经停用的文字模型名。

## 修复

### 选 DeepSeek 当文字模型会失败

**现象**：设置里选 DeepSeek，调用模型时报「模型不存在」。

**原因**：预设里写的还是 `deepseek-chat`。DeepSeek 在 2026-07-24 已按计划停用 `deepseek-chat` / `deepseek-reasoner`，现在只有 `deepseek-flash`（V4.1 Flash）和 `deepseek-v4-pro`。

**处理**：文字模型改为 `deepseek-flash`，默认配置里的旧值一并更新。

## 新增

### DeepSeek 可以当看图模型了

DeepSeek 从 2026-08 起开放视觉能力，9 月并进了 `deepseek-flash`（原生多模态），调用格式正好是我们在用的 OpenAI 兼容 `image_url` + base64。所以这次直接把 DeepSeek 加进看图模型下拉：

- 看图默认模型：`deepseek-flash`
- 文字和看图选同一家时，自动复用同一把 Key，不用填两遍
- 价格：输入空闲时段 ¥1 / 百万 tokens，一张图最多 1024 tokens，折合每张约 ¥0.001–0.002

### 关掉 DeepSeek 的思考模式

`deepseek-flash` 默认开启思考模式，而看图每帧只给 1024 token 的输出预算——不关的话推理过程会把预算吃光，正文直接为空。现在请求体里固定带 `thinking: disabled`，文字摘要和看图判断都走非思考模式：更快、更省，输出也更稳定。

### 设置页的保存提醒

设置项的改动不会即时保存（看图服务商和模型名除外，改完立刻存）。以前调完直接切走就丢了，现在每次进入设置页会弹一次提示：

- 「知道了」：关掉这一次；
- 「不再提醒」：记在本机，以后不再弹；
- 关掉提示后，只要有未保存的改动，右上角「保存设置」按钮会持续高亮。

## 验证

- 新增 `tests/smoke_llm_deepseek.py`，17 项全过：预设模型名、看图默认值、模型归属校验、请求体里 `thinking=disabled`、图片按 base64 发送、JSON 模式保留 `response_format`、从设置构造客户端、残留的别家模型名回落。
- 前端一致性检查与密钥审计全过（7 家供应商预设均不含内置 Key）。
- 设置页实测：下拉出现「DeepSeek（deepseek-flash · 便宜）」，选中后模型名自动填好，同家 Key 直接复用。

## 升级说明

- 覆盖安装即可，笔记、配置、API Key 都不受影响。
- 已经在 0.2.9 里用「自定义（OpenAI 兼容）」填 DeepSeek 看图的，升级后可以改成直接选 DeepSeek，地址和 Key 都不用再维护两份。
- 如果之前手动把文字模型固定成 `deepseek-chat`，升级后会自动走新预设，无需改配置。

## 校验

```
文件    BiliNotes-setup-0.2.10.exe
大小    待打包
SHA256  待打包
```
