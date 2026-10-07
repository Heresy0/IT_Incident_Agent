"""
记忆系统基础类型和抽象类
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4


class MemoryType(Enum):
    """记忆类型枚举"""
    SHORT_TERM = "short_term"      # 短期记忆 - 当前对话上下文
    SEMANTIC = "semantic"          # 语义记忆 - 事实、知识、用户画像
    EPISODIC = "episodic"          # 情景记忆 - 历史任务、执行轨迹
    PROCEDURAL = "procedural"      # 程序记忆 - 系统提示、行为模式


@dataclass
class MemoryEntry:
    """
    记忆条目数据类
    
    Attributes:
        content: 记忆内容
        memory_type: 记忆类型
        user_id: 用户标识
        thread_id: 线程标识（短期记忆使用）
        namespace: 命名空间（长期记忆使用）
        metadata: 附加元数据
        embedding: 向量嵌入（用于语义检索）
        created_at: 创建时间
        updated_at: 更新时间
        expires_at: 过期时间（短期记忆使用）
        access_count: 访问次数
        id: 唯一标识
    """
    content: Union[str, Dict[str, Any]]
    memory_type: MemoryType
    user_id: Optional[str] = None
    thread_id: Optional[str] = None
    namespace: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    access_count: int = 0
    id: str = field(default_factory=lambda: str(uuid4()))
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type.value,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "namespace": self.namespace,
            "metadata": self.metadata,
            "embedding": self.embedding,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "access_count": self.access_count,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """从字典创建"""
        return cls(
            id=data.get("id", str(uuid4())),
            content=data["content"],
            memory_type=MemoryType(data["memory_type"]),
            user_id=data.get("user_id"),
            thread_id=data.get("thread_id"),
            namespace=data.get("namespace"),
            metadata=data.get("metadata", {}),
            embedding=data.get("embedding"),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if "updated_at" in data else datetime.now(),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            access_count=data.get("access_count", 0),
        )


class BaseMemory(ABC):
    """
    记忆存储抽象基类
    
    所有记忆存储后端（内存、PostgreSQL、Redis、Milvus）都需要实现此接口
    """
    
    def __init__(self, memory_type: MemoryType):
        self.memory_type = memory_type
    
    @abstractmethod
    def save(self, entry: MemoryEntry) -> str:
        """
        保存记忆条目
        
        Args:
            entry: 记忆条目
            
        Returns:
            记忆条目 ID
        """
        pass
    
    @abstractmethod
    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """
        获取指定 ID 的记忆
        
        Args:
            memory_id: 记忆条目 ID
            
        Returns:
            记忆条目，不存在则返回 None
        """
        pass
    
    @abstractmethod
    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        namespace: Optional[str] = None,
        limit: int = 5,
        **kwargs
    ) -> List[MemoryEntry]:
        """
        搜索记忆
        
        Args:
            query: 查询文本
            user_id: 用户标识过滤
            namespace: 命名空间过滤
            limit: 返回数量限制
            **kwargs: 额外参数
            
        Returns:
            记忆条目列表
        """
        pass
    
    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """
        删除指定记忆
        
        Args:
            memory_id: 记忆条目 ID
            
        Returns:
            是否删除成功
        """
        pass
    
    @abstractmethod
    def clear(
        self,
        user_id: Optional[str] = None,
        namespace: Optional[str] = None
    ) -> int:
        """
        清除记忆
        
        Args:
            user_id: 用户标识过滤
            namespace: 命名空间过滤
            
        Returns:
            清除的记忆数量
        """
        pass
    
    @abstractmethod
    def list_namespaces(self, user_id: Optional[str] = None) -> List[str]:
        """
        列出所有命名空间
        
        Args:
            user_id: 用户标识过滤
            
        Returns:
            命名空间列表
        """
        pass
