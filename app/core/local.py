"""访问本机 BiliNotes 服务的 HTTP 请求，必须绕开系统代理。

坑（真实踩到）：Windows 上装了代理软件后 `HTTP_PROXY` 常指向
`127.0.0.1:34126` 之类的本地端口，而 `NO_PROXY` 往往是空的。
`urllib` 会把 `http://127.0.0.1:8765/healthz` 也发给这个代理，代理回 502：

  - `find_running()` 认不出已经运行着的实例 → 双击图标又起一个进程
    → 端口顺延到 8766 → 手机书签/二维码全指向旧地址，表现就是「网址又断了」；
  - `_wait_ready()` 等不到 healthz → 首屏白屏 5 秒；
  - 打包验收脚本的所有本地接口冒烟全部假失败。

本机地址永远不该走代理。这里统一用一个禁用 ProxyHandler 的 opener；
访问外部网络（GitHub、模型镜像、LLM）仍然走普通请求，该用代理就用。
"""
from __future__ import annotations

import urllib.request

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen_local(url: str, timeout: float = 1.0):
    """请求 127.0.0.1 / localhost，忽略 HTTP_PROXY / HTTPS_PROXY。"""
    return _opener.open(url, timeout=timeout)
