import base64
import json

import pytest

from jobsearch import cv
from jobsearch.cv import MAX_BYTES, parse_upload, sniff, write_file

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"
DOCX = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 20
TEXT = "Serhii Drozh — Backend Engineer\n".encode()


def upload(**payload) -> bytes:
    return json.dumps(payload).encode()


def test_a_pdf_is_recognised_by_its_magic_bytes():
    assert sniff(PDF) == ("application/pdf", ".pdf")


def test_a_docx_is_recognised_as_a_zip_container():
    assert sniff(DOCX)[1] == ".docx"


def test_utf8_text_is_accepted():
    assert sniff(TEXT)[1] == ".txt"


def test_a_file_that_is_none_of_those_is_rejected():
    # A client claiming content_type: application/pdf does not make it one.
    with pytest.raises(ValueError, match="unsupported"):
        sniff(b"\x00\x01\x02\xff\xfe")


def test_an_upload_carries_a_filename_and_base64_content():
    filename, data = parse_upload(upload(
        filename="cv.pdf", content=base64.b64encode(PDF).decode()))
    assert filename == "cv.pdf"
    assert data == PDF


@pytest.mark.parametrize("payload,match", [
    ({"filename": "cv.pdf", "content": "not base64!!"}, "base64"),
    ({"filename": "", "content": "AAAA"}, "filename"),
    ({"content": "AAAA"}, "filename"),
    ({"filename": "cv.pdf"}, "content"),
])
def test_a_malformed_upload_is_rejected(payload, match):
    with pytest.raises(ValueError, match=match):
        parse_upload(upload(**payload))


def test_an_oversized_upload_is_rejected_without_decoding(monkeypatch):
    # The guard's whole purpose is that an oversized body is never materialised.
    # Reaching b64decode at all means it failed, so make that reach an error.
    oversized = base64.b64encode(b"x" * (MAX_BYTES + 1)).decode()

    def explode(*args, **kwargs):
        raise AssertionError("b64decode was reached — the size guard did not run first")

    monkeypatch.setattr(cv.base64, "b64decode", explode)
    with pytest.raises(ValueError, match="too large"):
        parse_upload(upload(filename="cv.pdf", content=oversized))


def test_an_upload_at_exactly_the_limit_is_accepted():
    # The other side of the boundary: the exact arithmetic must not reject a
    # legitimate maximum-size file.
    payload = b"%PDF-" + b"x" * (MAX_BYTES - 5)
    filename, data = parse_upload(upload(
        filename="cv.pdf", content=base64.b64encode(payload).decode()))
    assert len(data) == MAX_BYTES


def test_the_same_bytes_are_written_once(tmp_path):
    first_hash, first_path = write_file(PDF, ".pdf", str(tmp_path))
    second_hash, second_path = write_file(PDF, ".pdf", str(tmp_path))
    assert first_hash == second_hash
    assert first_path == second_path
    assert len(list(tmp_path.iterdir())) == 1


def test_the_stored_name_is_the_hash_not_the_uploaded_name(tmp_path):
    # Path traversal has nothing to grab: the client's filename never reaches disk.
    digest, path = write_file(PDF, ".pdf", str(tmp_path))
    assert path.endswith(f"{digest}.pdf")
