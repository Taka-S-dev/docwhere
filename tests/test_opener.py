import pytest

from docwhere import opener, window


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(opener.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(opener, "start_file", lambda path: calls.append(["default", path]))
    return calls


@pytest.fixture
def program(tmp_path):
    exe = tmp_path / "SumatraPDF.exe"  # stands in for a portable viewer
    exe.write_bytes(b"")
    return str(exe)


def test_configured_program_gets_the_page_and_file(launched, program, monkeypatch):
    monkeypatch.setattr(opener, "_acrobat", lambda: r"C:\Acrobat\Acrobat.exe")
    command = [program, "-reuse-instance", "-page", "{page}", "{file}"]
    message = opener._pdf(r"C:\docs\m.pdf", {"page": 120}, {"command": command})
    assert launched == [[program, "-reuse-instance", "-page", "120", r"C:\docs\m.pdf"]]
    assert "SumatraPDF" in message


def test_default_is_acrobat_then_the_default_app(launched, monkeypatch):
    monkeypatch.setattr(opener, "_acrobat", lambda: r"C:\Acrobat\Acrobat.exe")
    opener._pdf("m.pdf", {"page": 2})
    monkeypatch.setattr(opener, "_acrobat", lambda: None)
    opener._pdf("m.pdf", {"page": 2})
    assert launched == [[r"C:\Acrobat\Acrobat.exe", "/A", "page=2", "m.pdf"], ["default", "m.pdf"]]


def test_missing_program_falls_back_to_the_default_app(launched, tmp_path):
    message = opener._pdf("m.pdf", {"page": 7}, {"command": [str(tmp_path / "gone.exe"), "{file}"]})
    assert launched == [["default", "m.pdf"]] and "not found" in message


def test_pdf_app_setting_is_validated(tmp_path, program):
    db = tmp_path / "index.db"
    assert window.pdf_app(db) == {}
    window.set_pdf_app(db, [program, "-page", "{page}"])
    assert window.pdf_app(db)["command"][1:] == ["-page", "{page}", "{file}"]  # {file} added when left out
    window.set_pdf_app(db, None)
    assert window.pdf_app(db) == {}
    with pytest.raises(FileNotFoundError):
        window.set_pdf_app(db, [str(tmp_path / "nope.exe"), "{file}"])


@pytest.mark.parametrize("name", ["cmd.exe", "evil.bat"])
def test_refused_program_in_settings_is_ignored_at_launch(launched, tmp_path, monkeypatch, name):
    monkeypatch.setattr(opener, "_acrobat", lambda: None)
    exe = tmp_path / name
    exe.write_bytes(b"")  # written into the settings file by hand, past set_pdf_app
    opener._pdf("m.pdf", {"page": 2}, {"command": [str(exe), "/c", "{file}"]})
    assert launched == [["default", "m.pdf"]]
