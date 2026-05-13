"""Telegram channel 散落常量集中处。

TG API 限制、内部 TTL、长时 HTTP 超时等。
"""

from __future__ import annotations

# Telegram 标准 Bot API sendDocument 上限 50MB；
# 本地 Bot API（telegram.base_url 配置）可放宽到 ~2GB。
TG_DOCUMENT_LIMIT = 50 * 1024 * 1024
LOCAL_BOT_API_DOCUMENT_LIMIT = 2 * 1024 * 1024 * 1024

# 大文件 sendDocument 上传 / get_file 下载用的 HTTP 超时（秒）。
# 90MB 上传在默认几十秒内必报 Timed out；本地 Bot API 下数据在本机走 local_mode，
# 这里给一个不会主动打断的上限。
TG_UPLOAD_TIMEOUT = 3600

# eh/ex 模式按钮的 pending 记录 TTL：超时认为用户放弃。
PENDING_TTL = 600

# Cancel token 兜底 TTL：任务正常结束/取消会主动清理，这里仅防异常路径下 dict 无限增长。
CANCEL_TOKEN_TTL = 3600
