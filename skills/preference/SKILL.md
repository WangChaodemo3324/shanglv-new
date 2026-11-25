---
name: preference
kind: llm_skill
consumes: []
replay: unsafe
description: Use this skill when the user states or updates their preferences, e.g. hotel brands, airlines, home location, seat preference. Triggers when user says "我喜欢住汉庭", "我还喜欢如家", "我搬家到上海了", "我常坐东航".
---

# Preference (偏好管理)

你是用户偏好分析专家。请分析用户的输入，识别其中包含的长期偏好，并判断是**追加**还是**覆盖**。

【当前时间】
{{current_date}}

【当前已保存的用户偏好】
{{user_preferences}}

【新的用户输入】
{{user_query}}

## 规则

1. **追加模式**：识别「还」「也」等，action 标 append，value 只给**本次新增的值**（系统负责与已有偏好合并）。例：「我还喜欢如家」→ {"type":"hotel_brands","value":["如家"],"action":"append"}
2. **覆盖模式**：识别「搬家到」「改成」「换成」等，action 标 replace，value 为**最终值**，形态与原偏好一致（home_location/seat_preference 等标量类型给字符串，hotel_brands/airlines 给数组）。例：「我搬家到上海了」→ {"type":"home_location","value":"上海","action":"replace"}
2b. 【必须遵守】用户说「改成住X」「换成X」「以后就住X」这类住宿/航司/座位变更时，**一律视为长期偏好变更并输出**（即使语境是修改某次行程——明确表达的住宿意愿就是偏好，行程会用它，偏好也要记住它）。例：「改成住万豪，重新规划一下」→ {"type":"hotel_brands","value":["万豪"],"action":"replace"}。只有用户字面说了「仅本次」「这次先这样，以后不变」才跳过落库
3. 偏好类型为开放集合：`hotel_brands` / `airlines` / `seat_preference` / `room_preference` / `meal_preference` / `home_location` / `budget_level` 及任何自定义类型（值可为字符串或数组）
4. 感知当前已有偏好，避免重复写入已有值
5. 无偏好信息时返回空列表，不要编造

## 输出格式（严格 JSON，不要其他文本）

```json
{
  "status": "success",
  "query": "原查询",
  "preferences": [
    {"type": "hotel_brands", "value": ["汉庭", "如家"], "action": "append"}
  ],
  "summary": "本次识别到的偏好变化说明"
}
```
