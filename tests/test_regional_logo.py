"""A regional profile may name its own report logo (a .png inside assets/): the United States reports use a different logo from the Australian ones.

Synthetic throughout: temporary configuration files and stubbed rendering; no browser, no Google call.
"""
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import clients, render_service as rs  # noqa: E402
from seo_report import report_templates as rt  # noqa: E402

PROFILE = """
sender:
  name: Jasper
regional_profiles:
  United States:
    title: "Project Manager | Jasper IT Inc."
    address_lines: ["1 Main St"]
    website_label: https://example.test/
    website_url: https://example.test/
{extra}
  Australia:
    title: "Project Manager | Jasper IT Solutions"
    address_lines: ["Ingleburn NSW 2565"]
    website_label: https://example.test.au/
    website_url: https://example.test.au/
"""


def _load(tmp_path, extra=""):
    path = tmp_path / "jasper.yaml"
    path.write_text(PROFILE.format(extra=extra), encoding="utf-8")
    return clients.load_default_sender(path)


# === the configuration =========================================================================================================

def test_a_profile_may_name_a_logo_and_a_profile_without_one_keeps_the_standard_logo(tmp_path):
    default = _load(tmp_path, "    logo: jasper-us-logo.png")
    us = clients.resolve_sender(default, "United States", "Pat Manager", "pat@client.test", "555-0100")
    au = clients.resolve_sender(default, "Australia", "Sam Manager", "sam@client.test", "555-0101")
    assert us.logo == "jasper-us-logo.png" and au.logo is None
    assert clients.regional_logo_path(us) == ROOT / "assets" / "jasper-us-logo.png" and clients.regional_logo_path(au) is None


@pytest.mark.parametrize("bad", ["../secret.png", "sub/logo.png", "C:\\logos\\x.png", "logo.jpg", "logo", ".png", "a b.png"])
def test_a_regional_logo_must_be_a_plain_png_file_name_inside_assets(tmp_path, bad):
    with pytest.raises(clients.SheetDataError):
        _load(tmp_path, f"    logo: '{bad}'")  # single quotes: a backslash is plain text in YAML
    assert clients.regional_logo_path(SimpleNamespace(logo=bad)) is None  # and nothing builds a path from it later either


def test_the_real_configuration_gives_the_us_profile_its_own_logo_and_australia_the_standard_one():
    default = clients.load_default_sender()
    us = clients.resolve_sender(default, "United States", "Pat Manager", "pat@client.test", "555-0100")
    au = clients.resolve_sender(default, "Australia", "Sam Manager", "sam@client.test", "555-0101")
    assert us.logo == "jasper-us-logo.png" and au.logo is None
    logo = clients.regional_logo_path(us)
    assert logo.is_file() and logo.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = rt.read_png_size(logo)
    assert width > 0 and height > 0
    assert (us.website_label, us.website_url) == ("https://jasperitinc.com/", "https://jasperitinc.com/")  # the contact block shows the address, not a name


def test_the_us_logo_is_a_different_file_from_the_standard_logo():
    assert (ROOT / "assets" / "jasper-us-logo.png").read_bytes() != (ROOT / "assets" / "jasper-logo.png").read_bytes()


# === the render uses it ========================================================================================================

def _render(monkeypatch, tmp_path, sender):
    seen = {}
    monkeypatch.setattr(rs.rstate, "finalized_render_hashes", lambda *a, **k: {})
    monkeypatch.setattr(rs.idn, "measure_render_fingerprints", lambda *, logo_path, font_paths: seen.update(measured=Path(logo_path)) or "FINGERPRINTS")
    monkeypatch.setattr(rs.idn, "build_render_input", lambda *a, **k: "CANDIDATE")

    def produce(reports, candidate, *, workdir, logo_path, fonts, **kw):
        seen["drawn"] = Path(logo_path)
        return SimpleNamespace(ok=True, render_input="RI", generation_date=dt.date(2026, 11, 20), code=None)

    monkeypatch.setattr(rs.ra, "produce_report_draft", produce)
    font = tmp_path / "font.ttf"
    font.write_bytes(b"x")
    settings = SimpleNamespace(output_root=tmp_path / "out", fonts=(SimpleNamespace(label="DejaVuSans", path=font, weight="400"),), logo_path=tmp_path / "standard.png",
                               work_root=tmp_path / "work", stage_timeouts=SimpleNamespace(rendering=60))
    run = SimpleNamespace(client_key="alpha-co", run_key="alpha-co/2026-10-01_2026-10-28", client=SimpleNamespace(sender=sender),
                          periods=SimpleNamespace(current=SimpleNamespace(start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 28))))
    rs.render(SimpleNamespace(bundle=None, history_insights=None), run, settings=settings, generation_date=dt.date(2026, 11, 20), attempt=1,
              workdir=tmp_path / "w", reports_dir=tmp_path / "r", reviewer_dir=tmp_path / "v")
    return seen, settings


def test_the_report_is_measured_and_drawn_with_the_regional_logo_when_there_is_one(monkeypatch, tmp_path):
    seen, settings = _render(monkeypatch, tmp_path, clients.Sender(name="Pat", logo="jasper-us-logo.png"))
    assert seen == {"measured": ROOT / "assets" / "jasper-us-logo.png", "drawn": ROOT / "assets" / "jasper-us-logo.png"}


def test_the_standard_logo_is_used_when_the_profile_names_none(monkeypatch, tmp_path):
    seen, settings = _render(monkeypatch, tmp_path, clients.Sender(name="Sam"))
    assert seen == {"measured": settings.logo_path, "drawn": settings.logo_path}


def test_the_regional_logo_changes_the_report_identity(tmp_path):
    from seo_report import render_identity as idn
    font = tmp_path / "font.ttf"
    font.write_bytes(b"x")
    fonts = {"DejaVuSans": font}
    standard = idn.measure_render_fingerprints(logo_path=ROOT / "assets" / "jasper-logo.png", font_paths=fonts)
    regional = idn.measure_render_fingerprints(logo_path=ROOT / "assets" / "jasper-us-logo.png", font_paths=fonts)
    assert standard.logo_sha256 != regional.logo_sha256  # the logo is part of what a finalized report is identified by


def test_a_missing_regional_logo_file_fails_the_render_with_a_fixed_code_not_a_crash(monkeypatch, tmp_path):
    from seo_report.contracts import StageError
    monkeypatch.setattr(rs.rstate, "finalized_render_hashes", lambda *a, **k: {})
    font = tmp_path / "font.ttf"
    font.write_bytes(b"x")
    settings = SimpleNamespace(output_root=tmp_path / "out", fonts=(SimpleNamespace(label="DejaVuSans", path=font, weight="400"),), logo_path=tmp_path / "s.png",
                               work_root=tmp_path / "work", stage_timeouts=SimpleNamespace(rendering=60))
    monkeypatch.setattr(clients, "ASSETS_DIR", tmp_path / "no-assets")
    run = SimpleNamespace(client_key="alpha-co", run_key="alpha-co/2026-10-01_2026-10-28", client=SimpleNamespace(sender=clients.Sender(name="Pat", logo="gone.png")),
                          periods=SimpleNamespace(current=SimpleNamespace(start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 28))))
    with pytest.raises(StageError) as raised:
        rs.render(SimpleNamespace(bundle=None, history_insights=None), run, settings=settings, generation_date=dt.date(2026, 11, 20), attempt=1,
                  workdir=tmp_path / "w", reports_dir=tmp_path / "r", reviewer_dir=tmp_path / "v")
    assert raised.value.code == "render_input_invalid"
