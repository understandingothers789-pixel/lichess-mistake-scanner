# What changed

## v0.1.9

- Refined the interface with slate panels, teal controls, lavender Maia values and a more compact toolbar inspired by Nocturne.
- The game score pairs White and Black on a single row, with separate clickable moves and evaluations. The analysis panel shows the current evaluation beside the position controls.

## v0.1.8

- Toggle Live Stockfish on the Analysis tab to evaluate the current board and explore engine variations.
- Live analysis shares the existing Stockfish worker limit and leaves saved scanner results intact.

## v0.1.7

- Mistake detection starts at move 1 by default for new scans and reanalysis.

## v0.1.6

- Windows and Python ZIPs extract into versioned folders.
- The board uses Nocturne’s default Classic squares and Nibbler (shaded Merida) pieces. Recorded player clocks appear at the board edges.
- Hovering an engine continuation opens a separate miniature board. Scroll to move forward or backward through the variation.
- Scanner shows the actual Maia checkpoint path, can open its folder, and separates waiting jobs from active inference.

## v0.1.5

- Maia setup installs the missing CPU runtime in the Python edition and verifies a local prediction before reporting success.
- Remove the active Stockfish installation when it has no unfinished Stockfish work, while keeping saved analysis.
- Pick a single UTC day for an archive scan and use a managed cache limit as low as 1 GB. Streaming remains available for larger monthly files.

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
