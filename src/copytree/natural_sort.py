"""自然排序：file2 排在 file10 前面。"""

import re

_NUM_SPLIT = re.compile(r"(\d+)")


def natural_sort_key(name: str) -> list[str | int]:
    """将文件名拆分为文本和数字交替的列表，数字段转为 int 用于比较。

    >>> natural_sort_key("file10")
    ['file', 10]
    >>> natural_sort_key("file2")
    ['file', 2]
    >>> natural_sort_key("file2") < natural_sort_key("file10")
    True
    """
    parts = _NUM_SPLIT.split(name.lower())
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(int(part))
        else:
            result.append(part)
    return result
