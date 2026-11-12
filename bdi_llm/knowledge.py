import json
from pathlib import Path
from .models import KnowledgeItem


class JsonKnowledgeBase:
    """Small JSON knowledge store. New records are stored as proposed, not approved."""
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[KnowledgeItem]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [KnowledgeItem.model_validate(item) for item in data]

    def save_proposal(self, item: KnowledgeItem) -> None:
        records = self.load()
        if any(old.id == item.id for old in records):
            raise ValueError(f"Knowledge item already exists: {item.id}")
        item.status = "proposed"
        records.append(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([x.model_dump(mode="json") for x in records],
                                        ensure_ascii=False, indent=2), encoding="utf-8")
