import json
from pydantic import ValidationError
from .context_builder import ContextBuilder
from .models import LLMRequest, LLMResult, Proposal, ValidationReport, PreviousAttempt
from .validator import ProposalValidator


class LLMModule:
    def __init__(self, provider, context_builder=None, validator=None, max_attempts=3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.provider = provider
        self.context_builder = context_builder or ContextBuilder()
        self.validator = validator or ProposalValidator()
        self.max_attempts = max_attempts

    def process(self, request: LLMRequest) -> LLMResult:
        context = self.context_builder.build(request)
        feedback, last_proposal = [], None
        last_report = ValidationReport(valid=False, errors=["No response received"])

        for attempt in range(1, self.max_attempts + 1):
            raw = self.provider.generate(context, feedback)
            try:
                proposal = Proposal.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                feedback = [f"Invalid JSON or response schema: {exc}"]
                last_report = ValidationReport(valid=False, errors=feedback)
                continue

            report = self.validator.validate(proposal, request)
            last_proposal, last_report = proposal, report
            if report.valid:
                status = "needs_human_review" if proposal.status == "proposal" else "accepted"
                message = ("Passed deterministic checks; engineering review is still required."
                           if status == "needs_human_review" else
                           "The model reports that more information or another solution is needed.")
                return LLMResult(task_id=request.task_id, status=status, proposal=proposal,
                                 validation=report, attempts_used=attempt, message=message)

            feedback = report.errors
            request.previous_attempts.append(PreviousAttempt(
                attempt=attempt, summary=proposal.problem, validation_errors=report.errors
            ))

        return LLMResult(task_id=request.task_id, status="failed", proposal=last_proposal,
                         validation=last_report, attempts_used=self.max_attempts,
                         message="Validation did not pass within the allowed number of attempts.")
