"""
记忆系统工具函数
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from .base import MemoryEntry, MemoryType

logger = logging.getLogger("mult_agents.memory")


def create_memory_checkpoint(
    thread_id: str,
    state: Dict[str, Any],
    checkpoint_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    创建记忆检查点
    
    将当前 State 保存为检查点，便于恢复
    
    Args:
        thread_id: 线程标识
        state: 当前状态
        checkpoint_id: 检查点 ID（自动生成）
        
    Returns:
        检查点数据
    """
    from datetime import datetime
    from uuid import uuid4
    
    checkpoint = {
        "id": checkpoint_id or str(uuid4()),
        "thread_id": thread_id,
        "state": {
            "query": state.get("query"),
            "intent": state.get("intent"),
            "plan": state.get("plan"),
            "analysis": state.get("analysis"),
            "final": state.get("final"),
        },
        "created_at": datetime.now().isoformat(),
    }
    
    return checkpoint


def extract_memory_from_messages(
    messages: List[BaseMessage],
    extract_facts: bool = True,
    extract_preferences: bool = True
) -> Dict[str, List[str]]:
    """
    从消息中提取可记忆的信息
    
    简单的规则提取，实际生产环境可以使用 LLM 进行更智能的提取
    
    Args:
        messages: 消息列表
        extract_facts: 是否提取事实
        extract_preferences: 是否提取偏好
        
    Returns:
        提取的记忆，按类型分类
    """
    memories = {
        "facts": [],
        "preferences": [],
        "tasks": [],
    }
    
    for msg in messages:
        content = str(msg.content)
        sentences = [item.strip() for item in re.split(r"[。！？!?\n；;]", content) if item.strip()]
        if not sentences:
            sentences = [content.strip()]
        
        # 提取事实（包含"是"、"有"等判断的句子）
        if extract_facts:
            # 简单的规则：包含特定关键词的句子
            fact_keywords = ["是", "有", "位于", "成立于", "负责", "使用", "叫", "my name is", "i am", "i'm"]
            for keyword in fact_keywords:
                if keyword in content.lower():
                    for sent in sentences:
                        if keyword in sent.lower() and len(sent) > 3:
                            memories["facts"].append(sent.strip())
                    break
        
        # 提取偏好（包含"喜欢"、"偏好"、"想要"等）
        if extract_preferences:
            pref_keywords = ["喜欢", "偏好", "想要", "希望", "不喜欢", "讨厌", "prefer", "i like", "style"]
            for keyword in pref_keywords:
                if keyword in content.lower():
                    for sent in sentences:
                        if keyword in sent.lower() and len(sent) > 3:
                            memories["preferences"].append(sent.strip())
                    break
    
    # 去重
    memories["facts"] = list(set(memories["facts"]))
    memories["preferences"] = list(set(memories["preferences"]))
    
    return memories


def format_memories_for_prompt(memories: List[MemoryEntry], max_length: int = 2000) -> str:
    """
    将记忆格式化为 Prompt 可用的文本
    
    Args:
        memories: 记忆条目列表
        max_length: 最大长度限制
        
    Returns:
        格式化后的文本
    """
    if not memories:
        return ""
    
    sections = []
    
    # 按类型分组
    semantic_memories = [m for m in memories if m.memory_type == MemoryType.SEMANTIC]
    episodic_memories = [m for m in memories if m.memory_type == MemoryType.EPISODIC]
    
    # 格式化语义记忆（事实、画像）
    if semantic_memories:
        sections.append("## 相关背景知识")
        for i, mem in enumerate(semantic_memories[:5], 1):
            content = mem.content if isinstance(mem.content, str) else json.dumps(mem.content, ensure_ascii=False)
            sections.append(f"{i}. {content}")
    
    # 格式化情景记忆（历史任务）
    if episodic_memories:
        sections.append("\n## 相关历史任务")
        for i, mem in enumerate(episodic_memories[:3], 1):
            if isinstance(mem.content, dict):
                task_type = mem.content.get("task_type", "未知任务")
                outcome = mem.content.get("outcome", "")
                sections.append(f"{i}. [{task_type}] {outcome or str(mem.content)[:100]}")
            else:
                sections.append(f"{i}. {str(mem.content)[:100]}")
    
    result = "\n".join(sections)
    
    # 截断到最大长度
    if len(result) > max_length:
        result = result[:max_length] + "\n...（更多记忆已省略）"
    
    return result


def merge_user_profile(existing: Optional[Dict], new_data: Dict) -> Dict:
    """
    合并用户画像数据
    
    智能合并新旧画像，保留历史信息同时更新新信息
    
    Args:
        existing: 现有画像
        new_data: 新画像数据
        
    Returns:
        合并后的画像
    """
    if existing is None:
        return new_data.copy()
    
    merged = existing.copy()
    
    for key, value in new_data.items():
        # 如果值是字典，递归合并
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        # 如果是列表，追加新元素
        elif key in merged and isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = list(set(merged[key] + value))  # 去重合并
        # 否则直接覆盖
        else:
            merged[key] = value
    
    # 添加更新时间
    from datetime import datetime
    merged["_last_updated"] = datetime.now().isoformat()
    
    return merged


def calculate_memory_relevance(
    query: str,
    memory: MemoryEntry,
    time_decay_hours: float = 168  # 7 天
) -> float:
    """
    计算记忆与查询的相关性分数
    
    综合考虑：
    - 文本相似度
    - 时间衰减（越新的记忆分数越高）
    - 访问频率
    
    Args:
        query: 查询文本
        memory: 记忆条目
        time_decay_hours: 时间衰减半衰期（小时）
        
    Returns:
        相关性分数 (0-1)
    """
    from datetime import datetime
    import math
    
    # 1. 基础文本相似度（简化版本）
    query_lower = query.lower()
    content_str = str(memory.content).lower()
    
    text_score = 0.0
    if query_lower in content_str:
        text_score = 0.5
    
    # 计算词重叠
    query_words = set(query_lower.split())
    content_words = set(content_str.split())
    if query_words:
        overlap = len(query_words & content_words) / len(query_words)
        text_score = max(text_score, overlap * 0.8)
    
    # 2. 时间衰减
    age_hours = (datetime.now() - memory.created_at).total_seconds() / 3600
    time_factor = math.exp(-age_hours / time_decay_hours)
    
    # 3. 访问频率加成
    access_bonus = min(memory.access_count * 0.05, 0.2)
    
    # 综合分数
    final_score = (text_score * 0.6 + time_factor * 0.3 + access_bonus * 0.1)
    
    return min(final_score, 1.0)


def compress_memories(
    memories: List[MemoryEntry],
    target_count: int = 10
) -> List[MemoryEntry]:
    """
    压缩记忆列表，保留最相关的条目
    
    策略：
    1. 去重（相似度过高的合并）
    2. 按重要性和时间排序
    3. 保留指定数量
    
    Args:
        memories: 原始记忆列表
        target_count: 目标数量
        
    Returns:
        压缩后的记忆列表
    """
    if len(memories) <= target_count:
        return memories
    
    # 去重：计算记忆间的相似度，合并相似的
    unique_memories = []
    for mem in memories:
        is_duplicate = False
        for existing in unique_memories:
            # 简化相似度计算
            sim = _simple_similarity(str(mem.content), str(existing.content))
            if sim > 0.8:  # 相似度阈值
                is_duplicate = True
                break
        
        if not is_duplicate:
            unique_memories.append(mem)
    
    # 按访问次数和时间排序
    def score(mem: MemoryEntry) -> float:
        from datetime import datetime
        age_days = (datetime.now() - mem.created_at).days
        return mem.access_count * 10 - age_days
    
    unique_memories.sort(key=score, reverse=True)
    
    return unique_memories[:target_count]


def _simple_similarity(text1: str, text2: str) -> float:
    """计算两段文本的简单相似度"""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    
    if not words1 or not words2:
        return 0.0
    
    intersection = len(words1 & words2)
    union = len(words1 | words2)
    
    return intersection / union if union > 0 else 0.0
