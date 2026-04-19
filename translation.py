#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Übersetzt `battlebase-data-en.json` nach Deutsch über die OpenAI API.

OpenAI API-Key erstellen:
1) https://platform.openai.com öffnen
2) Sign in -> API keys -> "Create new secret key"
3) Key sicher speichern (wird danach nicht mehr vollständig angezeigt)
WICHTIG: Niemals den API-Key in Git committen oder im Quellcode speichern.

`OPENAI_API_KEY` setzen:
- Windows PowerShell:
    $env:OPENAI_API_KEY="dein_key"
- Windows CMD:
    set OPENAI_API_KEY=dein_key
- macOS/Linux (bash/zsh):
    export OPENAI_API_KEY="dein_key"

Script starten:
    python translation.py

Optional:
    python translation.py --input battlebase-data-en.json --output battlebase-data.json --chunk-size 20
"""

import argparse
import json
import os
import time
from typing import Any, Dict, List, Tuple

import requests

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
MODEL_NAME = "gpt-4.1-mini"
DEFAULT_INPUT_FILE = "battlebase-data-en.json"
DEFAULT_OUTPUT_FILE = "battlebase-data.json"
STYLE_GUIDE_FILE = "GrundregelnBeispiel.txt"
GLOSSARY_FILE = "terminology_glossary.json"
REQUEST_PAUSE_SECONDS = 1.0
INITIAL_BACKOFF_SECONDS = 5
BACKOFF_MULTIPLIER = 2
MAX_BACKOFF_SECONDS = 30


def load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text_file(path: str, max_chars: int = 8000) -> str:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[...gekürzt für Prompt-Länge...]"


def schema_for_value(value: Any, force_id: str = "") -> Dict[str, Any]:
    """Erzeugt ein JSON-Schema für einen Wert, optional mit fixer ID-`const`."""
    if value is None:
        return {"type": "null"}

    if force_id:
        return {"type": "string", "const": force_id}

    if isinstance(value, bool):
        return {"type": "boolean"}

    if isinstance(value, int):
        return {"type": "integer"}

    if isinstance(value, float):
        return {"type": "number"}

    if isinstance(value, str):
        return {"type": "string"}

    if isinstance(value, list):
        if not value:
            return {"type": "array", "maxItems": 0}
        return {
            "type": "array",
            "minItems": len(value),
            "maxItems": len(value),
            "prefixItems": [schema_for_value(item) for item in value],
            "items": False,
        }

    if isinstance(value, dict):
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for key, val in value.items():
            required.append(key)
            if key == "id" and isinstance(val, str):
                properties[key] = schema_for_value(val, force_id=val)
            else:
                properties[key] = schema_for_value(val)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    # Fallback für unerwartete Typen
    return {"type": "string"}


def build_response_schema(chunk: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Erzeugt ein striktes Array-Schema mit exakt derselben Chunk-Struktur."""
    return {
        "type": "array",
        "minItems": len(chunk),
        "maxItems": len(chunk),
        "prefixItems": [schema_for_value(item) for item in chunk],
        "items": False,
    }


def build_messages(style_guide: str, glossary: Dict[str, str], chunk: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    glossary_lines = "\n".join(f"- {src} -> {dst}" for src, dst in glossary.items())

    system_prompt = f"""Du bist ein professioneller Warhammer-40.000-Regelübersetzer (EN -> DE).

AUFGABE:
- Übersetze nur semantischen Textinhalt in den JSON-Werten.
- Behalte JSON-Struktur, Reihenfolge, Schlüsselnamen und Datentypen exakt bei.
- IDs (`id`) niemals ändern.
- `null` muss `null` bleiben.
- Gib ausschließlich ein JSON-Array passend zum Schema zurück.

TERMINOLOGIE (HARTE REGEL):
Wenn ein Begriff im Glossar steht, MUSS exakt die angegebene deutsche Form verwendet werden.
{glossary_lines}

STIL-REFERENZ (Ton, Rhythmus, Terminologie):
{style_guide}
"""

    user_prompt = (
        "Übersetze folgenden JSON-Array ins Deutsche unter Einhaltung aller Regeln. "
        "Antworte nur im geforderten JSON-Schema.\n\n"
        + json.dumps(chunk, ensure_ascii=False)
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def validate_structure(original: Any, translated: Any, path: str = "$") -> Tuple[bool, str]:
    """Validiert rekursiv, dass Struktur/Typen/IDs der Übersetzung unverändert bleiben."""
    if original is None:
        if translated is not None:
            return False, f"{path}: expected null"
        return True, ""

    if isinstance(original, bool):
        return (isinstance(translated, bool), f"{path}: expected bool")

    if isinstance(original, int):
        return (isinstance(translated, int), f"{path}: expected int")

    if isinstance(original, float):
        return (isinstance(translated, (int, float)) and not isinstance(translated, bool), f"{path}: expected number")

    if isinstance(original, str):
        return (isinstance(translated, str), f"{path}: expected string")

    if isinstance(original, list):
        if not isinstance(translated, list):
            return False, f"{path}: expected list"
        if len(original) != len(translated):
            return False, f"{path}: list length mismatch ({len(original)} != {len(translated)})"
        for i, (o_item, t_item) in enumerate(zip(original, translated)):
            ok, msg = validate_structure(o_item, t_item, f"{path}[{i}]")
            if not ok:
                return False, msg
        return True, ""

    if isinstance(original, dict):
        if not isinstance(translated, dict):
            return False, f"{path}: expected object"
        if set(original.keys()) != set(translated.keys()):
            return False, f"{path}: keys changed"
        if "id" in original and original["id"] != translated["id"]:
            return False, f"{path}.id changed ({original['id']} != {translated['id']})"
        for key in original.keys():
            ok, msg = validate_structure(original[key], translated[key], f"{path}.{key}")
            if not ok:
                return False, msg
        return True, ""

    return False, f"{path}: unsupported type"


def call_openai_translate(
    api_key: str,
    style_guide: str,
    glossary: Dict[str, str],
    chunk: List[Dict[str, Any]],
    timeout_seconds: int,
) -> List[Dict[str, Any]]:
    """Ruft OpenAI mit JSON-Schema-Output auf und validiert das Antwort-Chunk."""
    schema = build_response_schema(chunk)
    payload = {
        "model": MODEL_NAME,
        "messages": build_messages(style_guide, glossary, chunk),
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "w40k_translation",
                "strict": True,
                "schema": schema,
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(OPENAI_API_URL, headers=headers, json=payload, timeout=timeout_seconds)
    response.raise_for_status()
    data = response.json()

    content = data["choices"][0]["message"]["content"]
    translated = json.loads(content)

    if len(translated) != len(chunk):
        raise ValueError(f"Chunk length mismatch: sent {len(chunk)}, got {len(translated)}")

    for i, (original, item) in enumerate(zip(chunk, translated)):
        ok, msg = validate_structure(original, item, f"$[{i}]")
        if not ok:
            raise ValueError(f"Invalid translated structure: {msg}")

    return translated


def translate_chunk_with_retry(
    api_key: str,
    style_guide: str,
    glossary: Dict[str, str],
    chunk: List[Dict[str, Any]],
    chunk_number: int,
    max_retries: int = 4,
    timeout_seconds: int = 180,
) -> List[Dict[str, Any]]:
    print(f"\nÜbersetzung von Chunk {chunk_number} ({len(chunk)} Einträge)")

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"  Retry {attempt}/{max_retries}...")
            translated = call_openai_translate(api_key, style_guide, glossary, chunk, timeout_seconds)
            print("  ✓ Chunk übersetzt")
            return translated
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            body = exc.response.text[:500] if exc.response is not None else str(exc)
            print(f"  ✗ HTTP error ({status}): {body}")
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"  ✗ Fehler: {type(exc).__name__}: {exc}")

        if attempt < max_retries:
            sleep_seconds = min(
                MAX_BACKOFF_SECONDS,
                INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
            )
            time.sleep(sleep_seconds)

    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate battlebase-data-en.json using OpenAI API")
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE, help="Input JSON file")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE, help="Output JSON file")
    parser.add_argument("--chunk-size", type=int, default=20, help="Initial chunk size")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"OPENAI_API_KEY is not set. See usage instructions at top of {os.path.basename(__file__)}"
        )

    style_guide = load_text_file(STYLE_GUIDE_FILE, max_chars=8000)
    glossary = load_json_file(GLOSSARY_FILE)
    data = load_json_file(args.input)

    if not isinstance(data, list):
        raise ValueError("Input JSON must be a list")

    print(f"Total entries: {len(data)}")
    translated_data: List[Dict[str, Any]] = []

    position = 0
    chunk_number = 0
    current_chunk_size = max(1, args.chunk_size)

    while position < len(data):
        chunk_number += 1
        chunk = data[position:position + current_chunk_size]

        translated_chunk = translate_chunk_with_retry(
            api_key=api_key,
            style_guide=style_guide,
            glossary=glossary,
            chunk=chunk,
            chunk_number=chunk_number,
        )

        if translated_chunk:
            translated_data.extend(translated_chunk)
            position += len(chunk)

            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(translated_data, f, indent=2, ensure_ascii=False)

            print(f"  Progress: {len(translated_data)}/{len(data)}")
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        if current_chunk_size > 1:
            current_chunk_size = max(1, current_chunk_size // 2)
            print(f"  Chunk failed, reducing chunk size to {current_chunk_size} and retrying...")
        else:
            raise RuntimeError(f"Unable to translate entry at position {position} with id={chunk[0].get('id')}")

    if len(translated_data) != len(data):
        raise RuntimeError(
            f"Translation incomplete: {len(translated_data)}/{len(data)} entries in output"
        )

    print("\n✅ Translation complete.")
    print(f"Output written to: {args.output}")


if __name__ == "__main__":
    main()
