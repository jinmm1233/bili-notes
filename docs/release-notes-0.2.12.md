# BiliNotes 0.2.12

> 修掉「语音模型加载失败」：上次没下完的模型目录，现在会自动补齐或重建。

## 修复

### 新电脑提示「语音模型 tiny 加载失败：Cannot load the vocabulary…」

**现象**：模型看起来已经下过，但每次转写都失败，报 `Cannot load the vocabulary from the model directory`。

**原因**：0.2.10 及更早的版本判断「模型装好了」只看两个文件：`model.bin` 和 `config.json`。首次下载如果中途退出（关窗口、断网、取消任务），目录里会留下这两个文件、却没有 `vocabulary.txt`。之后每次启动都认为模型已就绪，直接跳过下载，于是永远缺词表，CTranslate2 加载时必然报错。

**处理**：

- **按必需文件判断**：`config.json`、`model.bin`、`tokenizer.json`、`vocabulary.txt` 四个文件齐全才算装好，缺哪个补哪个，已下好的不重复下；
- **识别坏文件**：体积明显不足、或内容其实是网页／不是 JSON 的，判为没下好并重下；
- **加载失败自动重建**：万一还是加载不了，把旧目录改名留档、重新下载一套再试一次，成功后清掉旧目录；重下失败则恢复原目录，不丢数据；
- **不再请求用不到的文件**：只下推理真正需要的四个，首次下载少发几次请求。

## 验证

- 按故障现场复刻：只放 `model.bin` + `config.json`，新版只补 `tokenizer.json` 和 `vocabulary.txt`（2.6MB），**2.3 秒**下完，`WhisperModel` 加载成功。
- 新增 `tests/smoke_model_repair.py`，12 项全过：残缺目录只补缺文件、齐全目录不联网、坏文件识别、加载失败重建目录。
- `tests/smoke_model_source.py` 全过（ModelScope 优先、失败换源）。
- 打包产物验收、安装包验收与 0.2.11 同一套流程。

## 升级说明

- 覆盖安装即可，笔记、配置、API Key 都不受影响。
- 之前卡在「加载失败」的机器，升级后直接重跑任务即可，不需要手动删 `models` 目录。
- 本次先发 Windows 版；macOS 版待 Windows 实测通过后补齐。

## 校验

```
文件    BiliNotes-setup-0.2.12.exe
大小    224,679,730 字节
SHA256  6891a495122a09e8603bfe9711e0390f532b940417fe99873a9badb5e99cbdd4
```
