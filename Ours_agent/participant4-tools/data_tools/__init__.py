"""Набор инструментов первого агента (участник 4): проверка входных геологических данных.

Подключается к AgentRuntime (участник 1) как ActionExecutor и обслуживает планы
DataValidationProfile (участник 2).
"""

from .executor import ACTIONS, DataValidationExecutor

__all__ = ["ACTIONS", "DataValidationExecutor"]
