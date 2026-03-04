from __future__ import annotations

from render_agent.job_contract import extract_drive_id, parse_id_list


def test_parse_id_list_json_comma_newline_mixed() -> None:
    assert parse_id_list('["idA123456789","idB123456789"]') == ["idA123456789", "idB123456789"]
    assert parse_id_list("idA123456789,idB123456789") == ["idA123456789", "idB123456789"]
    assert parse_id_list("idA123456789\nidB123456789") == ["idA123456789", "idB123456789"]
    assert parse_id_list("idA123456789,\nidB123456789\nidA123456789") == ["idA123456789", "idB123456789"]


def test_extract_drive_id_from_urls() -> None:
    assert extract_drive_id("https://drive.google.com/drive/folders/1abcDEFghiJKLmnop_qrs") == "1abcDEFghiJKLmnop_qrs"
    assert extract_drive_id("https://drive.google.com/file/d/1abcDEFghiJKLmnop_qrs/view") == "1abcDEFghiJKLmnop_qrs"
    assert extract_drive_id("https://drive.google.com/open?id=1abcDEFghiJKLmnop_qrs") == "1abcDEFghiJKLmnop_qrs"
