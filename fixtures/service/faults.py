"""明确的故障窗口；故障消费计数保存在 SQLite 事务中。"""

import socket


FAULTS = frozenset({"none", "fail_once", "drop_receipt_once"})


def validate_fault(value: str) -> str:
    if value not in FAULTS:
        raise ValueError("未知模拟业务故障")
    return value


def drop_committed_response(handler) -> None:
    """调用方提交写入后关闭连接，不发送 HTTP 响应。"""
    handler.close_connection = True
    try:
        handler.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    handler.connection.close()
