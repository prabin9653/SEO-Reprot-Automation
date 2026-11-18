"""Operator conveniences of the CLI: `clients check`, `clients add` and `report open` (stage 1 of the operator-experience work).

    clients check   one table for every client: ready or what to fix, whether this device already has the report, whether Google is verified here
    clients add     register a sheet client in the local client registry (config/clients.yaml); never contacts Google except `--all-missing`, which reads the workbook
    report open     open a finished report PDF

What these commands never do
  * They never write the ledger, the history, the output folder or any report. `clients add` writes only the local registry file (a backup copy first, then an
    atomic replace) and refuses anything that would make the file invalid, duplicate a name or reuse a key.
  * `clients check` reads the reporting workbook (read-only Google Sheets access, like `clients list`) and the LOCAL verification attestation; it does not verify
    Google access itself (that is `verify live`).
  * `report open` opens only `report.pdf` inside the configured output folder.
"""

import json
import os
import re
import tempfile
from pathlib import Path

import yaml

from seo_report import activity, cli, client_config, guidance, identity
from seo_report.cli import CliError, emit, load_settings
from seo_report.cli_exit import Exit

_PERIOD_DIR = re.compile(r"\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}")
_DONE = ("audited", "publishing", "ready_for_delivery", "delivered")
_RUNNING = ("requested", "collecting", "rendering", "rendered", "finalizing", "finalized", "auditing")


# --- keys ---------------------------------------------------------------------------------------------------------------------------------

def suggest_key(name: str) -> str:
    """A candidate client key from a sheet name: lower case, `&` as `and`, apostrophes dropped, every other run of characters as one `-`, at most 63 characters.
    Only a suggestion for a NEW client: once it is written to the registry it is permanent."""
    text = re.sub(r"['’`]", "", name.replace("&", " and ").casefold())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:63].strip("-")


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


# --- clients add --------------------------------------------------------------------------------------------------------------------------

def _list_indent(existing: str) -> str:
    """The indentation of the registry's list items (taken from the last `- ` line, so entries added by hand or by a YAML dumper keep matching)."""
    found = re.findall(r"^([ \t]*)-[ \t]", existing, flags=re.MULTILINE)
    return found[-1] if found else "  "


def _entry_lines(name: str, key: str, newline: str, indent: str = "  ") -> str:
    return (f"{indent}- name: {json.dumps(name, ensure_ascii=False)}{newline}{indent}  client_key: {json.dumps(key)}{newline}")


def _read_registry_text(path: Path) -> tuple[str, "client_config.ClientRegistry | None"]:
    if not path.exists():
        return "clients:\n", None
    try:
        registry = client_config.load_client_registry(path)
        text = path.read_text(encoding="utf-8")
    except (client_config.ClientConfigError, OSError):
        raise CliError(Exit.CONFIG_INVALID, "the client registry (clients_config) is missing or invalid", "client_registry_invalid") from None
    return text, registry


def _conflicts(registry, name: str, key: str) -> CliError | None:
    if registry is None:
        return None
    for entry in registry.entries:
        if entry.name == name:
            return CliError(Exit.CONFIG_INVALID, f"{name!r} is already registered (key {entry.client_key}); nothing was changed", "client_already_registered")
        if _fold(entry.name) == _fold(name):
            return CliError(Exit.CONFIG_INVALID, f"a client that differs only in case or spacing is already registered: {entry.name!r} (key {entry.client_key}); "
                            "use the sheet's exact spelling in the sheet and here; nothing was changed", "client_name_similar")
        if entry.client_key == key:
            return CliError(Exit.CONFIG_INVALID, f"the key {key!r} is already used by {entry.name!r}; choose another with --key; nothing was changed",
                            "client_key_in_use")
    return None


def _write_registry(path: Path, existing: str, entries: list[tuple[str, str]]) -> bool:
    """Append `entries` to the registry text, check the result parses as a valid registry, then replace the file atomically (a `.bak` copy of the old one is
    kept next to it). Returns True when a backup was made. Writes nothing when the new text would be invalid."""
    newline = "\r\n" if "\r\n" in existing else "\n"
    base = existing if existing.endswith("\n") or not existing else existing + newline
    indent = _list_indent(existing)
    new_text = base + "".join(_entry_lines(name, key, newline, indent) for name, key in entries)
    try:
        client_config.parse_client_config(yaml.load(new_text, Loader=client_config._StrictLoader))
    except (client_config.ClientConfigError, yaml.YAMLError) as error:
        raise CliError(Exit.CONFIG_INVALID, f"the entry would make the client registry invalid ({error}); nothing was changed", "client_registry_invalid") from None
    backed_up = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if backed_up:
            path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(new_text.encode("utf-8"))
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError:
        raise CliError(Exit.CONFIG_INVALID, "the client registry file could not be written; nothing was changed", "client_registry_unwritable") from None
    return backed_up


def handle_clients_add(ctx) -> int:
    settings = load_settings(ctx)
    path = Path(settings.clients_config_path)
    existing, registry = _read_registry_text(path)
    args = ctx.args
    plan, skipped = [], []
    if args.name:
        try:
            name = client_config.validate_client_name(args.name, where="--name")
        except client_config.ClientConfigError as error:
            raise CliError(Exit.USAGE, str(error), "client_name_invalid") from None
        key = args.key or suggest_key(name)
        try:
            key = client_config.validate_client_key(key, where="the client key")
        except client_config.ClientConfigError as error:
            raise CliError(Exit.USAGE, f"{error}; choose one with --key", "client_key_invalid") from None
        problem = _conflicts(registry, name, key)
        if problem:
            raise problem
        plan.append((name, key))
    else:
        stand_in = None
        if registry is None:  # a new device without a registry file: read the sheet against a stand-in that matches no client, so every sheet client is "missing"
            stand_in = client_config.ClientRegistry([client_config.ClientEntry("(no registry yet)", "no-registry-yet")])
        _, loaded = cli._read_workbook(ctx, settings, registry=stand_in)
        used = {entry.client_key for entry in registry.entries} if registry else set()
        for name in sorted({name for _row, name in loaded.unregistered}):
            try:
                client_config.validate_client_name(name)
                key = client_config.validate_client_key(suggest_key(name))
            except client_config.ClientConfigError:
                skipped.append({"name": name, "reason": "no valid key can be made from this name; add it with --name and --key"})
                continue
            if key in used or _conflicts(registry, name, key):
                skipped.append({"name": name, "reason": "its key is already used or a similar name is registered; add it with --name and --key"})
                continue
            used.add(key)
            plan.append((name, key))
    added = [{"name": name, "client_key": key} for name, key in plan]
    if args.name or args.yes:
        backed_up = _write_registry(path, existing, plan) if plan else False
        lines = [f"Registered {name!r} as key {key} (permanent: do not change it once a report exists)" for name, key in plan] or ["Nothing to register."]
        lines += [f"Not registered: {s['name']!r}: {s['reason']}" for s in skipped]
        if plan:
            lines.append("Next: `clients check` shows whether the sheet row is ready." + (" The previous registry was kept as clients.yaml.bak." if backed_up else ""))
        emit(ctx, {"written": True, "added": added, "skipped": skipped, "backup": bool(plan and backed_up)}, lines)
        return int(Exit.OK)
    lines = [f"Plan only: {len(plan)} client(s) would be registered. Add --yes to write them."]
    lines += [f"  {name!r} -> {key}" for name, key in plan] or ["  (every sheet client already has a key)"]
    lines += [f"  not possible: {s['name']!r}: {s['reason']}" for s in skipped]
    emit(ctx, {"written": False, "added": added, "skipped": skipped, "backup": False}, lines)
    return int(Exit.OK)


# --- clients check ------------------------------------------------------------------------------------------------------------------------

def _report_info(ledger, key: str, period_id: str) -> tuple[str, str | None]:
    """(state, when) of the latest run of this client and period on this device. `when` is a UTC timestamp string from the ledger (shown in each viewer's own
    time zone by the page): for a finished report the moment it was finalized, otherwise the last update of the run; None when nothing was run."""
    if ledger is None:
        return "not_run", None
    run = ledger.latest_run(key, period_id)
    if run is None:
        return "not_run", None
    if run.state in _DONE:
        state, order = "done", ("finalized_at", "audited_at", "finished_at", "updated_at")
    else:
        state = "interrupted" if run.state in _RUNNING else run.state  # failed, blocked, cancelled ...
        order = ("updated_at", "finished_at")
    return state, next((value for value in (getattr(run, field, None) for field in order) if isinstance(value, str) and value), None)


def _report_state(ledger, key: str, period_id: str) -> str:
    return _report_info(ledger, key, period_id)[0]


def _utc_text(timestamp: str | None) -> str:
    """`2026-11-17 20:45 UTC` from a ledger timestamp (for the console; the page converts to the viewer's time zone)."""
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(timestamp).astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return ""


def _generated_by(ledger, key: str, period_id: str):
    """The person whose run finished the report on this device (None when unknown). Never raises: attribution is a convenience, not a requirement."""
    try:
        run = ledger.latest_run(key, period_id) if ledger is not None else None
        return identity.generated_by(ledger.requests_for_run(run.run_key)) if run is not None else None
    except Exception:
        return None


_STATE_TEXT = {"not_run": "not run yet", "done": "DONE (report exists on this device)", "interrupted": "interrupted (run again to resume)",
               "failed": "failed last time (run again to retry)", "blocked": "blocked (see `recovery inspect`)"}


def handle_clients_check(ctx) -> int:
    from seo_report import attestation, preflight
    from seo_report.cli_report import open_ledger
    settings = load_settings(ctx)
    wanted = ctx.args.client
    registry, loaded = cli._read_workbook(ctx, settings, only=wanted)
    names = {entry.client_key: entry.name for entry in registry.entries}
    verdict = preflight.attestation_verdict(cli._preflight_inputs(ctx, settings, launch=False))
    ledger = open_ledger(ctx, settings, "read")
    rows = []
    events = activity.read_events(settings.activity_dir) if getattr(settings, "activity_dir", None) else []  # the team's shared log (untrusted input), if configured
    this_device = identity.device_name(ctx.environ)
    try:
        for key, run in sorted(loaded.clients.items()):
            fix = attestation.client_coverage_code(verdict, key, run.client.gsc_property, run.client.ga4_property_id)
            state, when = _report_info(ledger, key, run.period_id)
            elsewhere = activity.latest_elsewhere(events, key, run.period_id, this_device) if state != "done" else None
            rows.append({"client_key": key, "name": names.get(key, key), "status": "ready", "period_id": run.period_id, "sheet_row": run.client.sheet_row,
                         "report": state, "last_run_at": when, "generated_by": _generated_by(ledger, key, run.period_id) if state == "done" else None,
                         "elsewhere": {"operator": elsewhere["operator"], "device": elsewhere["device"], "at": elsewhere["at"]} if elsewhere else None,
                         "google_verified": fix is None, "verify_code": fix, "code": None})
    finally:
        if ledger is not None:
            ledger.close()
    for key, code in loaded.skipped:
        rows.append({"client_key": key, "name": names.get(key, key), "status": "skipped", "code": code})
    problems = 0
    if wanted is None:
        for sheet_row, name in loaded.unregistered:
            rows.append({"client_key": None, "name": name, "status": "not_registered", "sheet_row": sheet_row, "code": "client_not_in_registry"})
            problems += 1
    for r in loaded.rejected:
        if r.code == "client_not_in_registry":
            continue  # shown above, with the client's name
        if r.client_key is None and wanted is not None:
            continue  # a row problem that cannot be tied to the requested client
        rows.append({"client_key": r.client_key, "name": names.get(r.client_key or "", r.client_key or "(unknown row)"), "status": "needs_fix",
                     "sheet_row": r.sheet_row, "code": r.code})
        problems += 1
    if wanted is None:
        seen = {row["client_key"] for row in rows if row["client_key"]}
        for key, name in names.items():
            if key not in seen:
                rows.append({"client_key": key, "name": name, "status": "not_in_sheet", "code": None})
    order = {"ready": 0, "needs_fix": 1, "not_registered": 2, "skipped": 3, "not_in_sheet": 4}
    rows.sort(key=lambda row: (order[row["status"]], str(row["name"]).casefold()))
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in order}
    lines = ["Client check (the workbook was read, read-only; Google access itself is checked by `verify live`)", ""]
    for row in rows:
        label, who = row["status"], f"{row['client_key'] or '(no key)'}"
        if label == "ready":
            verified = "Google verified here" if row["google_verified"] else f"needs verify: {guidance.row_fix(row['verify_code'])}"
            when_text = _utc_text(row.get("last_run_at"))
            by_text = f" by {row['generated_by']}" if row.get("generated_by") else ""
            stamp = f" ({when_text}{by_text})" if when_text else ""
            lines.append(f"  READY      {who:<32} {row['period_id']}  report: {_STATE_TEXT.get(row['report'], row['report'])}{stamp}; {verified}")
            if row.get("elsewhere"):
                other = row["elsewhere"]
                lines.append(f"             already generated on another computer ({other['device']}) by {other['operator']} at {_utc_text(other['at'])}; "
                             "running it here makes a second copy")
        elif label == "needs_fix":
            lines.append(f"  FIX        {who:<32} row {row['sheet_row']}: {row['code']} - {guidance.row_fix(row['code'])}")
        elif label == "not_registered":
            lines.append(f"  NOT KEYED  {row['name']!r} (row {row['sheet_row']}): {guidance.row_fix(row['code'])}")
        elif label == "skipped":
            lines.append(f"  SKIPPED    {who:<32} {guidance.row_fix(row['code'])}")
        else:
            lines.append(f"  NOT IN SHEET {who:<30} registered here but no row with this name was found in the sheet")
    lines += ["", f"{counts['ready']} ready, {counts['needs_fix'] + counts['not_registered']} need fixing, {counts['skipped']} skipped (not Active), "
              f"{counts['not_in_sheet']} registered but not in the sheet."]
    if counts["ready"] and any(not r["google_verified"] for r in rows if r["status"] == "ready"):
        lines.append("Before running a report: `verify live --client <key> --live` for each client marked \"needs verify\".")
    emit(ctx, {"clients": rows, "counts": counts}, lines, ok=problems == 0)
    return int(Exit.CONFIG_INVALID if problems else Exit.OK)


# --- report open --------------------------------------------------------------------------------------------------------------------------

def _start_file(path: str) -> None:
    if not hasattr(os, "startfile"):
        raise CliError(Exit.CONFIG_INVALID, "opening a PDF from here is only supported on Windows; use --no-open and open the printed location", "open_unsupported")
    os.startfile(path)  # noqa: S606 - the operator's own report, inside the output folder


def handle_report_open(ctx) -> int:
    settings = load_settings(ctx)
    key, period = ctx.args.client, ctx.args.period
    root = Path(settings.output_root).resolve()
    folder = root / "reports" / key
    if period:
        candidates = [period] if (folder / period / "report.pdf").is_file() else []
    else:
        candidates = sorted((p.name for p in folder.iterdir() if p.is_dir() and _PERIOD_DIR.fullmatch(p.name) and (p / "report.pdf").is_file()),
                            reverse=True) if folder.is_dir() else []
    if not candidates:
        raise CliError(Exit.CONFIG_INVALID, f"no finished report was found for {key}" + (f" and period {period}" if period else "") + " on this device",
                       "report_not_found")
    chosen = candidates[0]
    pdf = (folder / chosen / "report.pdf").resolve()
    try:
        pdf.relative_to(root)
    except ValueError:
        raise CliError(Exit.CONFIG_INVALID, "the report location is outside the output folder; refusing to open it", "report_path_unsafe") from None
    shown = cli_redact(ctx, pdf)
    opened = False
    if not ctx.args.no_open:
        (ctx.extra.get("opener") or _start_file)(str(pdf))
        opened = True
    lines = [f"{key}, period {chosen}: {shown}", "Opened in your PDF viewer." if opened else "Not opened (--no-open)."]
    if not period and len(candidates) > 1:
        lines.append(f"Other periods on this device: {', '.join(candidates[1:])} (use --period to pick one).")
    emit(ctx, {"client_key": key, "period_id": chosen, "pdf": shown, "opened": opened, "other_periods": candidates[1:] if not period else []}, lines)
    return int(Exit.OK)


def cli_redact(ctx, path) -> str:
    from seo_report import preflight
    return preflight.redact_path(path, ctx.environ)


# --- setup device -------------------------------------------------------------------------------------------------------------------------

def handle_setup_device(ctx) -> int:
    """The per-device setup (see `device_setup`). Needs no settings file: creating it is part of the job."""
    from seo_report import device_setup, preflight
    show = lambda path: preflight.redact_path(path, ctx.environ)  # noqa: E731
    result = device_setup.run_device_setup(ctx.environ, spreadsheet=ctx.args.spreadsheet_id, acl_applier=ctx.extra.get("acl_applier", device_setup.restrict_to_current_user), shared_folder=ctx.args.shared_folder,
                                          project_root=ctx.extra.get("project_root"), show=show)
    mark = {"ok": "ok     ", "created": "created", "todo": "TO DO  ", "warning": "warning", "problem": "PROBLEM"}
    lines = ["Device setup (nothing was sent anywhere, nothing was installed)", ""]
    for step in result.steps:
        lines.append(f"  [{mark[step.status]}] {step.name}: {step.detail}")
        if step.fix and step.status != "ok":
            lines.append(f"             -> {step.fix}")
    pending = [s for s in result.steps if s.status in ("todo", "problem")]
    lines += ["", "Everything on this device is ready. Next: `seo web` (or `seo clients check`)." if not pending and not [s for s in result.steps if s.status == "warning"]
              else f"{len(result.problems)} problem(s) and {len(result.todos)} step(s) still to do; follow the arrows above, then run this command again to check."]
    data = {"steps": [{"name": s.name, "status": s.status, "detail": s.detail, "fix": s.fix} for s in result.steps],
            "problems": len(result.problems), "todo": len(result.todos), "ready": not pending}
    emit(ctx, data, lines, ok=not result.problems)
    return int(Exit.CONFIG_INVALID if result.problems else Exit.OK)


# --- web ----------------------------------------------------------------------------------------------------------------------------------

def handle_web(ctx) -> int:
    """Serve the local web page (see `webui`) until Ctrl+C. The settings are validated first so a broken configuration is reported here, not inside the page."""
    import webbrowser
    from seo_report import webui
    load_settings(ctx)
    server = webui.make_server(webui.CliRunner(config_path=ctx.extra["config_path"], environ=ctx.environ), port=ctx.args.port)
    url = server.url()
    emit(ctx, {"url": url, "port": server.port, "listening_on": webui.HOST},
         [f"Jasper SEO Reports is running on this computer only (127.0.0.1, port {server.port}).", f"Open: {url}",
          "The address contains a one-time access token; it changes every time this starts. Press Ctrl+C here to stop."])
    try:
        if not ctx.args.no_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()
    return int(Exit.OK)


HANDLERS = {("clients", "check"): handle_clients_check, ("clients", "add"): handle_clients_add, ("report", "open"): handle_report_open,
            ("web", None): handle_web, ("setup", "device"): handle_setup_device}
