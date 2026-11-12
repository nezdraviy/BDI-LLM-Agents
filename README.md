# BDI LLM Module — MVP

Небольшой Python-модуль для взаимодействия BDI-агентов с LLM в проекте нефтегазового моделирования.

## Назначение

Модуль получает от BDI-агента задачу или пробел в знаниях, собирает контекст, запрашивает LLM, проверяет структурированный ответ и возвращает результат агенту. При ошибке проверки он передаёт обратную связь модели и ограниченно повторяет генерацию.

**Важно:** успешная программная проверка не означает инженерную корректность. Модуль проверяет схему ответа, ссылки на утверждённые знания и простые ограничения. Инженерная проверка и общий QA/QC модели остаются отдельными этапами.

## Схема работы

```text
BDI agent -> LLMRequest -> ContextBuilder + approved knowledge
          -> LLMProvider (mock / OpenAI) -> JSON response
          -> Pydantic schema validation -> ProposalValidator
             | valid                         | invalid
             v                               v
        LLMResult -> BDI                error feedback -> retry
                                            -> max attempts: failed
```

## Структура

```text
bdi_llm_mvp/
├── bdi_llm/
│   ├── models.py          # Контракты запроса, предложения и результата
│   ├── context_builder.py # Сбор релевантного контекста
│   ├── knowledge.py       # Чтение JSON-БЗ и сохранение новых правил как proposed
│   ├── prompts.py         # Инструкции и сборка prompt
│   ├── providers.py       # Mock- и OpenAI-провайдеры
│   ├── validator.py       # Программная проверка предложения
│   ├── pipeline.py        # Генерация, проверка и повторные попытки
│   └── config.py          # Настройки окружения
├── knowledge_base/rules.json
├── examples/demo.py
├── tests/test_mvp.py
├── requirements.txt
└── pyproject.toml
```

## Установка

Python 3.10+:

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск без API-ключа

По умолчанию используется `mock` — заглушка для проверки архитектуры, а не оценки качества LLM:

```bash
python -m examples.demo
```

## Запуск с OpenAI API

Установите SDK (он уже указан в `requirements.txt`) и задайте переменные окружения:

```bash
# PowerShell
$env:LLM_PROVIDER="openai"
$env:OPENAI_API_KEY="your_api_key"
$env:OPENAI_MODEL="gpt-4o-mini"
python -m examples.demo
```

`.env.example` приведён как шаблон; сам проект пока не загружает `.env` автоматически.

## Вызов из BDI-кода

```python
from bdi_llm import LLMModule, LLMRequest
from bdi_llm.providers import create_provider

request = LLMRequest(
    task_id="TASK-42",
    agent_id="reservoir-agent",
    task_type="resolve_knowledge_gap",
    goal="Identify missing information for the next modelling step",
    context={"available_data": ["porosity"]},
    knowledge=[],
    constraints=["Do not invent engineering values"],
)

module = LLMModule(provider=create_provider("mock"), max_attempts=3)
result = module.process(request)
print(result.model_dump(mode="json"))
```

В интеграции запрос формирует BDI-агент. Его поля нужно согласовать с командой и адаптировать под реальный BDI-контракт.

## Статусы

Статусы `LLMResult`:
- `accepted` — LLM сообщила, что данных недостаточно или решение не найдено; ответ можно передать BDI.
- `needs_human_review` — предложение прошло программные проверки, но требует инженерного подтверждения.
- `failed` — проверка не прошла за установленное число попыток.

Статусы `Proposal`: `proposal`, `insufficient_data`, `no_solution`.

## Тесты

```bash
pytest
```

## Ограничения и следующие шаги

1. Согласовать API-контракт BDI ↔ LLM.
2. Подключить реальную базу знаний и её схему.
3. Добавить предметные инженерные валидаторы совместно с экспертами.
4. Передавать ошибки QA/QC модели обратно в контекст.
5. Добавить журналирование запросов, ответов и версий промптов.
6. Реализовать согласование новых правил и допущений перед публикацией.

`DEMO-001` — демонстрационная запись для тестирования потока, а не реальное нефтегазовое правило.
