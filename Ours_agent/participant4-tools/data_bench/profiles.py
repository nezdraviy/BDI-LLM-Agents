"""Вариант профиля участника 2 для сценариев со сбоями (предложение к согласованию).

В DataValidationProfile v1 шаги планов объявлены без retry_safe. В AgentRuntime
провал такого шага навсегда исключает план для цели, а в профиле на каждую цель
только один план. Поэтому любой временный сбой (занят диск, сеть) проваливает
всю задачу. Все пять действий DataValidationExecutor идемпотентны, так что
шаги можно безопасно объявить retry_safe=True и разрешить повторы.

RetrySafeProfile ничего не меняет в логике профиля, только параметры повторов.
"""

from __future__ import annotations

from dataclasses import replace

from data_validation_profile import DataValidationProfile


class RetrySafeProfile(DataValidationProfile):
    MAX_RETRIES = 2

    async def plans(self, context):
        return tuple(
            replace(plan, steps=tuple(
                replace(step, retry_safe=True, max_retries=self.MAX_RETRIES,
                        retry_delay_seconds=0)
                for step in plan.steps))
            for plan in await super().plans(context))
