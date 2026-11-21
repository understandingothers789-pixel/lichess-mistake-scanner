# What changed

## v0.1.4

- Added a Python source download and launcher for systems that block the packaged executable.
- The launcher installs the core app first. Stockfish and Maia can be configured separately.
- Windows CI tests the extracted Python download before publishing it.

## v0.1.3

- Reanalyze a saved game directly with the currently selected Stockfish engine and retain both results.
- Saved scans retain their configured Stockfish worker limit alongside the global cap.
- Live integration tests now cover exports by game ID as well as game URL and username.

## v0.1.2

- The Windows download includes the Maia 3 CPU runtime. Download the verified model once from Scanner to enable predictions.
- The release workflow runs an actual Maia inference check from the packaged Windows executable before publication.

## v0.1.1

- Stream monthly Lichess archives, use public game exports, or import local PGN files.
- Discover mistakes with adaptive Stockfish searches and verify them at depth 20 with MultiPV 5.
- Review full games, evaluation graphs, engine lines, and missed punishments.
- Rank verified mistakes by Maia 3 move probability when the optional runtime and checkpoint are installed.
- Install official Stockfish development builds, keep older engine revisions, and resume interrupted scans.
- Export annotated PGN, FEN, CSV, JSON, and puzzle solutions.

The portable ZIP includes the scanner and Stockfish manager. For Maia, use the Windows source setup with the optional CPU runtime.
