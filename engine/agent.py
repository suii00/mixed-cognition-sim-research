from typing import List, Dict, Tuple, Optional
from typing import Any
import copy

from engine.message_selection import (
    RECENT_MESSAGE_SELECTION_POLICY,
    RETAIN_OFFICIAL_WARNING_SELECTION_POLICY,
    validate_message_selection_policy,
    validate_retention_limits,
)


class Agent:
    def __init__(self, agent_id: int, bloc: str, model: str,
                 base_url: str, position: Tuple[int, int],
                 memory_limit: int, memory_size: int,
                 message_history_limit: int, message_context_size: int,
                 llm_overrides: Optional[Dict] = None,
                 provider: str = "ollama",
                 endpoint_id: Optional[str] = None,
                 device_slot: Optional[str] = None,
                 message_selection_policy: str = RECENT_MESSAGE_SELECTION_POLICY):
        self.agent_id = agent_id
        self.bloc = bloc
        self.model = model
        self.base_url = base_url
        self.provider = provider
        self.endpoint_id = endpoint_id
        self.device_slot = device_slot
        self.position = position
        self.memory_limit = memory_limit
        self.memory_size = memory_size
        self.message_history_limit = message_history_limit
        self.message_context_size = message_context_size
        self.message_selection_policy = validate_message_selection_policy(
            message_selection_policy
        )
        validate_retention_limits(
            self.message_selection_policy, message_history_limit, message_context_size
        )
        self.llm_overrides = llm_overrides or {}
        self.memories: List[str] = []
        self.received_messages: List[Dict] = []
        self._retained_official_warning: Optional[Dict] = None

    def add_memory(self, memory_text: str) -> None:
        self.memories.append(memory_text)
        if len(self.memories) > self.memory_limit:
            self.memories = self.memories[-self.memory_limit:]

    def get_recent_memories(self) -> List[str]:
        return self.memories[-self.memory_size:]

    def add_received_message(self, sender_id: int, message: str, step: int) -> None:
        self.received_messages.append({
            "sender_id": sender_id,
            "message": message,
            "step": step,
        })
        if len(self.received_messages) > self.message_history_limit:
            self.received_messages = self.received_messages[-self.message_history_limit:]

    def add_official_warning(
        self,
        warning_id: str,
        payload: str | Dict[str, Any],
        step: int,
    ) -> None:
        warning = {
            "source_type": "official_warning",
            "warning_id": warning_id,
            "payload": payload,
            "step": step,
        }
        self.received_messages.append(copy.deepcopy(warning))
        if self.message_selection_policy == RETAIN_OFFICIAL_WARNING_SELECTION_POLICY:
            # Only this trusted delivery API can populate the retained slot.
            # Retaining a copy neither creates nor repeats a receipt event.
            self._retained_official_warning = copy.deepcopy(warning)
        if len(self.received_messages) > self.message_history_limit:
            self.received_messages = self.received_messages[-self.message_history_limit:]

    def get_recent_messages(self) -> List[Dict]:
        if (
            self.message_selection_policy == RETAIN_OFFICIAL_WARNING_SELECTION_POLICY
            and self._retained_official_warning is not None
        ):
            peer_slots = self.message_context_size - 1
            peers = [
                message for message in self.received_messages
                if "sender_id" in message
            ]
            return [copy.deepcopy(self._retained_official_warning)] + (
                peers[-peer_slots:] if peer_slots else []
            )
        return self.received_messages[-self.message_context_size:]
