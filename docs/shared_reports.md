# Sharing finished reports (Google Drive folder and dashboard)

Reports are made on the **operator computer** (Windows). To let teammates, including those on a Mac, read them, the system can copy every finished PDF into a shared
folder and keep one read-only page, `dashboard.html`, next to them.

## One-time setup (operator computer)
1. Install **Google Drive for desktop** and sign in with the team's Google account.
2. In Google Drive create a folder, for example `SEO Reports`, and **share it with named team accounts only** (never "anyone with the link": it holds client reports).
3. Add one line under `paths:` in `%LOCALAPPDATA%\JasperSEOReporting\config\runner.yaml`:
   ```
   shared_reports_dir: "G:\\My Drive\\SEO Reports"
   ```
   (use the real path of the folder on your computer; it must not be the project folder, the output folder or the credentials folder).
4. Copy the reports you already have: `.\seo.cmd report export`.

From then on every `report run` / `report batch` copies its new PDF and refreshes the dashboard by itself.

## What is in the folder
```
SEO Reports\
  dashboard.html                      read-only list: client, latest period, when, who, PDF link, earlier periods
  <client-key>\<client-key>_<period>.pdf   one verified copy of each finished report (the file name is unique in the whole folder)
```
* A copy is checked against the original (SHA-256) before it is put in place. A file that already exists is **never overwritten**; a different file with the same name is
  reported and left alone. Nothing is deleted.
* The original report stays in the operator computer's output folder. The shared folder is a convenience copy.
* `dashboard.html` is rewritten every time. It shows times in the viewer's own time zone and has a filter box. Each report link opens that file in **Google Drive** (straight to
  the PDF when its Drive ID is known, otherwise a search for its unique file name), in the viewer's own signed-in Drive, so the page works from anywhere, including a
  downloaded copy. Those links are the only web addresses in it.

## Optional: links that open the PDF directly
Without this step a dashboard link opens a Drive search that shows the PDF (one extra click). To make each link open the PDF itself, the system needs each file's Drive ID,
which only the Google Drive API can give. One-time, on the operator computer:
1. In the Google Cloud project that owns your OAuth client: **APIs & Services → Library → Google Drive API → Enable**. If the consent screen lists scopes, add
   `.../auth/drive.metadata.readonly`; while the app is in Testing mode, make sure the team account is a listed test user.
2. `.\seo.cmd setup drive` and sign in with the team account. This permission is **read-only and only covers file names and IDs** (no file content, no changes), but it can
   see the names of every file in that Drive. It is stored in its own file (`token_drive.json`, in the locked credentials folder) and is used only by `report export` and the
   automatic copy; reporting never uses it, and the reporting sign-in and the 7-day verification are not changed.
3. `.\seo.cmd report export` (repeat it a few minutes after new reports, once Drive has finished uploading them). Each report's ID is looked up once and remembered on this
   computer (`drive_file_ids.json` beside the ledger). A report whose ID is not found yet keeps the search link.
While the Google consent screen is in Testing mode this sign-in also expires after about 7 days; the links already in the dashboard keep working, only new lookups need
`setup drive` again.

## For teammates (including a Mac without Google Drive for desktop)
1. In Chrome, sign in to the team's Google account and open the `SEO Reports` folder at drive.google.com.
2. Download `dashboard.html` (right-click, **Download**) and open it in Chrome. Google Drive does **not** display an HTML file as a page, which is why it is downloaded.
3. Click a report link: the PDF opens in Chrome (or, if the operator has not turned on direct links yet, a Drive search shows it; click the file).
4. The operator's exports change the folder, but your downloaded `dashboard.html` is a snapshot: download it again to see new reports. (With Google Drive for desktop the page
   inside the synchronised folder is always current.)
* If Chrome is signed in to several Google accounts and a link says the file was not found, switch to the team account (Chrome's profile menu) and click the link again.
* The page is **read-only**. It shows the state as of the operator's last run or export.
* To ask for a new report: in the Google Sheet set the client's Status and a new period of exactly 28 days, then tell the operator, who runs it.

## Limits
* The operator computer must be on and signed in to Drive for new reports to appear. Drive may take a minute to sync.
* A link finds the report by its file name, so it works only while the PDF is still in that Drive account and the viewer has access to it.
* The dashboard lists finished reports only; it does not show clients that are waiting to run (that is on the operator's web page).
* It is one-way: changing or deleting files in Drive does not change the system.
