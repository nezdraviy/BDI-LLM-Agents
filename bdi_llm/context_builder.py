from .models import LLMRequest, KnowledgeItem


class ContextBuilder:
    def __init__(self, max_knowledge_items: int = 8):
        self.max_knowledge_items = max_knowledge_items

    @staticmethod
    def _score(item: KnowledgeItem, request: LLMRequest) -> int:
        words = set((request.goal + " " + request.task_type + " " +
                     " ".join(request.constraints)).lower().split())
        text = " ".join([item.title, item.content, item.category, *item.tags]).lower()
        score = sum(1 for word in words if len(word) > 3 and word in text)
        if request.task_type in item.applicable_task_types:
            score += 5
        return score

    def build(self, request: LLMRequest) -> dict:
        approved = [x for x in request.knowledge if x.status == "approved"]
        relevant = sorted(approved, key=lambda x: self._score(x, request), reverse=True)
        return {
            "task": {"task_id": request.task_id, "agent_id": request.agent_id,
                     "task_type": request.task_type, "goal": request.goal},
            "current_state": request.context,
            "constraints": request.constraints,
            "approved_knowledge": [x.model_dump(mode="json") for x in relevant[:self.max_knowledge_items]],
            "previous_attempts": [x.model_dump(mode="json") for x in request.previous_attempts],
            "instruction": "Suggest a cautious, checkable next step. Do not invent engineering facts. "
                           "If required information is missing, return insufficient_data."
        }
