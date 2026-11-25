"""
Skill Runners —— 声明式 Skill 的通用执行器

一个 skill 不再绑定一个 agent 类，而是 SKILL.md frontmatter 声明能力：

    ---
    name: preference
    kind: llm_skill          # llm_skill | rag_skill | tool_skill
    consumes: []             # 需要的前序 agent 结果（空 = 只用 context）
    description: ...
    ---

执行逻辑收敛到 3 个 runner：
  - llm_runner ：SKILL.md 正文即 prompt 模板 → 调 LLM → 返回 JSON（适用于纯提示词型能力）
  - rag_runner ：Milvus 混合检索 → LLM 生成（ask-question）
  - tool_runner：工具调用（wttr/DDGS）→ LLM 摘要（query-info）

原有 agent.py 的 80% 样板（入参解析、异步生成器兼容、JSON 围栏剥离、错误处理）全部收敛到这里。
"""
from typing import Optional, Union, List, Dict, Any
import asyncio
import json
import logging

from agentscope.agent import AgentBase
from agentscope.message import Msg

logger = logging.getLogger(__name__)

# ---------- 执行边界治理（M5） ----------

# 单次 skill 的 prompt 字符上限：超长直接拒绝而非烧一次 LLM 才超时失败
MAX_PROMPT_CHARS = 60_000


def _skill_timeout_sec() -> float:
    """skill 执行超时钳制：配置值 clamp 到 [30, 600] 秒，防呆配置错误"""
    try:
        from config import SYSTEM_CONFIG
        raw = SYSTEM_CONFIG.get("skill_timeout_sec", 240)
    except Exception:
        raw = 240
    return max(30.0, min(float(raw), 600.0))


# ---------- 公共样板 ----------

def _parse_input(x) -> Dict[str, Any]:
    """解析编排器传入的 Msg：content 为协议 JSON，拆出 context/previous_results"""
    content = x[-1].content if isinstance(x, list) else x.content
    try:
        data = json.loads(content) if isinstance(content, str) else content
        if not isinstance(data, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        data = {"context": {"rewritten_query": str(content)}}
    return data


async def _call_llm_text(model, messages: List[Dict[str, str]]) -> str:
    """调 LLM 并提取文本（兼容异步生成器 / 普通响应）。
    整体超时钳制：超过 _skill_timeout_sec() 抛 asyncio.TimeoutError（上层捕获为 error 结果）。"""
    response = await asyncio.wait_for(model(messages), timeout=_skill_timeout_sec())
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
    return text.strip()


def _parse_llm_json(text: str) -> Optional[dict]:
    """从 LLM 输出鲁棒解析 JSON（剥围栏 → 修复常见非法形态 → 区间提取 → raw_decode）"""
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
        # 常见非法形态修复：字符串值前的裸换行（'"key":\n"value"' → '"key":"value"'）
        import re as _re
        fixed = _re.sub(r'"\s*:\s*\n\s*"', '": "', text)
        if fixed != text:
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
                # 修复后仍失败，继续走区间提取
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[start:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    return None


def _reply(agent_name: str, payload: dict) -> Msg:
    return Msg(name=agent_name, content=json.dumps(payload, ensure_ascii=False), role="assistant")


# ---------- 通用 LLM Skill ----------

class LLMSkillAgent(AgentBase):
    """纯提示词型 skill：SKILL.md 正文即指令模板。

    SKILL.md 正文中的占位符（执行时替换）：
      {{user_query}} {{key_entities}} {{user_preferences}} {{current_date}}
      {{previous_results}}   —— 编排器按 consumes 过滤后的前序结果 JSON
      {{trip_history}} / {{chat_summary}} —— need_memory: true 时注入的记忆数据
    """

    def __init__(self, name: str = "LLMSkillAgent", model=None, skill_name: str = "",
                 memory_manager=None, skill_loader=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.skill_name = skill_name
        self.memory_manager = memory_manager
        self.skill_loader = skill_loader
        self._meta_cache = None

    def _load_meta(self) -> Dict[str, Any]:
        """读取 SKILL.md frontmatter（kind/consumes/need_memory 等）"""
        if self._meta_cache is None:
            self._meta_cache = {}
            if self.skill_loader:
                info = (self.skill_loader.load_skills() or {}).get(self.skill_name, {})
                self._meta_cache = info if isinstance(info, dict) else {}
        return self._meta_cache

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        data = _parse_input(x)
        context = data.get("context", {})

        # ---------- prepare（M8：拼 prompt，纯确定性逻辑，durable 端点单独调用） ----------
        prompt_or_error = await self.prepare_prompt(data)

        if isinstance(prompt_or_error, dict) and prompt_or_error.get("_error"):
            # 边界治理拒绝（超长 prompt 等）：短路终态
            return _reply(self.name, prompt_or_error["result"])

        prompt = prompt_or_error

        # ---------- generate（本地降级路径：Python 直调 LLM；durable 路径在 pi-durable 上跑） ----------
        try:
            text = await _call_llm_text(self.model, [{"role": "user", "content": prompt}])
        except Exception as e:
            logger.error(f"LLM skill '{self.skill_name}' failed: {e}")
            return _reply(self.name, {"status": "error", "message": str(e)})

        # ---------- parse（M8：解析输出，纯确定性逻辑，durable 端点单独调用） ----------
        result = self.parse_output(text)
        return _reply(self.name, result)

    async def prepare_prompt(self, data: Dict[str, Any]) -> Union[str, Dict[str, Any]]:
        """M8 prepare 阶段：拼好完整 prompt（含记忆取数/占位符替换/边界检查）。

        返回 prompt 字符串；边界拒绝时返回 {"_error": True, "result": {error 结构}}（短路终态）。
        纯确定性逻辑（无 LLM），pi-durable 的 SkillGenTask 经 /prepare 端点调用。"""
        context = data.get("context", {})
        meta = self._load_meta()

        instruction = (self.skill_loader.get_skill_content(self.skill_name) if self.skill_loader else "") \
                      or "请根据用户需求完成任务。"

        user_query = context.get("rewritten_query", "") or data.get("rewritten_query", "")
        ke = context.get("key_entities", {}) or {}

        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")

        # previous_results 已由编排器按 consumes 过滤（_execute_agent），此处直接使用
        prev = data.get("previous_results", [])

        # 记忆数据源（skill 声明 need_memory: true 时由 runner 注入，避免每个 skill 重复写取数代码）
        trip_history_str, chat_summary_str = "（暂无行程记录）", "（暂无历史对话摘要）"
        if meta.get("need_memory") and self.memory_manager:
            try:
                trips = self.memory_manager.long_term.get_trip_history(limit=50)
                trip_history_str = "\n".join(
                    f"- {t.get('origin', '?')} → {t.get('destination', '?')} "
                    f"({t.get('start_date', '')}{' 至 ' + t['end_date'] if t.get('end_date') else ''}) {t.get('purpose', '')}"
                    for t in trips) or "（暂无行程记录）"
            except Exception as e:
                logger.warning(f"trip_history load failed: {e}")
            try:
                chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=30)
                if chat_summary:
                    chat_summary_str = chat_summary
            except Exception as e:
                logger.warning(f"chat summary load failed: {e}")

        prompt = (instruction
                  .replace("{{user_query}}", user_query)
                  .replace("{{key_entities}}", json.dumps(ke, ensure_ascii=False))
                  .replace("{{user_preferences}}", json.dumps(context.get("user_preferences", {}), ensure_ascii=False))
                  .replace("{{current_date}}", current_date)
                  .replace("{{previous_results}}", json.dumps(prev, ensure_ascii=False, indent=2))
                  .replace("{{trip_history}}", trip_history_str)
                  .replace("{{chat_summary}}", chat_summary_str))

        # 边界治理：超长 prompt 直接拒绝，不烧 LLM（失败要早、要便宜）
        if len(prompt) > MAX_PROMPT_CHARS:
            logger.warning(f"LLM skill '{self.skill_name}' prompt too long: {len(prompt)} chars")
            return {"_error": True, "result": {
                "status": "error",
                "message": f"输入上下文超长（{len(prompt)} 字符 > {MAX_PROMPT_CHARS}），已拒绝执行",
            }}
        return prompt

    def parse_output(self, text: str) -> dict:
        """M8 parse 阶段：解析 LLM 输出文本为 skill 结果 dict。

        纯确定性逻辑（四层 JSON 解析 + 失败兜底），pi-durable 的 SkillGenTask 经 /parse 端点调用。"""
        result = _parse_llm_json(text)
        if result is None:
            return {"status": "success", "answer": text}
        return result


# ---------- RAG / Tool Skill ----------
# ask-question（Milvus 混合检索 + BM25）与 query-info（wttr/DDGS）含有实质基础设施：
# 连接管理、索引构建、错误分类、外部协议。这两类保留其 agent.py，
# 仅将入参解析 / LLM 输出提取复用本模块的 _parse_input / _call_llm_text / _parse_llm_json。
# （在对应 agent.py 中逐步替换，协议不变。）


def build_llm_agent(model, skill_name: str, skill_loader=None, memory_manager=None) -> LLMSkillAgent:
    """给 LazyAgentRegistry 用的工厂：kind=llm_skill 的 skill 全部走这个"""
    return LLMSkillAgent(name=skill_name, model=model, skill_name=skill_name,
                          memory_manager=memory_manager, skill_loader=skill_loader)
