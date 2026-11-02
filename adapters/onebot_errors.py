"""OneBot 动作失败的诊断摘要，不把协议端堆栈或凭据送入模型/聊天。"""

from __future__ import annotations


class OnebotActionError(RuntimeError):
    """保留动作、状态和返回码，并把常见 NapCat 错误映射为可操作的原因。"""

    def __init__(self, action: str, response):
        response = response if isinstance(response, dict) else {}
        status = response.get("status")
        self.status = status if status in ("ok", "failed", "async") else "unknown"
        code = response.get("retcode")
        self.retcode = code if type(code) is int else None
        self.action = action
        message = " ".join(str(response.get(key) or "")[:4096] for key in ("message", "wording")).lower()
        packet_unavailable = "packetbackend" in message and any(word in message for word in ("不可用", "unavailable", "not available"))
        if packet_unavailable and "发包" in message:
            self.reason = "packetBackend 发包能力不可用，请检查服务器 NapCat 的 Packet 后端"
        elif packet_unavailable:
            self.reason = "packetBackend 不可用，请检查服务器 NapCat 的 Packet 后端"
        elif any(word in message for word in ("unsupported api", "unknown action", "unsupported action", "未知接口", "不支持的接口")):
            self.reason = "当前 NapCat 未提供此动作，请检查运行版本和接口支持情况"
        elif "内层消息" in message:
            self.reason = "记录已过期或属于不能单独按 ID 获取的内层记录，请重新发送外层合并转发"
        elif any(word in message for word in ("已过期", "resid无效", "找不到相关的聊天记录", "消息不存在")):
            self.reason = "转发记录不存在或已失效，请重新发送合并转发"
        elif any(word in message for word in ("schema compilation error", "expected string", "请检查参数", "缺少", "required")):
            self.reason = "NapCat 拒绝了请求参数，请检查运行版本与参数格式"
        elif any(word in message for word in ("超时", "timeout", "timed out")):
            self.reason = "NapCat 内部操作超时，执行结果未确认"
        elif self.retcode in (1401, 1403):
            self.reason = "NapCat 拒绝了访问，请检查接口权限或鉴权配置"
        elif self.retcode == 1404:
            self.reason = "NapCat 找不到该接口或资源，请检查版本与资源是否仍有效"
        else:
            self.reason = "协议端未确认成功，请用 /probe 检查版本和 Packet 状态，必要时查看服务器日志"
        code_text = str(self.retcode) if self.retcode is not None else "未知"
        super().__init__(f"NapCat {action} 失败（status={self.status}, retcode={code_text}）：{self.reason}")
