"""同じ Requirements と同じ Decision Items から Decision Table を生成する.

Decision Items の抽出は実験につき1回だけ行い、その結果を全生成モデルで共有する
(このモジュールは抽出を行わない)。プロンプトは prompts/generate.txt。
"""

from __future__ import annotations

from services.openrouter_client import ChatResult, OpenRouterClient
from utils.file_manager import load_prompt, render_prompt

PROMPT_NAME = "generate"


def generate_decision_table(
    client: OpenRouterClient, model_id: str, requirements: str, decision_items: str
) -> ChatResult:
    template = load_prompt(PROMPT_NAME)
    prompt = render_prompt(
        template, requirements=requirements, decision_items=decision_items
    )
    return client.chat(model_id, prompt)
