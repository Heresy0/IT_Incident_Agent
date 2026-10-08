"""
短期记忆模块

基于 LangGraph Checkpoint 实现，管理当前对话线程的上下文
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from .base import BaseMemory, MemoryEntry, MemoryType

logger = logging.getLogger("mult_agents.memory")


class ConversationBuffer:
    """
    对话缓冲区
    
    管理单轮对话的消息历史，支持窗口裁剪和摘要生成
    """
    
    def __init__(
        self,
        max_messages: int = 20,
        max_tokens: int = 4000,
        summary_threshold: int = 10,
    ):
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self.summary_threshold = summary_threshold
        self.messages: List[BaseMessage] = []
        self.summary: Optional[str] = None
        self.token_count: int = 0
    
    def add_message(self, message: BaseMessage) -> None:
        """添加消息到缓冲区"""
        self.messages.append(message)
        self._update_token_count()
        
        # 如果超过阈值，触发裁剪或摘要
        if len(self.messages) > self.max_messages:
            self._compress_messages()
    
    def add_messages(self, messages: List[BaseMessage]) -> None:
        """批量添加消息"""
        for msg in messages:
            self.add_message(msg)
    
    def get_messages(
        self,
        include_summary: bool = True,
        last_n: Optional[int] = None
    ) -> List[BaseMessage]:
        """
        获取消息列表
        
        Args:
            include_summary: 是否包含历史摘要
            last_n: 只返回最近 N 条消息
            
        Returns:
            消息列表
        """
        result = []
        
        # 添加摘要作为系统消息
        if include_summary and self.summary:
            result.append(SystemMessage(content=f"历史对话摘要：{self.summary}"))
        
        # 添加实际消息
        messages_to_return = self.messages
        if last_n:
            messages_to_return = self.messages[-last_n:]
        
        result.extend(messages_to_return)
        return result
    
    def clear(self) -> None:
        """清空缓冲区"""
        self.messages = []
        self.summary = None
        self.token_count = 0
    
    def _update_token_count(self) -> None:
        """更新 token 计数（简化估算）"""
        # 简单估算：每个字符约 0.5 个 token
        total_chars = sum(len(str(msg.content)) for msg in self.messages)
        self.token_count = total_chars // 2
    
    def _compress_messages(self) -> None:
        """
        压缩消息历史
        
        策略：保留最近的消息，将旧消息生成摘要
        """
        if len(self.messages) <= self.summary_threshold:
            return
        
        # 保留最近的消息
        messages_to_summarize = self.messages[:-self.summary_threshold]
        self.messages = self.messages[-self.summary_threshold:]
        
        # 生成摘要（简化版本，实际可以调用 LLM）
        summary_parts = []
        for msg in messages_to_summarize:
            role = "用户" if isinstance(msg, HumanMessage) else "AI"
            content_preview = str(msg.content)[:100]
            summary_parts.append(f"{role}: {content_preview}...")
        
        new_summary = "\n".join(summary_parts)
        
        if self.summary:
            self.summary = f"{self.summary}\n\n[更早的对话]\n{new_summary}"
        else:
            self.summary = new_summary
        
        logger.debug(f"消息历史已压缩，当前消息数: {len(self.messages)}")


class ShortTermMemory(BaseMemory):
    """
    短期记忆实现
    
    基于内存存储，管理当前线程的对话上下文和临时状态
    与 LangGraph 的 State 和 Checkpoint 集成
    """
    
    def __init__(
        self,
        ttl_seconds: int = 3600,  # 默认 1 小时过期
        max_threads: int = 100,
    ):
        super().__init__(MemoryType.SHORT_TERM)
        self.ttl_seconds = ttl_seconds
        self.max_threads = max_threads
        
        # 存储结构: {thread_id: {"buffer": ConversationBuffer, "metadata": {}, "last_access": datetime}}
        self._storage: Dict[str, Dict[str, Any]] = {}
        self._checkpointer: Optional[BaseCheckpointSaver] = None
    
    def set_checkpointer(self, checkpointer: BaseCheckpointSaver) -> None:
        """设置 LangGraph Checkpoint 存储"""
        self._checkpointer = checkpointer
    
    def get_or_create_buffer(self, thread_id: str) -> ConversationBuffer:
        """获取或创建对话缓冲区"""
        self._cleanup_expired()
        
        if thread_id not in self._storage:
            self._storage[thread_id] = {
                "buffer": ConversationBuffer(),
                "metadata": {},
                "created_at": datetime.now(),
                "last_access": datetime.now(),
            }
            logger.debug(f"为新线程 {thread_id} 创建短期记忆缓冲区")
        else:
            self._storage[thread_id]["last_access"] = datetime.now()
        
        return self._storage[thread_id]["buffer"]
    
    def add_message(
        self,
        thread_id: str,
        message: BaseMessage,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        添加消息到短期记忆
        
        Args:
            thread_id: 线程标识
            message: LangChain 消息对象
            metadata: 附加元数据
        """
        buffer = self.get_or_create_buffer(thread_id)
        buffer.add_message(message)
        
        if metadata:
            self._storage[thread_id]["metadata"].update(metadata)
        
        logger.debug(f"消息已添加到线程 {thread_id} 的短期记忆")
    
    def get_messages(
        self,
        thread_id: str,
        include_summary: bool = True,
        last_n: Optional[int] = None
    ) -> List[BaseMessage]:
        """
        获取指定线程的消息历史
        
        Args:
            thread_id: 线程标识
            include_summary: 是否包含历史摘要
            last_n: 只返回最近 N 条
            
        Returns:
            消息列表
        """
        if thread_id not in self._storage:
            return []
        
        self._storage[thread_id]["last_access"] = datetime.now()
        buffer = self._storage[thread_id]["buffer"]
        return buffer.get_messages(include_summary=include_summary, last_n=last_n)
    
    def get_thread_metadata(self, thread_id: str) -> Dict[str, Any]:
        """获取线程元数据"""
        if thread_id not in self._storage:
            return {}
        return self._storage[thread_id]["metadata"].copy()
    
    def update_thread_metadata(
        self,
        thread_id: str,
        metadata: Dict[str, Any]
    ) -> None:
        """更新线程元数据"""
        buffer = self.get_or_create_buffer(thread_id)
        self._storage[thread_id]["metadata"].update(metadata)
    
    def clear_thread(self, thread_id: str) -> bool:
        """清空指定线程的记忆"""
        if thread_id in self._storage:
            del self._storage[thread_id]
            logger.debug(f"线程 {thread_id} 的短期记忆已清空")
            return True
        return False
    
    def list_active_threads(self) -> List[str]:
        """列出所有活跃线程"""
        self._cleanup_expired()
        return list(self._storage.keys())
    
    # 实现 BaseMemory 抽象方法
    
    def save(self, entry: MemoryEntry) -> str:
        """保存记忆条目（短期记忆使用专用方法）"""
        thread_id = entry.thread_id or "default"
        buffer = self.get_or_create_buffer(thread_id)
        
        # 将内容转换为消息
        if isinstance(entry.content, str):
            message = HumanMessage(content=entry.content)
        elif isinstance(entry.content, dict):
            content = entry.content.get("content", "")
            role = entry.content.get("role", "human")
            if role == "ai":
                message = AIMessage(content=content)
            else:
                message = HumanMessage(content=content)
        else:
            message = HumanMessage(content=str(entry.content))
        
        buffer.add_message(message)
        
        # 更新元数据
        if entry.metadata:
            self._storage[thread_id]["metadata"].update(entry.metadata)
        
        return entry.id
    
    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """获取指定 ID 的记忆（短期记忆不支持按 ID 获取）"""
        # 短期记忆不支持按 ID 获取，返回 None
        return None
    
    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        namespace: Optional[str] = None,
        limit: int = 5,
        **kwargs
    ) -> List[MemoryEntry]:
        """
        搜索短期记忆
        
        简单实现：返回最近的消息作为记忆条目
        """
        thread_id = namespace or "default"
        messages = self.get_messages(thread_id, include_summary=False)
        
        entries = []
        for msg in messages[-limit:]:
            entry = MemoryEntry(
                content=msg.content,
                memory_type=MemoryType.SHORT_TERM,
                thread_id=thread_id,
                user_id=user_id,
                metadata={"role": "ai" if isinstance(msg, AIMessage) else "human"},
            )
            entries.append(entry)
        
        return entries
    
    def delete(self, memory_id: str) -> bool:
        """删除指定记忆（短期记忆不支持按 ID 删除）"""
        return False
    
    def clear(
        self,
        user_id: Optional[str] = None,
        namespace: Optional[str] = None
    ) -> int:
        """清除短期记忆"""
        if namespace:
            # 清除指定线程
            if self.clear_thread(namespace):
                return 1
            return 0
        
        # 清除所有
        count = len(self._storage)
        self._storage.clear()
        logger.info(f"已清除所有短期记忆，共 {count} 个线程")
        return count
    
    def list_namespaces(self, user_id: Optional[str] = None) -> List[str]:
        """列出所有线程 ID 作为命名空间"""
        return self.list_active_threads()
    
    def _cleanup_expired(self) -> None:
        """清理过期的线程记忆"""
        now = datetime.now()
        expired_threads = []
        
        for thread_id, data in self._storage.items():
            last_access = data.get("last_access", data.get("created_at", now))
            if now - last_access > timedelta(seconds=self.ttl_seconds):
                expired_threads.append(thread_id)
        
        # 如果超过最大线程数，清理最旧的
        if len(self._storage) > self.max_threads:
            sorted_threads = sorted(
                self._storage.items(),
                key=lambda x: x[1].get("last_access", x[1].get("created_at", now))
            )
            threads_to_remove = len(self._storage) - self.max_threads
            expired_threads.extend([t[0] for t in sorted_threads[:threads_to_remove]])
        
        for thread_id in set(expired_threads):
            del self._storage[thread_id]
        
        if expired_threads:
            logger.debug(f"已清理 {len(set(expired_threads))} 个过期线程")
