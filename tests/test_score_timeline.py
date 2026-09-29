from __future__ import annotations

import pytest

from app.score_timeline import (
    TICKS_PER_QUARTER,
    TIMELINE_VERSION,
    ScoreParseError,
    generate_from_bytes,
    generate_from_content,
    parse_alpha_tex,
    parse_musicxml,
    score_revision,
    source_hash,
    validate_timeline,
)


def test_score_revision():
    assert score_revision(3, "abc") == "v3:abc"
    assert score_revision(0, None) == "v0:unprocessed"


class TestSourceHash:
    base = dict(format="ALPHA_TEX", content="x", storage_key=None, default_bpm=60, time_signature="4/4")

    def test_is_deterministic_sha256(self):
        digest = source_hash(**self.base)
        assert digest == source_hash(**self.base)
        assert len(digest) == 64

    @pytest.mark.parametrize(
        "change", [{"content": "y"}, {"default_bpm": 61}, {"time_signature": "3/4"}, {"storage_key": "k"}]
    )
    def test_changes_with_any_input(self, change):
        assert source_hash(**self.base) != source_hash(**{**self.base, **change})


class TestAlphaTex:
    def test_single_notes_positions_and_pitches(self):
        timeline = parse_alpha_tex(":4 1.6 2.6 3.6 4.6 | 0.1 |", default_bpm=80)
        events = timeline["events"]
        assert [e["pitches"] for e in events] == [[41], [42], [43], [44], [64]]
        assert [e["startTick"] for e in events] == [0, 960, 1920, 2880, 3840]
        assert [e["beat"] for e in events[:4]] == [1, 2, 3, 4]
        assert events[4]["measure"] == 2
        assert events[4]["id"] == "m2-b1-t0-v0-e0"
        assert timeline["totalMeasures"] == 2
        assert timeline["totalTicks"] == 2 * 4 * TICKS_PER_QUARTER
        assert timeline["version"] == TIMELINE_VERSION
        assert timeline["defaultBpm"] == 80
        assert timeline["tracks"][0]["name"] == "Guitar"

    def test_header_title_and_tempo(self):
        content = '\\title "Spider"\n\\tempo 96\n.\n:4 1.6 |'
        timeline = parse_alpha_tex(content, default_bpm=60)
        assert timeline["defaultBpm"] == 96
        assert timeline["tracks"][0]["name"] == "Spider"
        assert len(timeline["events"]) == 1

    def test_durations_chords_and_rests(self):
        timeline = parse_alpha_tex(":8 (0.4 1.3 0.2) r :2 5.1 |", default_bpm=60)
        chord, rest, half = timeline["events"]
        assert chord["pitches"] == [50, 56, 59]
        assert chord["durationTicks"] == 480
        assert rest["isRest"] and rest["pitches"] == [] and rest["startTick"] == 480
        assert half["durationTicks"] == 1920 and half["startTick"] == 960

    def test_effects(self):
        timeline = parse_alpha_tex(":8 1.6 {sd} 2.6 {su} (0.4 1.3) {pm} 3.6 {tp 2} |", default_bpm=60)
        down, up, muted, tremolo = timeline["events"]
        assert down["effects"] == [{"type": "PICK_STROKE", "direction": "DOWN"}]
        assert up["effects"] == [{"type": "PICK_STROKE", "direction": "UP"}]
        assert muted["techniques"] == ["PALM_MUTE"]
        assert all(note["effects"] == [{"type": "PALM_MUTE"}] for note in muted["notes"])
        assert tremolo["effects"][0] == {"type": "TREMOLO_PICKING", "marks": 2, "style": "DEFAULT"}

    def test_bend_release_with_explicit_points(self):
        timeline = parse_alpha_tex(":4 5.2{b (0 0 30 8 60 0)} |", default_bpm=60)
        event = timeline["events"][0]
        assert event["pitches"] == [64]
        assert event["techniques"] == ["BEND"]
        assert event["technique"]["type"] == "BEND_RELEASE"
        assert [p["targetMidi"] for p in event["technique"]["points"]] == [64, 66, 64]
        assert [p["offsetTicks"] for p in event["technique"]["points"]] == [0, 480, 960]

    @pytest.mark.xfail(
        strict=True,
        reason="Bug: a bend whose first point is non-zero (e.g. the seed's `{b (0 8)}`) produces a "
        "trajectory that starts at the bent pitch, which validate_timeline rejects.",
    )
    def test_whole_step_bend_shorthand(self):
        timeline = parse_alpha_tex(":4 5.2{b (0 8)} |", default_bpm=60)
        event = timeline["events"][0]
        assert event["technique"]["points"][0]["targetMidi"] == 64
        assert max(p["targetMidi"] for p in event["technique"]["points"]) == 66

    def test_invalid_time_signature_falls_back_to_4_4(self):
        timeline = parse_alpha_tex(":4 1.6 |", default_bpm=60, time_signature="bogus")
        assert timeline["timeSignature"] == "4/4"

    def test_three_four_measure_length(self):
        timeline = parse_alpha_tex(":4 1.6 | 1.6 |", default_bpm=60, time_signature="3/4")
        assert timeline["events"][1]["startTick"] == 3 * TICKS_PER_QUARTER

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("   ", "no measures"),
            (":4 1.7 |", "outside the track tuning"),
            (":4 37.1 |", "between 0 and 36"),
        ],
    )
    def test_errors(self, content, message):
        with pytest.raises(ScoreParseError, match=message):
            parse_alpha_tex(content, default_bpm=60)


def _musicxml(body: str, part_list: str = '<score-part id="P1"><part-name>Lead Guitar</part-name></score-part>') -> bytes:
    return f"""<?xml version="1.0"?>
<score-partwise version="3.1">
  <part-list>{part_list}</part-list>
  {body}
</score-partwise>""".encode()


def _note(step: str, octave: int, duration: int = 1, *, chord: bool = False, alter: int | None = None,
          voice: int | None = None) -> str:
    return (
        "<note>"
        + ("<chord/>" if chord else "")
        + f"<pitch><step>{step}</step>"
        + (f"<alter>{alter}</alter>" if alter is not None else "")
        + f"<octave>{octave}</octave></pitch><duration>{duration}</duration>"
        + (f"<voice>{voice}</voice>" if voice is not None else "")
        + "</note>"
    )


ATTRIBUTES = "<attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>"


class TestMusicXml:
    def test_notes_chords_and_rests(self):
        measure = (
            ATTRIBUTES
            + _note("E", 4)
            + _note("G", 4, chord=True)
            + "<note><rest/><duration>1</duration></note>"
            + _note("F", 4, alter=1, duration=2)
        )
        timeline = parse_musicxml(_musicxml(f'<part id="P1"><measure number="1">{measure}</measure></part>'), default_bpm=100)
        chord, rest, sharp = timeline["events"]
        assert chord["pitches"] == [64, 67]
        assert rest["isRest"] and rest["startTick"] == 960
        assert sharp["pitches"] == [66] and sharp["startTick"] == 1920 and sharp["durationTicks"] == 1920
        assert timeline["tracks"] == [
            {"id": "t0", "index": 0, "name": "Lead Guitar", "instrument": "GUITAR", "tuning": [64, 59, 55, 50, 45, 40]}
        ]
        assert timeline["defaultBpm"] == 100
        assert timeline["totalMeasures"] == 1

    def test_tempo_multiple_measures_and_parts(self):
        part_list = (
            '<score-part id="P1"><part-name>Guitar</part-name></score-part>'
            '<score-part id="P2"><part-name>Bass</part-name></score-part>'
        )
        tempo = '<direction><sound tempo="132"/></direction>'
        p1 = f'<part id="P1"><measure>{ATTRIBUTES}{tempo}{_note("C", 4, 4)}</measure><measure>{_note("D", 4, 4)}</measure></part>'
        p2 = f'<part id="P2"><measure>{ATTRIBUTES}{_note("E", 2, 4)}</measure></part>'
        timeline = parse_musicxml(_musicxml(p1 + p2, part_list), default_bpm=100)
        assert timeline["defaultBpm"] == 132
        assert timeline["totalMeasures"] == 2
        assert [t["instrument"] for t in timeline["tracks"]] == ["GUITAR", "BASS"]
        assert [(e["startTick"], e["trackId"]) for e in timeline["events"]] == [(0, "t0"), (0, "t1"), (3840, "t0")]
        assert timeline["events"][2]["measure"] == 2

    def test_backup_creates_second_voice(self):
        measure = ATTRIBUTES + _note("C", 5, 4, voice=1) + "<backup><duration>4</duration></backup>" + _note("C", 3, 4, voice=2)
        timeline = parse_musicxml(_musicxml(f'<part id="P1"><measure>{measure}</measure></part>'), default_bpm=90)
        assert [(e["startTick"], e["voiceIndex"], e["pitches"]) for e in timeline["events"]] == [(0, 0, [72]), (0, 1, [48])]

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            (b"<not xml", "not valid XML"),
            (b"<score-timewise/>", "partwise"),
            (_musicxml(""), "no parts"),
        ],
    )
    def test_errors(self, data, message):
        with pytest.raises(ScoreParseError, match=message):
            parse_musicxml(data, default_bpm=60)


class TestGenerate:
    def test_from_content_requires_alpha_tex(self):
        with pytest.raises(ScoreParseError, match="not supported"):
            generate_from_content(format="MUSIC_XML", content="x", default_bpm=60, time_signature="4/4")
        with pytest.raises(ScoreParseError, match="required"):
            generate_from_content(format="ALPHA_TEX", content="", default_bpm=60, time_signature="4/4")

    def test_from_bytes_alpha_tex_strips_bom(self):
        timeline = generate_from_bytes(format="ALPHA_TEX", data="﻿:4 1.6 |".encode(), default_bpm=60, time_signature="4/4")
        assert timeline["events"][0]["pitches"] == [41]

    def test_from_bytes_music_xml(self):
        data = _musicxml(f'<part id="P1"><measure>{ATTRIBUTES}{_note("A", 4, 4)}</measure></part>')
        assert generate_from_bytes(format="MUSIC_XML", data=data, default_bpm=60, time_signature="4/4")["events"][0]["pitches"] == [69]

    @pytest.mark.parametrize(
        ("format", "data", "message"),
        [
            ("ALPHA_TEX", b"", "empty"),
            ("ALPHA_TEX", b"\xff\xfe\xfa", "UTF-8"),
            ("GUITAR_PRO", b"GP5", "Guitar Pro"),
            ("MIDI", b"x", "Unsupported"),
        ],
    )
    def test_from_bytes_errors(self, format, data, message):
        with pytest.raises(ScoreParseError, match=message):
            generate_from_bytes(format=format, data=data, default_bpm=60, time_signature="4/4")


class TestValidateTimeline:
    def _event(self, **technique):
        return {"id": "e", "pitches": [60], "durationTicks": 960, "technique": technique}

    def test_rejects_missing_events_or_version(self):
        with pytest.raises(ScoreParseError):
            validate_timeline({"version": 3})
        with pytest.raises(ScoreParseError):
            validate_timeline({"version": 0, "events": []})

    def test_accepts_events_without_techniques(self):
        validate_timeline({"version": 3, "events": [{"id": "e", "pitches": [60]}]})

    @pytest.mark.parametrize(
        ("points", "message"),
        [
            ([{"offsetTicks": 0, "targetMidi": 62}], "starting pitch"),
            ([{"offsetTicks": 0, "targetMidi": 60}, {"offsetTicks": 2000, "targetMidi": 62}], "point offset"),
            ([{"offsetTicks": 0, "targetMidi": 60}, {"offsetTicks": 480, "targetMidi": 200}], "target pitch"),
            ([{"offsetTicks": 480, "targetMidi": 60}, {"offsetTicks": 0, "targetMidi": 62}], "point offset"),
        ],
    )
    def test_rejects_invalid_bend_points(self, points, message):
        with pytest.raises(ScoreParseError, match=message):
            validate_timeline({"version": 3, "events": [self._event(points=points)]})
