import json
from bdi_llm.models import LLMRequest, KnowledgeItem
from bdi_llm.pipeline import LLMModule
from bdi_llm.providers import MockProvider
from bdi_llm.validator import ProposalValidator
from bdi_llm.models import Proposal


def make_request():
    return LLMRequest(task_id="T-1", agent_id="A-1", task_type="resolve_knowledge_gap",
                      goal="Find a safe next step", context={"input_available": False},
                      knowledge=[KnowledgeItem(id="K-1", title="Missing data", content="Ask for missing input", status="approved")])


def test_mock_pipeline_returns_structured_result():
    result = LLMModule(MockProvider()).process(make_request())
    assert result.status == "accepted"
    assert result.proposal.status == "insufficient_data"
    assert result.validation.valid


def test_validator_rejects_unknown_reference():
    request = make_request()
    proposal = Proposal(status="insufficient_data", problem="Need data", proposal=None,
                        reasoning_summary="Context is incomplete", knowledge_references=["UNKNOWN"],
                        validation_requirements=["Provide missing input"])
    report = ProposalValidator().validate(proposal, request)
    assert not report.valid
    assert "UNKNOWN" in " ".join(report.errors)


class InvalidThenValidProvider:
    def __init__(self): self.calls = 0
    def generate(self, context, validation_errors=None):
        self.calls += 1
        if self.calls == 1:
            return json.dumps({"status": "insufficient_data", "problem": "Need data", "proposal": None,
                               "reasoning_summary": "Need input", "assumptions": [],
                               "knowledge_references": ["NOT-IN-KB"],
                               "validation_requirements": ["Provide input"], "confidence": 0.7})
        return MockProvider().generate(context, validation_errors)


def test_pipeline_retries_after_validation_error():
    result = LLMModule(InvalidThenValidProvider(), max_attempts=2).process(make_request())
    assert result.validation.valid
    assert result.attempts_used == 2
