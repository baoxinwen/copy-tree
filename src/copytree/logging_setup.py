"""集中配置 loguru 日志。

约定（全项目统一，各模块禁止自行添加 sink）：
- 文件：%APPDATA%/copy-tree/logs/copy-tree.log，DEBUG 起记，
  轮转 2MB、保留 5 份、UTF-8；线程安全由 sink 内置锁保证，
  不启用 enqueue——它依赖 multiprocessing，而打包 spec 已排除该模块；
- stderr：仅 CLI 模式启用，且只输出 WARNING 及以上，
  避免污染脚本的 stdout 管道与重定向产物；
- stdout 永远不接日志——它是目录树本身的数据通道；
- 隐私边界：只记录路径与统计信息，绝不记录文件内容或剪贴板文本。
"""

import os
import sys

from loguru import logger

from .constants import LOG_DIR, LOG_FILE

_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
_STDERR_FORMAT = "<level>{level}</level> | {message}"

_configured = False


def setup_logging(enable_stderr: bool = False) -> None:
    """初始化全局日志。幂等；日志自身故障不得影响主流程。"""
    global _configured
    if _configured:
        return
    _configured = True
    logger.remove()  # 丢弃默认 sink，改用下方显式配置
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        logger.add(
            LOG_FILE,
            level="DEBUG",
            rotation="2 MB",
            retention=5,
            encoding="utf-8",
            format=_FILE_FORMAT,
        )
    except OSError:
        # 文件不可写时退化为仅 stderr 最小配置，保证程序继续可用
        logger.add(sys.stderr, level="WARNING", format=_STDERR_FORMAT)
        return
    if enable_stderr:
        logger.add(sys.stderr, level="WARNING", format=_STDERR_FORMAT)
