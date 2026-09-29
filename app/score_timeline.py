from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict


TIMELINE_VERSION = 3
TICKS_PER_QUARTER = 960
STANDARD_GUITAR_TUNING = [64, 59, 55, 50, 45, 40]
TOKEN_RE = re.compile(r"\{[^}]*\}|\([^)]*\)|:[0-9]+|[0-9]+\.[0-9]+|\br\b")


class ScoreParseError(ValueError):
    pass


def score_revision(event_version: int, source_hash: str | None) -> str:
    return f"v{event_version}:{source_hash or 'unprocessed'}"


def source_hash(*, format: str, content: str | None, storage_key: str | None,
                default_bpm: int, time_signature: str) -> str:
    payload = {
        "format": format,
        "content": content,
        "storageKey": storage_key,
        "defaultBpm": default_bpm,
        "timeSignature": time_signature,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def generate_from_content(*, format: str, content: str | None, default_bpm: int,
                          time_signature: str, storage_key: str | None = None) -> dict:
    if format != "ALPHA_TEX":
        raise ScoreParseError(f"Score event generation from {format} content is not supported")
    if not content:
        raise ScoreParseError("ALPHA_TEX score content is required to generate score events")
    return parse_alpha_tex(content, default_bpm=default_bpm, time_signature=time_signature)


def generate_from_bytes(*, format: str, data: bytes, default_bpm: int,
                        time_signature: str) -> dict:
    if not data:
        raise ScoreParseError("Uploaded score file is empty")
    if format == "ALPHA_TEX":
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ScoreParseError("ALPHA_TEX file must be UTF-8 text") from error
        return parse_alpha_tex(content, default_bpm=default_bpm, time_signature=time_signature)
    if format == "MUSIC_XML":
        return parse_musicxml(data, default_bpm=default_bpm, time_signature=time_signature)
    if format == "GUITAR_PRO":
        raise ScoreParseError(
            "Binary Guitar Pro parsing is not included in this Python learning port. "
            "Export the score as MusicXML or alphaTex to continue."
        )
    raise ScoreParseError(f"Unsupported score format: {format}")


def _pitch(string_number: int, fret: int, tuning: list[int]) -> int:
    if not 1 <= string_number <= len(tuning):
        raise ScoreParseError(f"Guitar string {string_number} is outside the track tuning")
    midi = tuning[string_number - 1] + fret
    if not 0 <= midi <= 127:
        raise ScoreParseError("Score contains a note outside the MIDI pitch range")
    return midi


def _parse_note(token: str, tuning: list[int]) -> dict:
    match = re.fullmatch(r"(\d+)\.(\d+)", token.strip())
    if not match:
        raise ScoreParseError(f"Unrecognized alphaTex note: {token}")
    fret, string_number = int(match.group(1)), int(match.group(2))
    if fret < 0 or fret > 36:
        raise ScoreParseError("Fret number must be between 0 and 36")
    return {"midi": _pitch(string_number, fret, tuning), "string": string_number, "fret": fret}


def _bend_data(note: dict, modifier: str, duration_ticks: int) -> tuple[dict, dict | None] | None:
    match = re.search(r"\bb\s*\(([^)]*)\)", modifier, re.IGNORECASE)
    if not match:
        return None
    try:
        values = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", match.group(1))]
    except ValueError as error:
        raise ScoreParseError("Bend points must be numeric") from error
    if len(values) < 2 or len(values) % 2:
        raise ScoreParseError("A bend requires offset/value pairs")
    points = [
        {"offsetTicks": round(duration_ticks * values[index] / 60), "semitones": values[index + 1] / 4}
        for index in range(0, len(values), 2)
    ]
    if points[0]["offsetTicks"] != 0:
        points.insert(0, {"offsetTicks": 0, "semitones": 0})
    target = {
        "type": "BEND",
        "bendType": "BEND_RELEASE" if points[-1]["semitones"] == 0 and max(p["semitones"] for p in points) > 0 else "BEND",
        "style": "DEFAULT",
        "points": points,
    }
    peak = max(point["semitones"] for point in points)
    if peak <= 0:
        return None
    target["targetSemitones"] = peak
    if any(not float(note["midi"] + point["semitones"]).is_integer() for point in points):
        note.setdefault("effects", []).append(target)
        return target, None
    trajectory = {
        "type": "BEND_RELEASE" if target["bendType"] == "BEND_RELEASE" else "BEND",
        "points": [
            {"offsetTicks": point["offsetTicks"], "targetMidi": note["midi"] + point["semitones"]}
            for point in points
        ],
    }
    if target["bendType"] == "BEND_RELEASE":
        peak_offsets = [point["offsetTicks"] for point in trajectory["points"] if point["targetMidi"] == note["midi"] + peak]
        if len(peak_offsets) > 1 and peak_offsets[-1] > peak_offsets[0]:
            trajectory["requiredHoldTicks"] = peak_offsets[-1] - peak_offsets[0]
    note.setdefault("effects", []).append(target)
    return target, trajectory


def parse_alpha_tex(content: str, *, default_bpm: int, time_signature: str = "4/4") -> dict:
    title_match = re.search(r'^\s*\\title\s+"([^"]*)"', content, re.MULTILINE)
    tempo_match = re.search(r"^\s*\\tempo\s+(\d+)", content, re.MULTILINE)
    if tempo_match and int(tempo_match.group(1)) > 0:
        default_bpm = int(tempo_match.group(1))
    body_match = re.search(r"^\s*\.\s*$", content, re.MULTILINE)
    body = content[body_match.end():] if body_match else content
    body = re.sub(r"\\(?:title|tempo|tuning|capo)\b[^\n]*", " ", body)
    bars = [segment.strip() for segment in body.split("|") if segment.strip()]
    if not bars:
        raise ScoreParseError("alphaTex score contains no measures")

    try:
        numerator, denominator = (int(value) for value in time_signature.split("/", 1))
    except (ValueError, AttributeError):
        numerator, denominator = 4, 4
        time_signature = "4/4"
    beat_ticks = TICKS_PER_QUARTER * 4 // denominator
    measure_ticks = numerator * beat_ticks
    events: list[dict] = []
    event_index = 0
    for measure_index, bar in enumerate(bars, start=1):
        cursor = 0
        duration_denominator = 4
        last_event: dict | None = None
        local_event = 0
        for match in TOKEN_RE.finditer(bar):
            token = match.group(0).strip()
            if token.startswith(":"):
                duration_denominator = max(1, int(token[1:]))
                continue
            duration = max(1, TICKS_PER_QUARTER * 4 // duration_denominator)
            if token.startswith("{"):
                if last_event is None:
                    continue
                modifier = token[1:-1].strip()
                modifier_lower = modifier.lower()
                if modifier_lower in {"sd", "su"}:
                    effect = {"type": "PICK_STROKE", "direction": "DOWN" if modifier_lower == "sd" else "UP"}
                    last_event.setdefault("effects", []).append(effect)
                    last_event.setdefault("techniques", []).append("PICK_STROKE")
                if re.search(r"\bpm\b", modifier_lower):
                    for note in last_event["notes"]:
                        note.setdefault("effects", []).append({"type": "PALM_MUTE"})
                    last_event.setdefault("techniques", []).append("PALM_MUTE")
                tremolo = re.search(r"\btp\s*(\d+)?", modifier_lower)
                if tremolo:
                    last_event.setdefault("effects", []).append({
                        "type": "TREMOLO_PICKING", "marks": int(tremolo.group(1) or 1), "style": "DEFAULT"
                    })
                    last_event.setdefault("techniques", []).append("TREMOLO_PICKING")
                if "{au}" in token.lower() or re.search(r"\bau\b", modifier_lower):
                    last_event.setdefault("effects", []).append({"type": "ARPEGGIO", "direction": "UP"})
                    last_event.setdefault("techniques", []).append("ARPEGGIO")
                if "{ad}" in token.lower() or re.search(r"\bad\b", modifier_lower):
                    last_event.setdefault("effects", []).append({"type": "ARPEGGIO", "direction": "DOWN"})
                    last_event.setdefault("techniques", []).append("ARPEGGIO")
                for note in last_event["notes"]:
                    bend = _bend_data(note, modifier, last_event["durationTicks"])
                    if bend:
                        _, technique = bend
                        if technique and not last_event.get("technique"):
                            last_event["technique"] = technique
                        last_event.setdefault("techniques", []).append("BEND")
                continue

            if token == "r":
                notes: list[dict] = []
            elif token.startswith("("):
                notes = [_parse_note(value, STANDARD_GUITAR_TUNING) for value in re.findall(r"\d+\.\d+", token)]
            else:
                notes = [_parse_note(token, STANDARD_GUITAR_TUNING)]
            beat_number = cursor // beat_ticks + 1
            event = {
                "id": f"m{measure_index}-b{beat_number}-t0-v0-e{local_event}",
                "measure": measure_index,
                "beat": beat_number,
                "startTick": (measure_index - 1) * measure_ticks + cursor,
                "durationTicks": duration,
                "ticksPerQuarter": TICKS_PER_QUARTER,
                "trackIndex": 0,
                "trackId": "t0",
                "voiceIndex": 0,
                "pitches": [note["midi"] for note in notes],
                "notes": notes,
                "isRest": not notes,
            }
            events.append(event)
            last_event = event
            cursor += duration
            local_event += 1
            event_index += 1

    for event in events:
        if "techniques" in event:
            event["techniques"] = list(dict.fromkeys(event["techniques"]))
    timeline = {
        "version": TIMELINE_VERSION,
        "ticksPerQuarter": TICKS_PER_QUARTER,
        "totalTicks": max(len(bars) * measure_ticks, max((e["startTick"] + e["durationTicks"] for e in events), default=0)),
        "totalMeasures": len(bars),
        "timeSignature": time_signature,
        "defaultBpm": default_bpm,
        "tracks": [{
            "id": "t0", "index": 0, "name": (title_match.group(1).strip() if title_match else "Guitar"),
            "instrument": "GUITAR", "tuning": STANDARD_GUITAR_TUNING,
        }],
        "events": events,
    }
    validate_timeline(timeline)
    return timeline


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if _tag(item) == name), None)


def _descendants(element: ET.Element, name: str):
    return (item for item in element.iter() if _tag(item) == name)


def _musicxml_pitch(note: ET.Element) -> int | None:
    pitch = _child(note, "pitch")
    if pitch is None:
        return None
    step_el, octave_el = _child(pitch, "step"), _child(pitch, "octave")
    if step_el is None or octave_el is None or not step_el.text or not octave_el.text:
        return None
    steps = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    alter_el = _child(pitch, "alter")
    alter = int(float(alter_el.text)) if alter_el is not None and alter_el.text else 0
    return (int(octave_el.text) + 1) * 12 + steps[step_el.text.upper()] + alter


def parse_musicxml(data: bytes, *, default_bpm: int, time_signature: str = "4/4") -> dict:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ScoreParseError("MusicXML is not valid XML") from error
    if _tag(root) != "score-partwise":
        raise ScoreParseError("This parser supports partwise MusicXML scores")

    part_names: dict[str, str] = {}
    part_list = _child(root, "part-list")
    if part_list is not None:
        for part in _descendants(part_list, "score-part"):
            part_id = part.attrib.get("id")
            name = _child(part, "part-name")
            if part_id:
                part_names[part_id] = (name.text or "").strip() if name is not None else ""

    parts = [item for item in root if _tag(item) == "part"]
    if not parts:
        raise ScoreParseError("MusicXML score contains no parts")
    tracks: list[dict] = []
    events: list[dict] = []
    total_measures = 0
    parsed_time_signature = time_signature
    first_tempo: int | None = None

    for track_index, part in enumerate(parts):
        part_id = part.attrib.get("id", str(track_index))
        name = part_names.get(part_id) or f"Track {track_index + 1}"
        instrument = "BASS" if "bass" in name.lower() else "GUITAR" if "guitar" in name.lower() else "OTHER"
        tuning = STANDARD_GUITAR_TUNING.copy()
        track_events: list[dict] = []
        divisions = 1
        measure_start = 0
        part_measure_count = 0
        for measure_index, measure in enumerate((node for node in part if _tag(node) == "measure"), start=1):
            part_measure_count = measure_index
            attributes = _child(measure, "attributes")
            if attributes is not None:
                divisions_el = _child(attributes, "divisions")
                if divisions_el is not None and divisions_el.text:
                    divisions = max(1, int(divisions_el.text))
                time_el = _child(attributes, "time")
                if time_el is not None:
                    beats, beat_type = _child(time_el, "beats"), _child(time_el, "beat-type")
                    if beats is not None and beat_type is not None and beats.text and beat_type.text:
                        parsed_time_signature = f"{beats.text}/{beat_type.text}"
            numerator, denominator = (int(value) for value in parsed_time_signature.split("/", 1))
            measure_ticks = numerator * TICKS_PER_QUARTER * 4 // denominator
            cursor = 0
            last_onset = 0
            group_by_onset: dict[tuple[int, int], dict] = {}
            max_duration = measure_ticks
            for child in measure:
                kind = _tag(child)
                if kind == "backup":
                    duration = _child(child, "duration")
                    if duration is not None and duration.text:
                        cursor = max(0, cursor - round(int(duration.text) * TICKS_PER_QUARTER / divisions))
                elif kind == "forward":
                    duration = _child(child, "duration")
                    if duration is not None and duration.text:
                        cursor += round(int(duration.text) * TICKS_PER_QUARTER / divisions)
                elif kind == "note":
                    chord = _child(child, "chord") is not None
                    onset = last_onset if chord else cursor
                    if not chord:
                        last_onset = onset
                    duration_el = _child(child, "duration")
                    duration = round(int(duration_el.text) * TICKS_PER_QUARTER / divisions) if duration_el is not None and duration_el.text else TICKS_PER_QUARTER
                    voice_el = _child(child, "voice")
                    voice = int(voice_el.text) - 1 if voice_el is not None and voice_el.text and voice_el.text.isdigit() else 0
                    key = (onset, voice)
                    event = group_by_onset.get(key)
                    if event is None:
                        beat_ticks = TICKS_PER_QUARTER * 4 // denominator
                        beat_number = onset // beat_ticks + 1
                        event = {
                            "id": f"m{measure_index}-b{beat_number}-t{track_index}-v{voice}-e{len(group_by_onset)}",
                            "measure": measure_index,
                            "beat": beat_number,
                            "startTick": measure_start + onset,
                            "durationTicks": duration,
                            "ticksPerQuarter": TICKS_PER_QUARTER,
                            "trackIndex": track_index,
                            "trackId": f"t{track_index}",
                            "voiceIndex": voice,
                            "pitches": [],
                            "notes": [],
                            "isRest": True,
                        }
                        group_by_onset[key] = event
                    event["durationTicks"] = max(event["durationTicks"], duration)
                    if _child(child, "rest") is None:
                        midi = _musicxml_pitch(child)
                        if midi is not None:
                            event["pitches"].append(midi)
                            event["notes"].append({"midi": midi})
                            event["isRest"] = False
                    max_duration = max(max_duration, onset + duration)
                    if not chord:
                        cursor += duration
                elif kind == "direction":
                    sound = _child(child, "sound")
                    if sound is not None and sound.attrib.get("tempo"):
                        try:
                            first_tempo = first_tempo or round(float(sound.attrib["tempo"]))
                        except ValueError:
                            pass
                    for metronome in _descendants(child, "metronome"):
                        per_minute = _child(metronome, "per-minute")
                        if per_minute is not None and per_minute.text:
                            try:
                                first_tempo = first_tempo or round(float(per_minute.text))
                            except ValueError:
                                pass
            track_events.extend(group_by_onset.values())
            measure_start += max(measure_ticks, max_duration)
        total_measures = max(total_measures, part_measure_count)
        tracks.append({
            "id": f"t{track_index}", "index": track_index, "name": name,
            "instrument": instrument, "tuning": tuning,
        })
        events.extend(track_events)

    if first_tempo and first_tempo > 0:
        default_bpm = first_tempo
    timeline = {
        "version": TIMELINE_VERSION,
        "ticksPerQuarter": TICKS_PER_QUARTER,
        "totalTicks": max((event["startTick"] + event["durationTicks"] for event in events), default=0),
        "totalMeasures": total_measures,
        "timeSignature": parsed_time_signature,
        "defaultBpm": default_bpm,
        "tracks": tracks,
        "events": events,
    }
    timeline["events"].sort(key=lambda event: (event["startTick"], event["trackIndex"], event["voiceIndex"]))
    validate_timeline(timeline)
    return timeline


def validate_timeline(timeline: dict) -> None:
    events = timeline.get("events")
    if not isinstance(events, list) or timeline.get("version", 0) < 1:
        raise ScoreParseError("Generated score timeline is invalid")
    for event in events:
        technique = event.get("technique")
        if not technique:
            continue
        points = technique.get("points", [])
        if not event.get("pitches") or not points or points[0].get("targetMidi") != event["pitches"][0]:
            raise ScoreParseError(f"Bend event {event['id']} has no valid starting pitch")
        previous = -1
        for point in points:
            offset, midi = point.get("offsetTicks"), point.get("targetMidi")
            if not isinstance(offset, int) or offset < previous or offset < 0 or offset > event["durationTicks"]:
                raise ScoreParseError(f"Bend event {event['id']} has an invalid point offset")
            if not isinstance(midi, (int, float)) or not 0 <= midi <= 127:
                raise ScoreParseError(f"Bend event {event['id']} has an invalid target pitch")
            previous = offset
