from .models import LLMRequest, Proposal, ValidationReport


class ProposalValidator:
    """Deterministic checks only; not a substitute for engineering QA/QC."""
    def validate(self, proposal: Proposal, request: LLMRequest) -> ValidationReport:
        errors, warnings = [], []
        approved_ids = {x.id for x in request.knowledge if x.status == "approved"}
        unknown = [ref for ref in proposal.knowledge_references if ref not in approved_ids]
        if unknown:
            errors.append("Unknown or non-approved knowledge references: " + ", ".join(unknown))

        if proposal.status == "proposal":
            if proposal.proposal is None:
                errors.append("proposal object is required when status='proposal'")
            elif not proposal.proposal.action.strip():
                errors.append("proposal action must not be empty")
        elif proposal.proposal is not None:
            errors.append("proposal must be null for insufficient_data or no_solution")

        if proposal.status == "insufficient_data" and not proposal.validation_requirements:
            warnings.append("No validation requirement was provided for the missing information")

        # MVP convention: a constraint can be written as forbid_action:<action>.
        if proposal.proposal:
            for constraint in request.constraints:
                if constraint.startswith("forbid_action:"):
                    forbidden = constraint.split(":", 1)[1].strip()
                    if proposal.proposal.action == forbidden:
                        errors.append(f"Action '{forbidden}' is forbidden by the request")
        if not proposal.reasoning_summary.strip():
            warnings.append("Reasoning summary is empty")
        return ValidationReport(valid=not errors, errors=errors, warnings=warnings)
