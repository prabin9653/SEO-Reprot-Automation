import hashlib
import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import asset_fingerprints as af
from seo_report import render_input as ri
from seo_report import render_input_hash as rh

LOGO, FONT_A, FONT_B = b"\x89PNG synthetic logo bytes", b"synthetic font A bytes", b"synthetic font B bytes"


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def assets(tmp_path):
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / "logo.png").write_bytes(LOGO)
    (folder / "FontA-Regular.ttf").write_bytes(FONT_A)
    (folder / "FontA-Bold.ttf").write_bytes(FONT_B)
    return folder


def measure(assets, **overrides):
    options = dict(template_version="r3.0", logo_path=assets / "logo.png",
                   font_paths=[assets / "FontA-Regular.ttf", assets / "FontA-Bold.ttf"])
    options.update(overrides)
    return af.measure_fingerprints(**options)


def snapshot_of(folder):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(folder).iterdir()}


def test_known_bytes_have_the_known_sha256(assets):
    assert af.sha256_of_file(assets / "logo.png") == sha(LOGO)
    assert af.sha256_of_file(assets / "FontA-Regular.ttf") == sha(FONT_A)
    empty = assets / "empty.bin"
    empty.write_bytes(b"")
    assert af.sha256_of_file(empty) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_the_result_is_the_r1_type_with_the_expected_values(assets):
    result = measure(assets)
    assert type(result) is ri.RenderFingerprints
    assert (result.template_version, result.logo_sha256, result.page_size) == ("r3.0", sha(LOGO), "A4")
    assert [(f.name, f.sha256) for f in result.fonts] == [("FontA-Bold", sha(FONT_B)), ("FontA-Regular", sha(FONT_A))]
    assert all(type(f) is ri.FontFingerprint for f in result.fonts)


def test_a_mapping_gives_explicit_labels(assets):
    result = measure(assets, font_paths={"Body": assets / "FontA-Regular.ttf", "Heading": assets / "FontA-Bold.ttf"})
    assert [f.name for f in result.fonts] == ["Body", "Heading"]


def test_identical_files_and_version_give_identical_fingerprints(assets, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    for name in ("logo.png", "FontA-Regular.ttf", "FontA-Bold.ttf"):
        (other / name).write_bytes((assets / name).read_bytes())
    assert measure(assets) == measure(assets) == measure(other)  # the location does not matter


def test_changed_logo_bytes_change_the_logo_fingerprint_only(assets):
    before = measure(assets)
    (assets / "logo.png").write_bytes(LOGO + b"!")
    after = measure(assets)
    assert after.logo_sha256 != before.logo_sha256 and after.fonts == before.fonts


def test_changed_font_bytes_change_that_font_only(assets):
    before = measure(assets)
    (assets / "FontA-Bold.ttf").write_bytes(FONT_B + b"!")
    after = measure(assets)
    assert after.logo_sha256 == before.logo_sha256
    assert after.fonts[0].sha256 != before.fonts[0].sha256 and after.fonts[1] == before.fonts[1]


def test_a_changed_template_version_changes_the_fingerprint_and_the_render_hash(assets):
    from test_render_input import sample
    import dataclasses
    one, two = measure(assets), measure(assets, template_version="r3.1")
    assert one != two and one.logo_sha256 == two.logo_sha256
    base = sample()
    assert rh.render_input_sha256(dataclasses.replace(base, fingerprints=one)) != \
        rh.render_input_sha256(dataclasses.replace(base, fingerprints=two))


def test_fingerprints_feed_a_render_input_hash_without_any_path(assets):
    from test_render_input import sample
    import dataclasses
    value = dataclasses.replace(sample(), fingerprints=measure(assets))
    text = rh.canonical_render_input_json(value)
    assert str(assets) not in text and str(assets.parent) not in text and "\\\\" not in text and ".ttf" not in text


def test_no_absolute_path_appears_in_the_fingerprint_data(assets):
    result = measure(assets)
    text = repr(result)
    assert str(assets) not in text and os.fspath(assets.parent) not in text and "\\" not in text and "/" not in text


def test_the_same_font_order_does_not_matter(assets):
    one = measure(assets, font_paths=[assets / "FontA-Regular.ttf", assets / "FontA-Bold.ttf"])
    two = measure(assets, font_paths=[assets / "FontA-Bold.ttf", assets / "FontA-Regular.ttf"])
    assert one == two


def test_source_files_are_never_changed_and_nothing_is_created(assets):
    before = snapshot_of(assets)
    parent_before = sorted(p.name for p in assets.parent.iterdir())
    for _ in range(3):
        measure(assets)
    assert snapshot_of(assets) == before and sorted(p.name for p in assets.parent.iterdir()) == parent_before


def test_read_only_files_can_be_measured(assets):
    for path in assets.iterdir():
        os.chmod(path, stat.S_IREAD)
    try:
        assert measure(assets).logo_sha256 == sha(LOGO)
    finally:
        for path in assets.iterdir():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def test_large_files_are_read_in_chunks(assets, monkeypatch):
    monkeypatch.setattr(af, "CHUNK", 7)
    assert af.sha256_of_file(assets / "logo.png") == sha(LOGO)


# --- failures ----------------------------------------------------------------------------------------------------------------------

def fails(code, role=None, **call):
    with pytest.raises(af.AssetFingerprintError) as caught:
        af.measure_fingerprints(**call)
    assert caught.value.code == code and (role is None or caught.value.role == role)
    return caught.value


def test_a_missing_logo_or_font_fails_safely(assets):
    error = fails("file_not_found", "logo", template_version="r3.0", logo_path=assets / "missing.png",
                  font_paths=[assets / "FontA-Regular.ttf"])
    assert str(assets) not in str(error)
    fails("file_not_found", "Missing", template_version="r3.0", logo_path=assets / "logo.png",
          font_paths={"Missing": assets / "missing.ttf"})


def test_a_folder_is_not_a_file(assets):
    fails("not_a_regular_file", "logo", template_version="r3.0", logo_path=assets, font_paths=[assets / "FontA-Bold.ttf"])


def test_an_unreadable_file_fails_without_system_text(assets, monkeypatch):
    real_open = open

    def refuse(path, *args, **kwargs):
        if str(path).endswith("logo.png"):
            raise PermissionError(13, "C:\\private\\secret denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse)
    error = fails("file_unreadable", "logo", template_version="r3.0", logo_path=assets / "logo.png",
                  font_paths=[assets / "FontA-Bold.ttf"])
    assert "private" not in str(error) and "denied" not in str(error)


def test_a_file_that_is_too_large_is_refused(assets, monkeypatch):
    monkeypatch.setattr(af, "MAX_ASSET_BYTES", 5)
    fails("file_too_large", "logo", template_version="r3.0", logo_path=assets / "logo.png",
          font_paths=[assets / "FontA-Bold.ttf"])


@pytest.mark.parametrize("bad", [None, 5, b"logo.png", "", "bad\x00path", ["logo.png"], {"a": 1}])
def test_invalid_logo_paths_fail_clearly(assets, bad):
    fails("invalid_path", "logo", template_version="r3.0", logo_path=bad, font_paths=[assets / "FontA-Bold.ttf"])


@pytest.mark.parametrize("fonts,code", [([], "no_fonts"), ({}, "no_fonts"), (None, "invalid_font_list"),
                                        ("DejaVuSans.ttf", "invalid_font_list"), (5, "invalid_font_list")])
def test_invalid_font_inputs_fail_clearly(assets, fonts, code):
    fails(code, template_version="r3.0", logo_path=assets / "logo.png", font_paths=fonts)


def test_font_entries_must_be_paths_with_valid_unique_labels(assets):
    fails("invalid_path", template_version="r3.0", logo_path=assets / "logo.png", font_paths=[5])
    fails("invalid_font_label", template_version="r3.0", logo_path=assets / "logo.png",
          font_paths={"bad/label": assets / "FontA-Bold.ttf"})
    fails("invalid_font_label", template_version="r3.0", logo_path=assets / "logo.png",
          font_paths={5: assets / "FontA-Bold.ttf"})
    (assets / "other").mkdir()
    (assets / "other" / "FontA-Bold.ttf").write_bytes(b"x")
    fails("duplicate_font_label", template_version="r3.0", logo_path=assets / "logo.png",
          font_paths=[assets / "FontA-Bold.ttf", assets / "other" / "FontA-Bold.ttf"])


@pytest.mark.parametrize("version", ["", "has space", None, 3, "x" * 40])
def test_an_invalid_template_version_is_rejected(assets, version):
    fails("invalid_fingerprint_input", template_version=version, logo_path=assets / "logo.png",
          font_paths=[assets / "FontA-Bold.ttf"])


def test_an_unsupported_page_size_is_rejected(assets):
    fails("invalid_fingerprint_input", template_version="r3.0", logo_path=assets / "logo.png",
          font_paths=[assets / "FontA-Bold.ttf"], page_size="Letter")


def test_credentials_and_token_files_are_never_touched():
    source = (ROOT / "src" / "seo_report" / "asset_fingerprints.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("token", "credential", "secret", "environ", "shutil", "copy", "rename", "unlink", "remove", "write",
                 "mkdir", "requests", "urllib", "socket", "subprocess", "datetime", "random", '"wb"', "'wb'", '"ab"'):
        assert word not in code, word
    assert code.count('open(text, "rb")') == 1
