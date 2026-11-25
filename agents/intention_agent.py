"""
意图识别智能体 IntentionRecognitionAgent
职责：准确识别用户意图，并进行智能体调度

核心功能：
1. 多意图识别和分类：融合上下文对模糊意图进行消歧
2. 智能体调度决策：基于预定义的触发条件和业务规则，根据识别结果决定调度哪些 skill
3. Query改写：标准化用户口语化的query输入，补全上下文信息，提取和重组关键信息
4. 显示推理：输出的两段式结构（推理过程 + JSON决策），提升意图识别准确度

架构：
- 使用单一LLM（用户配置的模型）
- 输入：用户query（自然语言）
- 输出：推理过程生成（包含reasoning+原因） + 多意图识别（原因） + 智能Query改写 + 构建结构化决策
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List
import json
import logging
from utils.skill_loader import SkillLoader

logger = logging.getLogger(__name__)


class IntentionAgent(AgentBase):
    """意图识别智能体（IntentionRecognitionAgent）"""

    def __init__(self, name: str = "IntentionRecognitionAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.conversation_history = []
        self.skill_loader = SkillLoader()

    def build_prompt(self, x: Optional[Union[Msg, List[Msg]]] = None) -> str:
        """构建意图识别 prompt（本地路径与 pi-durable GenerationTask 路径共用的单一事实源）。
        输入与 reply() 同形：context_messages（system 长期记忆 + 最近对话 + 当前输入）。"""
        # 获取用户查询
        if isinstance(x, list):
            user_query = x[-1].content if x else ""
            # 提取历史对话，保留角色信息
            self.conversation_history = []
            for msg in x[:-1]:
                if hasattr(msg, 'content') and hasattr(msg, 'role'):
                    # 区分处理不同角色的消息
                    if msg.role == "system":
                        # 长期记忆（system）- 完整保留，不截断
                        self.conversation_history.append(f"[系统记忆]\n{msg.content}")
                    else:
                        # 对话历史（user/assistant）- 适当截断但保留更多信息
                        role_name = "用户" if msg.role == "user" else "助手"
                        content = msg.content[:800] if len(msg.content) > 800 else msg.content
                        if len(msg.content) > 800:
                            content += "..."
                        self.conversation_history.append(f"{role_name}: {content}")
        else:
            user_query = x.content if x is not None else ""

        # 构建上下文
        # 策略：长期记忆始终保留，短期对话全部保留（已在 cli.py 控制数量）
        context_parts = []
        system_memory = None
        dialogue_history = []

        for item in self.conversation_history:
            if item.startswith("[系统记忆]"):
                system_memory = item  # 保存长期记忆
            else:
                dialogue_history.append(item)  # 保存对话历史

        # 组装上下文：长期记忆 + 全部对话
        if system_memory:
            context_parts.append(system_memory)
        if dialogue_history:
            context_parts.extend(dialogue_history)

        context_str = "\n".join(context_parts) if context_parts else "无历史对话"

        # 获取当前时间
        from datetime import datetime
        current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        # 动态获取 Skills 描述
        skill_mapping = {
            "memory-query": "memory_query",
            "plan-trip": "itinerary_planning",
            "preference": "preference",
            "query-info": "information_query",
            "ask-question": "rag_knowledge",
            "budget-check": "budget_check",
            "compare-fares": "compare_fares",
            "itinerary-lookup": "itinerary_lookup",
            "weather-alert": "weather_alert",
            "expense-estimate": "expense_estimate",
            "trip-compare": "trip_compare",
            "conflict-check": "conflict_check"
        }
        
        dynamic_skills_prompt = self.skill_loader.get_skill_prompt(skill_mapping)
        
        # 构建意图识别Prompt
        prompt = f"""你是一个高级意图识别专家（IntentionRecognitionAgent）。请分析用户查询，识别意图并输出结构化的决策。
        
【当前时间】
{current_time} {weekday}
（重要：当用户说"2月28日"或"明天"等相对时间时，请根据当前时间进行推断完整日期）

【用户Query】
{user_query}

【对话历史上下文】
{context_str}

【可调度的 Skill】
{dynamic_skills_prompt}

【重要 - 意图区分原则】
请基于语义理解判断意图，不要机械匹配关键词。同一个词在不同语境下可能对应不同意图：
- "我去过北京吗？" → memory_query（询问自己的历史）
- "北京怎么样？" / "北京有什么好玩的？" → information_query（询问客观信息）
- "我想去北京" → itinerary_planning（规划未来行程）
- "我喜欢住如家" / "我常坐东航" / "我家住北京" → preference（第一人称陈述个人偏好/常住地/习惯，
  是在告知系统以便记住，不是提问——即使句中无"记住/偏好"字眼）
- 只有明确在**提问**（问政策/问标准/问历史/问信息）时才考虑其他意图；纯陈述个人情况的必是 preference

优先级规则：
- memory_query 优先于 information_query（当问题涉及用户自己的历史时）
- 如果用户明确询问"我的"、"我过去的"，必须识别为 memory_query

【任务要求】
请按以下步骤进行分析：

**第1步：推理过程生成**
- 分析用户query的核心诉求
- 识别query中的关键实体和意图信号
- 判断是否需要结合对话历史进行消歧
- 说明如何融合上下文信息进行推理

**第2步：多意图识别（原因）**
- 识别所有可能的用户意图（可以是多个）
- 说明为什么识别出该意图的原因

**第3步：智能Query改写**
- 识别口语化表达，进行标准化
- 补全省略的上下文信息（如指代消解："那里"→具体城市名）
- 提取和重组关键信息
- 【重要】改写只用于消解指代和补全省略，必须保留用户原文的核心用词，
  禁止添加原文没有的词（如"报销"、"限额"、"政策"等），
  否则会改变检索方向。例："北京住宿标准是多少" → "北京住宿标准是多少？"，
  而不是"查询北京差旅住宿报销标准（每人每晚限额）"

**第4步：构建结构化决策**
- 基于识别的意图，决定调度哪些 skill
- 说明调用顺序和优先级
- 输出结构化的调用策略
- key_entities 请完整提取行程要素（出发地/目的地/日期/天数/目的），缺失字段填 null 并在 missing_info 里列出字段名（仅当意图涉及行程规划时才需要）
- 判定复杂度 complexity：
  - "simple"：只需调用**一个**信息查询类智能体就能完整回答（如查政策、查天气、查预算、比价、问偏好）
  - "standard"：需要多个智能体协作（如 P1→P2 组合：预算+费用预估、历史+冲突检测、行程规划+偏好记忆等）
- 判定是否修改类请求 replan（布尔）：
  - true：用户在**修改上一轮的成果**（如"换希尔顿""改到下周""把第二天行程换成博物馆"），
    通常只涉及偏好/日期/某个要素的变更，前序信息查询无需重做
  - false（默认）：全新请求

【输出格式要求】
必须严格按照以下JSON格式输出（**只输出JSON，不要有其他文本**）：

{{
    "reasoning": "这里是详细的推理过程，包含第1步的分析，说明如何理解用户query，如何结合上下文，如何识别意图信号",

    "intents": [
        {{
            "type": "意图类型（如：itinerary_planning, preference_collection, information_query等）",
            "description": "该意图的具体说明",
            "reason": "为什么识别出该意图的原因"
        }}
    ],

    "key_entities": {{
        "origin": "出发地（缺失用 null；用户没说但有家庭住址等上下文时可推断）",
        "destination": "目的地（缺失用 null）",
        "start_date": "出发日期，完整 YYYY-MM-DD（相对时间按当前时间推断；缺失用 null）",
        "end_date": "返程日期，完整 YYYY-MM-DD（缺失用 null）",
        "duration_days": "行程天数，整数（'一日游'为 1；缺失用 null）",
        "return_location": "返程地（缺失用 null）",
        "trip_purpose": "行程目的：出差/旅游/探亲等（可从语气推断，默认 null）",
        "other": "其他关键信息"
    }},

    "rewritten_query": "标准化、补全后的查询内容",

    "missing_info": ["key_entities 中缺失且规划行程必需的字段名，如 start_date/origin；信息完整时为空数组"],

    "complexity": "simple 或 standard",

    "replan": false,

    "agent_schedule": [
        {{
            "agent_name": "skill 调度名",
            "priority": 1,
            "reason": "调用该智能体的原因和依据",
            "expected_output": "期望该智能体提供什么输出"
        }}
    ]
}}

【重要提示 - 优先级设置规则】
优先级数字相同的智能体会**并行执行**，不同优先级按顺序批次执行。

**所有智能体优先级分组：**

**Priority 1（并行执行）- 信息收集类：**
- memory_query: 记忆查询（叙述式回答历史/偏好问题）
- itinerary_lookup: 历史行程结构化查询（输出城市/频次数据，供后续分析用）
- preference: 偏好管理
- information_query: 信息查询（联网搜索/天气/地点/周边/驾车或步行路线，地图查询应在 key_entities.destination 中保留城市）
- rag_knowledge: 知识库问答（差旅政策/报销标准）
- budget_check: 差旅标准与预算上限计算
- compare_fares: 价格对比（机票/酒店/交通方式）
- weather_alert: 天气与出行建议（带伞/改签/着装）

**Priority 2（依赖 Priority 1）- 分析规划类：**
- itinerary_planning: 行程规划（行程要素直接从 key_entities 获取）
- expense_estimate: 费用预估（预估明细 vs 报销上限，超支预警；依赖 budget_check 的标准）
- trip_compare: 行程方案对比（依赖比价/预算/历史数据）
- conflict_check: 行程冲突检测（新计划 vs 历史行程的日期重叠/撞车）

**说明：**
- Priority 1 的智能体都是信息获取，互不依赖，可并行执行提升速度
- Priority 2 的智能体需要使用 Priority 1 收集的信息（系统会自动把 P1 结果传给 P2）
- 示例：用户说"下周去杭州出差3天要花多少、公司能报多少"
  → Priority 1: budget_check（并行）
  → Priority 2: expense_estimate（使用预算标准 + 行程要素）
- 意图相近时的选择：问政策原文 → rag_knowledge；问预算数字/上限 → budget_check；问大概花多少 → expense_estimate（通常与 budget_check 搭配调度）

请开始分析，直接输出JSON：
"""

        return prompt

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        意图识别主流程
        1. 推理过程生成
        2. 多意图识别
        3. 智能Query改写
        4. 构建结构化决策
        """
        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        # 构建 prompt（本地路径与 pi-durable GenerationTask 路径共用的单一事实源）
        prompt = self.build_prompt(x)

        # 调用LLM进行意图识别
        try:
            # 构建符合OpenAI格式的messages
            messages = [
                {"role": "system", "content": "你是一个高级意图识别专家。只输出JSON格式的结果，不要输出其他文本。"},
                {"role": "user", "content": prompt}
            ]
            response = await self.model(messages)

            # 获取响应文本 - 处理异步生成器
            text = ""
            if hasattr(response, '__aiter__'):
                # 异步生成器，需要迭代获取内容
                async for chunk in response:
                    if isinstance(chunk, str):
                        text = chunk
                    elif hasattr(chunk, 'content'):
                        if isinstance(chunk.content, str):
                            text = chunk.content
                        elif isinstance(chunk.content, list):
                            for item in chunk.content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
            elif hasattr(response, 'text'):
                text = response.text
            elif hasattr(response, 'content'):
                text = response.content
            elif isinstance(response, dict) and 'content' in response:
                text = response['content']
            else:
                text = str(response) if response else ""

            # 清理文本
            text = text.strip()
            if text.startswith('```json'):
                text = text[7:]
            if text.startswith('```'):
                text = text[3:]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

            # 解析JSON
            try:
                result = json.loads(text)
            except json.JSONDecodeError as e1:
                # 如果直接解析失败，尝试提取JSON
                start_idx = text.find('{')
                end_idx = text.rfind('}')

                if start_idx != -1 and end_idx != -1:
                    json_str = text[start_idx:end_idx+1]
                    try:
                        result = json.loads(json_str)
                    except json.JSONDecodeError as e2:
                        logger.error(f"JSON parse failed. Text sample: {json_str[:100]}")
                        raise ValueError(f"Failed to parse JSON. Error: {e2}")
                else:
                    raise ValueError(f"No JSON found in response. Parse error: {e1}")

        except Exception as e:
            logger.error(f"Intent recognition failed: {e}")
            # 兜底用原查询重写（build_prompt 的 user_query 是其局部变量，此处重新提取）
            if isinstance(x, list):
                user_query = x[-1].content if x else ""
            elif x is not None:
                user_query = x.content
            else:
                user_query = ""
            # 返回默认结果
            # 兜底策略：失败时路由到 rag_knowledge（本地向量库检索，不依赖 LLM 的再调用，
            # 差旅政策类问题命中率高）。information_query 兜底在网关故障时会跟着失败并返回空结果。
            result = {
                "reasoning": f"意图识别出错，使用默认策略（本地知识库兜底）。错误: {str(e)}",
                "intents": [
                    {
                        "type": "rag_knowledge",
                        "description": "默认知识库查询意图",
                        "reason": "无法解析用户意图，使用本地知识库兜底"
                    }
                ],
                "key_entities": {},
                "rewritten_query": user_query,
                "complexity": "simple",
                "agent_schedule": [
                    {
                        "agent_name": "rag_knowledge",
                        "priority": 1,
                        "reason": "默认查询（本地知识库，不依赖外部搜索）",
                        "expected_output": "查询结果"
                    }
                ]
            }

        # 将结果转换为JSON字符串，因为Msg的content必须是字符串
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
