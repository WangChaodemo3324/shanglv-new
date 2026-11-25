"""
敏感请求前置路由（敏感路由，EscalationAgent 思路的轻量版）

在意图识别之前做确定性检查：投诉/退款/紧急安全类表达不走 LLM 编排，
直接返回固定应答。理由：这类请求被路由到 rag_knowledge 会答非所问（答政策条文），
而它们真正需要的是人工通道，确定性规则比 LLM 路由可靠。
"""
import re
from typing import Optional, Dict, Any

# 敏感类别 → (触发模式, 应答)
_RULES: list = [
    (
        "personal_safety",
        re.compile(r"(人身安全|安全威胁|被威胁|遇到危险|人身危险|报警|急救|救护车|生命危险|拨打\s*1[12]0|打\s*1[12]0)", re.IGNORECASE),
        "您描述的情况涉及人身安全，请**立即拨打 110（报警）或 120（急救）**。\n\n"
        "差旅期间的紧急安全事项也可联系公司应急值班电话（详见差旅应急指南），"
        "系统不处理紧急安全求助，人工通道能更快帮到您。"
    ),
    (
        "complaint",
        re.compile(r"(投诉|举报|起诉|要告|很差劲|太差了|欺骗|欺诈|退款不|不给退)", re.IGNORECASE),
        "抱歉给您带来不好的体验。您的诉求涉及投诉/退款，需要人工客服处理：\n\n"
        "  • 差旅平台客服：400-800-8888（24 小时）\n"
        "  • 报销争议：联系财务部 + 直属主管\n\n"
        "如需查询相关政策规定（如退款条件、报销流程），我可以继续为您检索。"
    ),
    (
        "refund",
        re.compile(r"(退款|退钱|退票失败|扣款错误|重复扣款|多扣了)", re.IGNORECASE),
        "您描述的情况涉及资金/退款处理，请通过人工通道解决：\n\n"
        "  • 平台客服：400-800-8888（订单退款、扣款错误）\n"
        "  • 保留订单号、支付凭证，客服处理更快\n\n"
        "如果只是想了解「退款政策/条件是什么」，请换个说法（如「机票退票政策是什么」），我来帮您查。"
    ),
]


def check_sensitive_request(user_input: str) -> Optional[Dict[str, Any]]:
    """返回敏感路由结果；非敏感返回 None（走正常流程）。

    Returns: {"sensitive": True, "category": ..., "response": 固定应答}
    """
    text = (user_input or "").strip()
    if not text or len(text) > 500:
        return None
    for category, pattern, response in _RULES:
        if pattern.search(text):
            return {"sensitive": True, "category": category, "response": response}
    return None
