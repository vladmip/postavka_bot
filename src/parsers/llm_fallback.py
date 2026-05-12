"""LLM-fallback для случая когда жёсткий парсер не справился со схемой.

Идея (раздел 5.2 docx-стратегии): отдаём заголовки + 5 первых строк в Claude,
просим вернуть {наше_поле: column_index}. Используется только при ValueError из
жёстких парсеров. Не используется по умолчанию (платно).
"""
import json
from typing import Dict, List, Optional

from anthropic import Anthropic

from src.config import APIKEY_CLAUDE


def map_columns_via_llm(
    headers: List[str],
    sample_rows: List[List[str]],
    target_fields: List[str],
    file_kind_hint: str,
) -> Optional[Dict[str, int]]:
    """Возвращает {target_field: 0-based column_index} или None если LLM не справился.

    target_fields для opis_wb: ['barcode', 'qty', 'box_label', 'expiry']
    target_fields для opis_ozon: ['barcode', 'article', 'qty', 'zone', 'box_label', 'box_type', 'expiry']
    """
    if not APIKEY_CLAUDE:
        return None

    client = Anthropic(api_key=APIKEY_CLAUDE)

    sample_text = "\n".join(
        f"row {i+1}: {row}" for i, row in enumerate(sample_rows[:5])
    )
    fields_desc = ", ".join(target_fields)

    prompt = (
        f"Тип файла: {file_kind_hint}.\n"
        f"Заголовки (1-я строка): {headers}\n"
        f"Первые 5 строк данных:\n{sample_text}\n\n"
        f"Сопоставь заголовки с полями: {fields_desc}\n"
        f"Верни строго JSON без префиксов: {{\"<field>\": <0-based-index>}} только для найденных полей."
    )

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip() if msg.content else ""

    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()

    try:
        mapping = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(mapping, dict):
        return None

    result = {}
    for k, v in mapping.items():
        if k in target_fields and isinstance(v, int) and 0 <= v < len(headers):
            result[k] = v
    return result or None
