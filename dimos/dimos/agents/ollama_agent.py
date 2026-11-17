# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ollama

from dimos.utils.logging_config import setup_logger

logger = setup_logger()


def _configured_client() -> ollama.Client:
    """The ollama client against the configured server, if any.

    A remote Ollama (--ollama-base-url) serves its own models; the default
    client would look at localhost.
    """
    from dimos.core.global_config import global_config

    if global_config.ollama_base_url:
        return ollama.Client(host=global_config.ollama_base_url)
    return ollama.Client()


def ensure_ollama_model(model_name: str) -> None:
    client = _configured_client()
    available_models = client.list()
    model_exists = any(model_name == m.model for m in available_models.models)
    if not model_exists:
        logger.info(f"Ollama model '{model_name}' not found. Pulling...")
        client.pull(model_name)


def ollama_installed() -> str | None:
    try:
        _configured_client().list()
        return None
    except Exception:
        return (
            "Cannot connect to Ollama daemon. Please ensure Ollama is installed and running.\n"
            "\n"
            "   For installation instructions, visit https://ollama.com/download"
        )
