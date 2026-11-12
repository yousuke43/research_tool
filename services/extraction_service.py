"""Requirements から Decision Items を抽出する (抽出モデル1つ).

プロンプトは prompts/extract.txt。Python コードを変更せずに差し替えられる。
"""

from __future__ import annotations

from services.openrouter_client import ChatResult, OpenRouterClient
from utils.file_manager import load_prompt, render_prompt

PROMPT_NAME = "extract"


def extract_decision_items(
    client: OpenRouterClient, model_id: str, requirements: str
) -> ChatResult:
    template = load_prompt(PROMPT_NAME)
    prompt = render_prompt(template, requirements=requirements)
    return client.chat(model_id, prompt)
