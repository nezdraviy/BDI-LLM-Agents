"""Демо-профиль для стенда. ВРЕМЕННАЯ замена профиля участника 2 — только чтобы проверить
инструменты в полном цикле. Схема observations описана в README_executor.md."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from agent_runtime import Check, Goal, GoalAssessment, GoalStatus, Plan, Step

QC_CHECKS = ("check_ranges", "check_facies", "check_poro_perm",
             "check_contacts", "check_spatial", "check_well_tie")


@dataclass(frozen=True)
class GeoBeliefs:
    revision: str
    facts: Mapping[str, object]


class DemoGeoProfile:
    def __init__(self):
        self._facts: dict[str, object] = {"checks": {}}
        self._rev = 0

    # --- знания ---
    async def observe(self, task):
        facts = {**self._facts, "checks": MappingProxyType(dict(self._facts["checks"]))}
        return GeoBeliefs(f"r{self._rev}", MappingProxyType(facts))

    async def integrate(self, request, result):
        obs, action = result.observations, request.step.action
        if action.startswith("check_"):
            self._facts["checks"][action] = obs["issues"]
            if obs.get("unit_error_suspected"):
                self._facts["unit_error"] = True        # критично: дальше проверять бессмысленно
        elif action == "load_model":
            self._facts["model_loaded"] = True
        elif action == "write_report":
            self._facts["report_path"] = obs["report_path"]
            self._facts["issues_total"] = obs["issues_total"]
        elif action == "send_alert":
            self._facts["alert_path"] = obs["alert_path"]
        elif action == "compute_volumes":
            self._facts["stoiip"] = obs["stoiip_mln_m3"]
        self._rev += 1

    # --- цели ---
    async def goals(self, task, beliefs):
        f = beliefs.facts
        goals = [Goal("model-qc", priority=10)]
        if "report_path" in f:
            if f["issues_total"]:
                goals.append(Goal("alert-sent", priority=20))       # немедленная реакция
            else:
                goals.append(Goal("volumes-estimated", priority=5))
        return goals

    async def assess_goal(self, ctx):
        f, g = ctx.beliefs.facts, ctx.goal.id
        done = {"model-qc": "report_path", "alert-sent": "alert_path",
                "volumes-estimated": "stoiip"}[g] in f
        return GoalAssessment(GoalStatus.ACHIEVED if done else GoalStatus.PENDING)

    # --- планы ---
    async def plans(self, ctx):
        g = ctx.goal.id
        if g == "model-qc":
            load = Step("load", "load_model", max_retries=2, retry_safe=True)
            full = (load, Step("wells", "load_wells"),
                    *(Step(c.removeprefix("check_"), c) for c in QC_CHECKS),
                    Step("report", "write_report"))
            short = (Step("report", "write_report"),)
            return [Plan("qc-full", g, full, priority=10),
                    Plan("qc-stop-on-critical", g, short, priority=1)]
        if g == "alert-sent":
            return [Plan("alert-v1", g, (Step("alert", "send_alert", {"severity": "error"}),))]
        return [Plan("volumes-v1", g, (Step("volumes", "compute_volumes"),))]

    async def plan_applicable(self, plan, ctx):
        unit_error = ctx.beliefs.facts.get("unit_error", False)
        if plan.id == "qc-full" and unit_error:
            return Check(False, "пористость в процентах — полная проверка бессмысленна")
        if plan.id == "qc-stop-on-critical" and not unit_error:
            return Check(False, "критической ошибки нет")
        return Check(True)

    async def step_applicable(self, step, ctx):
        return Check(True)

    async def validate_proposal(self, proposal, ctx):
        return Check(False, "в демо предложения LLM не используются")
