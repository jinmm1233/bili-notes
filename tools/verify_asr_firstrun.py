"""首次转写验收：用**打包好的 exe** 真跑一条任务，覆盖模型下载与加载。

为什么需要它：`verify_bundle` / `verify_fresh_install` / `verify_installer`
都只验证「进程起得来、接口有响应」，从不真的转写。于是 0.2.10 的
「半成品模型目录被当成已装好」一直没被自动发现——它既不在冷启动路径上，
也不在空目录路径上，只在「上一次下载被中断」这种脏状态下出现。

这个脚本做两件事：
  fresh  全新用户目录：真下 75MB 模型，验证首次下载 + 加载 + 开始转写
  dirty  残缺模型目录：只放 model.bin + config.json（复刻 0.2.10 中断现场），
         验证会补齐词表并继续转写，而不是报 Cannot load the vocabulary

跑法（需要联网，会真的访问 B 站和模型源）：
  python tools/verify_asr_firstrun.py --mode both
  python tools/verify_asr_firstrun.py --mode dirty --keep

退出码 0 表示通过。临时目录默认自动清理，--keep 保留现场便于排查。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEFAULT_VIDEO = "https://www.bilibili.com/video/BV1WmwDzgEDG"   # 4 分钟，够短
results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def get_json(base: str, path: str, timeout: float = 10.0) -> dict:
    with LOCAL.open(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_json(base: str, path: str, body: dict, timeout: float = 20.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with LOCAL.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def wait_health(proc: subprocess.Popen, timeout: float = 60.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        for port in range(8765, 8776):
            try:
                with LOCAL.open(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
                    if r.status == 200:
                        return f"http://127.0.0.1:{port}"
            except Exception:  # noqa: BLE001
                continue
        time.sleep(1.0)
    return None


def prepare_appdata(tmp: Path, real_appdata: Path, model: str, dirty: bool,
                    source_model_dir: Path | None) -> Path:
    """造一个用户目录：配置/密钥沿用本机，模型按场景留空或残缺。"""
    appdata = tmp / "AppData" / "Roaming"
    user = appdata / "BiliNotes"
    user.mkdir(parents=True, exist_ok=True)

    conf = {}
    real_conf = real_appdata / "config.json"
    if real_conf.is_file():
        try:
            conf = json.loads(real_conf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            conf = {}
    docs = tmp / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    conf.update({
        "asr_model": model,
        "output_dir": str(docs / "BiliNotes"),
        "output_template": "{date}-{title}",
        "vision_frame_judge": False,
    })
    (user / "config.json").write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    real_secrets = real_appdata / "secrets.dat"
    if real_secrets.is_file():
        shutil.copy2(real_secrets, user / "secrets.dat")

    if dirty:
        assert source_model_dir and source_model_dir.is_dir(), (
            f"dirty 模式需要一份完整模型作参照，找不到 {source_model_dir}")
        target = user / "models" / f"fw-{model}"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("model.bin", "config.json"):      # 复刻中断现场：只有这两个
            shutil.copy2(source_model_dir / name, target / name)
    return appdata


def run_case(dist: Path, exe: Path, video: str, model: str, dirty: bool,
             real_appdata: Path, source_model_dir: Path | None,
             timeout: float) -> None:
    label = "残缺模型目录" if dirty else "全新用户目录"
    print(f"\n[{label}] 启动 {exe.name}")
    tmp = Path(tempfile.mkdtemp(prefix="bn_asr_"))
    appdata = prepare_appdata(tmp, real_appdata, model, dirty, source_model_dir)
    model_dir = appdata / "BiliNotes" / "models" / f"fw-{model}"
    if dirty:
        print("  预置文件：" + ", ".join(sorted(p.name for p in model_dir.iterdir())))

    env = dict(os.environ)
    env["APPDATA"] = str(appdata)
    env["USERPROFILE"] = str(tmp)
    env["HOME"] = str(tmp)
    env["BILINOTES_WINDOW"] = "0"
    env["BILINOTES_OPEN_BROWSER"] = "false"

    proc = subprocess.Popen([str(exe)], env=env, cwd=str(dist),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    base = None
    try:
        base = wait_health(proc)
        if not record(bool(base), f"{label}：服务启动", base or "60 秒内没起来"):
            return

        task = post_json(base, "/api/tasks", {
            "url": video, "frames": False, "ocr": False, "summary": False,
        })
        task_id = task["id"]
        record(True, f"{label}：任务已创建", task_id)

        deadline = time.time() + timeout
        seen: list[str] = []
        repair_seen = download_seen = asr_started = False
        bad_load = ""
        last = {}
        while time.time() < deadline:
            last = get_json(base, f"/api/tasks/{task_id}")
            msg = str(last.get("message") or "")
            if msg and (not seen or seen[-1] != msg):
                seen.append(msg)
            if "补齐" in msg or "不完整" in msg:
                repair_seen = True
            if "下载" in msg and model in msg:
                download_seen = True
            if "解码音频" in msg:      # 这一句在模型加载成功之后才发
                asr_started = True
                break
            if last.get("status") in ("failed", "canceled"):
                break
            time.sleep(1.0)

        err = str(last.get("error") or "")
        if "Cannot load the vocabulary" in err:
            bad_load = err
        record(not bad_load, f"{label}：没有词表加载错误", bad_load[:80] or "无")
        if dirty:
            record(repair_seen, f"{label}：识别出残缺并补齐",
                   " | ".join(m for m in seen if "补齐" in m or "不完整" in m)[:90] or "未出现补齐提示")
        else:
            record(download_seen, f"{label}：触发了首次下载",
                   " | ".join(m for m in seen if "下载" in m)[:90] or "未出现下载提示")
        record(asr_started, f"{label}：模型加载完成并开始转写",
               str(last.get("message") or last.get("error") or "")[:90])
        record((model_dir / "vocabulary.txt").is_file(),
               f"{label}：词表已落到磁盘",
               ", ".join(sorted(p.name for p in model_dir.iterdir())) if model_dir.is_dir() else "目录不存在")

        try:
            post_json(base, f"/api/tasks/{task_id}/cancel", {})
        except Exception:  # noqa: BLE001
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        if keep_tmp:
            print(f"  现场保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


keep_tmp = False


def main() -> int:
    global keep_tmp
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(ROOT / "dist" / "BiliNotes"))
    ap.add_argument("--video", default=DEFAULT_VIDEO)
    ap.add_argument("--model", default="tiny")
    ap.add_argument("--mode", choices=("fresh", "dirty", "both"), default="both")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--keep", action="store_true", help="保留临时用户目录")
    args = ap.parse_args()
    keep_tmp = args.keep

    dist = Path(args.dist)
    exe = dist / "BiliNotes.exe"
    if not exe.is_file():
        print(f"找不到 {exe}，先打包或用 --dist 指定目录")
        return 2

    real_appdata = Path(os.environ.get("APPDATA", "")) / "BiliNotes"
    source_model_dir = real_appdata / "models" / f"fw-{args.model}"
    if args.mode in ("dirty", "both") and not (source_model_dir / "model.bin").is_file():
        print(f"dirty 模式需要 {source_model_dir}（一份完整的 {args.model} 模型）作参照")
        return 2

    print(f"被测程序：{exe}")
    print(f"视频：{args.video}　模型：{args.model}")
    if args.mode in ("fresh", "both"):
        run_case(dist, exe, args.video, args.model, False, real_appdata,
                 source_model_dir, args.timeout)
    if args.mode in ("dirty", "both"):
        run_case(dist, exe, args.video, args.model, True, real_appdata,
                 source_model_dir, args.timeout)

    failed = sum(1 for ok, _, _ in results if not ok)
    print("\n" + "=" * 60)
    if failed:
        print(f"首次转写验收：{len(results) - failed} 通过，{failed} 失败")
        return 1
    print(f"首次转写验收：{len(results)} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
