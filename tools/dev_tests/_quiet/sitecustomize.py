"""
★ 1.9.0: 测试进程的 Windows 静音补丁（由 _sweep190.ps1 通过 PYTHONPATH 注入）。

【为什么需要】
  这些套件里有不少会创建 QApplication、抓屏量像素、起子进程。个别套件在
  Qt 退出阶段会踩到访问违例（实测弹窗：
      python.exe - 应用程序错误
      0x00007FFBADA4BCE1 指令引用了 0x0000000000000820 内存。该内存不能为 read。
  ）。进程崩了本身不可怕，可怕的是 **Windows 弹出一个模态错误框**：
    · 它会把控制台/回归**挂住**（没人点确定就一直等）；
    · 更糟的是它看起来像"桌宠崩了"，会误导排查方向。

【做法】
  进程启动时调 SetErrorMode，关掉"严重错误"和"通用保护错误"弹窗：
    · SEM_FAILCRITICALERRORS (0x0001)
    · SEM_NOGPFAULTERRORBOX (0x0002)
  这样真崩了就直接退出（可被回归脚本按"没有汇总行"识别出来），不会弹框。

  只在 Windows 生效；非 Windows 或调用失败都静默跳过，绝不影响测试。
"""
try:
    import ctypes
    import sys

    if sys.platform == "win32":
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        ctypes.windll.kernel32.SetErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
        )
except Exception:
    pass

# ★ 1.9.0: 再把 faulthandler 打开 —— 万一某个套件真的踩了访问违例，
#   它会把 Python 层的调用栈打到 **stderr**（回归脚本会把 stderr 存成
#   `<套件名>.err.txt`）。不打开的话，崩溃只表现为"进程没了"，
#   连是哪个套件、崩在哪一行都不知道（排查这个弹窗时就卡在这里）。
try:
    import faulthandler

    faulthandler.enable()
except Exception:
    pass

# ★ 1.9.2: config.json / .env 保镖（快照由 _sweep190.ps1 建，这里负责进程退出还原）
#   为什么需要：好几套件会真的实例化主窗口/主进程并触发"保存配置"，退出时把
#   **用户真实配置**写成测试形象（实测被改成 character=deepseek / multimodal=false）
#   → 用户下次开桌宠就"变样了"。所以测试跑完必须字节级还原。
#   为什么限定"顶层测试进程"：_test_*.py 起的子进程（main.py / pet_process.py）会
#   继承同一个环境变量，它们中途退出时若也去还原，会把**测试正在写入**的
#   config.json 抹掉 → _test_settings_persist_182 之类的落盘断言假红。
#   用 sys.argv[0] 的文件名区分：回归脚本起的是 `python -u ...\_test_xxx.py`。
#
# ★★ 1.9.3 第 26 轮补：**不管有没有 PRTS_CFG_GUARD，_test_*.py 都自己兜底**。
#   血的教训：一个"把 config_path 指到临时目录"的复现脚本以为很安全，实际
#   `config.save()` 里的 .env 路径是**写死的**，于是它把用户真实 `.env` 里的
#   `DEEPSEEK_API_KEY` 写成了测试占位符 `test` → 下次启动桌宠
#   `401 Authentication Fails`（密钥被自己的测试覆盖掉了）。
#   现在：任何顶层 `_test_*.py` 进程启动时都快照 config.json + .env，
#   退出时**无条件还原**（并删掉快照目录）。有 PRTS_CFG_GUARD 时多一层共享快照，
#   两层都还原、互不冲突（内容一样）。
try:
    import atexit
    import os
    import shutil
    import sys
    from pathlib import Path as _P

    _root = _P(__file__).resolve().parent.parent.parent   # _quiet → dev_tests → 项目根
    _argv0 = os.path.basename(sys.argv[0] or "")

    if _argv0.startswith("_test_"):
        # ── ① 自兜底快照（不依赖任何环境变量）──
        _auto = _root / "tools" / "dev_tests" / "_cfg_guard_auto" / f"p{os.getpid()}"
        _snapped = []
        try:
            _auto.mkdir(parents=True, exist_ok=True)
            for _n in ("config.json", ".env"):
                _src = _root / _n
                if _src.is_file():
                    shutil.copy2(_src, _auto / _n)
                    _snapped.append(_n)
        except Exception:
            _snapped = []

        def _restore_auto():
            for _n in _snapped:
                try:
                    shutil.copy2(_auto / _n, _root / _n)
                except Exception:
                    pass
            try:
                shutil.rmtree(_auto, ignore_errors=True)
            except Exception:
                pass

        # atexit 后进先出：先注册的先最后执行 —— 想让它**最后**跑，就得**最先**注册
        atexit.register(_restore_auto)

    # ── ② 回归脚本的共享保镖（原逻辑保留）──
    _guard = _root / "tools" / "dev_tests" / "_cfg_guard"
    if (os.environ.get("PRTS_CFG_GUARD") == "1"
            and _argv0.startswith("_test_")
            and _guard.is_dir()):
        def _restore_cfg_guard():
            # atexit 是后进先出：本函数注册得最早 → **最后**执行，
            # 所以就算被测程序自己也注册了"退出时保存配置"，也已经被它盖掉。
            for _n in ("config.json", ".env"):
                _g = _guard / _n
                if _g.is_file():
                    try:
                        shutil.copy2(_g, _root / _n)
                    except Exception:
                        pass
        atexit.register(_restore_cfg_guard)
except Exception:
    pass
