"""Explicit unavailable capabilities; none of them approves input data."""

from __future__ import annotations

from agent_runtime import PlanningGap, PlanProposal


def pending_capabilities() -> list[dict[str, str]]:
    return [
        {
            "id": "project_rules", "status": "stub", "owner": "2",
            "reason": "Правила для реальных данных отложены. Выполняются только базовые проверки и явно переданная конфигурация.",
        },
        {
            "id": "cross_file_consistency", "status": "stub", "owner": "2 + 4",
            "reason": "Сопоставление скважин и связанных наборов между файлами не реализовано.",
        },
        {
            "id": "metadata_normalization", "status": "stub", "owner": "2 + 4",
            "reason": "Единицы, UWI, системы координат и вертикальные привязки не нормализуются; исходные метаданные сохраняются лишь частично.",
        },
        {
            "id": "admission", "status": "stub", "owner": "2 + 4 + 1",
            "reason": "Окончательный допуск к следующему этапу не определён. Отсутствие технических ошибок не является допуском.",
        },
        {
            "id": "per_check_coverage", "status": "stub", "owner": "4 + 2",
            "reason": "Измеряется охват файлов; исполнитель пока не возвращает полный перечень выполненных и пропущенных проверок каждого поля.",
        },
        {
            "id": "llm", "status": "stub", "owner": "3",
            "reason": "LLM не подключена. При пробеле в планировании заглушка возвращает None и не придумывает план.",
        },
        {
            "id": "resume", "status": "stub", "owner": "1 + 2 + 4",
            "reason": "Автоматические повторы, фиксация версии входов и восстановление процесса не подключены. Повторная проверка запускается целиком с новым ID.",
        },
    ]


class NoLLMProposalProvider:
    """An explicit no-plan response, rather than a simulated LLM decision."""

    def __init__(self) -> None:
        self.requests = 0

    async def propose(self, gap: PlanningGap) -> PlanProposal | None:
        self.requests += 1
        return None


def admission_not_assessed() -> dict[str, str]:
    return {
        "status": "not_assessed",
        "reason": "Проверки пригодности для следующего этапа не согласованы; доступен только технический результат текущей обработки.",
    }
