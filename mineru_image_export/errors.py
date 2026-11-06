"""独立工具各阶段共享的可读错误类型。"""


class ExportError(ValueError):
    """输入、解析或图片资源不完整时，阻止生成不完整清单。"""
