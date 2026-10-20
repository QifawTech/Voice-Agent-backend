"""
Session Manager Service

Handles conversation state, history, and cart management in Redis.
"""

import json
import uuid
from datetime import datetime, timezone
from redis.asyncio import Redis
from app.logger import get_logger

logger = get_logger(__name__)

SESSION_PREFIX = "session:"
SESSION_TTL = 1800  # 30 minutes in seconds

class SessionManager:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def create_session(self, tenant_id: str) -> str:
        session_id = str(uuid.uuid4())
        key = f"{SESSION_PREFIX}{session_id}"
        
        now = datetime.now(timezone.utc).isoformat()
        
        session_data = {
            "session_id": session_id,
            "tenant_id": str(tenant_id),
            "status": "active",
            "created_at": now,
            "last_active": now,
            "history": json.dumps([]),
            "cart": json.dumps({"items": [], "total": 0.0})
        }
        
        await self.redis.hset(key, mapping=session_data)
        await self.redis.expire(key, SESSION_TTL)
        
        logger.info("session_created", session_id=session_id, tenant_id=tenant_id)
        return session_id

    async def get_session(self, session_id: str) -> dict | None:
        key = f"{SESSION_PREFIX}{session_id}"
        data = await self.redis.hgetall(key)
        
        if not data:
            return None
            
        # Deserialize JSON fields
        if "history" in data:
            data["history"] = json.loads(data["history"])
        if "cart" in data:
            data["cart"] = json.loads(data["cart"])
            
        return data

    async def update_session(self, session_id: str, updates: dict) -> bool:
        key = f"{SESSION_PREFIX}{session_id}"
        
        if not await self.redis.exists(key):
            return False
            
        # Serialize JSON fields if present
        redis_updates = {}
        for k, v in updates.items():
            if k in ["history", "cart"] and not isinstance(v, str):
                redis_updates[k] = json.dumps(v)
            else:
                redis_updates[k] = v
                
        redis_updates["last_active"] = datetime.now(timezone.utc).isoformat()
        
        await self.redis.hset(key, mapping=redis_updates)
        await self.redis.expire(key, SESSION_TTL)
        
        return True

    async def add_message(self, session_id: str, role: str, content: str) -> bool:
        session = await self.get_session(session_id)
        if not session:
            return False
            
        history = session.get("history", [])
        history.append({"role": role, "content": content})
        
        return await self.update_session(session_id, {"history": history})

    async def end_session(self, session_id: str, reason: str = "completed") -> bool:
        # In a real app, we would log this to Postgres analytics before deleting
        key = f"{SESSION_PREFIX}{session_id}"
        
        session = await self.get_session(session_id)
        if not session:
            return False
            
        logger.info("session_ended", session_id=session_id, reason=reason)
        
        await self.update_session(session_id, {"status": reason})
        # We could also delete it immediately, but keeping it with status=reason allows 
        # a background worker to sync to Postgres later. Let's just expire it naturally.
        return True
