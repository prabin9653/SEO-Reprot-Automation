# Credential relocation procedure (written at R8.4.6, when it was NOT performed; performed by the operator in R8.7d)

> **Current status (R8.7d, 2026-11-02).** The operator carried this procedure out from a normal Windows PowerShell session (not from Claude Desktop, whose packaged environment redirects `%LOCALAPPDATA%`): the OAuth client file and the cached sign-in were **copied** to `%LOCALAPPDATA%\JasperSEOReporting\credentials\`, the destination ACL (current user, SYSTEM, Administrators) and the source/destination SHA-256 hashes were verified at that time, and an external `runner.yaml` under `%LOCALAPPDATA%\JasperSEOReporting\config\` sets `paths.oauth_client_file` and `paths.oauth_cache_file`. **Not done:** §9 (retiring the OneDrive originals and revoking the old token); the original copies are retained. `doctor` verifies the configured locations (both files outside the project and cloud-synchronised folders) and file states, not ACLs or contents. Everything below is the original, historical procedure text.

**Status at R8.4.6: Proposed.** This is a procedure for a person to carry out later. In R8.4.6 no credential or token file was opened, printed,
copied, moved, renamed, rotated or deleted, and nothing in the code was changed to point at a new location. Every command below is
for the operator, run at the operator's own console, in a separate approved step.

## 0. Why

Today (verified from `auth.py` constants and `.gitignore`, not from the files' contents):

* the cached OAuth token is `<project>\token.json` and the OAuth client secret is `<project>\credentials\client_secret.json`;
* the project folder is under OneDrive on the author's machine, so both files are **synchronised to Microsoft's cloud**, may be
  restored from OneDrive version history, may be exposed through shared-folder settings, and are copied to every device that
  syncs the folder. They are excluded from Git, but Git is not the only copy mechanism.
* The token grants **read-only** access (spreadsheets, Search Console, Analytics) as the signed-in Google user, for as long as the
  refresh token is valid. The client secret identifies the Google Cloud OAuth client.

## 1. Destination

`%LOCALAPPDATA%\JasperSEOReporting\credentials\` for the Windows account that will run the reports (for example
`C:\Users\<runner-account>\AppData\Local\JasperSEOReporting\credentials\`).

* It is local, per-user, not synchronised by OneDrive (confirm with `scripts/check_deployment.py`-style canonical check or
  `Get-Item` that the folder is not under any OneDrive root), and next to the existing `output`, `history`, `ledger`, `work` and
  `logs` folders.
* Final layout: `...\credentials\token.json` and `...\credentials\client_secret.json`. Do not put either file anywhere under the
  project or any synchronised folder.

## 2. Windows ACL requirements

On the new folder, remove inherited permissions and grant Full Control to **only** the runner account (and, if your policy requires,
`SYSTEM` and Administrators for recovery):

```
icacls "%LOCALAPPDATA%\JasperSEOReporting\credentials" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"
icacls "%LOCALAPPDATA%\JasperSEOReporting\credentials" /verify
```

Verify with `icacls <folder>` that no `Users`, `Everyone` or `Authenticated Users` entry remains. The files inherit from the folder;
create them *after* the ACL is set so they are never briefly readable by others.

## 3. Same-user execution

The token is cached by, and refreshed for, one Windows account. The setup command (`python -m seo_report.auth setup`), the
reporting runner and the Scheduled Task must run as the **same Windows user**, because the ACL, `%LOCALAPPDATA%` and any later
DPAPI/Credential Manager storage are per account. Running the runner as another account (or as SYSTEM) will look in another
profile and fail with `auth_token_missing`.

## 4. Scheduled Task requirements

* Task "Run as" user = the runner account; "Run whether user is logged on or not" requires that account's password to be stored by
  Task Scheduler (or use "Run only when user is logged on").
* "Start in" = a folder outside OneDrive; the Python executable is the one in the deployment virtual environment.
* The account needs read/write on the credentials folder, the output/history/ledger/work/logs folders and the Playwright browser
  folder, and **no** interactive desktop: the runner never opens a browser (R8.4.5). If the token is missing or revoked the run fails
  fast with `auth_required` and the batch stops.
* Re-authentication (`auth setup`) is an attended action at a console of that same account, never part of the task.

## 5. Token refresh and atomic writes

`auth.get_credentials()` refreshes an expired access token with the stored refresh token and rewrites the token file **atomically**
(temporary file in the same folder, then replace). Consequences for the new location: the account needs create/rename permission in
the credentials folder (not just write on one file); antivirus or backup tools holding the file open can make the save fail — this is
logged (`the refreshed token could not be saved`) and the run continues with valid in-memory credentials, but the next run will
refresh again. A refresh token can still be revoked by Google (password change, removed app access, 7-day expiry while the OAuth
consent screen is in "Testing" status); that appears as `auth_credentials_revoked` / `auth_refresh_failed` and needs `auth setup`.

## 6. Backup and recovery precautions

1. **Before moving anything**, decide whether a backup is wanted. The token can always be recreated with `auth setup` (a few minutes
   at a console); the client secret can be re-downloaded from the Google Cloud console by an authorised person. Therefore the safest
   backup is *none outside the new folder*: do not leave extra copies in OneDrive, e-mail, chat, tickets or USB sticks.
2. If an offline backup is required, copy it to encrypted removable media or a password manager's secure-file feature, record where
   it is, and treat it like a password.
3. Never paste either file's contents into a chat, issue or log. The logging layer redacts token-shaped strings, but that is a safety
   net, not permission.
4. Plan for loss: if the new folder is lost, re-run `auth setup` and re-download the client secret.

## 7. How to verify authentication at the new location (after the move, by a person)

**Since R8.6b the code supports configurable locations** (Implemented and tested offline). Without configuration it still reads `auth.TOKEN_PATH` and
`auth.CLIENT_SECRET_PATH` (inside the project) — the migration-compatible fallback. To point the system at the new folder, a person sets, in the runner
settings file, **both**:

```
paths:
  oauth_client_file: C:\Users\<runner-account>\AppData\Local\JasperSEOReporting\credentials\client.json
  oauth_cache_file:  C:\Users\<runner-account>\AppData\Local\JasperSEOReporting\credentials\login.json
```

(`%LOCALAPPDATA%\...` is accepted too.) The key names avoid the words "secret" and "token" because the settings loader refuses credential-looking keys; the files may keep their
current names. The two settings are used by `setup auth` **and** by normal reporting, so they cannot disagree. **Exact migration steps (a person, later, separately authorised):**

1. Create the folder and apply the ACL (§2); do not copy anything yet.
2. Add the two settings above to the runner settings file (the old files still exist, nothing is read from the new place yet).
3. Move the two files by hand from the project to the new folder (do not leave a copy in OneDrive), **or** leave the token behind and run `python -m seo_report setup auth --config <settings>` at an
   attended console to create a fresh token directly in the new folder (the client file must be placed there first; the command refuses to start, and creates nothing, if it is missing).
4. Run `python -m seo_report clients validate --config <settings>`: it reports `oauth_client_file`/`oauth_cache_file` as `present`/`missing` and whether the locations are outside the project.
   Then `clients list` (a read-only workbook read) to prove the credentials work, and check the log for `auth_*` codes.
5. Retire the old copies as in §9.

After the files were moved:
1. `python -m seo_report.auth setup` is *not* needed if the moved token is still valid; instead run a dry, read-only check that calls
   `get_credentials()` and prints only the outcome code (success / `auth_*` code), never any value.
2. Run `scripts/check_deployment.py --settings <runner.yaml>` (assets, storage, dependencies; it does not look at credentials).
3. Run one **dry run** (`run_client(mode="dry_run")`) for one client from the scheduled-task account, then check `logs\seo_report.log`
   for `auth_*` codes.
4. Confirm that an expired/missing token produces `auth_required` and **no browser window** (run once with the token temporarily
   absent in a *test* credentials folder, not the real one).

## 8. Confirm that nothing still depends on the old path

* Search the repository for the old locations: `Select-String -Path src,scripts,tests -Pattern "token.json|client_secret|credentials"
  -Recurse` — after the configurable-directory change, the only remaining references should be the default-name constants and tests
  that use temporary folders. Today `scripts\inspect_sheet.py` imports `get_credentials` (now non-interactive) and will follow the
  change automatically; `docs\` mention the old names.
* `.gitignore` entries for the old names may stay (harmless) until the old files are retired.
* Check Scheduled Tasks, shortcuts and notes for any command that sets the working directory to the project for authentication.

## 9. Retire the old location safely (only after step 7 passed on at least one scheduled run)

1. Keep the old files in place until the new location has served at least one full successful scheduled run.
2. Then, by a person: move (not copy) any remaining old files into the new folder if they are still needed; otherwise delete them
   **through the OneDrive-aware route** — after deletion, empty the OneDrive recycle bin and remove the files from OneDrive **version
   history** (web interface), because a deleted file stays recoverable there.
3. Consider the old token exposed: **revoke it** at <https://myaccount.google.com/permissions> (remove the app's access) and run
   `auth setup` again, which issues a new refresh token that never touched OneDrive. If the client secret was exposed by
   synchronisation, create a new OAuth client secret in the Google Cloud console and retire the old one (this is credential rotation:
   a separate, explicitly authorised action; OAuth scopes and the project stay as they are unless a person decides otherwise).
4. Record the date and who did it in the operations log.

## 10. Credential-exposure risks from OneDrive synchronisation

| Risk | Detail |
|---|---|
| Cloud copy | Every file in the folder is uploaded to Microsoft's servers and to every other device signed in to the account |
| Version history / recycle bin | Deleted or overwritten token versions stay recoverable for the retention period |
| Sharing | A shared parent folder or link would expose the files to the recipients |
| Sync conflicts | Concurrent atomic rewrites of `token.json` on two devices can produce conflict copies (`token-DeviceName.json`) holding other refresh tokens |
| File locking | OneDrive holding a file open can make the atomic replace fail (`could not be saved`) |
| Malware/endpoint scope | Files synced to additional devices widen the number of machines that can steal them |
| Mitigation | Local-only per-user folder with an owner-only ACL; revoke and re-issue the token after the move |
