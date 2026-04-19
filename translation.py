#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import subprocess
import requests
import sys
import time
from datetime import datetime

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

def translate_chunk_with_claude(chunk, chunk_number, max_retries=3):
    """Übersetzt einen Chunk mit Claude inkl. automatischer Wiederholungsversuche."""
    print(f"\nÜbersetzung von Chunk {chunk_number} ({len(chunk)} Einträge)...")
    
    # Aktuell kein Vorprozessing nötig
    processed_chunk = chunk
    id_mapping = {}
    
    # Prompt erstellen
    prompt = """Übersetze das folgende JSON vollständig ins Deutsche und gib ausschließlich das übersetzte JSON-Array zurück.

KRITISCHE REGELN:
1. Die Antwort MUSS direkt mit [ beginnen (kein Text davor).
2. Die Antwort MUSS mit ] enden (kein Text danach).
3. Niemals Erklärungen, Kommentare oder sonstigen Zusatztext ausgeben.
4. Die JSON-Struktur exakt beibehalten (Objekte, Arrays, Reihenfolge, Schlüssel).
5. Jeder String-Wert muss rekursiv ins Deutsche übersetzt werden – in allen Feldern und Ebenen.
6. Ausnahme: Der Wert von Schlüsseln mit Namen "id" darf niemals verändert werden.
7. Schlüssel-Namen, Zahlen, Booleans und null müssen unverändert bleiben.
8. Datentypen dürfen nicht geändert werden.
9. Kontext: Warhammer-40.000-Regeln, nutze passendes Fachvokabular.

BEISPIEL KORREKTE ANTWORT:
[{"id":"abc123","body":"Ins Deutsche übersetzter Text","name":"Übersetzter Name"}]

BEISPIEL FALSCHE ANTWORT:
Hier ist die Übersetzung: [{"id":"abc123","body":"..."}]

ZU ÜBERSETZENDES JSON:
"""
    
    chunk_json = json.dumps(processed_chunk, indent=2, ensure_ascii=False)
    full_prompt = prompt + chunk_json
    
    for attempt in range(max_retries):
        try:
            # Claude aufrufen
            if attempt > 0:
                print(f"  Versuch {attempt + 1}/{max_retries}...")
            
            result = subprocess.run(
                ['claude'],
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=120  # Timeout: 2 Minuten
            )
            
            if result.returncode != 0:
                print(f"  Claude-Fehler (Code {result.returncode})")
                print(f"  Stderr: {result.stderr}")
                if result.stdout:
                    print(f"  Stdout: {result.stdout}")
                continue
            
            response = result.stdout.strip()

            if not (response.startswith('[') and response.endswith(']')):
                if attempt < max_retries - 1:
                    print("  ✗ Ungültiges Antwortformat: Muss mit [ beginnen und mit ] enden. Neuer Versuch...")
                    continue
                print("  ✗ Ungültiges Antwortformat nach allen Versuchen")
                return None
            
            # JSON aus Antwort extrahieren
            translated_chunk, extracted_json = extract_json_from_response(response)
            
            if translated_chunk:
                # Originale IDs bei Bedarf wiederherstellen
                if id_mapping:
                    translated_chunk = postprocess_translated_chunk(translated_chunk, id_mapping)
                    print("  ✓ Original-IDs wiederhergestellt")
                
                print("  ✓ Chunk erfolgreich übersetzt")
                # Prüfen, ob die Anzahl der Einträge unverändert blieb
                if len(translated_chunk) != len(chunk):
                    print(f"  ⚠️  Achtung: {len(chunk)} Einträge gesendet, {len(translated_chunk)} empfangen")
                    # Fehlende IDs anzeigen
                    sent_ids = {item['id'] for item in chunk}
                    received_ids = {item['id'] for item in translated_chunk}
                    missing_ids = sent_ids - received_ids
                    if missing_ids:
                        print(f"  Fehlende IDs in der Antwort: {missing_ids}")
                    if attempt < max_retries - 1:
                        print("  Neuer Versuch...")
                        continue

                sent_ids = {item['id'] for item in chunk}
                received_ids = {item['id'] for item in translated_chunk}
                if sent_ids != received_ids:
                    if attempt < max_retries - 1:
                        print(f"  ⚠️  ID-Menge geändert (gesendet={len(sent_ids)}, empfangen={len(received_ids)}). Neuer Versuch...")
                        continue
                    print("  ✗ ID-Menge nach allen Versuchen verändert")
                    return None

                translated_by_id = {item['id']: item for item in translated_chunk}
                # Sobald ein Problem gefunden wird, löst "break" einen erneuten Versuch aus.
                # Der nachfolgende for-else-Block ist der eigentliche Erfolgsfall:
                # Er läuft nur, wenn diese Schleife komplett ohne "break" endet.
                for source_item in chunk:
                    source_id = source_item['id']
                    translated_item = translated_by_id.get(source_id)
                    if translated_item is None:
                        if attempt < max_retries - 1:
                            print(f"  ⚠️  Eintrag mit ID '{source_id}' fehlt. Neuer Versuch...")
                            break
                        print(f"  ✗ Eintrag mit ID '{source_id}' fehlt nach allen Versuchen")
                        return None
                    is_valid, error = validate_translated_item_structure(source_item, translated_item)
                    if not is_valid:
                        if attempt < max_retries - 1:
                            print(f"  ⚠️  Strukturprüfung fehlgeschlagen: {error}. Neuer Versuch...")
                            break
                        print(f"  ✗ Strukturprüfung fehlgeschlagen: {error}")
                        return None
                # for-else: nur wenn kein "break" im Loop ausgelöst wurde, ist der Chunk gültig
                else:
                    return translated_chunk
            else:
                if attempt < max_retries - 1:
                    print("  ✗ Konnte kein gültiges JSON extrahieren, neuer Versuch...")
                    # Vollständige Antwort für Debug ausgeben
                    print("  Claude-Antwort:")
                    print("-" * 60)
                    print(response[:500] + "..." if len(response) > 500 else response)
                    print("-" * 60)
                else:
                    print(f"  ✗ Konnte nach {max_retries} Versuchen kein gültiges JSON extrahieren")
                    if "Execution error" in response or "error" in response.lower():
                        print("  Claude hat einen Fehler gemeldet. Chunk-Größe wird reduziert.")
                        return None  # None zurückgeben, damit das Skript mit kleinerem Chunk weitermacht
                    else:
                        print("  Vollständige Claude-Antwort:")
                        print("-" * 60)
                        print(response)
                        print("-" * 60)
                        # Problematische Antwort für Debug speichern
                        debug_file = f"debug_chunk_{chunk_number}_attempt_{attempt}.txt"
                        with open(debug_file, 'w', encoding='utf-8') as f:
                            f.write(f"Prompt:\n{full_prompt}\n\n")
                            f.write(f"Antwort:\n{response}")
                        print(f"  Antwort in {debug_file} gespeichert")
                        print("\nSkript wird beendet.")
                        sys.exit(1)
                    
        except subprocess.TimeoutExpired:
            print("  ✗ Timeout: Claude hat innerhalb von 2 Minuten nicht geantwortet")
            raise  # Ausnahme erneut werfen, damit sie weiter oben behandelt wird
        except Exception as e:
            print(f"  ✗ Fehler: {type(e).__name__}: {e}")
            if attempt < max_retries - 1:
                print("  Neuer Versuch...")
            else:
                print(f"  ✗ Fehlgeschlagen nach {max_retries} Versuchen")
                # Debug: IDs des Chunks ausgeben, um Apostroph-Probleme zu erkennen
                ids_in_chunk = [item.get('id', 'NO_ID') for item in chunk]
                if any("'" in id for id in ids_in_chunk):
                    print("  ⚠️  Dieser Chunk enthält IDs mit Apostrophen:")
                    for id in ids_in_chunk:
                        if "'" in id:
                            print(f"     - {id}")
                return None
    
    return None

# Funktion push_to_github entfernt, da Authentifizierung nötig wäre
# Push-Hinweise werden am Ende des Skripts ausgegeben

def main():
    # Reports-Verzeichnis anlegen, falls es nicht existiert
    import os
    os.makedirs('reports', exist_ok=True)
    
    # Originaldatei laden
    print("\nDatei wird geladen...")
    with open('battlebase-data-en.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"Gesamt: {len(data)} Einträge")
    
    # Initialisieren
    translated_data = []
    output_file = 'battlebase-data.json'
    
    # Variablen für adaptive Chunk-Logik
    current_chunk_size = 18  # Start mit 18 Einträgen
    optimal_chunk_size = None  # Gefundene optimale Chunk-Größe
    position = 0
    chunk_number = 0
    
    # Bereits übersetzte IDs nachverfolgen
    translated_ids = set()
    
    # Einträge verarbeiten
    while position < len(data):
        chunk_number += 1
        
        # Wenn eine optimale Größe existiert und nicht kürzlich timeoutete, diese verwenden
        if optimal_chunk_size is not None and current_chunk_size >= optimal_chunk_size:
            current_chunk_size = optimal_chunk_size
            
            # Verbleibende Chunks berechnen
            entries_remaining = len(data) - position
            chunks_remaining = (entries_remaining + current_chunk_size - 1) // current_chunk_size
            estimated_time_seconds = chunks_remaining * 120  # 1 Chunk ~= 2 Minuten
            
            # Ausgabeformat hh:mm:ss
            hours = estimated_time_seconds // 3600
            minutes = (estimated_time_seconds % 3600) // 60
            seconds = estimated_time_seconds % 60
            
            print(f"\n{'='*60}")
            print(f"Verwendung der optimalen Größe: {current_chunk_size} Einträge/Chunk")
            print(f"Verbleibende Chunks: {chunks_remaining}")
            print(f"Geschätzte Zeit: ~{hours:02d}:{minutes:02d}:{seconds:02d}")
            print(f"{'='*60}")
        else:
            print(f"\nTest mit {current_chunk_size} Einträgen/Chunk")
        
        # Chunk extrahieren
        chunk = data[position:position + current_chunk_size]
        
        try:
            # Chunk übersetzen
            translated_chunk = translate_chunk_with_claude(chunk, chunk_number)
            
            if translated_chunk:
                # Nur noch nicht vorhandene Einträge hinzufügen
                for item in translated_chunk:
                    if item['id'] not in translated_ids:
                        translated_data.append(item)
                        translated_ids.add(item['id'])
                
                position += len(chunk)
                
                # Optimale Chunk-Größe aktualisieren
                if optimal_chunk_size is None or current_chunk_size < optimal_chunk_size:
                    optimal_chunk_size = current_chunk_size
                    print(f"  ✅ Optimale Größe {'bestätigt' if optimal_chunk_size == current_chunk_size else 'aktualisiert'}: {optimal_chunk_size} Einträge/Chunk")
                
                # Nach jedem Chunk speichern
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
                # Falls die optimale Größe timeoutete, anpassen
                if optimal_chunk_size and optimal_chunk_size > current_chunk_size:
                    optimal_chunk_size = current_chunk_size
                    print(f"  Optimale Größe aktualisiert auf {optimal_chunk_size}")
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
                    translated_single = translate_chunk_with_claude([entry], chunk_number, max_retries=5)
                    if translated_single:
                        added = False
                        for item in translated_single:
                            if item['id'] not in translated_ids and item['id'] == entry['id']:
                                translated_data.append(item)
                                translated_ids.add(item['id'])
                                missing_entries.remove(entry)
                                added = True
                        # Speichern
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
                translated_retry = translate_chunk_with_claude(retry_chunk, chunk_number, max_retries=5)
                
                if translated_retry:
                    # Übersetzte Einträge hinzufügen
                    for item in translated_retry:
                        if item['id'] not in translated_ids:
                            translated_data.append(item)
                            translated_ids.add(item['id'])
                    
                    # Speichern
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
