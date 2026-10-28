import pytest

from docwhere.versions import family, newer_versions


@pytest.mark.parametrize(
    ("name", "key", "version"),
    [
        ("OrbitSync_CLI仕様書_v2.3.docx", "orbitsynccli仕様書", (2, 3)),
        ("OrbitSync_CLI仕様書_v1.4.pdf", "orbitsynccli仕様書", (1, 4)),
        ("設計書 Ver.3.docx", "設計書", (3,)),
        ("設計書_第2版.docx", "設計書", (2,)),
        ("設計書_最終版.docx", "設計書", ()),
        ("設計書 - コピー.docx", "設計書", ()),
        ("設計書(2).docx", "設計書", ()),
        ("議事録_20260915.md", "議事録20260915", ()),  # dates are not versions
        ("server2.docx", "server2", ()),
    ],
)
def test_family(name, key, version):
    assert family(name) == (key, version)


def test_newest_by_version_when_all_numbered_else_by_mtime():
    newer = newer_versions([("a/仕様_v2.docx", 1), ("b/仕様_v10.docx", 0), ("x/議事録_0915.md", 5), ("x/議事録_1015.md", 9)])
    assert newer == {"a/仕様_v2.docx": "b/仕様_v10.docx"}
    newer = newer_versions([("仕様_v3.docx", 1), ("仕様_最終.docx", 2)])
    assert newer == {"仕様_v3.docx": "仕様_最終.docx"}
