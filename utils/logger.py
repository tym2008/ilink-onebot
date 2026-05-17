"""
loguru 日志配置

用法：
    from loguru import logger as l
    l.info("message")
    l.debug("debug info")
"""
import sys
from pathlib import Path

from loguru import logger as l

from meta_config import meta_config


def setup_logger() -> None:
    """初始化 loguru 配置：stdout + 轮转文件"""
    l.remove()

    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    _ = l.add(
        sys.stdout,
        level=meta_config.LOG_LEVEL,
        format=log_format,
        colorize=True,
    )

    log_path = Path(meta_config.LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _ = l.add(
        str(log_path),
        level=meta_config.LOG_LEVEL,
        format=log_format,
        rotation=meta_config.LOG_ROTATION_SIZE,
        retention=meta_config.LOG_RETENTION_COUNT,
        compression=meta_config.LOG_COMPRESSION,
        encoding='utf-8',
    )
