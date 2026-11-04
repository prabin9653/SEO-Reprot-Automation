import datetime as dt
from pathlib import Path

import pytest

from seo_report import output_layout as ol
from seo_report.output_layout import OutputLayoutError

H = "0123456789abcdef"
H2 = "fedcba9876543210"


@pytest.fixture
def base(tmp_path):
    return tmp_path


def env(base, **extra):
    """An isolated environment: no real OneDrive or history variables leak in."""
    values = {"LOCALAPPDATA": str(base / "local")}
    values.update(extra)
    return values


# --- root precedence ----------------------------------------------------------------------------------------------

def test_explicit_path_beats_environment(base):
    explicit = base / "explicit"
    environ = env(base, JASPER_SEO_OUTPUT_ROOT=str(base / "from_env"))
    assert ol.resolve_output_root(explicit, environ=environ) == explicit


def test_environment_variable_used_when_no_explicit_path(base):
    environ = env(base, JASPER_SEO_OUTPUT_ROOT=str(base / "from_env"))
    assert ol.resolve_output_root(environ=environ) == base / "from_env"


def test_default_location(base):
    root = ol.resolve_output_root(environ=env(base))
    assert root == base / "local" / "JasperSEOReporting" / "output"


def test_empty_environment_value_falls_through_to_default(base):
    root = ol.resolve_output_root(environ=env(base, JASPER_SEO_OUTPUT_ROOT=""))
    assert root.name == "output"


def test_resolution_does_not_create_directories(base):
    root = ol.resolve_output_root(base / "new" / "deeper", environ=env(base))
    assert not root.exists()
    assert not (base / "new").exists()
    assert not (base / "local").exists()


def test_relative_paths_rejected(base):
    with pytest.raises(OutputLayoutError, match="absolute"):
        ol.resolve_output_root("relative/out", environ=env(base))
    with pytest.raises(OutputLayoutError, match="absolute"):
        ol.resolve_output_root(environ=env(base, JASPER_SEO_OUTPUT_ROOT="out"))


def test_relative_default_base_rejected():
    with pytest.raises(OutputLayoutError, match="absolute"):
        ol.resolve_output_root(environ={"LOCALAPPDATA": "relative"})


def test_drive_root_rejected(base):
    with pytest.raises(OutputLayoutError, match="drive root"):
        ol.resolve_output_root(Path(base.anchor), environ=env(base))


def test_dotdot_and_bad_types_rejected(base):
    with pytest.raises(OutputLayoutError, match=r"\.\."):
        ol.resolve_output_root(base / "a" / ".." / "b", environ=env(base))
    with pytest.raises(OutputLayoutError):
        ol.resolve_output_root(123, environ=env(base))
    with pytest.raises(OutputLayoutError):
        ol.resolve_output_root(str(base / "x") + "\x00", environ=env(base))


def test_invalid_windows_characters_rejected(base):
    with pytest.raises(OutputLayoutError, match="Windows"):
        ol.resolve_output_root(base / "bad?name", environ=env(base))


# --- history database overlap -------------------------------------------------------------------------------------

def test_root_equal_to_or_inside_database_path_rejected(base):
    db = base / "history" / "seo.sqlite"
    with pytest.raises(OutputLayoutError, match="overlaps"):
        ol.resolve_output_root(db, environ=env(base), db_path=db)
    with pytest.raises(OutputLayoutError, match="overlaps"):
        ol.resolve_output_root(db / "reports", environ=env(base), db_path=db)


def test_database_inside_root_rejected(base):
    db = base / "out" / "history" / "seo.sqlite"
    with pytest.raises(OutputLayoutError, match="overlaps"):
        ol.resolve_output_root(base / "out", environ=env(base), db_path=db)
    with pytest.raises(OutputLayoutError, match="overlaps"):
        ol.resolve_output_root(base / "out" / "history", environ=env(base), db_path=db)


def test_overlap_check_is_case_insensitive_and_uses_env_database(base):
    environ = env(base, JASPER_SEO_HISTORY_DB=str(base / "Hist" / "db.sqlite"))
    with pytest.raises(OutputLayoutError, match="overlaps"):
        ol.resolve_output_root(base / "hist", environ=environ)


def test_sibling_of_database_folder_is_allowed(base):
    db = base / "history" / "seo.sqlite"
    assert ol.resolve_output_root(base / "output", environ=env(base), db_path=db) == base / "output"


def test_default_root_does_not_overlap_default_database(base):
    ol.resolve_output_root(environ=env(base))  # default db is .../history/..., default root .../output


# --- OneDrive -----------------------------------------------------------------------------------------------------

def test_onedrive_refused_by_default(base):
    with pytest.raises(OutputLayoutError, match="OneDrive"):
        ol.resolve_output_root(base / "OneDrive" / "reports", environ=env(base))


def test_onedrive_refused_via_environment_root(base):
    environ = env(base, OneDrive=str(base / "cloud"))
    with pytest.raises(OutputLayoutError, match="OneDrive"):
        ol.resolve_output_root(base / "cloud" / "out", environ=environ)


def test_onedrive_allowed_by_argument(base):
    target = base / "OneDrive" / "reports"
    assert ol.resolve_output_root(target, environ=env(base), allow_onedrive_output=True) == target


def test_onedrive_allowed_only_by_exact_env_value(base):
    target = base / "OneDrive" / "reports"
    assert ol.resolve_output_root(target, environ=env(base, JASPER_SEO_ALLOW_ONEDRIVE_OUTPUT="1")) == target
    for value in ("0", "true", "yes", ""):
        with pytest.raises(OutputLayoutError):
            ol.resolve_output_root(target, environ=env(base, JASPER_SEO_ALLOW_ONEDRIVE_OUTPUT=value))


def test_allow_flag_must_be_bool(base):
    with pytest.raises(OutputLayoutError):
        ol.resolve_output_root(base / "x", environ=env(base), allow_onedrive_output="yes")


# --- period folders -----------------------------------------------------------------------------------------------

def test_valid_period_paths_for_each_subfolder(base):
    root = base / "out"
    expected = "example-client", "2026-01-01_2026-01-31"
    assert ol.report_dir(root, "example-client", "2026-01-01", "2026-01-31") == root / "reports" / Path(*expected)
    assert ol.reviewer_dir(root, "example-client", "2026-01-01", "2026-01-31") == root / "reviewer" / Path(*expected)
    assert ol.attempt_dir(root, "example-client", "2026-01-01", "2026-01-31") == root / "attempts" / Path(*expected)


def test_period_dates_may_be_date_objects(base):
    path = ol.report_dir(base, "k", dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert path.name == "2026-02-01_2026-02-28"


def test_path_building_creates_nothing(base):
    ol.report_dir(base / "out", "example-client", "2026-01-01", "2026-01-31")
    assert not (base / "out").exists()


@pytest.mark.parametrize("key", ["", "AR", "example client", "../x", "a/b", "a\\b", "-ab", "x" * 64, "con", "nul",
                                 "lpt1", None, 5])
def test_invalid_client_keys_rejected(base, key):
    with pytest.raises(OutputLayoutError):
        ol.report_dir(base, key, "2026-01-01", "2026-01-31")


@pytest.mark.parametrize("bad", ["2026-1-1", "20260101", "2026-02-30", "2026-13-01", "2026-01-01T00:00:00",
                                 " 2026-01-01", "2026-01-01\n", "٢٠٢٦-٠١-٠١", "", None, 20260101,
                                 dt.datetime(2026, 1, 1)])
def test_invalid_dates_rejected(base, bad):
    with pytest.raises(OutputLayoutError):
        ol.report_dir(base, "k", bad, "2026-01-31")
    with pytest.raises(OutputLayoutError):
        ol.report_dir(base, "k", "2026-01-01", bad)


def test_period_start_after_end_rejected(base):
    with pytest.raises(OutputLayoutError, match="after"):
        ol.report_dir(base, "k", "2026-02-01", "2026-01-31")


def test_relative_root_rejected_when_building(base):
    with pytest.raises(OutputLayoutError, match="absolute"):
        ol.report_dir("out", "k", "2026-01-01", "2026-01-31")



# --- path length --------------------------------------------------------------------------------------------------

RUN_ID = "0198c1a2-7b3c-7d4e-8f5a-1234567890ab"
SHA = "ab" * 32
STAMP = "2026-02-03T09:30:00+00:00"
PERIOD = ("2026-01-01", "2026-01-31")


def test_longest_file_names_per_folder():
    assert ol.MAX_PDF_FILENAME_CHARS == len(f"report.superseded.{H}.99.pdf") == 41
    assert ol.LONGEST_FILENAME_CHARS["reports"] == 41
    assert ol.LONGEST_FILENAME_CHARS["reviewer"] == len(f"reviewer.{RUN_ID}.{H}.json") == 67
    assert ol.LONGEST_FILENAME_CHARS["attempts"] == len(
        "20260203T093000Z." + H + ".verify_draft.attempt.999.json") == 63


def folder_fns():
    return [("reports", ol.report_dir), ("reviewer", ol.reviewer_dir), ("attempts", ol.attempt_dir)]


@pytest.mark.parametrize("subfolder", ["reports", "reviewer", "attempts"])
def test_folder_boundary_is_exact_for_the_longest_file_name(subfolder):
    fn = dict(folder_fns())[subfolder]
    longest = ol.LONGEST_FILENAME_CHARS[subfolder]
    key = "k" * 10
    fixed = 1 + len(subfolder) + 1 + len(key) + 1 + 21  # separators and names after the root
    root_len_ok = ol.MAX_PATH_CHARS - 1 - longest - fixed
    ok = fn("C:\\" + "r" * (root_len_ok - 3), key, *PERIOD)
    assert len(str(ok)) + 1 + longest == ol.MAX_PATH_CHARS
    with pytest.raises(OutputLayoutError, match="too long"):
        fn("C:\\" + "r" * (root_len_ok - 2), key, *PERIOD)


def test_reviewer_record_path_at_the_limit_and_one_over():
    key = "k" * 63
    fixed = 1 + len("reviewer") + 1 + 63 + 1 + 21
    root_len = ol.MAX_PATH_CHARS - 1 - 67 - fixed
    path = ol.reviewer_record_path("C:\\" + "r" * (root_len - 3), key, *PERIOD, RUN_ID, SHA)
    assert len(str(path)) == ol.MAX_PATH_CHARS
    assert path.name == f"reviewer.{RUN_ID}.{SHA[:16]}.json"
    with pytest.raises(OutputLayoutError, match="too long"):
        ol.reviewer_record_path("C:\\" + "r" * (root_len - 2), key, *PERIOD, RUN_ID, SHA)


def test_attempt_record_path_at_the_limit_and_one_over():
    key = "k" * 63
    fixed = 1 + len("attempts") + 1 + 63 + 1 + 21
    root_len = ol.MAX_PATH_CHARS - 1 - 63 - fixed
    path = ol.attempt_record_path("C:\\" + "r" * (root_len - 3), key, *PERIOD, STAMP, SHA, "verify_draft", 999)
    assert len(str(path)) == ol.MAX_PATH_CHARS
    with pytest.raises(OutputLayoutError, match="too long"):
        ol.attempt_record_path("C:\\" + "r" * (root_len - 2), key, *PERIOD, STAMP, SHA, "verify_draft", 999)


def test_longest_pdf_variant_path_at_the_limit_and_one_over():
    key = "k" * 63
    fixed = 1 + len("reports") + 1 + 63 + 1 + 21
    root_len = ol.MAX_PATH_CHARS - 1 - 41 - fixed
    name = ol.superseded_pdf_name(H, 99)
    path = ol.report_file_path("C:\\" + "r" * (root_len - 3), key, *PERIOD, name)
    assert len(str(path)) == ol.MAX_PATH_CHARS
    with pytest.raises(OutputLayoutError, match="too long"):
        ol.report_file_path("C:\\" + "r" * (root_len - 2), key, *PERIOD, name)


def test_accepted_folder_can_hold_every_supported_file_name():
    root = "C:\\" + "r" * 60
    for filename in (["report.pdf", ol.DRAFT_PDF_NAME, ol.DRAFT_PARTIAL_NAME]
                     + list(ol.variant_candidates("superseded", H)) + list(ol.variant_candidates("duplicate", H))):
        assert len(str(ol.report_file_path(root, "k" * 63, *PERIOD, filename))) <= ol.MAX_PATH_CHARS
    for step in ol.ATTEMPT_STEPS:
        for collision in (1, 2, 999):
            path = ol.attempt_record_path(root, "k" * 63, *PERIOD, STAMP, SHA, step, collision)
            assert len(str(path)) <= ol.MAX_PATH_CHARS


def test_root_limit_is_necessary_only_and_follows_the_longest_file_name():
    assert ol.MAX_ROOT_CHARS == 259 - (1 + 8 + 1 + 1 + 1 + 21 + 1 + 67)
    root = "C:\\" + "r" * (ol.MAX_ROOT_CHARS - 3)
    assert len(ol.reviewer_record_path(root, "k", *PERIOD, RUN_ID, SHA).as_posix()) == ol.MAX_PATH_CHARS
    with pytest.raises(OutputLayoutError, match="too long"):  # a short root still fails for a long key
        ol.reviewer_record_path(root, "k" * 63, *PERIOD, RUN_ID, SHA)


def test_root_over_the_limit_rejected(base):
    long_root = base / ("d" * ol.MAX_ROOT_CHARS)
    with pytest.raises(OutputLayoutError, match="characters"):
        ol.resolve_output_root(long_root, environ=env(base))


def test_unsupported_names_rejected_by_file_path_functions():
    root = "C:\\out"
    for bad in ("evil.pdf", "../report.pdf", "report.pdf.bak", f"report.superseded.{H}.1.pdf"):
        with pytest.raises(OutputLayoutError):
            ol.report_file_path(root, "k", *PERIOD, bad)


# --- reviewer and attempt file names ------------------------------------------------------------------------------

def test_reviewer_record_name_pattern():
    assert ol.reviewer_record_name(RUN_ID, SHA) == f"reviewer.{RUN_ID}.{'ab' * 8}.json"
    for bad_run in ("", "x", RUN_ID.upper(), RUN_ID + "\n", None):
        with pytest.raises(OutputLayoutError):
            ol.reviewer_record_name(bad_run, SHA)
    for bad_sha in ("a" * 63, "A" * 64, None):
        with pytest.raises(OutputLayoutError):
            ol.reviewer_record_name(RUN_ID, bad_sha)


def test_attempt_record_name_pattern_for_every_approved_step():
    assert ol.ATTEMPT_STEPS == ("preflight", "verify_draft", "persist", "event", "supersede", "promote", "record")
    for step in ol.ATTEMPT_STEPS:
        assert ol.attempt_record_name(STAMP, SHA, step) == f"20260203T093000Z.{'ab' * 8}.{step}.attempt.json"
        assert ol.attempt_record_name(STAMP, None, step, 2) == f"20260203T093000Z.nopdf.{step}.attempt.2.json"


@pytest.mark.parametrize("step", ["assemble", "draft_pdf", "Persist", "", None, "persist/../x"])
def test_attempt_record_name_rejects_unapproved_steps(step):
    with pytest.raises(OutputLayoutError):
        ol.attempt_record_name(STAMP, SHA, step)


@pytest.mark.parametrize("stamp", ["2026-02-03", "2026-02-03T09:30:00Z", "2026-02-03T09:30:00+01:00",
                                   "2026-02-30T09:30:00+00:00", "2026-02-03T09:30:00+00:00\n", None, 5])
def test_attempt_record_name_rejects_bad_timestamps(stamp):
    with pytest.raises(OutputLayoutError):
        ol.attempt_record_name(stamp, SHA, "persist")


@pytest.mark.parametrize("collision", [0, -1, 1000, True, 2.0, "2", None])
def test_attempt_record_name_rejects_bad_collision_numbers(collision):
    with pytest.raises(OutputLayoutError):
        ol.attempt_record_name(STAMP, SHA, "persist", collision)

# --- file names ---------------------------------------------------------------------------------------------------

def test_standard_name_and_variant_one_have_no_number():
    assert ol.STANDARD_PDF_NAME == "report.pdf"
    assert ol.superseded_pdf_name(H) == f"report.superseded.{H}.pdf"
    assert ol.duplicate_pdf_name(H) == f"report.duplicate.{H}.pdf"
    assert ol.superseded_pdf_name(H, 1) == ol.superseded_pdf_name(H)


def test_numbered_variants_two_to_ninety_nine():
    assert ol.superseded_pdf_name(H, 2) == f"report.superseded.{H}.2.pdf"
    assert ol.duplicate_pdf_name(H, 99) == f"report.duplicate.{H}.99.pdf"


@pytest.mark.parametrize("number", [0, -1, 100, True, False, 2.0, "2", None])
def test_invalid_variant_numbers_rejected_by_builders(number):
    with pytest.raises(OutputLayoutError):
        ol.superseded_pdf_name(H, number)


@pytest.mark.parametrize("bad", ["", "0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0", "0123456789abcdeg",
                                 " 0123456789abcdef", "0123456789abcdef\n", None, 1234567890123456])
def test_invalid_hashes_rejected(bad):
    with pytest.raises(OutputLayoutError):
        ol.duplicate_pdf_name(bad)


def test_hash16_of_full_digest():
    digest = H + H2 + H + H2
    assert ol.hash16_of(digest) == H
    for bad in (digest.upper(), digest[:-1], digest + "0", None):
        with pytest.raises(OutputLayoutError):
            ol.hash16_of(bad)


def test_unknown_kind_rejected():
    with pytest.raises(OutputLayoutError):
        ol.variant_pdf_name("backup", H)


def test_parser_round_trips_every_variant():
    for kind in ol.VARIANT_KINDS:
        for number in range(1, 100):
            name = ol.variant_pdf_name(kind, H, number)
            assert ol.parse_variant_pdf_name(name) == ol.VariantName(kind, H, number)


@pytest.mark.parametrize("name", [
    f"report.superseded.{H.upper()}.pdf", "report.superseded.0123.pdf", f"report.superseded.{H}.PDF",
    f"report.superseded.{H}.pdf.bak", f"report.superseded.{H}.pdf ", f"report.superseded.{H}.pdf\n",
    f"report.superseded.{H}.0.pdf", f"report.superseded.{H}.1.pdf", f"report.superseded.{H}.01.pdf",
    f"report.superseded.{H}.02.pdf", f"report.superseded.{H}.100.pdf", f"report.superseded.{H}.-2.pdf",
    f"report.superseded.{H}.2.3.pdf", f"report.superseded.{H}.x.pdf", f"report.superseded.{H}..pdf",
    f"Report.superseded.{H}.pdf", f"report.other.{H}.pdf", f"report.superseded.{H}.txt",
    f"sub/report.superseded.{H}.pdf", f"sub\\report.superseded.{H}.pdf", f"../report.duplicate.{H}.pdf",
    f"report.duplicate.{H}", f"report.superseded.{H}.duplicate.pdf", "report.pdf", "", None, 5,
])
def test_parser_rejects_malformed_names(name):
    with pytest.raises(OutputLayoutError):
        ol.parse_variant_pdf_name(name)


def test_report_pdf_name_parser_accepts_standard_name():
    assert ol.parse_report_pdf_name("report.pdf") is None
    assert ol.parse_report_pdf_name(f"report.duplicate.{H}.7.pdf").number == 7
    for bad in ("Report.pdf", "report.pdf ", "report.PDF", "other.pdf"):
        with pytest.raises(OutputLayoutError):
            ol.parse_report_pdf_name(bad)


def test_ninety_nine_candidates_in_order_and_boundaries():
    names = ol.variant_candidates("superseded", H)
    assert len(names) == 99 == len(set(names))
    assert names[0] == f"report.superseded.{H}.pdf"
    assert names[1] == f"report.superseded.{H}.2.pdf"
    assert names[-1] == f"report.superseded.{H}.99.pdf"


def test_first_free_variant_skips_taken_names_case_insensitively():
    taken = [f"report.duplicate.{H}.pdf", f"REPORT.DUPLICATE.{H}.2.PDF", f"report.duplicate.{H2}.3.pdf"]
    assert ol.first_free_variant("duplicate", H, taken) == f"report.duplicate.{H}.3.pdf"
    assert ol.first_free_variant("duplicate", H, []) == f"report.duplicate.{H}.pdf"


def test_first_free_variant_reaches_ninety_nine_then_fails():
    taken = list(ol.variant_candidates("superseded", H))[:-1]
    assert ol.first_free_variant("superseded", H, taken) == f"report.superseded.{H}.99.pdf"
    with pytest.raises(OutputLayoutError, match="99"):
        ol.first_free_variant("superseded", H, ol.variant_candidates("superseded", H))


def test_first_free_variant_rejects_bad_input():
    with pytest.raises(OutputLayoutError):
        ol.first_free_variant("superseded", H, "report.pdf")
    with pytest.raises(OutputLayoutError):
        ol.first_free_variant("superseded", H, [None])
