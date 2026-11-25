"""
Rubric 评测（LLM-as-Judge）：多准则质量评分，补充关键词验收判不了的语义维度。

设计要点：
  - 双层判定：规则层（路由/must_contain/错误关键词，在 run_eval 里）先出布尔字段；
    本模块只做规则判不了的语义判定（相关性/事实依据/完整性/清晰度）。
  - criterion 四态：pass / fail / unverifiable / not_applicable——
    unverifiable 与 not_applicable 不进 Rubric 得分分母（缺失不算错）。
  - fingerprint 缓存：query+回答+rubric 版本 的哈希 → Judge 结果落 .judge_cache.jsonl，
    重跑同输入不重复烧 LLM 调用（可复现评测的成本控制）。
  - Judge 模型 = config.py 当前 LLM_CONFIG（与被测同一模型）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

RUBRIC_VERSION = "v1"

# ---------- Rubric 注册表：每条 criterion 的维度定义与适用意图 ----------

RUBRICS: List[Dict] = [
    {
        "id": "ANSWER_RELEVANCE_V1",
        "name": "回答相关性",
        "criterion": "回答是否直接回应了用户的问题（答非所问 = fail；部分相关 = fail；"
                     "跑题去讲别的内容 = fail）",
        "applies_to": None,  # None = 全部意图
    },
    {
        "id": "FACT_GROUNDED_V1",
        "name": "事实依据",
        "criterion": "回答中的数字/标准/政策陈述是否可信——与公司差旅常识一致、无凭据编造的"
                     "具体金额或规定（给出数字但明显与常识或题目背景矛盾 = fail；"
                     "无法核对的笼统说法不算 fail）",
        "applies_to": {"rag_knowledge", "budget_check", "expense_estimate", "conflict_check"},
    },
    {
        "id": "COMPLETENESS_V1",
        "name": "完整性",
        "criterion": "回答是否覆盖了用户问题的核心要点（问题问了多个方面时只答了一部分 = fail；"
                     "单点问题答出核心即可 = pass）",
        "applies_to": None,
    },
    {
        "id": "CLARITY_V1",
        "name": "表达清晰",
        "criterion": "回答结构是否清晰可读（分点/分节/顺序合理 = pass；"
                     "大段混乱、信息堆砌难读 = fail）",
        "applies_to": None,
    },
    {
        "id": "SAFETY_COMPLIANCE_V1",
        "name": "敏感合规",
        "criterion": "敏感场景（投诉/退款/人身安全）是否走了人工通道话术：安抚得体、给出转接路径、"
                     "不擅自承诺处理结果（自行承诺退款/赔付结论 = fail）",
        "applies_to": {"escalation"},
    },
]

# 意图默认评分点（case 未写 rubric_points 时的兜底——保证 Judge 有具体锚点）
_DEFAULT_POINTS = {
    "rag_knowledge": "回答应基于知识库内容，数字/标准准确，无编造",
    "budget_check": "预算标准数字准确、城市分类正确",
    "expense_estimate": "费用预估分项合理、合计与分项一致",
    "itinerary_planning": "行程结构完整、必去点覆盖、时间安排合理",
    "compare_fares": "对比维度清晰、有明确结论",
    "trip_compare": "方案对比有依据、给出推荐",
    "conflict_check": "冲突结论明确、日期核对逻辑可信",
    "weather_alert": "天气信息具体、建议可执行",
    "information_query": "信息准确、与问题相关",
    "memory_query": "基于历史数据回答、不编造记忆",
    "preference": "正确识别偏好意图、确认记录",
    "escalation": "转人工话术得体、不擅自承诺",
}

JUDGE_PROMPT = """你是商旅助手系统的回复质量评测员。请对下面这条回复按逐条准则打分。

【用户问题】
{query}

【系统回复】
{answer}

【该问题的质量评分点（出题方视角的期望要点）】
{points}

【评测准则（逐条给出 pass / fail / unverifiable）】
{criteria_text}

【输出要求】
只输出 JSON（不要其他文本），格式：
{{
  "results": [
    {{"id": "<准则ID>", "status": "pass 或 fail 或 unverifiable", "reason": "<一句话理由，fail 必须指出具体问题>"}},
    ...
  ]
}}
说明：
- pass = 该准则达标；fail = 不达标（理由必须具体到回答中的问题）；unverifiable = 依据不足无法判定（如回答为空/纯报错占位）。
- 逐条独立判定，不要互相影响。
"""


def _load_judge_model():
    """Judge 用 config 当前 LLM_CONFIG（与被测同一模型）。"""
    from config import LLM_CONFIG, SYSTEM_CONFIG
    from agentscope.model import OpenAIChatModel
    timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
    return OpenAIChatModel(
        model_name=LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": float(timeout_sec)},
        generate_kwargs={
            "temperature": LLM_CONFIG.get("temperature", 0.7),
            "max_tokens": LLM_CONFIG.get("max_tokens", 16384),
        },
    )


class JudgeCache:
    """fingerprint → Judge 结果缓存（JSONL 追加式，重跑复用）"""

    def __init__(self, cache_path: Path):
        self.path = cache_path
        self._cache: Dict[str, dict] = {}
        if cache_path.exists():
            for line in cache_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self._cache[rec["fingerprint"]] = rec["result"]
                except Exception:
                    continue

    def get(self, fingerprint: str) -> Optional[dict]:
        return self._cache.get(fingerprint)

    def put(self, fingerprint: str, result: dict):
        self._cache[fingerprint] = result
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps({"fingerprint": fingerprint, "result": result},
                                   ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"judge cache write failed: {e}")


def _fingerprint(query: str, answer: str) -> str:
    raw = f"{RUBRIC_VERSION}|{query}|{answer}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _applicable_rubrics(intent: str) -> List[Dict]:
    return [r for r in RUBRICS if r["applies_to"] is None or intent in r["applies_to"]]


async def judge_case(query: str, answer: str, intent: str, rubric_points: Optional[List[str]],
                     model, cache: JudgeCache) -> Dict:
    """对一条 case 跑 Rubric Judge（带缓存）。

    返回 {criteria: [{id, status, reason}], score: float|None, from_cache: bool}
    score = pass / (pass + fail)；全 unverifiable 时为 None（不进分母）。"""
    points = rubric_points or [_DEFAULT_POINTS.get(intent, "回答应准确、相关、完整")]
    rubrics = _applicable_rubrics(intent)

    # 空回答/报错占位 → 全部 unverifiable，不烧 Judge
    if not answer or not answer.strip() or "执行失败" in answer or "请求失败" in answer:
        return {
            "criteria": [{"id": r["id"], "status": "unverifiable",
                          "reason": "回答为空或报错占位，无法语义判定"} for r in rubrics],
            "score": None, "from_cache": False,
        }

    fp = _fingerprint(query, answer)
    cached = cache.get(fp)
    if cached is not None:
        return {**cached, "from_cache": True}

    criteria_text = "\n".join(f"- {r['id']}（{r['name']}）：{r['criterion']}" for r in rubrics)
    prompt = JUDGE_PROMPT.format(query=query, answer=answer[:4000],
                                 points="；".join(points), criteria_text=criteria_text)

    # 逐条结果（解析失败的兜底形态）
    results: List[dict] = [{"id": r["id"], "status": "unverifiable", "reason": "Judge 输出解析失败"}
                           for r in rubrics]
    try:
        # agentscope OpenAIChatModel 的 await 返回异步生成器——逐块取最终文本
        # （复用 utils/skill_runners.py 的成熟提取模式：chunk 为 str / .content str / .content[].text）
        response = await model([{"role": "user", "content": prompt}])
        text = ""
        if hasattr(response, "__aiter__"):
            async for chunk in response:
                if isinstance(chunk, str):
                    text = chunk
                elif hasattr(chunk, "content"):
                    c = chunk.content
                    if isinstance(c, str):
                        text = c
                    elif isinstance(c, list):
                        for item in c:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item.get("text", "")
        elif hasattr(response, "content"):
            text = str(response.content)
        else:
            text = str(response)
        parsed = _parse_json(text)
        if parsed and isinstance(parsed.get("results"), list):
            by_id = {r["id"]: r for r in rubrics}
            norm = []
            for item in parsed["results"]:
                rid = item.get("id")
                if rid in by_id:
                    status = item.get("status")
                    if status not in ("pass", "fail", "unverifiable"):
                        status = "unverifiable"
                    norm.append({"id": rid, "status": status,
                                 "reason": str(item.get("reason", ""))[:200]})
            # 补齐 Judge 漏掉的准则
            got = {n["id"] for n in norm}
            for r in rubrics:
                if r["id"] not in got:
                    norm.append({"id": r["id"], "status": "unverifiable", "reason": "Judge 未输出该准则"})
            results = norm
    except Exception as e:
        logger.warning(f"judge call failed: {e}")

    n_pass = sum(1 for r in results if r["status"] == "pass")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    score = round(n_pass / (n_pass + n_fail), 3) if (n_pass + n_fail) else None
    out = {"criteria": results, "score": score, "from_cache": False}
    cache.put(fp, out)
    return out


def _parse_json(text: str) -> Optional[dict]:
    """Judge 输出的容错 JSON 解析（剥围栏 → 直接 loads → 区间提取）"""
    text = text.strip()
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None
