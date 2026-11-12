import json
from bdi_llm import LLMModule, LLMRequest, KnowledgeItem
from bdi_llm.config import max_attempts, provider_name
from bdi_llm.providers import create_provider
from bdi_llm.knowledge import JsonKnowledgeBase
from pathlib import Path


def main():
    # Load approved demonstration knowledge from the JSON file.
    knowledge = JsonKnowledgeBase(Path("knowledge_base/rules.json")).load()
    request = LLMRequest(
        task_id="TASK-001", agent_id="reservoir-modelling-agent",
        task_type="resolve_knowledge_gap",
        goal="Identify what information is needed before selecting a modelling step.",
        context={"reservoir_type": "not specified", "geometry_available": False},
        knowledge=knowledge,
        constraints=["Do not invent reservoir properties", "Use approved knowledge only"]
    )
    module = LLMModule(create_provider(provider_name()), max_attempts=max_attempts())
    result = module.process(request)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
