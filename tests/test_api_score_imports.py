from __future__ import annotations

import pytest

from conftest import ALPHA_TEX_SONG

MUSIC_XML = b"""<?xml version="1.0"?>
<score-partwise><part-list><score-part id="P1"><part-name>Bass</part-name></score-part></part-list>
<part id="P1"><measure><attributes><divisions>1</divisions></attributes>
<note><pitch><step>E</step><octave>2</octave></pitch><duration>4</duration></note></measure></part>
</score-partwise>"""


def _payload(**overrides) -> dict:
    return {"title": "Song", "format": "ALPHA_TEX", "fileName": "song.txt", "mimeType": "text/plain",
            "sizeBytes": 10, **overrides}


class TestCreate:
    def test_creates_song_import_and_upload_url(self, client, auth, user, storage):
        response = client.post(
            "/api/v1/score-imports", headers=auth,
            json=_payload(title="  Song  ", artist="   ", fileName="my song!.txt"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["song"]["title"] == "Song"
        assert body["song"]["artist"] is None
        imported = body["scoreImport"]
        assert imported["status"] == "UPLOAD_PENDING"
        assert imported["originalFileName"] == "my song!.txt"
        assert imported["storageKey"] == f"user-scores/{user['user']['id']}/{body['song']['id']}/my_song_.txt"
        assert body["upload"]["uploadMethod"] == "PUT"
        assert body["upload"]["uploadHeaders"] == {"Content-Type": "text/plain"}

    @pytest.mark.parametrize(
        ("format", "file_name"),
        [("ALPHA_TEX", "song.xml"), ("MUSIC_XML", "song.gp5"), ("GUITAR_PRO", "song"), ("MUSIC_XML", "song.txt")],
    )
    def test_rejects_mismatched_extension(self, client, auth, storage, format, file_name):
        response = client.post("/api/v1/score-imports", headers=auth, json=_payload(format=format, fileName=file_name))
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "overrides",
        [{"title": "   "}, {"fileName": " "}, {"mimeType": "not a mime"}, {"sizeBytes": 0},
         {"sizeBytes": 10 * 1024 * 1024 + 1}, {"format": "PDF"}],
    )
    def test_validation(self, client, auth, storage, overrides):
        assert client.post("/api/v1/score-imports", headers=auth, json=_payload(**overrides)).status_code == 422

    def test_storage_unavailable_rolls_back(self, client, auth, storage):
        storage.unavailable = True
        response = client.post("/api/v1/score-imports", headers=auth, json=_payload())
        assert response.status_code == 503
        storage.unavailable = False
        assert client.get("/api/v1/score-imports", headers=auth).json()["items"] == []


class TestComplete:
    def test_alpha_tex_import_becomes_ready(self, create_import, auth):
        detail = create_import(auth)
        assert detail["status"] == "READY"
        assert detail["parseError"] is None
        assert detail["timeline"]["defaultBpm"] == 100
        assert detail["timeline"]["totalMeasures"] == 3
        assert detail["score"]["revision"].startswith("v3:")
        assert detail["song"]["title"] == "Riff"
        assert [(t["sourceTrackId"], t["name"], t["tuning"]) for t in detail["tracks"]] == [
            ("t0", "Riff", [64, 59, 55, 50, 45, 40])
        ]

    def test_music_xml_import(self, create_import, auth):
        detail = create_import(auth, data=MUSIC_XML, format="MUSIC_XML", file_name="bass.musicxml")
        assert detail["status"] == "READY"
        assert detail["tracks"][0]["instrument"] == "BASS"
        assert detail["timeline"]["events"][0]["pitches"] == [40]

    def test_completing_ready_import_is_a_no_op(self, client, create_import, auth, storage):
        detail = create_import(auth)
        storage.files.clear()
        again = client.post(f"/api/v1/score-imports/{detail['id']}/complete", headers=auth)
        assert again.status_code == 200
        assert again.json()["score"] == detail["score"]

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"data": b"GP5-binary", "format": "GUITAR_PRO", "file_name": "song.gp5"}, "Guitar Pro"),
            ({"data": None}, "Supabase Storage request failed"),
            ({"data": b":4 1.9 |"}, "outside the track tuning"),
        ],
    )
    def test_failures_are_recorded(self, client, create_import, auth, kwargs, message):
        created = create_import(auth, complete=False, **kwargs)
        import_id = created["scoreImport"]["id"]
        response = client.post(f"/api/v1/score-imports/{import_id}/complete", headers=auth)
        assert response.status_code == 400
        assert message in response.json()["detail"]
        detail = client.get(f"/api/v1/score-imports/{import_id}", headers=auth).json()
        assert detail["status"] == "FAILED"
        assert message in detail["parseError"]
        assert detail["tracks"] == []

    def test_oversized_file(self, client, create_import, auth):
        created = create_import(auth, data=b"x" * (10 * 1024 * 1024 + 1), complete=False)
        import_id = created["scoreImport"]["id"]
        response = client.post(f"/api/v1/score-imports/{import_id}/complete", headers=auth)
        assert response.status_code == 413
        assert client.get(f"/api/v1/score-imports/{import_id}", headers=auth).json()["status"] == "FAILED"

    def test_failed_import_can_be_retried(self, client, create_import, auth, storage):
        created = create_import(auth, data=b":4 1.9 |", complete=False)
        import_id = created["scoreImport"]["id"]
        assert client.post(f"/api/v1/score-imports/{import_id}/complete", headers=auth).status_code == 400
        storage.files[created["scoreImport"]["storageKey"]] = ALPHA_TEX_SONG
        response = client.post(f"/api/v1/score-imports/{import_id}/complete", headers=auth)
        assert response.status_code == 200
        assert response.json()["status"] == "READY"
        assert response.json()["parseError"] is None


class TestReadAndDelete:
    def test_list_paginates_with_cursor(self, client, create_import, auth):
        ids = {create_import(auth, title=f"Song {n}")["id"] for n in range(3)}
        first = client.get("/api/v1/score-imports?limit=2", headers=auth).json()
        assert len(first["items"]) == 2 and first["nextCursor"]
        second = client.get(f"/api/v1/score-imports?limit=2&cursor={first['nextCursor']}", headers=auth).json()
        assert len(second["items"]) == 1 and second["nextCursor"] is None
        assert {item["id"] for item in first["items"] + second["items"]} == ids
        preview = first["items"][0]["timeline"]
        assert set(preview) == {"version", "defaultBpm", "tracks"}
        assert preview["tracks"][0]["capo"] is None

    def test_list_preview_is_null_before_processing(self, client, create_import, auth):
        create_import(auth, complete=False)
        assert client.get("/api/v1/score-imports", headers=auth).json()["items"][0]["timeline"] is None

    @pytest.mark.parametrize("cursor", ["garbage", "eyJmb28iOiAxfQ", "e30"])
    def test_list_rejects_invalid_cursor(self, client, auth, cursor):
        assert client.get(f"/api/v1/score-imports?cursor={cursor}", headers=auth).status_code == 400

    def test_imports_are_private(self, client, create_import, auth, register):
        detail = create_import(auth)
        intruder = register()["headers"]
        assert client.get("/api/v1/score-imports", headers=intruder).json()["items"] == []
        for method, path in [
            ("get", f"/api/v1/score-imports/{detail['id']}"),
            ("get", f"/api/v1/score-imports/{detail['id']}/source"),
            ("post", f"/api/v1/score-imports/{detail['id']}/complete"),
            ("delete", f"/api/v1/score-imports/{detail['id']}"),
        ]:
            assert getattr(client, method)(path, headers=intruder).status_code == 404

    def test_source_url(self, client, create_import, auth):
        pending = create_import(auth, complete=False)
        assert client.get(f"/api/v1/score-imports/{pending['scoreImport']['id']}/source", headers=auth).status_code == 400
        ready = create_import(auth)
        body = client.get(f"/api/v1/score-imports/{ready['id']}/source", headers=auth).json()
        assert body["sourceUrl"].startswith("https://storage.test/download/")
        assert body["format"] == "ALPHA_TEX"
        assert body["expiresInSeconds"] == 600

    def test_delete_removes_file_and_rows(self, client, create_import, auth, storage, db):
        detail = create_import(auth)
        assert client.delete(f"/api/v1/score-imports/{detail['id']}", headers=auth).status_code == 204
        assert len(storage.removed) == 1
        assert client.get(f"/api/v1/score-imports/{detail['id']}", headers=auth).status_code == 404
        assert db.execute("SELECT COUNT(*) FROM score_import_tracks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM user_songs").fetchone()[0] == 0

    def test_delete_keeps_import_when_storage_fails(self, client, create_import, auth, storage):
        detail = create_import(auth)
        storage.unavailable = True
        assert client.delete(f"/api/v1/score-imports/{detail['id']}", headers=auth).status_code == 503
        storage.unavailable = False
        assert client.get(f"/api/v1/score-imports/{detail['id']}", headers=auth).status_code == 200
