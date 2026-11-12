import json
import os
from .prompts import SYSTEM_PROMPT, build_user_prompt


class MockProvider:
    """Deterministic provider for local development; no API key required."""
    def generate(self, context: dict, validation_errors=None) -> str:
        knowledge = context.get("approved_knowledge", [])
        output = {
            "status": "insufficient_data",
            "problem": "There is not enough information for a justified modelling decision.",
            "proposal": None,
            "reasoning_summary": "Request the missing reservoir description and boundary conditions before selecting the next modelling step.",
            "assumptions": [],
            "knowledge_references": [knowledge[0]["id"]] if knowledge else [],
            "validation_requirements": ["Check that the missing input data has been provided."],
            "confidence": 0.9
        }
        if validation_errors:
            output["reasoning_summary"] += " Validation feedback was received: " + "; ".join(validation_errors)
        return json.dumps(output, ensure_ascii=False)


class OpenAIProvider:
    """Optional OpenAI-compatible provider using the OpenAI Python SDK."""
    def __init__(self, model=None):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install the OpenAI SDK: pip install 'openai>=1.30'") from exc
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.client = OpenAI(api_key=api_key)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def generate(self, context: dict, validation_errors=None) -> str:
        response = self.client.chat.completions.create(
            model=self.model, temperature=0.2, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": build_user_prompt(context, validation_errors)}]
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("The LLM returned an empty response")
        return content


def create_provider(name="mock"):
    if name.lower() == "mock":
        return MockProvider()
    if name.lower() == "openai":
        return OpenAIProvider()
    raise ValueError(f"Unsupported provider: {name}")
