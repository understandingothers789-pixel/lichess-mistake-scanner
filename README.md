# Second Look

Second Look is a standalone Windows x64 desktop application for finding chess mistakes in Lichess games. It streams games, records Stockfish analysis in a local SQLite library, and uses Maia 3 79M to rank verified mistakes by how likely a human at the relevant rating was to play them. Open a discovery to see its game, engine lines, and probabilities immediately.

[**Download Second Look for Windows 11 x64**](https://github.com/understandingothers789-pixel/lichess-mistake-scanner/releases/latest/download/SecondLook-windows-x64.zip) · [**Download the Python edition**](https://github.com/understandingothers789-pixel/lichess-mistake-scanner/releases/latest/download/SecondLook-Python.zip) · [What changed](CHANGELOG.md)

The Windows release includes the Maia CPU runtime. Extract the whole ZIP, launch SecondLook.exe, then use **Scanner → Download model** to fetch the verified checkpoint.

## If Smart App Control blocks SecondLook.exe

Download the **Python edition**. Install **Python 3.12 x64** with the launcher from python.org, extract the ZIP, and open PowerShell in the extracted versioned `SecondLook-Python-v…` folder. [Official Python 3.12 Windows installer](https://www.python.org/downloads/release/python-31210/)

~~~powershell
py -3.12 launch.py
~~~

The launcher installs the core app into a local virtual environment on the first run. In **Engines**, select a Stockfish executable permitted by Windows. For Maia, choose **Scanner → Download model** or **Choose checkpoint**. The Python edition installs any missing Maia CPU runtime in its own environment, then verifies local inference. Both editions use the same default library.

Keep Smart App Control enabled. Windows evaluates the Python runtime and native dependencies independently. If Windows Security blocks a Stockfish executable or a DLL, check the file name in Protection History. An allowed build of that component is needed to use it.

## Run from source on Windows 11

Install **Python 3.12 x64** with the Python launcher. Install **Git for Windows** if you plan to use Maia or compile Stockfish. From the repository in PowerShell:

~~~powershell
.\scripts\setup-windows.ps1 -WithMaia
.\.venv\Scripts\python.exe -m mistake_scanner
~~~

Without the optional Maia runtime, omit `-WithMaia`. Stockfish verification works independently and saves jobs for later Maia inference.

1. In **Engines**, select **Download dev build & activate** or **Select existing executable**.
2. In **Scanner**, select **Download model** under **Maia 3 · 79M**. The checkpoint is about 301 MiB. Alternatively, use **Choose checkpoint** for an existing copy of the official file.
3. Choose a source and filters in **Scanner**, then select **Start scan**. The default limit is 250 selected games. Set it to **Unlimited** for an unrestricted scan.
4. Open a result in **Library → Discoveries**. The default order is highest Maia probability first, then larger Stockfish loss. A dash means a probability is unavailable or pending.

The default Maia checkpoint is `%LOCALAPPDATA%\LichessMistakeScanner\models\maia3-79m.pt`. Scanner shows the actual configured path. **Open model folder** opens its folder, and **Choose checkpoint** starts at that path. **Ready** means a checkpoint is configured. **Waiting** counts Maia jobs still queued, separate from jobs **in progress** and **complete**. Pausing prevents new jobs from starting while active Stockfish work can finish and queue more Maia jobs.

The library, engine profiles, sources, and model use a separate `LichessMistakeScanner` application data directory under `%LOCALAPPDATA%` on Windows. The `--data-dir PATH` option selects a different library. The application has no dependency on Nocturne.

## Game sources

- **Monthly archives** discover official standard rated PGNs by month. Rating, speed, opening, date, and other game filters are applied locally before Stockfish searches. Enable **Scan a single UTC day** in the archive controls and pick a date to analyze that day. Lichess serves monthly archives, so a daily scan reads the compressed monthly stream and filters by date locally.
- **Lichess API** exports public games by username, game IDs, or supported game URL. Username exports use supported date, rating-status, speed, opponent, color, and count parameters. The same local filters apply. Requests are serial and wait at least one minute after HTTP 429.
- **Local files** accept multiple `.pgn` and `.pgn.zst` files, including monthly dumps downloaded earlier. The scanner uses standard chess. Missing metadata remains unknown.

Streaming transfers the compressed data and parses complete PGN records incrementally. It can stop at the game limit without storing the archive or reading to its end. The official checksum is checked when a stream is consumed in full. For a full, verified file, enable **Download to managed archive cache**. The application checks Lichess's published SHA-256 before opening a saved archive and resumes partial HTTP downloads when the server accepts a byte range. The cache limit defaults to 1 GB and includes partial files. For monthly archives larger than the cap, leave managed caching off and stream the archive. If the limit is reached, clear unused files from the chosen cache's `lichess-monthly` folder or raise the limit. **Delete this archive after intake** removes a verified cached file after the scan has finished reading it or reached its game limit. [Lichess database](https://database.lichess.org/) · [Lichess API](https://lichess.org/api)

Pause stops new work from being scheduled and lets active analysis finish. On restart, interrupted scans are paused until you choose **Resume / retry**. Compressed streams may need to transfer and replay earlier records to reach the saved checkpoint. Mutable API exports are replayed against saved game identities. SQLite retains finished work and separate discovery, verification, and Maia jobs. Local files must retain the original size and modification time to resume from their saved checkpoint.

## Review and exports

- **Reanalyze** queues a new scan of the saved game with the selected engine and current settings. Both results remain in the library.
- The **game score** opens any played move and includes variations in the original PGN. Click the **evaluation graph** to seek through the game, or use **Previous/Next Moment** to jump between findings.
- **Before move** and **After move** show stored Stockfish lines, search depths, acceptable moves, and Maia probabilities. Hover a line to open a miniature board. Keep the pointer over the line and scroll down to advance or up to go back. Click a line to play it on the main board and use Left/Right to step through it. **Main line** returns to the result.
- Saved game clocks appear above and below the board when reviewing games. A dash marks an unavailable recorded time. Click or drag a piece for a legal side variation. Right-drag draws an arrow. The evaluation bar and graph use White's perspective. The **View** menu controls dockable panels.
- **All Maia moves** shows the full legal-move distribution. **Position occurrences** shows empirical counts from analyzed games and links to their source positions. Bookmarks and the last selected position are saved.
- **Export** on an analysis supports original or annotated PGN, FEN, CSV, JSON, and puzzle positions with solutions. The library can export selected items or a filtered collection. Annotated PGN includes compatible evaluation comments, continuations, and engine identity.

## Search behavior

Discovery defaults to eight separate single-threaded Stockfish processes with 32 MiB of Hash each. Discovery and verification share this worker limit. Workers run below normal priority. Optional affinity uses physical-core topology when the platform exposes it. Discovery uses depth 12 and switches to depth 4 after the same side has been decisively ahead for six consecutive plies. A shallow collapse triggers a deeper search. The fast mode can miss tactics.

Mistake detection starts at move 1 by default. The scanner looks for at least 0.50 pawns of loss from the mover's perspective. It excludes changes where both moves retain the same decisive outcome (at least 3.0 pawns for the same side). It handles mate changes separately. The discovery pass uses consecutive positions to screen moves and a restricted root to check the actual move when necessary.

Verification defaults to depth 20 and MultiPV 5 on both sides of a candidate. It checks the actual move even when it falls outside those lines. It also checks the opponent's actual response for a missed punishment and links verified follow-ups in the game. An apparent only move is a finding at the displayed search depth.

One shared CPU Maia process predicts only the positions involved in verified mistakes. Both rating inputs use the lower original player rating rounded to the nearest 200 (halfway values round up, clamped to 600–3000). Predictions use up to eight positions from the original game history. If either player has no rating, the game retains its Stockfish results and the probability stays unavailable. Maia probabilities are model estimates. Observed move counts come from the local sample and are displayed separately.

## Stockfish Engine Manager

**Check for updates** shows the release commit and date alongside installed development revisions. **Download dev build & activate** resolves the upstream prerelease to a full Git commit, verifies the published GitHub SHA-256, checks the Windows x64 executable in the ZIP, and validates a real UCI search. Separate profile directories retain old versions, and a verified existing installation can be reused. **Remove engine** also accepts the active profile once its unfinished Stockfish work is complete. Removing a managed profile frees its installation space and keeps saved analysis. Changing engines applies to newly created scans. Each saved scan retains its original engine fingerprint, NNUE configuration, and search settings.

**Build from source** uses a native Windows MSYS2 toolchain with Git for Windows. In an MSYS2 **UCRT64** shell, install:

~~~sh
pacman -S --needed make mingw-w64-ucrt-x86_64-gcc
~~~

Choose the MSYS2 installation directory in Second Look. Leave the commit field blank for current development source, or enter the official commit SHA. The build fetches and checks the exact source revision and required NNUE files, caps compiler jobs, runs Stockfish's built-in benchmark, and validates the resulting Windows x64 executable through UCI. The log and benchmark result are available from the profile. Uncheck **Profile-guided optimization** for an ordinary build. Pause active scans before operations that launch validation searches or a compiler.

The official universal build selects at runtime from the architectures available to it. A source build can use upstream native detection, portable x86-64, or AVX2. Use the reported benchmark on your machine to compare compatible builds. Ryzen 7 9800X3D performance and relative merits of BMI2, AVX2, or AVX-512 variants have not been measured here. Locally selected engines that depend on an external network should have the matching `nn-*.nnue` file beside the executable. Windows executable-origin information is preserved or recorded for official ZIP installs. If Windows blocks a downloaded or unsigned executable, inspect the reported error and Windows Security history or select a build your system allows. [Stockfish releases](https://github.com/official-stockfish/Stockfish/releases?q=prerelease%3Atrue) · [Stockfish build documentation](https://official-stockfish.github.io/docs/stockfish-wiki/Compiling-from-source.html)

## Package and test

Run on Windows x64 from the repository:

~~~powershell
.\scripts\setup-windows.ps1
.\scripts\build-windows.ps1
~~~

The build script uses a hash-locked dependency list, runs the test suite, and produces `dist\SecondLook-windows-x64.zip`. Extract the entire directory and launch `SecondLook.exe`. This is an unsigned portable build. Keep the included runtime directory with the executable. For a larger bundle with the optional Maia CPU runtime, pass `-WithMaia` to **both** setup and build. The verified checkpoint is still downloaded separately. See [third-party notices](THIRD_PARTY.md) for distribution obligations.

The Windows CI workflow builds the portable bundle on standard runs. Its manual options exercise the live Windows engine downloader and separately build an official revision on an isolated MSYS2 toolchain. Run automated tests locally with `.\.venv\Scripts\python.exe -m pytest -q`. Set `STOCKFISH_EXE` to run real UCI and depth-20 tests, `MAIA_CHECKPOINT` to run real Maia inference tests, `STOCKFISH_WINDOWS_ZIP` to inspect an official package's x64 executable, and `LIVE_LICHESS=1` for bounded live public acquisition tests. These integration checks are skipped when their inputs are absent.

### Command line

~~~powershell
.\.venv\Scripts\python.exe -m mistake_scanner scan --pgn C:\games\sample.pgn --engine C:\engines\stockfish.exe --limit 50
.\.venv\Scripts\python.exe -m mistake_scanner scan --archive 2025-01 2025-01 --min-rating 1600 --max-rating 1800 --speed rapid --limit 50
.\.venv\Scripts\python.exe -m mistake_scanner scans
.\.venv\Scripts\python.exe -m mistake_scanner resume 1
~~~

Use `-m mistake_scanner --help` or `scan --help` for parameters. The repository is also testable with a local Stockfish binary on Linux. Native Windows launching, compilation, and packaging require a Windows runner for verification.
