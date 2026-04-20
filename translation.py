#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import subprocess
import requests
import sys
import time
from datetime import datetime
import os
import openai

REPORTS_DIR = 'reports'
CHUNKS_DIR = os.path.join(REPORTS_DIR, 'chunks')
FAILED_CHUNKS_DIR = os.path.join(REPORTS_DIR, 'failed_chunks')

# OpenAI API-Schlüssel aus der Systemumgebungsvariable ladenecho $Env:OPENAI_API_KEY
openai.api_key = os.getenv("OPENAI_API_KEY")

if not openai.api_key:
    print("Error: OPENAI_API_KEY environment variable is not set.")
    sys.exit(1)  # Exit if the API key is not found

def extract_json_from_response(response):
    """Extrahiert JSON aus der Claude-Antwort, auch bei zusätzlichem Text."""
    # Erstes "[" und letztes "]" suchen
    start = response.find('[')
    end = response.rfind(']') + 1
    
    if start != -1 and end > start:
        json_str = response[start:end]
        try:
            # JSON direkt parsen
            return json.loads(json_str), json_str
        except json.JSONDecodeError as e:
            # Bei Fehlern: JSON bereinigen
            # Zuerst fehlerhaft maskierte Apostrophe korrigieren
            json_str_fixed = json_str
            
            # Nicht maskierte Apostrophe in JSON-Werten ersetzen
            # IDs dabei aussparen ("id": "...").
            import re
            # IDs temporär schützen
            id_pattern = r'("id"\s*:\s*"[^"]*")'
            ids = re.findall(id_pattern, json_str_fixed)
            for i, id_match in enumerate(ids):
                json_str_fixed = json_str_fixed.replace(id_match, f"__ID_PLACEHOLDER_{i}__")
            
            # Restliche Apostrophe maskieren
            json_str_fixed = json_str_fixed.replace("'", "\\'")
            
            # IDs zurücksetzen
            for i, id_match in enumerate(ids):
                json_str_fixed = json_str_fixed.replace(f"__ID_PLACEHOLDER_{i}__", id_match)
            
            try:
                return json.loads(json_str_fixed), json_str_fixed
            except:
                # Falls weiterhin ungültig: Zeilenkommentare entfernen
                lines = json_str.split('\n')
                cleaned_lines = []
                for line in lines:
                    # Kommentare nach // abschneiden
                    comment_pos = line.find('//')
                    if comment_pos != -1:
                        line = line[:comment_pos]
                    cleaned_lines.append(line)
                
                cleaned_json = '\n'.join(cleaned_lines)
                try:
                    return json.loads(cleaned_json), cleaned_json
                except:
                    pass
    
    # Einzelne JSON-Objekte finden
    # Manchmal kommt "{obj1}{obj2}" statt "[{obj1},{obj2}]"
    import re
    objects = re.findall(r'\{[^{}]*\}', response)
    if objects:
        try:
            parsed_objects = [json.loads(obj) for obj in objects]
            return parsed_objects, json.dumps(parsed_objects)
        except:
            pass
    
    return None, None

def preprocess_chunk_for_translation(chunk):
    """Bereitet einen Chunk vor, um Apostroph-Probleme zu vermeiden."""
    # Kopie erstellen, damit das Original unverändert bleibt
    processed_chunk = []
    id_mapping = {}
    
    for i, item in enumerate(chunk):
        item_copy = item.copy()
        original_id = item_copy['id']
        
        # Bei IDs mit Apostroph (normal oder typografisch) temporäre ID verwenden
        if "'" in original_id or "’" in original_id:
            id_safe = original_id.replace("'", "_APOS_").replace("’", "_APOS_")
            temp_id = f"TEMP_ID_{i}_{id_safe}"
            item_copy['id'] = temp_id
            id_mapping[temp_id] = original_id
        
        processed_chunk.append(item_copy)
    
    return processed_chunk, id_mapping

def postprocess_translated_chunk(translated_chunk, id_mapping):
    """Stellt originale IDs nach der Übersetzung wieder her."""
    if not id_mapping:
        return translated_chunk
    
    processed = []
    for item in translated_chunk:
        item_copy = item.copy()
        if item_copy['id'] in id_mapping:
            item_copy['id'] = id_mapping[item_copy['id']]
        processed.append(item_copy)
    
    return processed

def validate_translated_item_structure(source_item, translated_item, path=""):
    """Prüft rekursiv Struktur, Datentypen und unveränderte IDs."""
    current_path = path or "$"

    if type(source_item) != type(translated_item):
        return False, f"Typabweichung bei {current_path}: {type(source_item).__name__} != {type(translated_item).__name__}"

    if isinstance(source_item, dict):
        source_keys = set(source_item.keys())
        translated_keys = set(translated_item.keys())
        if source_keys != translated_keys:
            missing = source_keys - translated_keys
            extra = translated_keys - source_keys
            return False, f"Schlüsselabweichung bei {current_path}: fehlend={sorted(missing)}, zusätzlich={sorted(extra)}"

        for key in source_item:
            key_path = f"{current_path}.{key}"
            source_value = source_item[key]
            translated_value = translated_item[key]

            if key == "id":
                if source_value != translated_value:
                    return False, f"ID wurde verändert bei {key_path}: '{source_value}' != '{translated_value}'"
                continue

            is_valid, error = validate_translated_item_structure(source_value, translated_value, key_path)
            if not is_valid:
                return False, error
        return True, None

    if isinstance(source_item, list):
        if len(source_item) != len(translated_item):
            return False, f"Längenabweichung bei {current_path}: {len(source_item)} != {len(translated_item)}"
        for index, (source_value, translated_value) in enumerate(zip(source_item, translated_item)):
            is_valid, error = validate_translated_item_structure(source_value, translated_value, f"{current_path}[{index}]")
            if not is_valid:
                return False, error
        return True, None

    if isinstance(source_item, str):
        if not isinstance(translated_item, str):
            return False, f"String-Typ verletzt bei {current_path}"
        return True, None

    if source_item != translated_item:
        return False, f"Wertabweichung bei {current_path}: {source_item!r} != {translated_item!r}"

    return True, None

def check_executable_exists(executable):
    """Prüft, ob ein ausführbares Programm im Systempfad vorhanden ist."""
    from shutil import which
    if which(executable) is None:
        print(f"Error: Executable '{executable}' not found in PATH.")
        sys.exit(1)

def get_chunk_file_path(chunk_number):
    return os.path.join(CHUNKS_DIR, f"chunk_{chunk_number:04d}.json")

def save_chunk_result(chunk_number, translated_chunk):
    os.makedirs(CHUNKS_DIR, exist_ok=True)
    chunk_path = get_chunk_file_path(chunk_number)
    with open(chunk_path, 'w', encoding='utf-8') as f:
        json.dump(translated_chunk, f, indent=2, ensure_ascii=False)

def validate_chunk_translation(source_chunk, translated_chunk):
    if not isinstance(translated_chunk, list):
        return False, f"Ergebnis ist kein JSON-Array, sondern: {type(translated_chunk).__name__}"

    if len(translated_chunk) != len(source_chunk):
        return False, f"Länge des übersetzten Chunks stimmt nicht: {len(translated_chunk)} != {len(source_chunk)}"

    for index, (source_item, translated_item) in enumerate(zip(source_chunk, translated_chunk)):
        is_valid, error = validate_translated_item_structure(source_item, translated_item, path=f"$[{index}]")
        if not is_valid:
            return False, error

    return True, None

def load_existing_chunk_if_valid(source_chunk, chunk_number):
    chunk_path = get_chunk_file_path(chunk_number)
    if not os.path.exists(chunk_path):
        return None

    try:
        with open(chunk_path, 'r', encoding='utf-8') as f:
            existing_chunk = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠️  Konnte vorhandenen Chunk nicht laden ({chunk_path}): {e}")
        return None

    is_valid, error = validate_chunk_translation(source_chunk, existing_chunk)
    if not is_valid:
        print(f"  ⚠️  Vorhandener Chunk ungültig ({chunk_path}): {error}")
        return None

    print(f"  ♻️  Verwende vorhandenen Chunk: {chunk_path}")
    return existing_chunk

def log_failed_chunk(chunk_number, source_chunk, raw_response=None, extracted_json=None, error=None):
    os.makedirs(FAILED_CHUNKS_DIR, exist_ok=True)
    failed_path = os.path.join(FAILED_CHUNKS_DIR, f"chunk_{chunk_number:04d}.json")
    payload = {
        "timestamp": datetime.now().isoformat(),
        "chunk_number": chunk_number,
        "chunk_size": len(source_chunk),
        "error": error,
        "source_chunk": source_chunk,
        "raw_response": raw_response,
        "extracted_json": extracted_json
    }
    with open(failed_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"  ℹ️  Fehlerdetails gespeichert: {failed_path}")

def merge_chunk_files(output_file, max_chunk_number=None):
    if not os.path.exists(CHUNKS_DIR):
        return None

    chunk_files_with_numbers = []
    for filename in os.listdir(CHUNKS_DIR):
        if not (filename.startswith('chunk_') and filename.endswith('.json')):
            continue
        try:
            chunk_no = int(filename.replace('chunk_', '').replace('.json', ''))
        except ValueError:
            continue
        if max_chunk_number is not None and chunk_no > max_chunk_number:
            continue
        chunk_files_with_numbers.append((chunk_no, filename))

    chunk_files_with_numbers.sort(key=lambda item: item[0])
    chunk_files = [filename for _, filename in chunk_files_with_numbers]

    if not chunk_files:
        return None

    merged_data = []
    for chunk_file in chunk_files:
        chunk_path = os.path.join(CHUNKS_DIR, chunk_file)
        with open(chunk_path, 'r', encoding='utf-8') as f:
            chunk_data = json.load(f)
        if not isinstance(chunk_data, list):
            raise ValueError(f"{chunk_path} enthält kein JSON-Array")
        merged_data.extend(chunk_data)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(merged_data, f, indent=2, ensure_ascii=False)

    print(f"  ♻️  Ausgabe aus {len(chunk_files)} Chunk-Dateien zusammengeführt")
    return merged_data

def translate_chunk_with_openai(chunk, chunk_number, max_retries=2):
    """Übersetzt einen Chunk mit der OpenAI API."""
    print(f"\nÜbersetzung von Chunk {chunk_number} ({len(chunk)} Einträge)...")

    # Prompt erstellen
    prompt = """Übersetze das folgende JSON vollständig ins Deutsche und gib ausschließlich das übersetzte JSON-Array zurück.

KRITISCHE REGELN:
1. Gib ausschließlich ein gültiges JSON-Array zurück (kein Markdown, keine ```json```-Codefences).
2. Niemals Erklärungen, Kommentare oder sonstigen Zusatztext ausgeben.
3. Die JSON-Struktur exakt beibehalten (Objekte, Arrays, Reihenfolge, Schlüssel).
4. Jeder String-Wert muss rekursiv ins Deutsche übersetzt werden – in allen Feldern und Ebenen.
5. Ausnahme: Der Wert von Schlüsseln mit Namen "id" darf niemals verändert werden.
6. Schlüssel-Namen, Zahlen, Booleans und null müssen unverändert bleiben.
7. Datentypen dürfen nicht geändert werden.
8. Stil-Priorität: Natürliches, korrektes und gut verständliches Deutsch ist wichtiger als eine wörtliche 40K-Übersetzung.
9. Formuliere aktiv, klar und möglichst in kurzen Sätzen. Vermeide holprige oder unnötig verschachtelte Formulierungen.
10. Vermeide unklare Pronomen wie "sie/ihr/deren", wenn die Referenz unklar sein kann. Nutze stattdessen eindeutige Formulierungen wie "der Spieler" oder "der Spieler, dessen Zug es ist".
11. Terminologie konsistent halten: gleiche Begriffe innerhalb eines Textes immer gleich übersetzen.
12. Verwende in Regeltexten "SP (Siegpunkte)" statt "VP" (z. B. "3 SP", "bis zu 15 SP pro Runde").
13. Übersetze "CHARACTER" und "CHARAKTER" als "Charakter"; verwende für "CHARACTER models"/"CHARAKTER-Modelle" bevorzugt "Charakter-Modelle".
14. Behalte die vorhandene String-Formatierung bestmöglich bei (Absätze, Zeilenumbrüche, Aufzählungspunkte wie •).
15. Mini-Glossar: VP -> SP (Siegpunkte); CHARACTER models/CHARAKTER-Modelle -> Charakter-Modelle.

ZU ÜBERSETZENDES JSON:
"""

    chunk_json = json.dumps(chunk, indent=2, ensure_ascii=False)
    full_prompt = prompt + chunk_json

    print(f"  Prompt erstellt, Länge: {len(full_prompt)} Zeichen")

    for attempt in range(1, max_retries + 1):
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "Du bist ein hilfreicher Übersetzer."},
                    {"role": "user", "content": full_prompt}
                ]
            )

            response_text = response['choices'][0]['message']['content'].strip()
            parsed_chunk, extracted_json = extract_json_from_response(response_text)

            if parsed_chunk is None:
                error = "JSON konnte aus der Modellantwort nicht extrahiert oder geparst werden."
                print(f"  ✗ {error}")
                log_failed_chunk(chunk_number, chunk, raw_response=response_text, error=error)
            else:
                is_valid, validation_error = validate_chunk_translation(chunk, parsed_chunk)
                if is_valid:
                    print("  ✓ Chunk erfolgreich übersetzt und validiert")
                    return parsed_chunk

                print(f"  ✗ Strukturprüfung fehlgeschlagen: {validation_error}")
                log_failed_chunk(
                    chunk_number,
                    chunk,
                    raw_response=response_text,
                    extracted_json=extracted_json,
                    error=validation_error
                )

        except openai.error.OpenAIError as e:
            error = f"OpenAI API-Fehler: {e}"
            print(f"  ✗ {error}")
            log_failed_chunk(chunk_number, chunk, error=error)

        if attempt < max_retries:
            print(f"  ↻ Neuer Versuch ({attempt + 1}/{max_retries})...")
            time.sleep(1)

    return None

# Funktion push_to_github entfernt, da Authentifizierung nötig wäre
# Push-Hinweise werden am Ende des Skripts ausgegeben

def check_file_exists(file_path):
    if not os.path.exists(file_path):
        error_message = f"Error: File not found - {file_path}"
        print(error_message)
        try:
            log_path = os.path.join(os.getcwd(), 'error_log.txt')
            with open(log_path, 'a', encoding='utf-8') as log_file:  # Use 'a' mode to append to the file
                log_file.write(f"[{datetime.now()}] {error_message}\n")
            print(f"Logged error to: {log_path}")
        except Exception as log_error:
            print(f"Failed to write to log file: {log_error}")
        sys.exit(1)  # Exit immediately if the file is not found

def main():
    # Reports-Verzeichnis anlegen, falls es nicht existiert
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(CHUNKS_DIR, exist_ok=True)
    os.makedirs(FAILED_CHUNKS_DIR, exist_ok=True)

    # Originaldatei laden
    input_file = 'battlebase-data-en.json'
    check_file_exists(input_file)  # Exit immediately if the file is missing

    print("\nDatei wird geladen...")
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"Gesamt: {len(data)} Einträge")

    # Initialisieren
    translated_data = []
    output_file = 'battlebase-data.json'

    # Check if output file is writable
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            pass
    except IOError:
        print(f"Error: Cannot write to output file - {output_file}")
        sys.exit(1)  # Exit immediately if the output file is not writable

    # Variablen für adaptive Chunk-Logik
    optimal_chunk_size = 14  # Nur Startgröße für den ersten Chunk-Versuch
    current_chunk_size = optimal_chunk_size
    position = 0
    chunk_number = 0

    # Bereits übersetzte IDs nachverfolgen
    translated_ids = set()

    # Einträge verarbeiten
    while position < len(data):
        chunk_number += 1

        print(f"\nTest mit {current_chunk_size} Einträgen/Chunk")

        # Chunk extrahieren
        chunk = data[position:position + current_chunk_size]

        try:
            existing_chunk = load_existing_chunk_if_valid(chunk, chunk_number)
            used_existing_chunk = existing_chunk is not None
            if used_existing_chunk:
                translated_chunk = existing_chunk
            else:
                # Chunk übersetzen
                translated_chunk = translate_chunk_with_openai(chunk, chunk_number)

            if translated_chunk is not None:
                if not used_existing_chunk:
                    save_chunk_result(chunk_number, translated_chunk)

                # Nur noch nicht vorhandene Einträge hinzufügen
                for item in translated_chunk:
                    if item['id'] not in translated_ids:
                        translated_data.append(item)
                        translated_ids.add(item['id'])

                position += len(chunk)

                # Nach jedem Chunk speichern
                merged_data = merge_chunk_files(output_file, max_chunk_number=chunk_number)
                if merged_data is None:
                    with open(output_file, 'w', encoding='utf-8') as f:
                        json.dump(translated_data, f, indent=2, ensure_ascii=False)

                print(f"  Fortschritt: {len(translated_data)}/{len(data)} Einträge übersetzt")

                # Kurze Pause zwischen Chunks
                if position < len(data):
                    time.sleep(1)
            else:
                # Übersetzung fehlgeschlagen (kein Timeout)
                print(f"  ⚠️  Übersetzung von Chunk {chunk_number} fehlgeschlagen")
                # Chunk-Größe reduzieren und erneut versuchen
                if current_chunk_size > 1:
                    current_chunk_size -= 1
                    print(f"  Reduziere Größe auf {current_chunk_size}")
                else:
                    # Wenn sogar mit 1 Eintrag fehlgeschlagen: nächsten Eintrag versuchen
                    print("  Dieser Eintrag konnte nicht übersetzt werden, nächster Eintrag")
                    position += 1

        except subprocess.TimeoutExpired:
            # Timeout erreicht
            print(f"  ⚠️  Timeout bei {current_chunk_size} Einträgen")
            if current_chunk_size > 1:
                # Größe um 1 reduzieren
                current_chunk_size -= 1
                print(f"  Reduziere Größe auf {current_chunk_size}")
                # Position bleibt gleich: derselbe Chunk mit weniger Einträgen
            else:
                # Timeout auch mit 1 Eintrag: nächsten Eintrag versuchen
                print("  Timeout auch mit 1 Eintrag, nächster Eintrag")
                position += 1

    # Abschlussprüfung und Behandlung fehlender Einträge
    print(f"\n{'='*60}")
    print("Übersetzung abgeschlossen!")
    print(f"  Originale Einträge: {len(data)}")
    print(f"  Übersetzte Einträge: {len(translated_data)}")

    if optimal_chunk_size:
        print(f"  Verwendete optimale Größe: {optimal_chunk_size} Einträge pro Chunk")

    # Fehlende Einträge prüfen, bis alles übersetzt ist oder kein Fortschritt mehr möglich ist
    max_retry_rounds = 3
    retry_round = 0

    while len(translated_data) < len(data) and retry_round < max_retry_rounds:
        retry_round += 1
        missing = len(data) - len(translated_data)
        print(f"\n{'='*60}")
        print(f"Nachholrunde {retry_round}/{max_retry_rounds}")
        print(f"  ⚠️  {missing} fehlende Einträge")
        print(f"  Übersetzte IDs: {len(translated_ids)}")
        print(f"  Einträge in translated_data: {len(translated_data)}")

        # Fehlende Einträge präzise bestimmen
        missing_entries = [item for item in data if item['id'] not in translated_ids]

        if missing_entries:
            print(f"\nNachholphase für {len(missing_entries)} Einträge...")

            # Fehlende IDs für Debug anzeigen
            print("\nFehlende Einträge:")
            apostrophe_entries = []
            for entry in missing_entries[:10]:  # Maximal 10 anzeigen
                if "'" in entry['id']:
                    print(f"  - {entry['id']} (enthält Apostroph)")
                    apostrophe_entries.append(entry)
                else:
                    print(f"  - {entry['id']}")
            if len(missing_entries) > 10:
                print(f"  ... und {len(missing_entries) - 10} weitere")

            # Einträge mit Apostrophen zuerst einzeln verarbeiten
            if apostrophe_entries:
                print(f"\nSonderbehandlung für {len(apostrophe_entries)} Einträge mit Apostrophen...")
                for entry in apostrophe_entries:
                    chunk_number += 1
                    print(f"\nEinzelübersetzung von: {entry['id']}")
                    translated_single = translate_chunk_with_openai([entry], chunk_number, max_retries=5)
                    if translated_single is not None:
                        save_chunk_result(chunk_number, translated_single)
                        added = False
                        for item in translated_single:
                            if item['id'] not in translated_ids and item['id'] == entry['id']:
                                translated_data.append(item)
                                translated_ids.add(item['id'])
                                missing_entries.remove(entry)
                                added = True
                        # Speichern
                        merged_data = merge_chunk_files(output_file, max_chunk_number=chunk_number)
                        if merged_data is None:
                            with open(output_file, 'w', encoding='utf-8') as f:
                                json.dump(translated_data, f, indent=2, ensure_ascii=False)
                        if added:
                            print("  ✓ Erfolgreich übersetzt")
                        else:
                            print("  ⚠️  Übersetzt, aber ID stimmt nicht überein")
                    else:
                        print("  ✗ Übersetzung fehlgeschlagen")
                        # Als letzte Option: manuelle Einzelübersetzung
                        print("  Manueller Übersetzungsversuch...")
                        manual_prompt = f"""Übersetze dieses Warhammer 40.000 Stratagem vollständig ins Deutsche. Gib NUR ein gültiges JSON-Objekt zurück.

{json.dumps(entry, indent=2, ensure_ascii=False)}

REGELN:
- Übersetze rekursiv jeden String-Wert in allen Feldern und Ebenen.
- Der Wert von "id" darf niemals geändert werden.
- Behalte Schlüssel, Struktur und Datentypen exakt bei.
- Bei null muss null erhalten bleiben.

Gib NUR das übersetzte JSON zurück, ohne Text davor oder danach."""
                        
                        try:
                            result = subprocess.run(
                                ['claude'],
                                input=manual_prompt,
                                capture_output=True,
                                text=True,
                                encoding='utf-8',
                                timeout=60
                            )
                            
                            if result.returncode == 0:
                                response = result.stdout.strip()
                                # Versuchen, ein einzelnes JSON-Objekt zu extrahieren
                                import re
                                json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
                                if json_match:
                                    try:
                                        translated_obj = json.loads(json_match.group())
                                        if translated_obj['id'] == entry['id']:
                                            translated_data.append(translated_obj)
                                            translated_ids.add(translated_obj['id'])
                                            missing_entries.remove(entry)
                                            save_chunk_result(chunk_number, [translated_obj])
                                            merged_data = merge_chunk_files(output_file, max_chunk_number=chunk_number)
                                            if merged_data is None:
                                                with open(output_file, 'w', encoding='utf-8') as f:
                                                    json.dump(translated_data, f, indent=2, ensure_ascii=False)
                                            print("    ✓ Manuelle Übersetzung erfolgreich!")
                                    except:
                                        print("    ✗ Manuelles JSON-Parsen fehlgeschlagen")
                        except:
                            print("    ✗ Manuelle Übersetzung fehlgeschlagen")

            # Sichere Chunk-Größe für übrigen Nachhollauf
            safe_chunk_size = min(6, optimal_chunk_size or 6)
            retry_position = 0
            
            while retry_position < len(missing_entries):
                chunk_number += 1
                # Chunk aus fehlenden Einträgen bilden
                retry_chunk = missing_entries[retry_position:retry_position + safe_chunk_size]
                
                print(f"\nÜbersetzung des Nachhol-Chunks ({len(retry_chunk)} Einträge)...")
                
                # Mit mehr Wiederholungsversuchen übersetzen
                translated_retry = translate_chunk_with_openai(retry_chunk, chunk_number, max_retries=5)
                
                if translated_retry is not None:
                    save_chunk_result(chunk_number, translated_retry)
                    # Übersetzte Einträge hinzufügen
                    for item in translated_retry:
                        if item['id'] not in translated_ids:
                            translated_data.append(item)
                            translated_ids.add(item['id'])
                    
                    # Speichern
                    merged_data = merge_chunk_files(output_file, max_chunk_number=chunk_number)
                    if merged_data is None:
                        with open(output_file, 'w', encoding='utf-8') as f:
                            json.dump(translated_data, f, indent=2, ensure_ascii=False)
                    
                    print(f"  ✓ Nachholen erfolgreich - Fortschritt: {len(translated_data)}/{len(data)}")
                    retry_position += len(retry_chunk)
                else:
                    # Bei Fehler: Größe reduzieren oder nächsten Eintrag versuchen
                    if safe_chunk_size > 1:
                        safe_chunk_size = max(1, safe_chunk_size // 2)
                        print(f"  Reduziere Nachhol-Chunk-Größe auf {safe_chunk_size}")
                    else:
                        print("  ❌ Dieser Eintrag konnte nicht übersetzt werden, nächster Eintrag")
                        retry_position += 1
            
            print("\nNach dem Nachholen:")
            print(f"  Übersetzte Einträge: {len(translated_data)}/{len(data)}")
        
        # Wenn kein Fortschritt erzielt wurde, abbrechen
        if len(missing_entries) == missing:
            print("\n⚠️  Kein Fortschritt in dieser Nachholrunde")
            break

    # Endstand speichern
    merged_data = merge_chunk_files(output_file, max_chunk_number=chunk_number)
    if merged_data is not None:
        translated_data = merged_data
    else:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(translated_data, f, indent=2, ensure_ascii=False)
    
    # Abschlussprüfung unter Berücksichtigung möglicher Duplikate
    print("\n" + "="*60)
    print("Abschlussprüfung...")
    
    # Ausgabedatei laden und enthaltene IDs prüfen
    with open(output_file, 'r', encoding='utf-8') as f:
        output_data = json.load(f)
    
    # Menge aller IDs in der Ausgabedatei
    output_ids_raw = {item['id'] for item in output_data}
    
    # Menge aller Original-IDs
    original_ids = {item['id'] for item in data}
    
    # Fehlende Original-IDs ermitteln
    missing_ids = original_ids - output_ids_raw
    
    # Fehlende Originaleinträge sammeln
    truly_missing_entries = []
    for item in data:
        if item['id'] in missing_ids:
            truly_missing_entries.append(item)
    
    print(f"  Einträge in der Originaldatei: {len(data)}")
    print(f"  Eindeutig übersetzte Einträge: {len(translated_data)}")
    print(f"  Einträge in der Ausgabedatei: {len(output_data)}")
    
    # Doppelte IDs in der Ausgabedatei identifizieren
    duplicate_entries = []
    seen_ids = set()
    for item in output_data:
        item_id = item['id']
        if item_id in seen_ids:
            duplicate_entries.append(item)
        else:
            seen_ids.add(item_id)

    duplicates_count = len(duplicate_entries)
    if duplicates_count > 0:
        print(f"  ℹ️  Hinweis: {duplicates_count} Einträge mit doppelter ID in der Ausgabedatei gefunden")
        print(f"\n  📋 Liste der {duplicates_count} Duplikate:")
        for i, entry in enumerate(duplicate_entries, 1):
            print(f"     {i}. {entry['id']}")

        # Duplikate in Datei speichern
        with open('reports/duplicate_entries.json', 'w', encoding='utf-8') as f:
            json.dump(duplicate_entries, f, indent=2, ensure_ascii=False)
        print("\n  💾 Vollständige Liste in reports/duplicate_entries.json gespeichert")
    
    print(f"\n  Tatsächlich fehlende Einträge: {len(truly_missing_entries)}")
    
    # Zusammenfassungsbericht erstellen
    report_data = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_entries": len(data),
        "translated_entries": len(output_data),
        "missing_entries": len(truly_missing_entries),
        "duplicate_entries": len(duplicate_entries) if 'duplicate_entries' in locals() else 0,
        "translation_complete": len(truly_missing_entries) == 0
    }
    
    with open('reports/translation_report.json', 'w', encoding='utf-8') as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)
    
    # Endergebnis
    if len(truly_missing_entries) == 0:
        print("\n✅ Alle Einträge wurden erfolgreich übersetzt!")
        print("\n📋 Für die Erstellung einer Pull Request:")
        print(f"   1. git checkout -b new_translation_{datetime.now().strftime('%Y_%m_%d')}")
        print("   2. git add battlebase-data.json")
        print(f"   3. git commit -m \"Automatische Übersetzung vom {datetime.now().strftime('%Y_%m_%d')}\"")
        print("   4. git push -u origin <branch_name>")
        print("   5. PR auf GitHub erstellen")
        print("\n📁 Berichte in ./reports/ erzeugt")
        print("   - translation_report.json : Zusammenfassungsbericht")
        if report_data["duplicate_entries"] > 0:
            print("   - duplicate_entries.json : Liste der Duplikate")
    else:
        print(f"\n⚠️  {len(truly_missing_entries)} Einträge konnten nach {retry_round} Nachholrunden nicht übersetzt werden")
        print("⚠️  Die Übersetzung ist nicht vollständig")
        
        # IDs der tatsächlich nicht übersetzten Einträge speichern
        untranslated_ids = [item['id'] for item in truly_missing_entries]
        with open('reports/untranslated_ids.txt', 'w', encoding='utf-8') as f:
            if untranslated_ids:
                f.write('\n'.join(untranslated_ids))
                print(f"   {len(untranslated_ids)} nicht übersetzte IDs wurden in reports/untranslated_ids.txt gespeichert")
                # Erste fehlende IDs anzeigen
                print("\n   Beispiele fehlender IDs:")
                for id in untranslated_ids[:5]:
                    print(f"     - {id}")
                if len(untranslated_ids) > 5:
                    print(f"     ... und {len(untranslated_ids) - 5} weitere")
            else:
                f.write("Keine nicht übersetzten IDs gefunden")
                print("   ✅ Keine tatsächlich fehlenden IDs")
        
        # Vollständige nicht übersetzte Einträge speichern
        if truly_missing_entries:
            with open('reports/missing_entries.json', 'w', encoding='utf-8') as f:
                json.dump(truly_missing_entries, f, indent=2, ensure_ascii=False)
            print("   💾 Vollständige Einträge in reports/missing_entries.json gespeichert")
        
        print("\n📁 Berichte in ./reports/ erzeugt")
        print("   - translation_report.json : Zusammenfassungsbericht")
        print("   - untranslated_ids.txt : Liste nicht übersetzter IDs")
        print("   - missing_entries.json : Vollständige nicht übersetzte Einträge")
        if report_data["duplicate_entries"] > 0:
            print("   - duplicate_entries.json : Liste der Duplikate")

if __name__ == "__main__":
    main()
