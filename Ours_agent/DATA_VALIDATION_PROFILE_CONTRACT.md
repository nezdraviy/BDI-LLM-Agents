# DataValidationProfile v1 — контракт с ActionExecutor

Документ описывает **предлагаемый** контракт между `DataValidationProfile` (участник 2)
и `ActionExecutor` (участник 4). В исходном `AgentRuntime` схема `observations`
намеренно не задана, поэтому перед интеграцией её нужно согласовать в команде.

## 1. Задача и запуск

Профиль ожидает `Task.payload` со следующими необязательными/обязательными ключами:

| Ключ | Обязательность | Значение |
|---|---|---|
| `source_path` | обязательно | Путь к папке или источнику входных данных |
| `recursive` | нет, default `True` | Рекурсивно сканировать подкаталоги |
| `report_path` | нет | Желаемый путь сохранения отчёта |
| `report_format` | нет, default `json` | Формат отчёта |

Пример:

```python
task = Task(
    id="validation-001",
    description="Проверить входные геологические данные",
    payload={
        "source_path": "data/input",
        "recursive": True,
        "report_path": "reports/validation-001.json",
        "report_format": "json",
    },
)
```

## 2. Разрешённые действия

`ActionExecutor.supports(action)` должен возвращать `True` для этих имён:

- `scan_source_directory`
- `parse_input_files`
- `validate_input_data`
- `build_validation_report`
- `save_validation_report`

Профиль не выполняет файловые операции и не парсит LAS/CSV самостоятельно. Он передаёт
действия исполнителю, принимает наблюдения и обновляет Beliefs.

## 3. Схемы observations

Все результаты должны возвращаться как `ActionResult(observations={...})`.

### 3.1 `scan_source_directory`

Входные параметры шага:

```python
{
    "source_path": "data/input",
    "recursive": True,
}
```

Успешный результат:

```python
{
    "inventory_completed": True,
    "files": [
        {"path": "data/input/well_01.las", "format": "las"},
        {"path": "data/input/metadata.csv", "format": "csv"},
    ],
    "findings": [],
}
```

`files` может быть пустым списком. В этом случае профиль сам добавит finding
`no_input_files` с severity `error`, но агент всё равно сформирует отчёт.

Допустимые значения `format`: `las`, `csv`, `unknown`.

### 3.2 `parse_input_files`

Шаг получает `accepted_formats`, `required_fields_by_format` и `numeric_ranges`
из `ValidationPolicy`.

Успешный результат должен содержать запись **для каждого** файла из инвентаризации:

```python
{
    "parsing_completed": True,
    "files": [
        {
            "path": "data/input/well_01.las",
            "format": "las",
            "parse_status": "parsed",
            "fields": ["DEPT", "GR", "RHOB"],
            "row_count": 1200,
            "parse_error": None,
        },
        {
            "path": "data/input/metadata.csv",
            "format": "csv",
            "parse_status": "failed",
            "fields": [],
            "row_count": None,
            "parse_error": "Malformed CSV row",
        },
    ],
    "findings": [
        {
            "file_path": "data/input/metadata.csv",
            "check_id": "parse_error",
            "severity": "error",
            "message": "Malformed CSV row",
        },
    ],
}
```

Допустимые `parse_status`: `parsed`, `failed`, `unsupported`.

Ошибка одного файла — это finding, если исполнитель смог завершить пакетную обработку
и классифицировать остальные файлы. Если сам инструмент технически не смог выполнить
действие, он должен выбросить `ActionError`.

Результат обязан содержать полный набор путей из инвентаризации: ни один файл не должен
молча исчезать из результата парсинга.

### 3.3 `validate_input_data`

Успешный результат:

```python
{
    "validation_completed": True,
    "findings": [
        {
            "file_path": "data/input/well_01.las",
            "check_id": "required_field_missing",
            "severity": "error",
            "message": "Required field GR is missing",
        },
    ],
}
```

`validation_completed=True` означает, что все предусмотренные проверки выполнены.
Наличие findings с severity `warning`, `error` или `critical` **не означает**, что цель
агента не достигнута: агент должен зафиксировать проблемы и отразить их в отчёте.

### 3.4 `build_validation_report`

Параметры шага:

```python
{"format": "json"}
```

Успешный результат:

```python
{"report_built": True}
```

Исполнитель может хранить промежуточный артефакт внутри себя или в рабочем каталоге.

### 3.5 `save_validation_report`

Параметры шага:

```python
{"output_path": "reports/validation-001.json"}
```

Если `report_path` не задан в `Task.payload`, параметр `output_path` будет отсутствовать:
исполнитель выбирает путь по умолчанию.

Успешный результат:

```python
{
    "report_saved": True,
    "report_path": "reports/validation-001.json",
}
```

## 4. Модель Beliefs

Профиль хранит только компактные метаданные, не большие массивы кривых:

- путь и формат каждого файла;
- статус парсинга, имена полей и число записей;
- результаты проверок (`ValidationFinding`);
- состояние построения и сохранения отчёта.

`revision` пересчитывается после каждого успешного `integrate()` по содержимому Beliefs
и конфигурации `ValidationPolicy`.

## 5. Настройка геологических правил

По умолчанию профиль не предполагает, что конкретная кривая (например, GR, RHOB или NPHI)
обязательна для каждого проекта. Правила задаются явно:

```python
policy = ValidationPolicy(
    accepted_formats=("las", "csv"),
    required_fields_by_format={
        "las": ("DEPT", "GR", "RHOB"),
        "csv": ("WELL", "DATE"),
    },
    numeric_ranges={
        "GR": (0, 250),
        "RHOB": (1.0, 3.5),
    },
)
profile = DataValidationProfile(policy=policy)
```

Приведённые поля и диапазоны — только пример конфигурации, а не универсальные
геологические нормативы. Их нужно подтвердить по требованиям проекта и данным.

## 6. Запуск тестов

Из корня `ours_agent`:

```powershell
python -m unittest discover -s tests -v
```

Тест `test_data_validation_profile.py` использует mock-executor и проверяет полный
цикл. Он не заменяет интеграционную проверку с реальными LAS/CSV-функциями участника 4.


## 7. Подключение к runtime

Файл `data_validation_profile.py` должен лежать рядом с папкой `agent_runtime`:

```text
ours_agent/
    agent_runtime/
    tests/
    data_validation_profile.py
    DATA_VALIDATION_PROFILE_CONTRACT.md
```

Минимальный запуск:

```python
from agent_runtime import AgentRuntime, Task
from data_validation_profile import DataValidationProfile, ValidationPolicy

profile = DataValidationProfile(
    policy=ValidationPolicy(
        required_fields_by_format={
            "las": ("DEPT", "GR", "RHOB"),
            "csv": ("WELL", "DATE"),
        },
    )
)

task = Task(
    id="validation-001",
    description="Проверить входные геологические данные",
    payload={"source_path": "data/input"},
)

runtime = AgentRuntime(
    profile=profile,
    executor=your_action_executor,  # реализация участника 4
    proposals=your_proposal_provider,  # можно передать None
)

result = await runtime.run(task)
print(result.status, result.reason)
```

В примере `your_action_executor` и `your_proposal_provider` — placeholders:
их нужно заменить реализациями участников 4 и 3.


## 8. Идентификаторы задач

Профиль хранит состояние отдельно по `Task.id`. Поэтому `Task.id` должен быть уникальным
для каждого нового запуска проверки. Если повторно запустить задачу с тем же ID и теми же
параметрами, профиль будет считать её продолжением прежней задачи и вернёт уже накопленные
Beliefs. Для повторной проверки тех же файлов создавайте новый `Task.id`.
