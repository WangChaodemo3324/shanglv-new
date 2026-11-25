#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
懒加载智能体注册器
基于 skills 目录结构的插件化加载机制
"""
import sys
import importlib.util
import inspect
from pathlib import Path
from typing import Dict, Optional
from rich.console import Console
from agentscope.agent import AgentBase

class LazyAgentRegistry:
    """
    懒加载智能体注册器 - 插件化版本
    
    自动扫描 skills 下的技能目录，动态加载 script/agent.py
    """

    def __init__(self, model, cache: Dict, memory_manager=None):
        """
        初始化懒加载注册器

        Args:
            model: 共享的 LLM 模型实例
            cache: 用于缓存已加载智能体的字典
            memory_manager: 记忆管理器 (可选，用于注入给需要它的 Agent)
        """
        self.model = model
        self.cache = cache
        self.memory_manager = memory_manager
        self.console = Console()
        
        # 技能目录路径
        self.skills_root = Path("skills")
        
        # 技能映射表: skill_name -> agent_script_path（llm_skill 为 None）
        self._skill_map: Dict[str, Path] = {}
        # 声明式 llm_skill 集合（SKILL.md 即全部定义）
        self._llm_skills = set()
        
        # 发现技能
        self._discover_skills()

        # 调度名别名（历史名 → 目录名），与编排器的 consumes 判定共用同一张表
        from utils.skill_loader import DISPATCH_ALIASES
        self._legacy_mapping = DISPATCH_ALIASES

    def _discover_skills(self):
        """扫描 skills 目录寻找可用的 Agent"""
        if not self.skills_root.exists():
            self.console.print(f"[yellow]Warning: Skills directory {self.skills_root} not found[/yellow]")
            return

        count = 0
        for skill_dir in self.skills_root.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_md = skill_dir / "SKILL.md"
            # 声明式：SKILL.md frontmatter 有 kind 字段即可被调度（llm_skill 无需 agent.py）
            kind = None
            if skill_md.exists():
                try:
                    from utils.skill_loader import SkillLoader
                    _sl = SkillLoader()
                    info = (_sl.load_skills() or {}).get(skill_dir.name, {})
                    kind = info.get("kind")
                except Exception:
                    kind = None

            # 查找 script/agent.py（rag_skill / tool_skill 需要；llm_skill 走通用 runner）
            agent_script = skill_dir / "script" / "agent.py"
            if agent_script.exists():
                skill_name = skill_dir.name
                self._skill_map[skill_name] = agent_script
                count += 1
            elif kind == "llm_skill":
                # 声明式 llm_skill：由 build_llm_agent 执行，无需 agent.py
                self._skill_map[skill_dir.name] = None
                self._llm_skills.add(skill_dir.name)
                count += 1
                
        # self.console.print(f"[dim]已发现 {count} 个技能插件[/dim]")

    def _resolve_agent_name(self, agent_name: str) -> Optional[str]:
        """解析智能体名称到技能目录名"""
        # 1. 直接匹配技能名
        if agent_name in self._skill_map:
            return agent_name
            
        # 2. 尝试遗留映射
        if agent_name in self._legacy_mapping:
            skill_name = self._legacy_mapping[agent_name]
            if skill_name in self._skill_map:
                return skill_name
                
        return None

    def __getitem__(self, agent_name: str):
        """获取智能体 (懒加载)"""
        if agent_name in self.cache:
            return self.cache[agent_name]

        skill_name = self._resolve_agent_name(agent_name)
        if not skill_name:
             raise KeyError(f"Agent '{agent_name}' not found in skills directory")

        # 声明式 llm_skill → 通用 runner（无独立 agent.py）
        if skill_name in self._llm_skills:
            from utils.skill_runners import build_llm_agent
            from utils.skill_loader import SkillLoader
            instance = build_llm_agent(self.model, skill_name,
                                        skill_loader=SkillLoader(),
                                        memory_manager=self.memory_manager)
            instance.name = agent_name
            self.cache[agent_name] = instance
            self.console.print(f"[dim]✓ {agent_name} 加载完成 (llm_runner: {skill_name})[/dim]")
            return instance

        script_path = self._skill_map[skill_name]
        
        self.console.print(f"[dim]🔄 正在加载 {agent_name} (from {skill_name})...[/dim]")
        
        try:
            # 1. 动态加载模块
            module_name = f"skills.{skill_name}.agent"
            spec = importlib.util.spec_from_file_location(module_name, script_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load spec from {script_path}")
                
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            
            # 关键：确保模块能找到项目根目录的包 (utils, config 等)
            project_root = str(Path(__file__).parent.parent.absolute())
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
                
            spec.loader.exec_module(module)
            
            # 2. 查找 Agent 类
            agent_class = None
            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and issubclass(obj, AgentBase) and obj is not AgentBase:
                    agent_class = obj
                    break
            
            if not agent_class:
                raise ValueError(f"No AgentBase subclass found in {script_path}")
                
            # 3. 实例化
            # 构造参数：name, model, memory_manager (如果需要), **kwargs
            init_params = {
                "name": agent_name, # 使用请求的名字，或者可以用 skill_name
                "model": self.model,
            }
            
            # 检查是否需要 memory_manager
            sig = inspect.signature(agent_class.__init__)
            if "memory_manager" in sig.parameters:
                init_params["memory_manager"] = self.memory_manager
                
            agent_instance = agent_class(**init_params)
            
            # 4. 缓存
            self.cache[agent_name] = agent_instance
            self.console.print(f"[dim]✓ {agent_name} 加载完成[/dim]")
            
            return agent_instance
            
        except Exception as e:
            self.console.print(f"[red]✗ 加载 {agent_name} 失败: {e}[/red]")
            import traceback
            traceback.print_exc()
            raise

    def __contains__(self, agent_name: str) -> bool:
        return self._resolve_agent_name(agent_name) is not None or agent_name in self.cache

    def get(self, agent_name: str, default=None):
        try:
            return self[agent_name]
        except KeyError:
            return default

    def keys(self):
        # 返回所有可能的 key（包括 legacy mapping 的 key，为了兼容 orchestrator）
        keys = set(self._skill_map.keys())
        for legacy_key, skill_val in self._legacy_mapping.items():
            if skill_val in self._skill_map:
                keys.add(legacy_key)
        return list(keys)

    def values(self):
        return self.cache.values()

    def items(self):
        return self.cache.items()
