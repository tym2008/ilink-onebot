"""
单文件 exe 打包脚本

用 PyInstaller 把 ilink-onebot 打包为单个 ``dist/ilink-onebot.exe``
（Windows）/ ``dist/ilink-onebot`` （Linux/macOS）。

为什么是 Python 脚本而非 .ps1 / .sh：
- Python 已经是项目运行时，不引入新的脚本语言依赖
- 跨平台（同一份脚本在 Windows / Linux / macOS 都能跑）
- 类型友好，方便维护

用法::

    # 项目根目录下运行（用虚拟环境的 python）
    .venv/Scripts/python.exe scripts/build.py
    # 或类 Unix 上
    .venv/bin/python scripts/build.py

可选参数::

    --no-clean   保留 build/ 和 dist/ 之前的产物（默认会清空，避免污染）
    --debug      用 PyInstaller --debug=imports 模式构建，便于调查打包后
                 ImportError（产物体积变大但有详细 import 日志）

输出::

    dist/ilink-onebot[.exe]   单文件可执行
    build/                    PyInstaller 中间产物（.gitignore）
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
SPEC_FILE: Path = PROJECT_ROOT / 'ilink-onebot.spec'
DIST_DIR: Path = PROJECT_ROOT / 'dist'
BUILD_DIR: Path = PROJECT_ROOT / 'build'


def _format_size(num_bytes: int) -> str:
    """把字节数格式化为 MB / GB（人类可读）"""
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes //= 1024
    return f"{num_bytes:.1f} TB"


def _check_pyinstaller_available() -> None:
    """确认当前 Python 环境装了 PyInstaller，未装则给出修复指引退出"""
    try:
        import PyInstaller  # noqa: F401  # pyright: ignore[reportMissingImports]
    except ImportError:
        print("[FAIL] PyInstaller 未安装在当前 Python 环境中。", file=sys.stderr)
        print(
            "  请运行 `uv sync --all-groups` 安装所有开发依赖，或直接 "
            "`uv add --group dev pyinstaller`",
            file=sys.stderr,
        )
        sys.exit(1)


def _clean_artifacts() -> None:
    """清理 dist/ 和 build/ 目录，避免与上次构建残留混合"""
    for path in (DIST_DIR, BUILD_DIR):
        if path.exists():
            print(f"[INFO] 清理 {path}")
            shutil.rmtree(path)


def _run_pyinstaller(debug: bool) -> None:
    """调用 PyInstaller 执行打包"""
    cmd = [
        sys.executable,
        '-m', 'PyInstaller',
        str(SPEC_FILE),
        '--noconfirm',  # 跳过覆盖确认
    ]
    if debug:
        # --debug=imports 在打包二进制中打印每个 import 的解析日志，
        # 用于调查"打包后 ImportError"
        cmd.extend(['--debug', 'imports'])

    print(f"[INFO] 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print(f"[FAIL] PyInstaller 退出码 {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)


def _report_output() -> None:
    """打印产物路径和文件大小"""
    exe_name = 'ilink-onebot.exe' if sys.platform == 'win32' else 'ilink-onebot'
    exe_path = DIST_DIR / exe_name
    if not exe_path.exists():
        print(f"[FAIL] 期望的产物 {exe_path} 不存在", file=sys.stderr)
        sys.exit(1)
    size = exe_path.stat().st_size
    print()
    print(f"[OK]  打包完成: {exe_path}")
    print(f"[OK]  文件大小: {_format_size(size)}")
    print(f"[OK]  运行: {exe_path.name} --help")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='ilink-onebot 单文件 exe 打包工具',
    )
    _ = parser.add_argument(
        '--no-clean', action='store_true',
        help='保留 build/ 和 dist/ 之前的产物（默认会清空）',
    )
    _ = parser.add_argument(
        '--debug', action='store_true',
        help='用 PyInstaller --debug=imports 模式构建（用于调查 ImportError）',
    )
    args = parser.parse_args()

    if not SPEC_FILE.exists():
        print(f"[FAIL] spec 文件不存在: {SPEC_FILE}", file=sys.stderr)
        sys.exit(1)

    _check_pyinstaller_available()

    if not args.no_clean:
        _clean_artifacts()

    _run_pyinstaller(debug=args.debug)
    _report_output()


if __name__ == '__main__':
    main()
