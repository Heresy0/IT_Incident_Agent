"""
长期记忆模块

提供语义记忆（Semantic）和情景记忆（Episodic）的持久化存储
支持 PostgreSQL 和内存两种后端
"""

import json
import logging
import sqlite3
from abc import ABC
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .base import BaseMemory, MemoryEntry, MemoryType

logger = logging.getLogger("mult_agents.memory")


class BaseLongTermMemory(BaseMemory, ABC):
    """长期记忆基类"""
    
    def __init__(self, memory_type: MemoryType):
        super().__init__(memory_type)
        self._embedding_dim: Optional[int] = None
    
    def _generate_embedding(self, text: str) -> List[float]:
        """
        生成文本的向量嵌入（简化版本）
        
        实际生产环境应该使用：
        - DashScope Embeddings
        - OpenAI Embeddings
        - 本地模型如 all-MiniLM-L6-v2
        
        Args:
            text: 输入文本
            
        Returns:
            向量嵌入
        """
        # 这里使用简单的哈希模拟，实际应该调用 Embedding 模型
        import hashlib
        
        hash_obj = hashlib.md5(text.encode())
        hash_bytes = hash_obj.digest()
        
        # 生成 128 维的伪向量
        embedding = []
        for i in range(0, len(hash_bytes), 2):
            val = (hash_bytes[i] + hash_bytes[i+1] if i+1 < len(hash_bytes) else hash_bytes[i]) / 255.0
            embedding.append(val)
        
        # 扩展到 384 维（与 all-MiniLM-L6-v2 一致）
        while len(embedding) < 384:
            embedding.extend(embedding[:384 - len(embedding)])
        
        return embedding[:384]
    
    def _calculate_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        if len(vec1) != len(vec2):
            return 0.0
        
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)


class SQLiteLongTermMemory(BaseLongTermMemory):
    """
    基于 SQLite 的长期记忆实现
    
    适合开发和测试环境，生产环境建议使用 PostgreSQL
    """
    
    def __init__(
        self,
        memory_type: MemoryType,
        db_path: Optional[str] = None,
    ):
        super().__init__(memory_type)
        
        if db_path is None:
            db_path = str(Path(__file__).resolve().parents[2] / "data" / "memory.db")
        
        self.db_path = db_path
        self._ensure_db_directory()
        self._init_tables()
    
    def _ensure_db_directory(self) -> None:
        """确保数据库目录存在"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_tables(self) -> None:
        """初始化数据库表"""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    user_id TEXT,
                    namespace TEXT,
                    metadata TEXT,
                    embedding TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    access_count INTEGER DEFAULT 0
                )
            """)
            
            # 创建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_user 
                ON memories(user_id, memory_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_namespace 
                ON memories(namespace)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_created 
                ON memories(created_at)
            """)
            
            conn.commit()
    
    def save(self, entry: MemoryEntry) -> str:
        """保存记忆条目"""
        # 如果没有 embedding，自动生成
        if entry.embedding is None and isinstance(entry.content, str):
            entry.embedding = self._generate_embedding(entry.content)
        
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO memories 
                (id, content, memory_type, user_id, namespace, metadata, embedding,
                 created_at, updated_at, access_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id,
                json.dumps(entry.content) if isinstance(entry.content, dict) else entry.content,
                entry.memory_type.value,
                entry.user_id,
                entry.namespace,
                json.dumps(entry.metadata),
                json.dumps(entry.embedding) if entry.embedding else None,
                entry.created_at.isoformat(),
                datetime.now().isoformat(),
                entry.access_count,
            ))
            conn.commit()
        
        logger.debug(f"长期记忆已保存: {entry.id}")
        return entry.id
    
    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """获取指定 ID 的记忆"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,)
            ).fetchone()
            
            if row is None:
                return None
            
            return self._row_to_entry(row)
    
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
        
        使用向量相似度 + 关键词匹配
        """
        query_embedding = self._generate_embedding(query)
        
        # 构建查询条件
        conditions = ["memory_type = ?"]
        params = [self.memory_type.value]
        
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        
        if namespace:
            conditions.append("namespace = ?")
            params.append(namespace)
        
        where_clause = " AND ".join(conditions)
        
        with self._get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {where_clause}",
                params
            ).fetchall()
        
        # 计算相似度并排序
        entries_with_scores = []
        for row in rows:
            entry = self._row_to_entry(row)
            
            # 向量相似度
            vec_score = 0.0
            if entry.embedding and query_embedding:
                vec_score = self._calculate_similarity(entry.embedding, query_embedding)
            
            # 关键词匹配分数
            text_score = 0.0
            content_str = str(entry.content).lower()
            query_lower = query.lower()
            
            if query_lower in content_str:
                text_score = 0.5
            
            # 综合分数
            total_score = vec_score * 0.7 + text_score * 0.3
            
            entries_with_scores.append((entry, total_score))
        
        # 按分数排序并返回
        entries_with_scores.sort(key=lambda x: x[1], reverse=True)
        return [entry for entry, _ in entries_with_scores[:limit]]
    
    def delete(self, memory_id: str) -> bool:
        """删除指定记忆"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE id = ?",
                (memory_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def clear(
        self,
        user_id: Optional[str] = None,
        namespace: Optional[str] = None
    ) -> int:
        """清除记忆"""
        conditions = ["memory_type = ?"]
        params = [self.memory_type.value]
        
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        
        if namespace:
            conditions.append("namespace = ?")
            params.append(namespace)
        
        where_clause = " AND ".join(conditions)
        
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"DELETE FROM memories WHERE {where_clause}",
                params
            )
            conn.commit()
            return cursor.rowcount
    
    def list_namespaces(self, user_id: Optional[str] = None) -> List[str]:
        """列出所有命名空间"""
        query = "SELECT DISTINCT namespace FROM memories WHERE memory_type = ?"
        params = [self.memory_type.value]
        
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [row["namespace"] for row in rows if row["namespace"]]
    
    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        """将数据库行转换为 MemoryEntry"""
        content = row["content"]
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            pass
        
        embedding = None
        if row["embedding"]:
            try:
                embedding = json.loads(row["embedding"])
            except json.JSONDecodeError:
                pass
        
        metadata = {}
        if row["metadata"]:
            try:
                metadata = json.loads(row["metadata"])
            except json.JSONDecodeError:
                pass
        
        return MemoryEntry(
            id=row["id"],
            content=content,
            memory_type=MemoryType(row["memory_type"]),
            user_id=row["user_id"],
            namespace=row["namespace"],
            metadata=metadata,
            embedding=embedding,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            access_count=row["access_count"],
        )


class SemanticMemoryStore(SQLiteLongTermMemory):
    """
    语义记忆存储
    
    存储事实知识、用户画像、偏好设置等
    使用 namespace 区分不同类型的语义记忆：
    - "user_profile": 用户画像
    - "preferences": 用户偏好
    - "facts": 事实知识
    """
    
    def __init__(self, db_path: Optional[str] = None):
        super().__init__(MemoryType.SEMANTIC, db_path)
    
    def save_profile(
        self,
        user_id: str,
        profile_data: Dict[str, Any],
        merge: bool = True
    ) -> str:
        """
        保存用户画像
        
        Args:
            user_id: 用户标识
            profile_data: 画像数据
            merge: 是否合并现有画像
            
        Returns:
            记忆条目 ID
        """
        if merge:
            # 获取现有画像
            existing = self.get_profile(user_id)
            if existing:
                existing.update(profile_data)
                profile_data = existing
        
        entry = MemoryEntry(
            content=profile_data,
            memory_type=MemoryType.SEMANTIC,
            user_id=user_id,
            namespace="user_profile",
            metadata={"type": "profile", "version": "1.0"},
        )
        
        return self.save(entry)
    
    def get_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """获取用户画像"""
        results = self.search(
            query="user_profile",
            user_id=user_id,
            namespace="user_profile",
            limit=1
        )
        
        if results:
            content = results[0].content
            if isinstance(content, dict):
                return content
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return None
        return None
    
    def save_fact(
        self,
        user_id: str,
        fact: str,
        category: Optional[str] = None
    ) -> str:
        """
        保存事实知识
        
        Args:
            user_id: 用户标识
            fact: 事实内容
            category: 事实类别
            
        Returns:
            记忆条目 ID
        """
        entry = MemoryEntry(
            content=fact,
            memory_type=MemoryType.SEMANTIC,
            user_id=user_id,
            namespace=f"facts/{category}" if category else "facts",
            metadata={"category": category},
        )
        
        return self.save(entry)


class EpisodicMemoryStore(SQLiteLongTermMemory):
    """
    情景记忆存储
    
    存储历史任务、执行轨迹、交互记录
    使用 namespace 区分不同类型的情景记忆：
    - "tasks/{task_type}": 任务执行记录
    - "conversations": 对话历史
    - "actions": 操作记录
    """
    
    def __init__(self, db_path: Optional[str] = None):
        super().__init__(MemoryType.EPISODIC, db_path)
    
    def save_task_record(
        self,
        user_id: str,
        task_type: str,
        task_data: Dict[str, Any],
        outcome: Optional[str] = None
    ) -> str:
        """
        保存任务执行记录
        
        Args:
            user_id: 用户标识
            task_type: 任务类型
            task_data: 任务数据
            outcome: 任务结果
            
        Returns:
            记忆条目 ID
        """
        content = {
            "task_type": task_type,
            "data": task_data,
            "outcome": outcome,
            "timestamp": datetime.now().isoformat(),
        }
        
        entry = MemoryEntry(
            content=content,
            memory_type=MemoryType.EPISODIC,
            user_id=user_id,
            namespace=f"tasks/{task_type}",
            metadata={"task_type": task_type, "has_outcome": outcome is not None},
        )
        
        return self.save(entry)
    
    def get_similar_tasks(
        self,
        user_id: str,
        task_description: str,
        limit: int = 5
    ) -> List[MemoryEntry]:
        """
        获取相似的历史任务
        
        Args:
            user_id: 用户标识
            task_description: 任务描述
            limit: 返回数量
            
        Returns:
            相似任务列表
        """
        return self.search(
            query=task_description,
            user_id=user_id,
            limit=limit
        )
    
    def get_task_history(
        self,
        user_id: str,
        task_type: Optional[str] = None,
        limit: int = 10
    ) -> List[MemoryEntry]:
        """
        获取任务历史
        
        Args:
            user_id: 用户标识
            task_type: 任务类型过滤
            limit: 返回数量
            
        Returns:
            任务记录列表
        """
        namespace = f"tasks/{task_type}" if task_type else None
        
        # 获取所有匹配的任务
        with self._get_connection() as conn:
            query = """
                SELECT * FROM memories 
                WHERE memory_type = ? AND user_id = ?
            """
            params = [self.memory_type.value, user_id]
            
            if namespace:
                query += " AND namespace = ?"
                params.append(namespace)
            
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            
            rows = conn.execute(query, params).fetchall()
        
        return [self._row_to_entry(row) for row in rows]


# 别名，便于使用
LongTermMemory = SQLiteLongTermMemory
