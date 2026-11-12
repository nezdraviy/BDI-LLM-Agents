import json

SYSTEM_PROMPT = """You assist a BDI agent in an oil and gas modelling project.
You are not the final engineering authority.
Use only supplied context and approved knowledge. Never invent engineering facts,
measurements, rules, or sources. If important data is missing, use
status=insufficient_data. If no safe relevant proposal is possible, use
status=no_solution. State assumptions explicitly. Return JSON only with fields:
status, problem, proposal (object with action and parameters, or null),
reasoning_summary, assumptions, knowledge_references, validation_requirements,
confidence (0..1). Never claim engineering approval."""

def build_user_prompt(context: dict, validation_errors=None) -> str:
    payload = {"context": context}
    if validation_errors:
        payload["validation_errors"] = validation_errors
        payload["correction_instruction"] = "Correct the validation errors using only supplied context."
    return json.dumps(payload, ensure_ascii=False, indent=2)
