"""The documentation keeps up with the command line: every command is described in the CLI reference, every error code with guidance is real, and the cheat sheet and
guides name the commands people actually type."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import cli, guidance  # noqa: E402

REFERENCE = (ROOT / "docs" / "r8" / "r86_cli_reference.md").read_text(encoding="utf-8")


def test_every_command_is_described_in_the_cli_reference():
    missing = []
    for command, subcommand in sorted(cli.default_handlers(), key=lambda key: (key[0], key[1] or "")):
        words = f"{command} {subcommand}" if subcommand else command
        if f"`{words}`" not in REFERENCE and f"`{words} " not in REFERENCE:
            missing.append(words)
    assert missing == [], f"commands missing from docs/r8/r86_cli_reference.md: {missing}"


def test_every_command_has_help_text():
    parser = cli.build_parser()
    help_text = parser.format_help()
    for command in ("web", "clients", "report", "setup", "verify", "doctor", "status", "recovery", "system-info"):
        assert command in help_text, command
    for argv in (["clients", "check"], ["clients", "add"], ["report", "open"], ["web"], ["setup", "device"]):
        try:
            parser.parse_args(argv + ["--help"])
        except SystemExit as stop:
            assert stop.code == 0, argv


def test_the_guides_name_the_commands_people_type():
    guides = {name: (ROOT / "docs" / name).read_text(encoding="utf-8") for name in ("cheat_sheet.md", "operator_quickstart.md", "team_setup.md")}
    assert "seo.cmd web" in guides["cheat_sheet.md"] and "setup auth" in guides["cheat_sheet.md"] and "exactly 28 days" in guides["cheat_sheet.md"]
    assert "clients add" in guides["operator_quickstart.md"] and "report open" in guides["operator_quickstart.md"] and "shared-folder" in guides["operator_quickstart.md"]
    assert "install_device.ps1" in guides["team_setup.md"] and "setup device" in guides["team_setup.md"]


def test_every_documented_command_in_the_guides_exists():
    handlers = {f"{c} {s}" if s else c for c, s in cli.default_handlers()}
    for name in ("cheat_sheet.md", "operator_quickstart.md", "team_setup.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        for words in re.findall(r"seo(?:\.cmd)? ((?:[a-z-]+)(?: (?:[a-z-]+))?)", text):
            parts = words.split()
            candidates = {parts[0], " ".join(parts[:2])}
            assert candidates & handlers, f"{name}: `seo {words}` is not a command"


def test_the_guidance_texts_only_name_real_commands():
    handlers = {f"{c} {s}" if s else c for c, s in cli.default_handlers()}
    for code in guidance.known_codes():
        item = guidance.explain(code)
        for words in re.findall(r"`((?:clients|report|verify|setup|status|doctor|recovery|web|system-info)(?: [a-z-]+)?)", item.fix):
            parts = words.split()
            assert {parts[0], " ".join(parts[:2])} & handlers, f"{code}: `{words}`"
